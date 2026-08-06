"""Decision persistence — one SQLite transaction, then journal projection.

Doctrine (directive §4.2): the decision row and its outbox event commit together
in a SINGLE SQLite transaction. The JSONL journal is a deterministic projection
of the outbox, replayed idempotently after commit. SQLite and a flat file do NOT
share an atomic commit and this module does not pretend they do.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import uuid

from ..core.protocols import DecisionRecord, Disposition
from ..storage.audit_journal import JSONLAuditJournal
from ..storage.sqlite_store import SQLiteStore


@dataclass(slots=True)
class DecisionPersister:
    """Outbox-backed decision writer with crash-safe projection."""

    store: SQLiteStore
    journal: JSONLAuditJournal

    # --- projection ---

    def project_pending(self) -> int:
        """Write any committed-but-unprojected outbox events to the journal.

        Idempotent: an event already present in the journal (matched on event_id)
        is acknowledged rather than written twice. Safe to call on every startup
        and after every decision.
        """
        pending = self.store.unprojected_outbox()
        if not pending:
            return 0

        already = {}
        for offset, rec in self.journal.read_range(0, None):
            eid = rec.get("event_id")
            if eid:
                already[eid] = offset

        projected = 0
        for event in pending:
            eid = event["event_id"]
            payload = event["payload"]
            seq = payload.get("decision_seq")
            if eid in already:
                self.store.mark_projected(eid, already[eid], seq)
                continue
            record = {"type": event["event_type"], **payload}
            offset = self.journal.append(record)
            self.store.mark_projected(eid, offset, seq)
            projected += 1
        return projected

    # --- write path ---

    def record_decision(
        self,
        run_id: str,
        block_cid: str,
        block_instance_id: str,
        disposition: Disposition,
        reason: str,
        override_note: str | None = None,
    ) -> DecisionRecord:
        """Commit a decision, then project it. Never overwrites earlier decisions."""
        timestamp = datetime.now(timezone.utc)
        event_id = uuid.uuid4().hex

        draft = DecisionRecord(
            run_id=run_id,
            block_cid=block_cid,
            block_instance_id=block_instance_id,
            disposition=disposition,
            reason=reason,
            override_note=override_note,
            timestamp=timestamp,
            sqlite_txn_id=0,
            jsonl_offset=-1,
        )
        payload = {
            "run_id": run_id,
            "block_cid": block_cid,
            "block_instance_id": block_instance_id,
            "disposition": disposition.value,
            "reason": reason,
            "override_note": override_note,
            "timestamp": timestamp.isoformat(),
        }

        # 1. Atomic: decision + outbox event.
        seq, txn_id = self.store.commit_decision_with_outbox(draft, event_id, payload)

        # 2. Projection. A crash here leaves the outbox row unprojected; the next
        #    project_pending() call completes it. Nothing is lost or duplicated.
        self.project_pending()

        for d in self.store.get_decisions(run_id):
            if d.block_instance_id == block_instance_id and d.sqlite_txn_id == txn_id:
                return d
        return DecisionRecord(
            run_id=run_id, block_cid=block_cid, block_instance_id=block_instance_id,
            disposition=disposition,
            reason=reason, override_note=override_note, timestamp=timestamp,
            sqlite_txn_id=txn_id, jsonl_offset=-1,
        )

    # --- read path ---

    def get_decision(self, run_id: str, block_instance_id: str) -> DecisionRecord | None:
        """Latest decision for one occurrence. Full history via get_history."""
        return self.store.get_effective_decisions(run_id).get(block_instance_id)

    def get_history(self, run_id: str, block_instance_id: str) -> list[DecisionRecord]:
        return [d for d in self.store.get_decisions(run_id)
                if d.block_instance_id == block_instance_id]

    def verify_dual_write(self, run_id: str) -> tuple[bool, list[str]]:
        """Check SQLite decisions against the journal projection.

        Returns (ok, problems). Called as a blocking gate before sealing.
        """
        problems: list[str] = []

        if not self.journal.verify_chain():
            problems.append("audit chain failed verification")

        sqlite_decisions = list(self.store.get_decisions(run_id))
        journal_decisions = [
            rec for _off, rec in self.journal.read_range(0, None)
            if rec.get("type") == "DECISION" and rec.get("run_id") == run_id
        ]

        pending = [e for e in self.store.unprojected_outbox() if e["run_id"] == run_id]
        if pending:
            problems.append(f"{len(pending)} outbox event(s) not yet projected")

        if len(sqlite_decisions) != len(journal_decisions):
            problems.append(
                f"count mismatch: {len(sqlite_decisions)} in SQLite, "
                f"{len(journal_decisions)} in journal"
            )

        by_seq = {r.get("decision_seq"): r for r in journal_decisions}
        for idx, d in enumerate(sqlite_decisions, start=1):
            match = by_seq.get(idx) or next(
                (r for r in journal_decisions
                 if r.get("block_instance_id") == d.block_instance_id
                 and r.get("disposition") == d.disposition.value
                 and r.get("reason") == d.reason), None)
            if match is None:
                problems.append(f"decision for {d.block_instance_id} missing from journal")
                continue
            if match.get("disposition") != d.disposition.value:
                problems.append(f"disposition mismatch for {d.block_instance_id}")
            if match.get("reason") != d.reason:
                problems.append(f"reason mismatch for {d.block_instance_id}")

        return (not problems), problems
