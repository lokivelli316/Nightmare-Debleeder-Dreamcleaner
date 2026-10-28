"""Evidence channel engines — decomposed, calibrated, extraction-quality-aware."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Iterator
import re
import math

from ..core.protocols import (
    BlockRecord, EvidenceRecord, Channel, StrengthBand, ConfidenceBand,
    ExtractionQuality, MethodVersion,
)
from ..core.config import Config, Thresholds


@dataclass(slots=True)
class ChannelEngine:
    """Base channel engine — calibrated thresholds, quality propagation."""

    config: Config
    thresholds: Thresholds
    method_version: MethodVersion

    def _band_from_score(self, score: float, channel: Channel) -> StrengthBand:
        """Map raw score to ordinal band using channel thresholds."""
        thresh = getattr(self.thresholds, channel.value)
        if score >= thresh["high"]:
            return StrengthBand.HIGH
        elif score >= thresh["medium"]:
            return StrengthBand.MEDIUM
        elif score >= thresh["low"]:
            return StrengthBand.LOW
        return StrengthBand.NONE

    def _confidence_from_quality(self, quality: ExtractionQuality) -> ConfidenceBand:
        """Extraction quality → confidence band."""
        mapping = {
            ExtractionQuality.HIGH: ConfidenceBand.HIGH,
            ExtractionQuality.MEDIUM: ConfidenceBand.MEDIUM,
            ExtractionQuality.LOW: ConfidenceBand.LOW,
            ExtractionQuality.FAILED: ConfidenceBand.LOW,
        }
        return mapping[quality]

    def _pair_id(self, target, source) -> str:
        """Pair id over block OCCURRENCES, so identical content in two documents
        is a real pair rather than a degenerate self-match."""
        t = getattr(target, "instance_id", target)
        sc = getattr(source, "instance_id", source)
        return f"{t}:{sc}"


class PhraseChannel(ChannelEngine):
    """Phrase overlap — shingle Jaccard, calibrated."""

    channel = Channel.PHRASE

    def analyze(self, target: BlockRecord, source: BlockRecord) -> EvidenceRecord | None:
        # Word-level shingle Jaccard (5-grams)
        t_words = target.normalized_content.split()
        s_words = source.normalized_content.split()

        if len(t_words) < 5 or len(s_words) < 5:
            return None

        t_shingles = set(" ".join(t_words[i:i+5]) for i in range(len(t_words) - 4))
        s_shingles = set(" ".join(s_words[i:i+5]) for i in range(len(s_words) - 4))

        if not t_shingles or not s_shingles:
            return None

        intersection = len(t_shingles & s_shingles)
        union = len(t_shingles | s_shingles)
        jaccard = intersection / union if union > 0 else 0.0

        # Boost for rare shingles (handled in candidate funnel)
        raw_score = jaccard
        strength = self._band_from_score(raw_score, self.channel)
        confidence = self._confidence_from_quality(
            min(target.extraction_quality, source.extraction_quality, key=lambda q: q.value)
        )

        return EvidenceRecord(
            run_id=target.run_id,
            pair_id=self._pair_id(target, source),
            channel=self.channel,
            raw_score=raw_score,
            strength=strength,
            confidence=confidence,
            details={
                "jaccard": jaccard,
                "intersection": intersection,
                "union": union,
                "target_shingles": len(t_shingles),
                "source_shingles": len(s_shingles),
            },
            method_version=str(self.method_version),
            extraction_quality=min(target.extraction_quality, source.extraction_quality, key=lambda q: q.value),
        )


class NumericChannel(ChannelEngine):
    """Numeric literal overlap — exact match with rounding awareness."""

    channel = Channel.NUMERIC

    # Rounding-aware comparison (O-01 fix)
    SIGNIFICANT_FIGURES = 3

    def analyze(self, target: BlockRecord, source: BlockRecord) -> EvidenceRecord | None:
        t_nums = self._extract_significant_numbers(target.normalized_content)
        s_nums = self._extract_significant_numbers(source.normalized_content)

        if not t_nums or not s_nums:
            return None

        # Exact match on normalized significant figures
        matches = []
        for tn in t_nums:
            for sn in s_nums:
                if self._numbers_match(tn, sn):
                    matches.append((tn, sn))

        if not matches:
            return None

        # Score = matched unique numbers / total unique numbers
        all_nums = set(t_nums) | set(s_nums)
        matched_unique = set(m[0] for m in matches) | set(m[1] for m in matches)
        raw_score = len(matched_unique) / len(all_nums) if all_nums else 0.0

        strength = self._band_from_score(raw_score, self.channel)
        confidence = self._confidence_from_quality(
            min(target.extraction_quality, source.extraction_quality, key=lambda q: q.value)
        )

        return EvidenceRecord(
            run_id=target.run_id,
            pair_id=self._pair_id(target, source),
            channel=self.channel,
            raw_score=raw_score,
            strength=strength,
            confidence=confidence,
            details={
                "matches": matches,
                "target_numbers": list(t_nums),
                "source_numbers": list(s_nums),
                "matched_count": len(matches),
                "rounding_aware": True,
            },
            method_version=str(self.method_version),
            extraction_quality=min(target.extraction_quality, source.extraction_quality, key=lambda q: q.value),
        )

    def _extract_significant_numbers(self, text: str) -> set[str]:
        """Extract numbers normalized to significant figures."""
        nums = set()
        for match in re.finditer(r"\b(\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\b", text):
            raw = match.group(1)
            normalized = self._normalize_significant_figures(raw)
            nums.add(normalized)
        return nums

    def _normalize_significant_figures(self, num_str: str) -> str:
        """Normalize to significant figures — handles 6.02e23 * 2 = 1.20e24 correctly."""
        try:
            if "e" in num_str.lower():
                # Scientific notation
                mantissa, exp = re.split(r"[eE]", num_str)
                mantissa = float(mantissa)
                exp = int(exp)
                # Round mantissa to significant figures
                if mantissa != 0:
                    import math
                    sig = self.SIGNIFICANT_FIGURES
                    rounded = round(mantissa, sig - 1 - int(math.floor(math.log10(abs(mantissa)))))
                    return f"{rounded:.{sig-1}e}".replace("e+0", "e").replace("e-0", "e-").replace("e+", "e")
            else:
                # Decimal
                val = float(num_str)
                if val != 0:
                    import math
                    sig = self.SIGNIFICANT_FIGURES
                    rounded = round(val, sig - 1 - int(math.floor(math.log10(abs(val)))))
                    return f"{rounded:.{sig-1}g}"
        except Exception:
            pass
        return num_str  # fallback

    def _numbers_match(self, a: str, b: str) -> bool:
        """Compare normalized significant figures."""
        return a == b


class SymbolChannel(ChannelEngine):
    """Symbol overlap — Greek, operators, math notation."""

    channel = Channel.SYMBOL

    SYMBOL_PATTERN = re.compile(r"[α-ωΑ-Ω∑∏∫∂∇±×÷≤≥≠≈∞∈∉⊂⊃∪∩∧∨¬→↔∀∃∇]")

    def analyze(self, target: BlockRecord, source: BlockRecord) -> EvidenceRecord | None:
        t_syms = set(self.SYMBOL_PATTERN.findall(target.normalized_content))
        s_syms = set(self.SYMBOL_PATTERN.findall(source.normalized_content))

        if not t_syms or not s_syms:
            return None

        intersection = t_syms & s_syms
        union = t_syms | s_syms
        raw_score = len(intersection) / len(union) if union else 0.0

        strength = self._band_from_score(raw_score, self.channel)
        confidence = self._confidence_from_quality(
            min(target.extraction_quality, source.extraction_quality, key=lambda q: q.value)
        )

        return EvidenceRecord(
            run_id=target.run_id,
            pair_id=self._pair_id(target, source),
            channel=self.channel,
            raw_score=raw_score,
            strength=strength,
            confidence=confidence,
            details={
                "intersection": list(intersection),
                "target_symbols": list(t_syms),
                "source_symbols": list(s_syms),
            },
            method_version=str(self.method_version),
            extraction_quality=min(target.extraction_quality, source.extraction_quality, key=lambda q: q.value),
        )


class CitationChannel(ChannelEngine):
    """Citation overlap — exact citation match."""

    channel = Channel.CITATION

    CITATION_PATTERN = re.compile(
        r"(\[\d+\]|\(\w+(?: et al\.)?, \d{4}\)|doi:\S+|arxiv:\S+)",
        re.IGNORECASE
    )

    def analyze(self, target: BlockRecord, source: BlockRecord) -> EvidenceRecord | None:
        t_cites = set(c.lower() for c in self.CITATION_PATTERN.findall(target.normalized_content))
        s_cites = set(c.lower() for c in self.CITATION_PATTERN.findall(source.normalized_content))

        if not t_cites or not s_cites:
            return None

        intersection = t_cites & s_cites
        union = t_cites | s_cites
        raw_score = len(intersection) / len(union) if union else 0.0

        strength = self._band_from_score(raw_score, self.channel)
        confidence = self._confidence_from_quality(
            min(target.extraction_quality, source.extraction_quality, key=lambda q: q.value)
        )

        return EvidenceRecord(
            run_id=target.run_id,
            pair_id=self._pair_id(target, source),
            channel=self.channel,
            raw_score=raw_score,
            strength=strength,
            confidence=confidence,
            details={
                "intersection": list(intersection),
                "target_citations": list(t_cites),
                "source_citations": list(s_cites),
            },
            method_version=str(self.method_version),
            extraction_quality=min(target.extraction_quality, source.extraction_quality, key=lambda q: q.value),
        )


class EquationChannel(ChannelEngine):
    """Equation structure overlap — OMML/text normalized."""

    channel = Channel.EQUATION

    def analyze(self, target: BlockRecord, source: BlockRecord) -> EvidenceRecord | None:
        # Extract equation markers from normalized content
        t_eqs = set(re.findall(r"\[EQUATION:([^\]]+)\]", target.normalized_content))
        s_eqs = set(re.findall(r"\[EQUATION:([^\]]+)\]", source.normalized_content))

        if not t_eqs or not s_eqs:
            return None

        # Normalize equation structure (variables → placeholders)
        t_norm = {self._normalize_equation(eq) for eq in t_eqs}
        s_norm = {self._normalize_equation(eq) for eq in s_eqs}

        intersection = t_norm & s_norm
        union = t_norm | s_norm
        raw_score = len(intersection) / len(union) if union else 0.0

        strength = self._band_from_score(raw_score, self.channel)
        confidence = self._confidence_from_quality(
            min(target.extraction_quality, source.extraction_quality, key=lambda q: q.value)
        )

        return EvidenceRecord(
            run_id=target.run_id,
            pair_id=self._pair_id(target, source),
            channel=self.channel,
            raw_score=raw_score,
            strength=strength,
            confidence=confidence,
            details={
                "intersection": list(intersection),
                "target_equations": list(t_norm),
                "source_equations": list(s_norm),
            },
            method_version=str(self.method_version),
            extraction_quality=min(target.extraction_quality, source.extraction_quality, key=lambda q: q.value),
        )

    def _normalize_equation(self, eq: str) -> str:
        """Normalize equation — variables to placeholders, preserve structure."""
        # Replace variable names (single letters, subscripts) with placeholders
        eq = re.sub(r"\b[a-zA-Z]\b", "x", eq)  # single-letter vars
        eq = re.sub(r"_[a-zA-Z0-9]+", "_i", eq)  # subscripts
        eq = re.sub(r"\^[a-zA-Z0-9]+", "^n", eq)  # superscripts
        eq = re.sub(r"\d+(?:\.\d+)?", "N", eq)  # numbers → N
        return eq.strip()


class LineageChannel(ChannelEngine):
    """Lineage evidence — cross-version ancestry, modifies confidence not strength."""

    channel = Channel.LINEAGE

    def analyze(self, target: BlockRecord, source: BlockRecord, lineage_map: dict[str, str] | None = None) -> EvidenceRecord | None:
        lineage_map = lineage_map or {}
        """Check if target and source share lineage ancestry."""
        # lineage_map: cid -> root_cid (ultimate ancestor)
        t_root = lineage_map.get(target.cid, target.cid)
        s_root = lineage_map.get(source.cid, source.cid)

        if t_root != s_root:
            return None

        # Same lineage root — this is contextual, not direct strength
        # Return evidence with NONE strength but HIGH confidence for direction
        return EvidenceRecord(
            run_id=target.run_id,
            pair_id=self._pair_id(target, source),
            channel=self.channel,
            raw_score=1.0,
            strength=StrengthBand.NONE,  # lineage never contributes to strength
            confidence=ConfidenceBand.HIGH,
            details={
                "shared_root": t_root,
                "target_cid": target.cid,
                "source_cid": source.cid,
                "direction_hint": "common_ancestor",
            },
            method_version=str(self.method_version),
            extraction_quality=ExtractionQuality.HIGH,
        )


class ReferenceChannel(ChannelEngine):
    """Declared Reference Documents — contextual, never evidence strength.

    Emits a record even at StrengthBand.NONE so that reference context always
    reaches triage. Reference status changes ROUTING, never provenance: the
    ordinary channels keep their own strengths untouched.
    """

    channel = Channel.SEED   # wire-compatible with the existing vocabulary

    def __init__(self, config: Config, thresholds: Thresholds,
                 method_version: MethodVersion,
                 reference_index: dict[str, dict] | None = None):
        super().__init__(config, thresholds, method_version)
        # instance_id -> reference entry
        self.reference_index = reference_index or {}

    @property
    def is_contextual(self) -> bool:
        return True

    def analyze(self, target: BlockRecord, source: BlockRecord) -> EvidenceRecord | None:
        t_ref = self.reference_index.get(target.instance_id)
        s_ref = self.reference_index.get(source.instance_id)

        if not t_ref and not s_ref:
            return None

        if t_ref and s_ref:
            # Reference-to-reference. Not ordinary user triage in this patch.
            return EvidenceRecord(
                run_id=target.run_id,
                pair_id=self._pair_id(target, source),
                channel=self.channel,
                raw_score=0.0,
                strength=StrengthBand.NONE,
                confidence=ConfidenceBand.HIGH,
                details={
                    "match_type": "reference_to_reference",
                    "suppress_from_user_triage": True,
                },
                method_version=str(self.method_version),
                extraction_quality=ExtractionQuality.HIGH,
            )

        # Exactly one side is a reference. Orient working -> reference.
        if t_ref:
            reference, working, ref_entry = target, source, t_ref
        else:
            reference, working, ref_entry = source, target, s_ref

        exact = working.normalized_content == reference.normalized_content
        working_tokens = set(working.normalized_content.split())
        reference_tokens = set(reference.normalized_content.split())
        token_union = working_tokens | reference_tokens
        token_shared = working_tokens & reference_tokens
        token_jaccard = len(token_shared) / len(token_union) if token_union else 0.0
        return EvidenceRecord(
            run_id=target.run_id,
            pair_id=self._pair_id(target, source),
            channel=self.channel,
            raw_score=1.0 if exact else token_jaccard,
            strength=StrengthBand.NONE,   # context is never strength
            confidence=ConfidenceBand.HIGH,
            details={
                "match_type": "exact_match" if exact else "reference_difference",
                "reference_instance_id": reference.instance_id,
                "reference_cid": reference.cid,
                "reference_doc_hash": ref_entry.get("doc_hash", reference.addr.doc_version_hash),
                "reference_source_name": ref_entry.get("source_name", ""),
                "reference_ordinal": reference.addr.ordinal,
                "working_instance_id": working.instance_id,
                "working_cid": working.cid,
                "working_source_name": working.metadata.get("source_name", ""),
                "working_ordinal": working.addr.ordinal,
                "shared_seed": exact,
                "token_jaccard": token_jaccard,
                "shared_token_count": len(token_shared),
            },
            method_version=str(self.method_version),
            extraction_quality=ExtractionQuality.HIGH,
        )


# Compatibility alias — the disposition vocabulary still says keep_seeded.
SeedChannel = ReferenceChannel
