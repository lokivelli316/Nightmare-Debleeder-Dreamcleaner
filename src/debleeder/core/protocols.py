"""Core protocol interfaces — zero dependencies, pure Python."""

from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Generic, Iterator, Protocol, TypeVar
import uuid

T = TypeVar("T")


class Profile(str, Enum):
    """Execution profile — phone or desktop."""
    PHONE = "phone"
    DESKTOP = "desktop"


class DocumentRole(str, Enum):
    """How the user declared a document. NEVER inferred."""
    WORKING = "working"      # evaluated for possible drift
    REFERENCE = "reference"  # declared canon / template / approved definitions


class SuggestionStatus(str, Enum):
    """Lifecycle of a correction suggestion. A suggestion is not a decision."""
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    EDITED = "EDITED"
    REJECTED = "REJECTED"
    STALE = "STALE"
    APPLIED = "APPLIED"


class PairClassification(str, Enum):
    """How a pair is routed for display. Routing, not provenance."""
    ORDINARY = "ordinary"
    INTENTIONAL_CONTINUITY = "intentional_continuity"
    REFERENCE_DISAGREEMENT = "reference_disagreement"


class Disposition(str, Enum):
    """Human disposition vocabulary — frozen per doctrine."""
    KEEP = "keep"
    KEEP_SEEDED = "keep_seeded"
    REWRITE = "rewrite"
    REVIEW = "review"
    QUARANTINE = "quarantine"
    SHRED = "shred"
    PURGE = "purge"


class Channel(str, Enum):
    """Evidence channels — decomposed, never combined into single score."""
    PHRASE = "phrase"
    NUMERIC = "numeric"
    SYMBOL = "symbol"
    CITATION = "citation"
    EQUATION = "equation"
    LINEAGE = "lineage"
    SEED = "seed"


class StrengthBand(str, Enum):
    """Ordinal triage strength — primary public display."""
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ConfidenceBand(str, Enum):
    """Evidence confidence — separate axis from strength."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ExtractionQuality(str, Enum):
    """Extraction fidelity — propagates to confidence."""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    FAILED = "failed"


class Direction(str, Enum):
    """Direction inference — explicit unknown when unsupported."""
    SOURCE_TO_TARGET = "source_to_target"
    TARGET_TO_SOURCE = "target_to_source"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class MethodVersion:
    """Immutable method version for reproducibility."""
    phrase: str = "v1.0"
    numeric: str = "v1.0"
    symbol: str = "v1.0"
    citation: str = "v1.0"
    equation: str = "v1.0"
    lineage: str = "v1.0"
    seed: str = "v1.0"

    def __str__(self) -> str:
        from dataclasses import fields
        return ",".join(f"{f.name}:{getattr(self, f.name)}" for f in fields(self))

    @classmethod
    def parse(cls, text: str) -> "MethodVersion":
        """Inverse of __str__. Unknown keys ignored; missing keys default."""
        from dataclasses import fields
        known = {f.name for f in fields(cls)}
        kwargs = {}
        for part in (text or "").split(","):
            if ":" not in part:
                continue
            k, _, v = part.partition(":")
            k = k.strip()
            if k in known:
                kwargs[k] = v.strip()
        return cls(**kwargs)


@dataclass(frozen=True, slots=True)
class BlockIdentity:
    """Content identity — immutable across versions."""
    cid: str  # sha256(normalized_content)[:64]
    content_hash: str  # full sha256

    @classmethod
    def from_content(cls, normalized: str) -> BlockIdentity:
        import hashlib
        full = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return cls(cid=full[:16], content_hash=full)


@dataclass(frozen=True, slots=True)
class RunAddress:
    """Position within a specific run — stable for that run only."""
    doc_version_hash: str  # sha256 of document bytes
    ordinal: int  # 0-based position in document
    heading_path_hash: str  # sha256 of heading path[:8]


@dataclass(frozen=True, slots=True)
class LineageRecord:
    """Cross-version lineage — append-only, auditable."""
    prev_cid: str
    new_cid: str
    transform_type: str  # UNCHANGED, EDITED, REORDERED, SPLIT, MERGED, MOVED, DELETED, INSERTED
    confidence: float  # 0.0-1.0
    method_version: str
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """Single channel evidence — never combined."""
    run_id: str
    pair_id: str  # f"{target_cid}:{source_cid}"
    channel: Channel
    raw_score: float  # internal only
    strength: StrengthBand
    confidence: ConfidenceBand
    details: dict[str, Any]  # channel-specific payload
    method_version: str
    extraction_quality: ExtractionQuality


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """Human disposition on ONE block occurrence."""
    run_id: str
    block_cid: str          # content identity (denormalised, for grouping)
    block_instance_id: str  # occurrence identity — what the decision applies to
    disposition: Disposition
    reason: str
    override_note: str | None
    timestamp: datetime
    sqlite_txn_id: int
    jsonl_offset: int


@dataclass(frozen=True, slots=True)
class PatchSuggestion:
    """A surgical, hash-anchored correction proposal.

    A suggestion is a PROPOSAL. It is never applied without an explicit human
    approval record, and never applied at all if the working block's raw content
    has changed since the suggestion was generated.
    """
    suggestion_id: str
    run_id: str
    working_instance_id: str
    reference_instance_id: str
    reference_doc_hash: str
    reference_source_name: str
    before_hash: str            # sha256 of the working block's raw content
    current_text: str
    proposed_text: str
    diff: str
    detected_channels: tuple[str, ...]
    status: str = SuggestionStatus.PROPOSED.value
    requires_human_approval: bool = True
    auto_apply: bool = False
    approval_note: str | None = None
    created_at: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "suggestion_id": self.suggestion_id,
            "run_id": self.run_id,
            "working_instance_id": self.working_instance_id,
            "reference_instance_id": self.reference_instance_id,
            "reference_doc_hash": self.reference_doc_hash,
            "reference_source_name": self.reference_source_name,
            "before_hash": self.before_hash,
            "current_text": self.current_text,
            "proposed_text": self.proposed_text,
            "diff": self.diff,
            "detected_channels": list(self.detected_channels),
            "status": self.status,
            "requires_human_approval": self.requires_human_approval,
            "auto_apply": self.auto_apply,
            "approval_note": self.approval_note,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """Outcome of an attempted surgical application."""
    applied: bool
    suggestion_id: str
    output_path: Path | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ReferenceEntry:
    """One reference block occurrence in the deterministic reference index."""
    doc_hash: str
    source_name: str
    instance_id: str
    cid: str
    normalized_hash: str
    raw_hash: str
    ordinal: int
    extraction_quality: str

    def to_json(self) -> dict[str, Any]:
        return {
            "doc_hash": self.doc_hash,
            "source_name": self.source_name,
            "instance_id": self.instance_id,
            "cid": self.cid,
            "normalized_hash": self.normalized_hash,
            "raw_hash": self.raw_hash,
            "ordinal": self.ordinal,
            "extraction_quality": self.extraction_quality,
        }


@dataclass(frozen=True, slots=True)
class RunManifest:
    """Immutable run descriptor — sealed at Stage 6."""
    run_id: str
    profile: Profile
    method_version: MethodVersion
    corpus_snapshot_hash: str
    started_at: datetime
    sealed_at: datetime | None
    seal_hash: str | None  # hash of SEAL_COMPLETE record
    clean_report_hash: str | None
    xlsx_hash: str | None
    status: str  # RUNNING, SEALED, FAILED, ABANDONED


class RunStore(Protocol):
    """Operational storage — SQLite with WAL."""

    def create_run(self, profile: Profile, method_version: MethodVersion, corpus_snapshot_hash: str) -> RunManifest: ...
    def get_run(self, run_id: str) -> RunManifest | None: ...
    def update_run_status(self, run_id: str, status: str) -> None: ...
    def seal_run(self, run_id: str, seal_hash: str, clean_report_hash: str, xlsx_hash: str) -> None: ...

    def put_blocks(self, run_id: str, blocks: list[BlockRecord]) -> None: ...
    def get_blocks(self, run_id: str) -> Iterator[BlockRecord]: ...
    def get_block(self, run_id: str, instance_id: str) -> BlockRecord | None: ...

    def put_evidence(self, run_id: str, evidence: list[EvidenceRecord]) -> None: ...
    def get_evidence(self, run_id: str, pair_id: str) -> list[EvidenceRecord]: ...
    def get_all_evidence(self, run_id: str) -> Iterator[EvidenceRecord]: ...

    def put_decision(self, decision: DecisionRecord) -> None: ...
    def get_decisions(self, run_id: str) -> Iterator[DecisionRecord]: ...
    def get_undecided_blocks(self, run_id: str) -> Iterator[BlockRecord]: ...

    def put_lineage(self, run_id: str, lineage: list[LineageRecord]) -> None: ...
    def get_lineage(self, run_id: str) -> Iterator[LineageRecord]: ...


class AuditJournal(Protocol):
    """Append-only JSONL audit projection — portable, hash-chained."""

    def append(self, record: dict[str, Any]) -> int: ...  # returns byte offset
    def verify_chain(self, up_to_offset: int | None = None) -> bool: ...
    def read_range(self, start: int, end: int) -> Iterator[dict[str, Any]]: ...


class CorpusIndex(Protocol):
    """Persistent corpus index — append-only, versioned snapshots."""

    def add_document(self, doc_id: str, blocks: list[BlockRecord]) -> None: ...
    def get_snapshot(self, snapshot_hash: str) -> CorpusSnapshot | None: ...
    def create_snapshot(self) -> CorpusSnapshot: ...
    def get_rarity(self, cid: str) -> float: ...  # inverse document frequency


@dataclass(frozen=True, slots=True)
class BlockRecord:
    """Operational block — occurrence identity + content identity + address.

    `cid` is CONTENT identity: two identical paragraphs share it.
    `instance_id` is OCCURRENCE identity: one exact appearance in one document
    at one ordinal. Evidence, decisions and quarantine reference occurrences,
    because a paragraph copied into two documents is two things to decide about,
    not one (directive §4.1).
    """
    run_id: str
    cid: str
    instance_id: str
    addr: RunAddress
    normalized_content: str
    raw_content: str
    extraction_quality: ExtractionQuality
    heading_path: list[str]
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CorpusSnapshot:
    """Immutable corpus version consumed by runs."""
    snapshot_hash: str
    created_at: datetime
    doc_count: int
    block_count: int
    index_path: Path | None = None
    index_path: Path