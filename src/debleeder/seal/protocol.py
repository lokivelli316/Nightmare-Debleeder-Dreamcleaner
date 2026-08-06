"""Stage 6 seal — explicit state machine, atomic publish, portable bundle.

Ordering doctrine: the run is marked SEALED only AFTER the bundle is durably
published and its PUBLISHED bytes have been re-read and verified. A crash at any
boundary leaves a state the next startup can name and reconcile.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import shutil

from ..core.config import Config
from ..core.protocols import Disposition, RunManifest, RunStore
from ..import_.baseline import generate_baseline_xlsx
from ..rebuild.generator import generate_clean_report

# Explicit, persisted states (directive §4.4)
STATE_RUNNING = "RUNNING"
STATE_PREPARED = "PREPARED"
STATE_OUTPUTS_WRITTEN = "OUTPUTS_WRITTEN"
STATE_OUTPUTS_VERIFIED = "OUTPUTS_VERIFIED"
STATE_BUNDLE_READY = "BUNDLE_READY"
STATE_BUNDLE_PUBLISHED = "BUNDLE_PUBLISHED"
STATE_SEALED = "SEALED"
STATE_SEAL_FAILED = "SEAL_FAILED"

SEAL_STATES = (
    STATE_RUNNING, STATE_PREPARED, STATE_OUTPUTS_WRITTEN, STATE_OUTPUTS_VERIFIED,
    STATE_BUNDLE_READY, STATE_BUNDLE_PUBLISHED, STATE_SEALED,
)

BLOCKING_DISPOSITIONS = {Disposition.REVIEW}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_DIRECTORY | os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(slots=True)
class SealResult:
    success: bool
    seal_hash: str | None
    clean_report_hash: str | None
    xlsx_hash: str | None
    bundle_path: Path | None
    error: str | None
    state: str = STATE_RUNNING
    blockers: tuple[str, ...] = ()


class Stage6Sealer:
    """Crash-reconcilable seal: build → fsync → verify → rename → verify → mark."""

    def __init__(self, config: Config, store: RunStore, persister=None, core=None):
        self.config = config
        self.store = store
        self.persister = persister
        self.core = core

    # --- gate (directive §4.6: no implicit decisions) ---

    def preflight(self, run_id: str) -> list[str]:
        blockers: list[str] = []

        undecided = list(self.store.get_undecided_blocks(run_id))
        if undecided:
            blockers.append(f"{len(undecided)} block(s) have no decision")

        effective = self.store.get_effective_decisions(run_id)
        review = [cid for cid, d in effective.items() if d.disposition in BLOCKING_DISPOSITIONS]
        if review:
            blockers.append(f"{len(review)} block(s) still marked REVIEW")

        rewrite_unresolved = [
            cid for cid, d in effective.items()
            if d.disposition == Disposition.REWRITE and not (d.override_note or "").strip()
        ]
        if rewrite_unresolved:
            blockers.append(
                f"{len(rewrite_unresolved)} REWRITE block(s) have no approved replacement artifact")

        if self.persister is not None:
            ok, problems = self.persister.verify_dual_write(run_id)
            if not ok:
                blockers.extend(f"audit: {p}" for p in problems)

        return blockers

    # --- seal ---

    def seal(self, run_id: str, manifest: RunManifest, output_dir: Path) -> SealResult:
        state = STATE_RUNNING
        try:
            blockers = self.preflight(run_id)
            if blockers:
                return SealResult(False, None, None, None, None,
                                  "Seal refused: unresolved preconditions.",
                                  state=STATE_RUNNING, blockers=tuple(blockers))

            bundles_root = Path(output_dir) / "bundles"
            bundles_root.mkdir(parents=True, exist_ok=True)
            final_dir = bundles_root / f"run_{run_id}"
            # Sibling on the SAME filesystem so the publish is a real rename.
            staging = bundles_root / f".staging_run_{run_id}"
            if staging.exists():
                shutil.rmtree(staging)
            staging.mkdir(parents=True)

            state = STATE_PREPARED
            self.store.update_run_status(run_id, state)

            # 1. Artifacts into staging
            clean_path = staging / "clean_report.md"
            generate_clean_report(run_id, self.store, clean_path)
            xlsx_path = staging / "evidence.xlsx"
            xlsx_hash = generate_baseline_xlsx(run_id, self.store, xlsx_path, manifest)

            blocks_path = staging / "blocks.jsonl"
            with open(blocks_path, "w", encoding="utf-8") as fh:
                for b in sorted(self.store.get_blocks(run_id), key=lambda x: (x.addr.doc_version_hash, x.addr.ordinal)):
                    fh.write(json.dumps({
                        "instance_id": b.instance_id,
                        "cid": b.cid,
                        "doc_version_hash": b.addr.doc_version_hash,
                        "ordinal": b.addr.ordinal,
                        "heading_path_hash": b.addr.heading_path_hash,
                        "normalized_content": b.normalized_content,
                        "raw_content": b.raw_content,
                        "extraction_quality": b.extraction_quality.value,
                        "heading_path": b.heading_path,
                        "metadata": b.metadata,
                    }, separators=(",", ":"), sort_keys=True) + "\n")

            decisions_path = staging / "decisions.jsonl"
            with open(decisions_path, "w", encoding="utf-8") as fh:
                for d in self.store.get_decisions(run_id):
                    fh.write(json.dumps({
                        "block_cid": d.block_cid,
                        "block_instance_id": d.block_instance_id,
                        "disposition": d.disposition.value,
                        "reason": d.reason,
                        "override_note": d.override_note,
                        "timestamp": d.timestamp.isoformat(),
                        "sqlite_txn_id": d.sqlite_txn_id,
                        "jsonl_offset": d.jsonl_offset,
                    }, separators=(",", ":"), sort_keys=True) + "\n")

            quarantine_path = staging / "quarantine.jsonl"
            effective = self.store.get_effective_decisions(run_id)
            removed = {Disposition.QUARANTINE, Disposition.SHRED, Disposition.PURGE}
            with open(quarantine_path, "w", encoding="utf-8") as fh:
                for b in sorted(self.store.get_blocks(run_id), key=lambda x: (x.addr.doc_version_hash, x.addr.ordinal)):
                    d = effective.get(b.instance_id)
                    if d and d.disposition in removed:
                        fh.write(json.dumps({
                            "instance_id": b.instance_id,
                            "cid": b.cid,
                            "ordinal": b.addr.ordinal,
                            "disposition": d.disposition.value,
                            "reason": d.reason,
                            "original_content": b.raw_content,
                            "normalized_content": b.normalized_content,
                        }, separators=(",", ":"), sort_keys=True) + "\n")

            # v0.2.1: Reference Documents must survive into bundle-only inspection.
            reference_manifest = (
                self.core.reference_manifest(run_id) if self.core is not None
                else {"run_id": run_id, "format": "debleeder-reference-manifest/1",
                      "entries": [], "manifest_hash": ""})
            (staging / "reference_manifest.json").write_text(
                json.dumps(reference_manifest, separators=(",", ":"), sort_keys=True),
                encoding="utf-8")

            suggestions_path = staging / "patch_suggestions.jsonl"
            with open(suggestions_path, "w", encoding="utf-8") as fh:
                for sug in self.store.get_patch_suggestions(run_id):
                    fh.write(json.dumps(sug.to_json(), separators=(",", ":"),
                                        sort_keys=True) + "\n")

            journal_src = Path(self.config.data_dir) / "audit.jsonl"
            if journal_src.exists():
                shutil.copy2(journal_src, staging / "audit.jsonl")

            (staging / "LIMITATIONS.md").write_text(
                "# Bundle limitations\n\n"
                "- Evidence thresholds are PROVISIONAL and uncalibrated.\n"
                "- Scores are triage, not verdicts. No guilt is asserted or implied.\n"
                "- Direction of carryover is reported as UNKNOWN unless lineage supports it.\n"
                "- Extraction quality varies by source format and is recorded per block.\n",
                encoding="utf-8")

            clean_hash = _sha256(clean_path)

            files = {}
            for f in sorted(staging.iterdir()):
                if f.is_file() and f.name != "manifest.json":
                    files[f.name] = _sha256(f)
                    _fsync_file(f)

            bundle_manifest = {
                "run_id": run_id,
                "profile": manifest.profile.value,
                "method_version": str(manifest.method_version),
                "corpus_snapshot_hash": manifest.corpus_snapshot_hash,
                "clean_report_hash": clean_hash,
                "xlsx_hash": xlsx_hash,
                "sealed_at": _utcnow(),
                "files": files,
                "bundle_format": "debleeder-bundle/1",
            }
            seal_hash = hashlib.sha256(json.dumps(
                {k: v for k, v in bundle_manifest.items() if k != "sealed_at"},
                separators=(",", ":"), sort_keys=True).encode()).hexdigest()
            bundle_manifest["seal_hash"] = seal_hash

            manifest_path = staging / "manifest.json"
            manifest_path.write_text(json.dumps(
                bundle_manifest, separators=(",", ":"), sort_keys=True), encoding="utf-8")
            _fsync_file(manifest_path)
            _fsync_dir(staging)

            state = STATE_OUTPUTS_WRITTEN
            self.store.update_run_status(run_id, state)

            # 2. Verify staged bytes
            for name, digest in files.items():
                if _sha256(staging / name) != digest:
                    raise RuntimeError(f"staged artifact {name} failed verification")

            state = STATE_OUTPUTS_VERIFIED
            self.store.update_run_status(run_id, state)
            state = STATE_BUNDLE_READY
            self.store.update_run_status(run_id, state)

            # 3. Atomic publish
            if final_dir.exists():
                shutil.rmtree(final_dir)
            os.rename(staging, final_dir)
            _fsync_dir(bundles_root)

            state = STATE_BUNDLE_PUBLISHED
            self.store.update_run_status(run_id, state)

            # 4. Verify the PUBLISHED bytes, not the staged ones
            published = json.loads((final_dir / "manifest.json").read_text())
            for name, digest in published["files"].items():
                target = final_dir / name
                if not target.exists():
                    raise RuntimeError(f"published bundle missing {name}")
                if _sha256(target) != digest:
                    raise RuntimeError(f"published artifact {name} failed verification")

            # 5. Only now is the run sealed.
            self.store.seal_run(run_id, seal_hash, clean_hash, xlsx_hash)
            state = STATE_SEALED

            return SealResult(True, seal_hash, clean_hash, xlsx_hash, final_dir, None, state=state)

        except Exception as exc:
            try:
                self.store.update_run_status(run_id, STATE_SEAL_FAILED)
            except Exception:
                pass
            return SealResult(False, None, None, None, None,
                              f"Seal failed in state {state}: {exc}", state=state)

    # --- restart reconciliation ---

    def reconcile(self, run_id: str, output_dir: Path) -> dict:
        """Name the state after an interrupted seal. Never guesses."""
        manifest = self.store.get_run(run_id)
        if manifest is None:
            return {"run_id": run_id, "state": "UNKNOWN", "action": "no such run"}

        state = manifest.status
        bundles_root = Path(output_dir) / "bundles"
        staging = bundles_root / f".staging_run_{run_id}"
        final_dir = bundles_root / f"run_{run_id}"

        report = {
            "run_id": run_id, "state": state,
            "staging_present": staging.exists(),
            "published_present": final_dir.exists(),
        }

        if state == STATE_SEALED and not final_dir.exists():
            report["action"] = "INCONSISTENT: marked SEALED with no published bundle — re-seal required"
            report["trustworthy"] = False
        elif state in (STATE_PREPARED, STATE_OUTPUTS_WRITTEN, STATE_OUTPUTS_VERIFIED, STATE_BUNDLE_READY):
            report["action"] = "incomplete seal — discard staging and re-seal; run is NOT sealed"
            report["trustworthy"] = False
        elif state == STATE_BUNDLE_PUBLISHED:
            report["action"] = "bundle published but run not marked sealed — verify bundle, then finalize"
            report["trustworthy"] = False
        elif state == STATE_SEALED:
            report["action"] = "sealed and published"
            report["trustworthy"] = True
        else:
            report["action"] = "run in progress"
            report["trustworthy"] = True
        return report
