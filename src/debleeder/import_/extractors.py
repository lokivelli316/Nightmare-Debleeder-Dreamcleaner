
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import hashlib
import re
import zipfile
import xml.etree.ElementTree as ET
from typing import Iterator

from ..core.protocols import BlockIdentity, ExtractionQuality, RunAddress
from ..core.config import Config


@dataclass(frozen=True, slots=True)
class ExtractedBlock:
    """Raw extraction result before normalization."""
    content: str
    raw_content: str
    heading_path: list[str]
    metadata: dict
    extraction_quality: ExtractionQuality


def extract_docx(path: Path, config: Config) -> Iterator[ExtractedBlock]:
    """Extract DOCX — OMML equations via stdlib zipfile + xml.etree."""
    with zipfile.ZipFile(path, "r") as z:
        # Document structure
        try:
            doc_xml = z.read("word/document.xml")
        except KeyError:
            return

        root = ET.fromstring(doc_xml)
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}

        # Extract paragraphs with heading tracking
        heading_stack: list[str] = []
        ordinal = 0

        for p in root.findall(".//w:p", ns):
            # Check for heading style
            p_style = p.find(".//w:pStyle", ns)
            if p_style is not None:
                val = p_style.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val")
                if val and val.startswith("Heading"):
                    level = int(val[-1]) if val[-1].isdigit() else 1
                    # Get heading text
                    texts = [t.text for t in p.findall(".//w:t", ns) if t.text]
                    heading_text = "".join(texts).strip()
                    if heading_text:
                        heading_stack = heading_stack[:level-1] + [heading_text]

            # Extract paragraph text + OMML equations
            para_texts: list[str] = []
            eq_count = 0
            for child in p:
                if child.tag.endswith("}t") and child.text:
                    para_texts.append(child.text)
                elif child.tag.endswith("}oMath") or child.tag.endswith("}oMathPara"):
                    eq_count += 1
                    # Extract OMML as normalized text
                    omml_text = _extract_omml_text(child)
                    if omml_text:
                        para_texts.append(f"[EQUATION:{omml_text}]")

            content = "".join(para_texts).strip()
            if not content and eq_count == 0:
                continue

            # Normalize for identity
            normalized = _normalize_for_identity(content)

            quality = ExtractionQuality.HIGH
            if eq_count > 0 and config.profile.value == "phone":
                quality = ExtractionQuality.MEDIUM  # OMML only, no rendering

            yield ExtractedBlock(
                content=content,
                raw_content=ET.tostring(p, encoding="unicode"),
                heading_path=heading_stack.copy(),
                metadata={"ordinal": ordinal, "equation_count": eq_count},
                extraction_quality=quality,
            )
            ordinal += 1


def _extract_omml_text(omml_elem: ET.Element) -> str:
    """Extract readable text from OMML — deterministic, no rendering."""
    # Simplified: extract variables, numbers, operators
    texts = []
    for elem in omml_elem.iter():
        if elem.tag.endswith("}t") and elem.text:
            texts.append(elem.text)
        elif elem.tag.endswith("}r") and elem.text:
            texts.append(elem.text)
    return "".join(texts).strip()


def extract_pdf(path: Path, config: Config) -> Iterator[ExtractedBlock]:
    """Extract PDF — text layer only on phone, richer on desktop."""
    try:
        import pypdf
    except ImportError:
        return

    with open(path, "rb") as f:
        reader = pypdf.PdfReader(f)
        for page_num, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            if not text.strip():
                continue

            # Simple heading detection (all caps, short lines)
            lines = text.split("\n")
            heading_stack: list[str] = []
            current_para: list[str] = []
            ordinal = 0

            for line in lines:
                stripped = line.strip()
                if not stripped:
                    if current_para:
                        content = " ".join(current_para).strip()
                        normalized = _normalize_for_identity(content)
                        yield ExtractedBlock(
                            content=content, raw_content=content,
                            heading_path=heading_stack.copy(),
                            metadata={"page": page_num, "ordinal": ordinal},
                            extraction_quality=ExtractionQuality.MEDIUM,
                        )
                        current_para = []
                        ordinal += 1
                    continue

                # Heuristic: short all-caps line = heading
                if len(stripped) < 80 and stripped.isupper() and not stripped.endswith("."):
                    if current_para:
                        content = " ".join(current_para).strip()
                        normalized = _normalize_for_identity(content)
                        yield ExtractedBlock(
                            content=content, raw_content=content,
                            heading_path=heading_stack.copy(),
                            metadata={"page": page_num, "ordinal": ordinal},
                            extraction_quality=ExtractionQuality.MEDIUM,
                        )
                        current_para = []
                        ordinal += 1
                    heading_stack = [stripped]
                else:
                    current_para.append(stripped)

            if current_para:
                content = " ".join(current_para).strip()
                normalized = _normalize_for_identity(content)
                yield ExtractedBlock(
                    content=content, raw_content=content,
                    heading_path=heading_stack.copy(),
                    metadata={"page": page_num, "ordinal": ordinal},
                    extraction_quality=ExtractionQuality.MEDIUM,
                )


def extract_markdown(path: Path, config: Config) -> Iterator[ExtractedBlock]:
    """Extract Markdown — deterministic, no external deps."""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.split("\n")
    heading_stack: list[str] = []
    current_para: list[str] = []
    ordinal = 0

    for line in lines:
        # ATX headings
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            heading_text = line.lstrip("# ").strip()
            if heading_text:
                heading_stack = heading_stack[:level-1] + [heading_text]
            if current_para:
                content = "\n".join(current_para).strip()
                yield ExtractedBlock(
                    content=content, raw_content=content,
                    heading_path=heading_stack.copy(),
                    metadata={"ordinal": ordinal},
                    extraction_quality=ExtractionQuality.HIGH,
                )
                current_para = []
                ordinal += 1
        elif not line.strip():
            if current_para:
                content = "\n".join(current_para).strip()
                yield ExtractedBlock(
                    content=content, raw_content=content,
                    heading_path=heading_stack.copy(),
                    metadata={"ordinal": ordinal},
                    extraction_quality=ExtractionQuality.HIGH,
                )
                current_para = []
                ordinal += 1
        else:
            current_para.append(line)

    if current_para:
        content = "\n".join(current_para).strip()
        yield ExtractedBlock(
            content=content, raw_content=content,
            heading_path=heading_stack.copy(),
            metadata={"ordinal": ordinal},
            extraction_quality=ExtractionQuality.HIGH,
        )


def normalize_text(text: str) -> str:
    """Normalization used for CONTENT IDENTITY and matching only.

    Never use the result as document output: it is lowercased and
    punctuation-folded. Output renders raw_content.
    """
    return _normalize_for_identity(text)


def _normalize_for_identity(text: str) -> str:
    """Normalize content for stable identity — aggressive but deterministic."""
    # Unicode normalize
    import unicodedata
    text = unicodedata.normalize("NFC", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text)
    # Remove zero-width chars
    text = re.sub(r"[\u200b-\u200f\ufeff]", "", text)
    # Normalize quotes/dashes
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = text.replace("\u2013", "-").replace("\u2014", "-")
    # Lowercase for identity (case-insensitive matching)
    return text.strip().lower()


def extract_file(path: Path, config: Config) -> Iterator[ExtractedBlock]:
    """Dispatch by extension.

    A malformed or unreadable source NEVER aborts the import. It yields a single
    block flagged ExtractionQuality.FAILED so the failure is visible in the
    baseline and propagates into confidence, instead of crashing the run.
    """
    suffix = path.suffix.lower()
    try:
        if suffix == ".docx":
            yield from extract_docx(path, config)
        elif suffix == ".pdf":
            yield from extract_pdf(path, config)
        elif suffix in (".md", ".markdown", ".txt"):
            yield from extract_markdown(path, config)
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
            if text.strip():
                yield ExtractedBlock(
                    content=normalize_text(text), raw_content=text,
                    heading_path=[], metadata={"extractor": "fallback_text"},
                    extraction_quality=ExtractionQuality.LOW,
                )
    except Exception as exc:
        yield ExtractedBlock(
            content="",
            raw_content="",
            heading_path=[],
            metadata={
                "extraction_error": f"{type(exc).__name__}: {exc}",
                "source_name": path.name,
                "extractor": suffix or "unknown",
            },
            extraction_quality=ExtractionQuality.FAILED,
        )
