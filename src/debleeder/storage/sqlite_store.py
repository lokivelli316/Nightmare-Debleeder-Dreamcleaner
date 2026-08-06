"""SQLite operational store — WAL, synchronous=FULL, outbox-backed."""

from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
import threading
import uuid

from ..core.protocols import (
    BlockRecord, CorpusSnapshot, DecisionRecord, EvidenceRecord,
    LineageRecord, MethodVersion, Profile, RunManifest, RunStore, RunAddress,
    DocumentRole, PatchSuggestion, ReferenceEntry, SuggestionStatus,
    Channel, Disposition, StrengthBand, ConfidenceBand, ExtractionQuality,
)
from ..core.config import Config


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 30000;

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    profile TEXT NOT NULL,
    method_version TEXT NOT NULL,
    corpus_snapshot_hash TEXT NOT NULL,
    started_at TEXT NOT NULL,
    sealed_at TEXT,
    seal_hash TEXT,
    clean_report_hash TEXT,
    xlsx_hash TEXT,
    status TEXT NOT NULL DEFAULT 'RUNNING'
);

CREATE TABLE IF NOT EXISTS blocks (
    run_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    cid TEXT NOT NULL,
    doc_version_hash TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    heading_path_hash TEXT NOT NULL,
    normalized_content TEXT NOT NULL,
    raw_content TEXT NOT NULL,
    extraction_quality TEXT NOT NULL,
    heading_path TEXT NOT NULL,  -- JSON
    metadata TEXT NOT NULL,      -- JSON
    PRIMARY KEY (run_id, instance_id),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_blocks_cid ON blocks(run_id, cid);
CREATE INDEX IF NOT EXISTS idx_blocks_run_ordinal ON blocks(run_id, ordinal);

CREATE TABLE IF NOT EXISTS evidence (
    run_id TEXT NOT NULL,
    pair_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    raw_score REAL NOT NULL,
    strength TEXT NOT NULL,
    confidence TEXT NOT NULL,
    details TEXT NOT NULL,       -- JSON
    method_version TEXT NOT NULL,
    extraction_quality TEXT NOT NULL,
    PRIMARY KEY (run_id, pair_id, channel),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_evidence_pair ON evidence(run_id, pair_id);

-- Append-only: a correction INSERTS a new row. Nothing is ever replaced.
CREATE TABLE IF NOT EXISTS decisions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    block_cid TEXT NOT NULL,
    block_instance_id TEXT NOT NULL,
    disposition TEXT NOT NULL,
    reason TEXT NOT NULL,
    override_note TEXT,
    timestamp TEXT NOT NULL,
    sqlite_txn_id INTEGER NOT NULL,
    jsonl_offset INTEGER NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_decisions_run_block ON decisions(run_id, block_instance_id, seq);

-- Outbox: written in the SAME transaction as the decision. The JSONL journal is
-- a projection of this table, replayed idempotently after commit.
CREATE TABLE IF NOT EXISTS outbox (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    projected INTEGER NOT NULL DEFAULT 0,
    jsonl_offset INTEGER
);

CREATE INDEX IF NOT EXISTS idx_outbox_unprojected ON outbox(projected, created_at);

CREATE TABLE IF NOT EXISTS lineage (
    run_id TEXT NOT NULL,
    prev_cid TEXT NOT NULL,
    new_cid TEXT NOT NULL,
    transform_type TEXT NOT NULL,
    confidence REAL NOT NULL,
    method_version TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    PRIMARY KEY (run_id, prev_cid, new_cid),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS corpus_index (
    cid TEXT PRIMARY KEY,
    doc_count INTEGER NOT NULL DEFAULT 1,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

-- v0.2.1: explicit document role. A file is a reference only when the user
-- says so at import. Nothing is inferred.
CREATE TABLE IF NOT EXISTS run_documents (
    run_id TEXT NOT NULL,
    doc_hash TEXT NOT NULL,
    source_name TEXT NOT NULL,
    role TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (run_id, doc_hash),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS reference_index (
    run_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    cid TEXT NOT NULL,
    doc_hash TEXT NOT NULL,
    source_name TEXT NOT NULL,
    normalized_hash TEXT NOT NULL,
    raw_hash TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    extraction_quality TEXT NOT NULL,
    PRIMARY KEY (run_id, instance_id),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_reference_cid ON reference_index(run_id, cid);

CREATE TABLE IF NOT EXISTS patch_suggestions (
    suggestion_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    working_instance_id TEXT NOT NULL,
    reference_instance_id TEXT NOT NULL,
    reference_doc_hash TEXT NOT NULL,
    reference_source_name TEXT NOT NULL,
    before_hash TEXT NOT NULL,
    current_text TEXT NOT NULL,
    proposed_text TEXT NOT NULL,
    diff TEXT NOT NULL,
    detected_channels TEXT NOT NULL,
    status TEXT NOT NULL,
    requires_human_approval INTEGER NOT NULL DEFAULT 1,
    auto_apply INTEGER NOT NULL DEFAULT 0,
    approval_note TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS corpus_docs (
    doc_id TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS corpus_membership (
    cid TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    PRIMARY KEY (cid, doc_id)
);

CREATE TABLE IF NOT EXISTS corpus_snapshots (
    snapshot_hash TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    doc_count INTEGER NOT NULL,
    block_count INTEGER NOT NULL,
    index_path TEXT NOT NULL
);
"""


@dataclass(slots=True)
class SQLiteStore(RunStore):
    """Thread-safe SQLite store with WAL durability."""

    _conn: sqlite3.Connection
    _lock: threading.RLock
    _txn_counter: int = 0

    def __init__(self, db_path: Path):
        self._txn_counter = 0
        self._conn = sqlite3.connect(
            db_path,
            check_same_thread=False,
            isolation_level=None,  # manual transaction control
        )
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = FULL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    @classmethod
    def open(cls, db_path: Path) -> SQLiteStore:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return cls(db_path)

    @contextmanager
    def _transaction(self):
        """Manual transaction with txn_id tracking."""
        with self._lock:
            self._txn_counter += 1
            txn_id = self._txn_counter
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield txn_id
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # --- Run Management ---

    def create_run(self, profile: Profile, method_version: MethodVersion, corpus_snapshot_hash: str) -> RunManifest:
        run_id = uuid.uuid4().hex[:16]
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._transaction() as txn_id:
            self._conn.execute(
                """INSERT INTO runs (run_id, profile, method_version, corpus_snapshot_hash, started_at, status)
                   VALUES (?, ?, ?, ?, ?, 'RUNNING')""",
                (run_id, profile.value, str(method_version), corpus_snapshot_hash, now),
            )
        return RunManifest(
            run_id=run_id,
            profile=profile,
            method_version=method_version,
            corpus_snapshot_hash=corpus_snapshot_hash,
            started_at=datetime.fromisoformat(now.rstrip("Z")),
            sealed_at=None,
            seal_hash=None,
            clean_report_hash=None,
            xlsx_hash=None,
            status="RUNNING",
        )

    def get_run(self, run_id: str) -> RunManifest | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if not row:
            return None
        return RunManifest(
            run_id=row[0],
            profile=Profile(row[1]),
            method_version=MethodVersion.parse(row[2]) if row[2] else MethodVersion(),
            corpus_snapshot_hash=row[3],
            started_at=datetime.fromisoformat(row[4].rstrip("Z")),
            sealed_at=datetime.fromisoformat(row[5].rstrip("Z")) if row[5] else None,
            seal_hash=row[6],
            clean_report_hash=row[7],
            xlsx_hash=row[8],
            status=row[9],
        )

    def update_run_status(self, run_id: str, status: str) -> None:
        with self._transaction():
            self._conn.execute(
                "UPDATE runs SET status = ? WHERE run_id = ?", (status, run_id)
            )

    def seal_run(self, run_id: str, seal_hash: str, clean_report_hash: str, xlsx_hash: str) -> None:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._transaction():
            self._conn.execute(
                """UPDATE runs SET sealed_at = ?, seal_hash = ?, clean_report_hash = ?, xlsx_hash = ?, status = 'SEALED'
                   WHERE run_id = ?""",
                (now, seal_hash, clean_report_hash, xlsx_hash, run_id),
            )

    # --- Blocks ---

    def put_blocks(self, run_id: str, blocks: list[BlockRecord]) -> None:
        with self._transaction():
            for b in blocks:
                import json
                self._conn.execute(
                    """INSERT OR REPLACE INTO blocks
                       (run_id, instance_id, cid, doc_version_hash, ordinal, heading_path_hash,
                        normalized_content, raw_content, extraction_quality, heading_path, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        b.run_id, b.instance_id, b.cid, b.addr.doc_version_hash, b.addr.ordinal,
                        b.addr.heading_path_hash, b.normalized_content, b.raw_content,
                        b.extraction_quality.value, json.dumps(b.heading_path),
                        json.dumps(b.metadata),
                    ),
                )

    def get_blocks(self, run_id: str):
        with self._lock:
            for row in self._conn.execute(
                "SELECT * FROM blocks WHERE run_id = ? ORDER BY ordinal", (run_id,)
            ):
                import json
                yield BlockRecord(
                    run_id=row[0], instance_id=row[1], cid=row[2],
                    addr=RunAddress(doc_version_hash=row[3], ordinal=row[4],
                                    heading_path_hash=row[5]),
                    normalized_content=row[6], raw_content=row[7],
                    extraction_quality=ExtractionQuality(row[8]),
                    heading_path=json.loads(row[9]), metadata=json.loads(row[10]),
                )

    def get_block(self, run_id: str, instance_id: str) -> BlockRecord | None:
        """Fetch by OCCURRENCE id. Falls back to content id for convenience."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM blocks WHERE run_id = ? AND instance_id = ?",
                (run_id, instance_id)
            ).fetchone()
            if row is None:
                row = self._conn.execute(
                    "SELECT * FROM blocks WHERE run_id = ? AND cid = ? ORDER BY ordinal",
                    (run_id, instance_id)
                ).fetchone()
        if not row:
            return None
        import json
        return BlockRecord(
            run_id=row[0], instance_id=row[1], cid=row[2],
            addr=RunAddress(doc_version_hash=row[3], ordinal=row[4],
                            heading_path_hash=row[5]),
            normalized_content=row[6], raw_content=row[7],
            extraction_quality=ExtractionQuality(row[8]),
            heading_path=json.loads(row[9]), metadata=json.loads(row[10]),
        )

    # --- Evidence ---

    def put_evidence(self, run_id: str, evidence: list[EvidenceRecord]) -> None:
        with self._transaction():
            for e in evidence:
                import json
                self._conn.execute(
                    """INSERT OR REPLACE INTO evidence
                       (run_id, pair_id, channel, raw_score, strength, confidence,
                        details, method_version, extraction_quality)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        e.run_id, e.pair_id, e.channel.value, e.raw_score,
                        e.strength.value, e.confidence.value, json.dumps(e.details),
                        e.method_version, e.extraction_quality.value,
                    ),
                )

    def get_evidence(self, run_id: str, pair_id: str) -> list[EvidenceRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM evidence WHERE run_id = ? AND pair_id = ?", (run_id, pair_id)
            ).fetchall()
        import json
        return [
            EvidenceRecord(
                run_id=r[0], pair_id=r[1], channel=Channel(r[2]), raw_score=r[3],
                strength=StrengthBand(r[4]), confidence=ConfidenceBand(r[5]),
                details=json.loads(r[6]), method_version=r[7],
                extraction_quality=ExtractionQuality(r[8]),
            ) for r in rows
        ]

    def get_all_evidence(self, run_id: str):
        with self._lock:
            for row in self._conn.execute(
                "SELECT * FROM evidence WHERE run_id = ?", (run_id,)
            ):
                import json
                yield EvidenceRecord(
                    run_id=row[0], pair_id=row[1], channel=Channel(row[2]), raw_score=row[3],
                    strength=StrengthBand(row[4]), confidence=ConfidenceBand(row[5]),
                    details=json.loads(row[6]), method_version=row[7],
                    extraction_quality=ExtractionQuality(row[8]),
                )

    # --- Decisions ---

    def put_decision(self, decision: DecisionRecord) -> int:
        """Append a decision row. Returns the assigned seq."""
        with self._transaction() as txn_id:
            cur = self._conn.execute(
                """INSERT INTO decisions
                   (run_id, block_cid, block_instance_id, disposition, reason, override_note,
                    timestamp, sqlite_txn_id, jsonl_offset)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision.run_id, decision.block_cid, decision.block_instance_id,
                    decision.disposition.value,
                    decision.reason, decision.override_note, decision.timestamp.isoformat(),
                    txn_id, decision.jsonl_offset,
                ),
            )
            return cur.lastrowid

    def commit_decision_with_outbox(
        self, decision: DecisionRecord, event_id: str, payload: dict
    ) -> tuple[int, int]:
        """Single SQLite transaction: decision row + outbox event.

        This is the only atomic commit in the system. The JSONL journal is a
        projection of the outbox, replayed after this returns.
        """
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._transaction() as txn_id:
            cur = self._conn.execute(
                """INSERT INTO decisions
                   (run_id, block_cid, block_instance_id, disposition, reason, override_note,
                    timestamp, sqlite_txn_id, jsonl_offset)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, -1)""",
                (
                    decision.run_id, decision.block_cid, decision.block_instance_id,
                    decision.disposition.value,
                    decision.reason, decision.override_note, decision.timestamp.isoformat(),
                    txn_id,
                ),
            )
            seq = cur.lastrowid
            body = dict(payload)
            body["decision_seq"] = seq
            body["event_id"] = event_id
            self._conn.execute(
                """INSERT INTO outbox (event_id, run_id, event_type, payload, created_at, projected)
                   VALUES (?, ?, ?, ?, ?, 0)""",
                (event_id, decision.run_id, "DECISION",
                 json.dumps(body, separators=(",", ":"), sort_keys=True), now),
            )
        return seq, txn_id

    def unprojected_outbox(self):
        """Outbox events not yet acknowledged as written to the journal."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT event_id, run_id, event_type, payload, created_at FROM outbox "
                "WHERE projected = 0 ORDER BY created_at, rowid"
            ).fetchall()
        return [
            {"event_id": r[0], "run_id": r[1], "event_type": r[2],
             "payload": json.loads(r[3]), "created_at": r[4]}
            for r in rows
        ]

    def mark_projected(self, event_id: str, jsonl_offset: int, decision_seq: int | None = None) -> None:
        with self._transaction():
            self._conn.execute(
                "UPDATE outbox SET projected = 1, jsonl_offset = ? WHERE event_id = ?",
                (jsonl_offset, event_id),
            )
            if decision_seq is not None:
                self._conn.execute(
                    "UPDATE decisions SET jsonl_offset = ? WHERE seq = ?",
                    (jsonl_offset, decision_seq),
                )

    def get_decisions(self, run_id: str):
        """Full append-only decision history, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT run_id, block_cid, block_instance_id, disposition, reason,
                          override_note, timestamp, sqlite_txn_id, jsonl_offset
                   FROM decisions WHERE run_id = ? ORDER BY seq""", (run_id,)
            ).fetchall()
        for row in rows:
            yield DecisionRecord(
                run_id=row[0], block_cid=row[1], block_instance_id=row[2],
                disposition=Disposition(row[3]),
                reason=row[4], override_note=row[5],
                timestamp=datetime.fromisoformat(row[6].rstrip("Z")),
                sqlite_txn_id=row[7], jsonl_offset=row[8],
            )

    def get_effective_decisions(self, run_id: str) -> dict[str, DecisionRecord]:
        """Latest decision per block — history preserved, newest wins."""
        effective: dict[str, DecisionRecord] = {}
        for d in self.get_decisions(run_id):
            effective[d.block_instance_id] = d
        return effective

    def get_undecided_blocks(self, run_id: str):
        with self._lock:
            for row in self._conn.execute(
                """SELECT b.* FROM blocks b
                   LEFT JOIN decisions d ON b.run_id = d.run_id
                        AND b.instance_id = d.block_instance_id
                   WHERE b.run_id = ? AND d.block_instance_id IS NULL
                   ORDER BY b.ordinal""", (run_id,)
            ):
                import json
                yield BlockRecord(
                    run_id=row[0], instance_id=row[1], cid=row[2],
                    addr=RunAddress(doc_version_hash=row[3], ordinal=row[4],
                                    heading_path_hash=row[5]),
                    normalized_content=row[6], raw_content=row[7],
                    extraction_quality=ExtractionQuality(row[8]),
                    heading_path=json.loads(row[9]), metadata=json.loads(row[10]),
                )

    # --- v0.2.1: document roles, reference index, patch suggestions ---

    def register_document(self, run_id: str, doc_hash: str, source_name: str,
                          role: DocumentRole) -> None:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._transaction():
            self._conn.execute(
                """INSERT INTO run_documents (run_id, doc_hash, source_name, role, imported_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(run_id, doc_hash) DO UPDATE SET
                     source_name = excluded.source_name, role = excluded.role""",
                (run_id, doc_hash, source_name, role.value, now))

    def get_document_roles(self, run_id: str) -> dict[str, dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT doc_hash, source_name, role FROM run_documents WHERE run_id = ?",
                (run_id,)).fetchall()
        return {r[0]: {"source_name": r[1], "role": r[2]} for r in rows}

    def put_reference_entries(self, run_id: str, entries: list[ReferenceEntry]) -> None:
        with self._transaction():
            for e in entries:
                self._conn.execute(
                    """INSERT OR REPLACE INTO reference_index
                       (run_id, instance_id, cid, doc_hash, source_name,
                        normalized_hash, raw_hash, ordinal, extraction_quality)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (run_id, e.instance_id, e.cid, e.doc_hash, e.source_name,
                     e.normalized_hash, e.raw_hash, e.ordinal, e.extraction_quality))

    def get_reference_entries(self, run_id: str) -> list[ReferenceEntry]:
        """Deterministic order: the manifest hash depends on it."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT doc_hash, source_name, instance_id, cid, normalized_hash,
                          raw_hash, ordinal, extraction_quality
                   FROM reference_index WHERE run_id = ?
                   ORDER BY doc_hash, ordinal, instance_id""", (run_id,)).fetchall()
        return [ReferenceEntry(*r) for r in rows]

    def put_patch_suggestion(self, s: PatchSuggestion) -> None:
        with self._transaction():
            self._conn.execute(
                """INSERT OR REPLACE INTO patch_suggestions
                   (suggestion_id, run_id, working_instance_id, reference_instance_id,
                    reference_doc_hash, reference_source_name, before_hash, current_text,
                    proposed_text, diff, detected_channels, status,
                    requires_human_approval, auto_apply, approval_note, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (s.suggestion_id, s.run_id, s.working_instance_id, s.reference_instance_id,
                 s.reference_doc_hash, s.reference_source_name, s.before_hash, s.current_text,
                 s.proposed_text, s.diff, json.dumps(list(s.detected_channels)), s.status,
                 int(s.requires_human_approval), int(s.auto_apply), s.approval_note,
                 s.created_at))

    def _suggestion_from_row(self, row) -> PatchSuggestion:
        return PatchSuggestion(
            suggestion_id=row[0], run_id=row[1], working_instance_id=row[2],
            reference_instance_id=row[3], reference_doc_hash=row[4],
            reference_source_name=row[5], before_hash=row[6], current_text=row[7],
            proposed_text=row[8], diff=row[9],
            detected_channels=tuple(json.loads(row[10])), status=row[11],
            requires_human_approval=bool(row[12]), auto_apply=bool(row[13]),
            approval_note=row[14], created_at=row[15])

    def get_patch_suggestions(self, run_id: str) -> list[PatchSuggestion]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM patch_suggestions WHERE run_id = ? ORDER BY suggestion_id",
                (run_id,)).fetchall()
        return [self._suggestion_from_row(r) for r in rows]

    def get_patch_suggestion(self, run_id: str, suggestion_id: str) -> PatchSuggestion | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM patch_suggestions WHERE run_id = ? AND suggestion_id = ?",
                (run_id, suggestion_id)).fetchone()
        return self._suggestion_from_row(row) if row else None

    def set_suggestion_status(self, run_id: str, suggestion_id: str,
                              status: str, note: str | None = None) -> None:
        with self._transaction():
            self._conn.execute(
                "UPDATE patch_suggestions SET status = ?, approval_note = COALESCE(?, approval_note) "
                "WHERE run_id = ? AND suggestion_id = ?",
                (status, note, run_id, suggestion_id))

    def replace_block_content(self, run_id: str, instance_id: str, new_raw: str) -> None:
        """Test/repair hook: mutate the stored WORKING COPY of a block.

        This never touches an imported original. It exists so staleness can be
        exercised, and so an approved edit can be reflected in the store.
        """
        with self._transaction():
            self._conn.execute(
                "UPDATE blocks SET raw_content = ? WHERE run_id = ? AND instance_id = ?",
                (new_raw, run_id, instance_id))

    # --- Lineage ---

    def put_lineage(self, run_id: str, lineage: list[LineageRecord]) -> None:
        with self._transaction():
            for l in lineage:
                self._conn.execute(
                    """INSERT OR REPLACE INTO lineage
                       (run_id, prev_cid, new_cid, transform_type, confidence, method_version, timestamp)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run_id, l.prev_cid, l.new_cid, l.transform_type,
                        l.confidence, l.method_version, l.timestamp.isoformat(),
                    ),
                )

    def get_lineage(self, run_id: str):
        with self._lock:
            for row in self._conn.execute(
                "SELECT * FROM lineage WHERE run_id = ?", (run_id,)
            ):
                yield LineageRecord(
                    prev_cid=row[1], new_cid=row[2], transform_type=row[3],
                    confidence=row[4], method_version=row[5],
                    timestamp=datetime.fromisoformat(row[6].rstrip("Z")),
                )

    # --- Corpus Index ---

    def add_document(self, doc_id: str, blocks: list[BlockRecord]) -> None:
        """Register a document in the corpus index.

        doc_count is the number of DISTINCT DOCUMENTS a cid appears in, so a
        block repeated twice inside one document still counts once. This is what
        makes get_rarity a real inverse-document-frequency.
        """
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._transaction():
            self._conn.execute(
                """INSERT INTO corpus_docs (doc_id, first_seen) VALUES (?, ?)
                   ON CONFLICT(doc_id) DO NOTHING""", (doc_id, now))
            for cid in sorted({b.cid for b in blocks}):
                self._conn.execute(
                    """INSERT INTO corpus_membership (cid, doc_id) VALUES (?, ?)
                       ON CONFLICT(cid, doc_id) DO NOTHING""", (cid, doc_id))
                self._conn.execute(
                    """INSERT INTO corpus_index (cid, doc_count, first_seen, last_seen)
                       VALUES (?, 0, ?, ?)
                       ON CONFLICT(cid) DO UPDATE SET last_seen = excluded.last_seen""",
                    (cid, now, now))
                self._conn.execute(
                    """UPDATE corpus_index SET doc_count =
                       (SELECT COUNT(*) FROM corpus_membership WHERE cid = ?)
                       WHERE cid = ?""", (cid, cid))

    def get_snapshot(self, snapshot_hash: str) -> CorpusSnapshot | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM corpus_snapshots WHERE snapshot_hash = ?", (snapshot_hash,)
            ).fetchone()
        if not row:
            return None
        return CorpusSnapshot(
            snapshot_hash=row[0],
            created_at=datetime.fromisoformat(row[1].rstrip("Z")),
            doc_count=row[2], block_count=row[3], index_path=Path(row[4]),
        )

    def create_snapshot(self) -> CorpusSnapshot:
        with self._lock:
            import hashlib
            # Hash current corpus index state
            rows = list(self._conn.execute(
                "SELECT cid, doc_count, first_seen, last_seen FROM corpus_index ORDER BY cid"
            ))
            content = "".join(f"{r[0]}|{r[1]}|{r[2]}|{r[3]}" for r in rows)
            snapshot_hash = hashlib.sha256(content.encode()).hexdigest()[:32]
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            doc_count = self._conn.execute("SELECT COUNT(*) FROM corpus_docs").fetchone()[0] or 0
            block_count = len(rows)
            index_path = f"corpus_snapshots/{snapshot_hash}.sqlite"
            self._conn.execute(
                """INSERT OR REPLACE INTO corpus_snapshots
                   (snapshot_hash, created_at, doc_count, block_count, index_path)
                   VALUES (?, ?, ?, ?, ?)""",
                (snapshot_hash, now, doc_count, block_count, index_path),
            )
        return CorpusSnapshot(
            snapshot_hash=snapshot_hash,
            created_at=datetime.fromisoformat(now.rstrip("Z")),
            doc_count=doc_count, block_count=block_count, index_path=Path(index_path),
        )

    def get_rarity(self, cid: str) -> float:
        """Smoothed inverse document frequency.

        Bleed is a *distributional* property: content in exactly two documents is
        a seam, content in most documents is the author's own voice. High return
        value means rare, therefore interesting.
        """
        import math
        with self._lock:
            total_docs = self._conn.execute(
                "SELECT COUNT(*) FROM corpus_docs").fetchone()[0] or 0
            row = self._conn.execute(
                "SELECT doc_count FROM corpus_index WHERE cid = ?", (cid,)).fetchone()
        if total_docs == 0:
            return 0.0
        df = row[0] if row and row[0] else 0
        return math.log((total_docs + 1) / (df + 1))

    def corpus_totals(self) -> tuple[int, int]:
        with self._lock:
            docs = self._conn.execute("SELECT COUNT(*) FROM corpus_docs").fetchone()[0] or 0
            cids = self._conn.execute("SELECT COUNT(*) FROM corpus_index").fetchone()[0] or 0
        return docs, cids

    def close(self) -> None:
        with self._lock:
            self._conn.close()