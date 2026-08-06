"""JSONL audit journal — append-only, hash-chained, tamper-preserving.

Doctrine: history is append-only and corruption is NEVER silently truncated.
On chain mismatch the journal refuses to append, preserves the original bytes,
copies the damaged tail to quarantine, and opens a recorded recovery branch.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import shutil
import threading

GENESIS = "0" * 64


class JournalIntegrityError(Exception):
    """Raised when the hash chain does not verify. Carries recovery details."""

    def __init__(self, message: str, *, path: Path, bad_offset: int,
                 quarantine_path: Path | None = None,
                 recovery_branch: Path | None = None):
        super().__init__(message)
        self.path = path
        self.bad_offset = bad_offset
        self.quarantine_path = quarantine_path
        self.recovery_branch = recovery_branch


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_DIRECTORY | os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def canonical_envelope(record: dict, prev_hash: str) -> bytes:
    """The exact bytes that get hashed. Used by BOTH writer and verifier.

    The record passed here must NOT contain `_audit_hash`; it MUST contain
    `_audit_prev_hash` and `_audit_timestamp`.
    """
    body = json.dumps(record, separators=(",", ":"), sort_keys=True)
    return (prev_hash + body).encode("utf-8")


def compute_hash(record: dict, prev_hash: str) -> str:
    return hashlib.sha256(canonical_envelope(record, prev_hash)).hexdigest()


class JSONLAuditJournal:
    """Hash-chained JSONL. Each record commits to the previous record's hash."""

    def __init__(self, path: Path, *, allow_recovery_branch: bool = True):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._prev_hash = GENESIS
        self._offset = 0
        self._sealed_readonly = False
        self._allow_recovery_branch = allow_recovery_branch

        created = not self._path.exists()
        # O_APPEND: the kernel forces every write to the end. The handle is
        # structurally incapable of overwriting earlier offsets.
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        self._file = os.fdopen(fd, "r+b", buffering=0)
        if created:
            _fsync_dir(self._path.parent)
        self._scan_chain()

    # --- integrity ---

    def _iter_lines(self):
        """Yield (offset, raw_line_bytes) reading the whole file once."""
        with open(self._path, "rb") as fh:
            data = fh.read()
        offset = 0
        for raw in data.splitlines(keepends=True):
            yield offset, raw
            offset += len(raw)

    def _scan_chain(self) -> None:
        """Verify the existing chain. NEVER truncates."""
        prev = GENESIS
        end = 0
        for offset, raw in self._iter_lines():
            line = raw.rstrip(b"\n")
            if not line.strip():
                end = offset + len(raw)
                continue
            try:
                record = json.loads(line)
                stored_hash = record.pop("_audit_hash")
                stored_prev = record.get("_audit_prev_hash")
                if stored_prev != prev:
                    raise ValueError("prev_hash link broken")
                if compute_hash(record, stored_prev) != stored_hash:
                    raise ValueError("record hash mismatch")
            except Exception as exc:
                self._handle_corruption(offset, exc)
                return
            prev = stored_hash
            end = offset + len(raw)
        self._prev_hash = prev
        self._offset = end

    def _handle_corruption(self, bad_offset: int, exc: Exception) -> None:
        """Preserve bytes, quarantine the tail, open a recovery branch, refuse writes."""
        self._sealed_readonly = True
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        qdir = self._path.parent / "quarantine"
        qdir.mkdir(parents=True, exist_ok=True)

        with open(self._path, "rb") as fh:
            fh.seek(bad_offset)
            tail = fh.read()
        qpath = qdir / f"{self._path.name}.tail_{bad_offset}_{stamp}.bin"
        qpath.write_bytes(tail)

        original_copy = qdir / f"{self._path.name}.preserved_{stamp}.jsonl"
        shutil.copy2(self._path, original_copy)

        branch = None
        if self._allow_recovery_branch:
            branch = self._path.parent / f"{self._path.stem}.recovery_{stamp}.jsonl"
            damaged_hash = hashlib.sha256(self._path.read_bytes()).hexdigest()
            branch_journal = JSONLAuditJournal(branch, allow_recovery_branch=False)
            branch_journal.append({
                "type": "RECOVERY_BRANCH_OPENED",
                "damaged_journal": str(self._path),
                "damaged_journal_sha256": damaged_hash,
                "damaged_journal_bytes": self._path.stat().st_size,
                "first_bad_offset": bad_offset,
                "quarantined_tail": str(qpath),
                "preserved_copy": str(original_copy),
                "reason": str(exc),
            })
            branch_journal.close()

        _fsync_dir(qdir)
        raise JournalIntegrityError(
            f"Audit chain broken at byte offset {bad_offset}: {exc}. "
            f"History preserved; journal is read-only. Tail quarantined at {qpath}.",
            path=self._path, bad_offset=bad_offset,
            quarantine_path=qpath, recovery_branch=branch,
        )

    # --- append ---

    def append(self, record: dict) -> int:
        if self._sealed_readonly:
            raise JournalIntegrityError(
                "Journal is read-only after a detected integrity failure.",
                path=self._path, bad_offset=self._offset,
            )
        with self._lock:
            record = dict(record)
            record["_audit_prev_hash"] = self._prev_hash
            record["_audit_timestamp"] = _utcnow()
            digest = compute_hash(record, self._prev_hash)
            record["_audit_hash"] = digest

            line = json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
            payload = line.encode("utf-8")
            offset = self._offset
            self._file.write(payload)
            self._file.flush()
            os.fsync(self._file.fileno())
            self._offset += len(payload)   # bytes, not characters
            self._prev_hash = digest
            return offset

    # --- read / verify ---

    def verify_chain(self, up_to_offset: int | None = None) -> bool:
        prev = GENESIS
        for offset, raw in self._iter_lines():
            if up_to_offset is not None and offset >= up_to_offset:
                break
            line = raw.rstrip(b"\n")
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                stored_hash = record.pop("_audit_hash")
                stored_prev = record.get("_audit_prev_hash")
                if stored_prev != prev:
                    return False
                if compute_hash(record, stored_prev) != stored_hash:
                    return False
                prev = stored_hash
            except Exception:
                return False
        return True

    def read_range(self, start: int = 0, end: int | None = None):
        """Yield (offset, record) pairs in [start, end)."""
        for offset, raw in self._iter_lines():
            if offset < start:
                continue
            if end is not None and offset >= end:
                break
            line = raw.rstrip(b"\n")
            if not line.strip():
                continue
            yield offset, json.loads(line)

    @property
    def offset(self) -> int:
        return self._offset

    @property
    def head_hash(self) -> str:
        return self._prev_hash

    def close(self) -> None:
        with self._lock:
            if not self._file.closed:
                self._file.close()
