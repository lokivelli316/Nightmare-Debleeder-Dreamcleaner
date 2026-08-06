"""Stage orchestrator — enforces canonical workflow order, no silent reorder."""

from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import json
from typing import Callable

from .protocols import (
    Profile, RunManifest, MethodVersion, Channel, StrengthBand,
    Disposition, DecisionRecord, BlockRecord, BlockIdentity, RunAddress,
    ExtractionQuality, DocumentRole, PatchSuggestion, ApplyResult,
    ReferenceEntry, SuggestionStatus,
)
from .config import Config
from ..storage.sqlite_store import SQLiteStore
from ..storage.audit_journal import JSONLAuditJournal
from ..import_.extractors import extract_file, normalize_text
from ..import_.baseline import generate_baseline_xlsx
from ..evidence.candidate_funnel import CandidateFunnel
from ..evidence.channels import (
    PhraseChannel, NumericChannel, SymbolChannel,
    CitationChannel, EquationChannel, LineageChannel, ReferenceChannel,
)
from ..triage.engine import TriageEngine
from ..decisions.persistence import DecisionPersister
from ..seal.protocol import Stage6Sealer
from ..rebuild.generator import generate_clean_report, rebuild_from_bundle


@dataclass(slots=True)
class DebleederCore:
    """Core engine — all stages, shared by CLI and GUI."""

    config: Config
    store: SQLiteStore
    journal: JSONLAuditJournal
    persister: DecisionPersister
    sealer: Stage6Sealer
    triage: TriageEngine

    # Stage functions in canonical order — DO NOT REORDER
    STAGES = (
        "import",
        "baseline",
        "analyze",
        "evaluate",
        "choose",      # human decisions
        "seal",
        "generate",
    )

    def __init__(self, config: Config):
        self.config = config
        data_dir = config.data_dir
        data_dir.mkdir(parents=True, exist_ok=True)

        self.store = SQLiteStore.open(data_dir / "debleeder.sqlite")
        self.journal = JSONLAuditJournal(data_dir / "audit.jsonl")
        self.persister = DecisionPersister(self.store, self.journal)
        self.sealer = Stage6Sealer(config, self.store, self.persister, core=self)
        # Complete any projection interrupted by a crash before this start.
        self.persister.project_pending()
        self.triage = TriageEngine(config)

    # --- Stage 1: IMPORT ---

    def stage_import(
        self,
        run_id: str,
        source_paths: list[Path],
        reference_paths: list[Path] | None = None,
    ) -> dict[str, int]:
        """Import documents → extract blocks → store with identity/address/role.

        `reference_paths` declares Reference Documents explicitly. Nothing becomes
        a reference by inference; callers that pass only `source_paths` keep the
        previous behaviour exactly.
        """
        import hashlib

        doc_counts: dict[str, int] = {}
        jobs = [(p, DocumentRole.WORKING) for p in source_paths]
        jobs += [(p, DocumentRole.REFERENCE) for p in (reference_paths or [])]

        reference_entries: list[ReferenceEntry] = []

        for src_path, role in jobs:
            doc_hash = hashlib.sha256(src_path.read_bytes()).hexdigest()
            self.store.register_document(run_id, doc_hash, src_path.name, role)
            blocks_data = []

            for idx, extracted in enumerate(extract_file(src_path, self.config)):
                # Identity is computed over NORMALIZED text so trivial casing or
                # whitespace differences do not read as different content.
                normalized = normalize_text(extracted.content)
                raw = extracted.raw_content or extracted.content
                identity = BlockIdentity.from_content(normalized)

                heading_path_hash = hashlib.sha256(
                    ">".join(extracted.heading_path).encode()
                ).hexdigest()[:8]
                addr = RunAddress(doc_hash, idx, heading_path_hash)

                # Occurrence identity: one exact appearance, deterministic and
                # REPRODUCIBLE ACROSS RUNS. The run id is deliberately excluded
                # so the same document yields the same occurrence ids every time
                # (blocks are scoped by run_id in the primary key regardless).
                instance_id = hashlib.sha256(
                    f"{doc_hash}|{idx}|{identity.cid}".encode()
                ).hexdigest()[:16]

                metadata = dict(extracted.metadata or {})
                metadata["source_name"] = src_path.name
                metadata["document_role"] = role.value
                metadata["doc_hash"] = doc_hash

                block = BlockRecord(
                    run_id=run_id,
                    cid=identity.cid,
                    instance_id=instance_id,
                    addr=addr,
                    normalized_content=normalized,
                    raw_content=raw,
                    extraction_quality=extracted.extraction_quality,
                    heading_path=extracted.heading_path,
                    metadata=metadata,
                )
                blocks_data.append(block)

                if role is DocumentRole.REFERENCE:
                    reference_entries.append(ReferenceEntry(
                        doc_hash=doc_hash,
                        source_name=src_path.name,
                        instance_id=instance_id,
                        cid=identity.cid,
                        normalized_hash=hashlib.sha256(normalized.encode()).hexdigest(),
                        raw_hash=hashlib.sha256(raw.encode()).hexdigest(),
                        ordinal=idx,
                        extraction_quality=extracted.extraction_quality.value,
                    ))

            self.store.put_blocks(run_id, blocks_data)
            doc_counts[src_path.name] = len(blocks_data)
            self.store.add_document(doc_hash, blocks_data)

        if reference_entries:
            self.store.put_reference_entries(run_id, reference_entries)

        return doc_counts

    # --- Reference index ---

    def _reference_index(self, run_id: str) -> dict[str, dict]:
        """instance_id -> reference entry, for the reference channel."""
        return {e.instance_id: e.to_json() for e in self.store.get_reference_entries(run_id)}

    def reference_manifest(self, run_id: str) -> dict:
        """Deterministic, hashable reference manifest for this run."""
        import hashlib
        entries = [e.to_json() for e in self.store.get_reference_entries(run_id)]
        body = json.dumps(entries, separators=(",", ":"), sort_keys=True)
        return {
            "run_id": run_id,
            "format": "debleeder-reference-manifest/1",
            "entries": entries,
            "manifest_hash": hashlib.sha256(body.encode()).hexdigest(),
        }

    # --- Stage 2: BASELINE ---

    def stage_baseline(self, run_id: str, manifest: RunManifest, output_dir: Path) -> str:
        """Generate baseline XLSX → return hash."""
        xlsx_path = output_dir / "baseline.xlsx"
        return generate_baseline_xlsx(run_id, self.store, xlsx_path, manifest)

    # --- Stage 3: ANALYZE (Candidate Funnel + Evidence) ---

    def stage_analyze(self, run_id: str) -> int:
        """Run candidate funnel + all evidence channels → store evidence."""
        reference_index = self._reference_index(run_id)
        funnel = CandidateFunnel(self.config, self.store,
                                 reference_instances=set(reference_index))
        funnel.build_indexes(run_id)

        # Initialize channel engines
        channels = [
            PhraseChannel(self.config, self.config.thresholds, self.config.method_version),
            NumericChannel(self.config, self.config.thresholds, self.config.method_version),
            SymbolChannel(self.config, self.config.thresholds, self.config.method_version),
            CitationChannel(self.config, self.config.thresholds, self.config.method_version),
            EquationChannel(self.config, self.config.thresholds, self.config.method_version),
        ]

        # Lineage + reference context
        lineage_map = self._build_lineage_map(run_id)
        lineage_channel = LineageChannel(self.config, self.config.thresholds, self.config.method_version)
        channels.append(lineage_channel)
        channels.append(ReferenceChannel(self.config, self.config.thresholds,
                                         self.config.method_version, reference_index))

        evidence_count = 0
        for candidate in funnel.generate_candidates(run_id):
            # Get blocks
            target_block = self.store.get_block(run_id, candidate.target_instance)
            source_block = self.store.get_block(run_id, candidate.source_instance)
            if not target_block or not source_block:
                continue

            # Evaluate EVERY channel on every candidate pair.
            #
            # trigger_channels records how the pair was FOUND, not what to
            # measure. Gating evaluation on the discovery route made evidence a
            # function of which funnel tier fired first, which is the opposite of
            # decomposed evidence: a pair found by a shared number would never
            # report its phrase overlap.
            for channel_engine in channels:
                if channel_engine.channel == Channel.LINEAGE:
                    ev = channel_engine.analyze(target_block, source_block, lineage_map)
                else:
                    ev = channel_engine.analyze(target_block, source_block)

                if not ev:
                    continue
                # Contextual channels (reference, lineage) carry meaning at
                # StrengthBand.NONE. Dropping them here is what made the
                # keep_seeded path unreachable in v0.2.0.
                contextual = getattr(channel_engine, "is_contextual", False) or \
                    channel_engine.channel in (Channel.SEED, Channel.LINEAGE)
                if ev.strength != StrengthBand.NONE or contextual:
                    self.store.put_evidence(run_id, [ev])
                    evidence_count += 1

        self._generate_reference_suggestions(run_id, reference_index)
        return evidence_count

    # --- v0.2.1: surgical, hash-anchored correction suggestions ---

    def _generate_reference_suggestions(self, run_id: str, reference_index: dict) -> int:
        """Propose corrections where a working block disagrees with a reference.

        Suggestions are PROPOSALS. Nothing is decided, nothing is applied, and no
        imported original is touched.
        """
        import difflib
        import hashlib
        from datetime import datetime, timezone

        made = 0
        by_pair: dict[str, list] = {}
        for ev in self.store.get_all_evidence(run_id):
            by_pair.setdefault(ev.pair_id, []).append(ev)

        for pair_id, evidence in by_pair.items():
            ref_ev = next((e for e in evidence if e.channel is Channel.SEED), None)
            if not ref_ev or ref_ev.details.get("match_type") != "reference_difference":
                continue

            working = self.store.get_block(run_id, ref_ev.details["working_instance_id"])
            reference = self.store.get_block(run_id, ref_ev.details["reference_instance_id"])
            if not working or not reference:
                continue

            detected = tuple(sorted(
                e.channel.value for e in evidence
                if e.channel is not Channel.SEED and e.strength != StrengthBand.NONE))
            if not detected:
                # Honest attribution: no ordinary channel fired. The declared
                # reference is what surfaced this pair, and the record says so
                # rather than leaving the field empty and looking like a bug.
                detected = ("reference_overlap",)

            diff = "\n".join(difflib.unified_diff(
                working.raw_content.splitlines(),
                reference.raw_content.splitlines(),
                fromfile="working", tofile="reference", lineterm="", n=1))

            suggestion_id = hashlib.sha256(
                f"{run_id}|{working.instance_id}|{reference.instance_id}".encode()
            ).hexdigest()[:16]

            self.store.put_patch_suggestion(PatchSuggestion(
                suggestion_id=suggestion_id,
                run_id=run_id,
                working_instance_id=working.instance_id,
                reference_instance_id=reference.instance_id,
                reference_doc_hash=ref_ev.details.get("reference_doc_hash", ""),
                reference_source_name=ref_ev.details.get("reference_source_name", ""),
                before_hash=hashlib.sha256(working.raw_content.encode()).hexdigest(),
                current_text=working.raw_content,
                proposed_text=reference.raw_content,
                diff=diff,
                detected_channels=detected,
                status=SuggestionStatus.PROPOSED.value,
                requires_human_approval=True,
                auto_apply=False,
                created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            ))
            made += 1
        return made

    def get_suggestion(self, run_id: str, suggestion_id: str) -> PatchSuggestion | None:
        return self.store.get_patch_suggestion(run_id, suggestion_id)

    def list_suggestions(self, run_id: str) -> list[PatchSuggestion]:
        return self.store.get_patch_suggestions(run_id)

    def approve_suggestion(self, run_id: str, suggestion_id: str, note: str) -> PatchSuggestion | None:
        """Explicit human approval. Recorded in the audit journal."""
        s = self.store.get_patch_suggestion(run_id, suggestion_id)
        if s is None:
            return None
        self.store.set_suggestion_status(run_id, suggestion_id,
                                         SuggestionStatus.APPROVED.value, note)
        self.journal.append({
            "type": "SUGGESTION_APPROVED", "run_id": run_id,
            "suggestion_id": suggestion_id, "before_hash": s.before_hash, "note": note,
        })
        return self.store.get_patch_suggestion(run_id, suggestion_id)

    def reject_suggestion(self, run_id: str, suggestion_id: str, note: str) -> PatchSuggestion | None:
        self.store.set_suggestion_status(run_id, suggestion_id,
                                         SuggestionStatus.REJECTED.value, note)
        self.journal.append({
            "type": "SUGGESTION_REJECTED", "run_id": run_id,
            "suggestion_id": suggestion_id, "note": note,
        })
        return self.store.get_patch_suggestion(run_id, suggestion_id)

    def apply_suggestion(self, run_id: str, suggestion_id: str, output_dir: Path) -> ApplyResult:
        """Apply an APPROVED suggestion to a NEW artifact, exact-anchored only.

        Refuses unless: the suggestion is approved, and the working block's raw
        content still hashes to the suggestion's before_hash. No fuzzy placement,
        no best-effort matching, and no partial output on refusal.
        """
        import hashlib

        s = self.store.get_patch_suggestion(run_id, suggestion_id)
        if s is None:
            return ApplyResult(False, suggestion_id, None, "No such suggestion")

        if s.status not in (SuggestionStatus.APPROVED.value, SuggestionStatus.EDITED.value):
            return ApplyResult(False, suggestion_id, None,
                               f"Suggestion is not approved (status {s.status}); "
                               "explicit human approval is required")

        block = self.store.get_block(run_id, s.working_instance_id)
        if block is None:
            self.store.set_suggestion_status(run_id, suggestion_id, SuggestionStatus.STALE.value)
            return ApplyResult(False, suggestion_id, None, "Working block no longer exists")

        current_hash = hashlib.sha256(block.raw_content.encode()).hexdigest()
        if current_hash != s.before_hash:
            self.store.set_suggestion_status(run_id, suggestion_id, SuggestionStatus.STALE.value)
            self.journal.append({
                "type": "SUGGESTION_STALE", "run_id": run_id,
                "suggestion_id": suggestion_id,
                "expected_before_hash": s.before_hash, "actual_hash": current_hash,
            })
            return ApplyResult(
                False, suggestion_id, None,
                "Refused: working block hash changed since approval "
                f"(expected {s.before_hash[:12]}, found {current_hash[:12]}). "
                "Suggestion marked STALE; regenerate and approve again.")

        if s.current_text not in block.raw_content:
            self.store.set_suggestion_status(run_id, suggestion_id, SuggestionStatus.STALE.value)
            return ApplyResult(False, suggestion_id, None,
                               "Refused: exact anchor text not found in the working block")

        # Ledger before output.
        self.journal.append({
            "type": "SUGGESTION_APPLY_INTENT", "run_id": run_id,
            "suggestion_id": suggestion_id, "before_hash": s.before_hash,
            "working_instance_id": s.working_instance_id,
            "reference_instance_id": s.reference_instance_id,
        })

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = output_dir / f"corrected_{s.working_instance_id}.md"
        new_text = block.raw_content.replace(s.current_text, s.proposed_text, 1)
        out_path.write_text(new_text, encoding="utf-8")

        self.store.set_suggestion_status(run_id, suggestion_id, SuggestionStatus.APPLIED.value)
        self.journal.append({
            "type": "SUGGESTION_APPLIED", "run_id": run_id,
            "suggestion_id": suggestion_id,
            "output_path": str(out_path),
            "after_hash": hashlib.sha256(new_text.encode()).hexdigest(),
        })
        return ApplyResult(True, suggestion_id, out_path, None)

    def _build_lineage_map(self, run_id: str) -> dict[str, str]:
        """Build cid -> root_cid map from lineage table."""
        lineage_map = {}
        for l in self.store.get_lineage(run_id):
            lineage_map[l.new_cid] = l.prev_cid
        # Resolve transitive
        for cid in list(lineage_map.keys()):
            while lineage_map[cid] in lineage_map:
                lineage_map[cid] = lineage_map[lineage_map[cid]]
        return lineage_map

    def _load_seed_manifest(self) -> dict[str, str]:
        """DEPRECATED in v0.2.1. Reference Documents replaced the dead stub.

        Retained only so older callers do not break. Returns an empty mapping by
        design; use reference_paths= on stage_import instead.
        """
        return {}

    # --- Stage 4: EVALUATE (Triage) ---

    def stage_evaluate(self, run_id: str) -> dict[str, any]:
        """Compute triage for all pairs → return for GUI/CLI."""
        all_evidence = self.store.get_all_evidence(run_id)
        lineage_map = self._build_lineage_map(run_id)
        reference_index = self._reference_index(run_id)

        triage_results = self.triage.triage_all(all_evidence, lineage_map, reference_index)

        # Convert to serializable
        return {
            pair_id: {
                "strength": r.strength.value,
                "raw_strength": r.raw_strength.value,
                "confidence": r.confidence.value,
                "direction": r.direction.value,
                "classification": r.classification,
                "recommendation": r.recommendation,
                "reference_disagreement": r.reference_disagreement,
                "channels": {
                    c.value: {
                        "strength": e.strength.value,
                        "confidence": e.confidence.value,
                        "raw_score": e.raw_score,
                        "details": e.details,
                    } for c, e in r.channel_evidence.items()
                },
                "modifiers": r.modifiers,
            }
            for pair_id, r in triage_results.items()
        }

    # --- Stage 5: CHOOSE (Human Decisions) ---
    # This is interactive — core provides API, GUI/CLI drives

    def record_decision(
        self, run_id: str, block_instance_id: str,
        disposition: Disposition, reason: str, override_note: str | None = None
    ) -> DecisionRecord:
        """Record a human decision on ONE block occurrence."""
        block = self.store.get_block(run_id, block_instance_id)
        if block is None:
            raise ValueError(f"No such block occurrence in run {run_id}: {block_instance_id}")
        return self.persister.record_decision(
            run_id, block.cid, block.instance_id, disposition, reason, override_note
        )

    def get_next_undecided(self, run_id: str):
        """Get next undecided block for review."""
        return next(self.store.get_undecided_blocks(run_id), None)

    # --- Stage 6: SEAL ---

    def stage_seal(self, run_id: str, manifest: RunManifest, output_dir: Path):
        """Execute seal protocol."""
        return self.sealer.seal(run_id, manifest, output_dir)

    # --- Stage 7: GENERATE ---

    def stage_generate(self, run_id: str, output_dir: Path) -> Path:
        """Generate clean report from sealed run."""
        report_path = output_dir / "clean_report.md"
        generate_clean_report(run_id, self.store, report_path)
        return report_path

    # --- Recovery ---

    def resume_run(self, run_id: str) -> RunManifest | None:
        """Resume interrupted run — returns manifest or None."""
        manifest = self.store.get_run(run_id)
        if not manifest or manifest.status == "SEALED":
            return None
        return manifest

    def verify_bundle(self, bundle_path: Path) -> bool:
        """Verify deterministic rebuild using only the bundle's own contents."""
        return rebuild_from_bundle(bundle_path)

    def reconcile_seal(self, run_id: str, output_dir: Path) -> dict:
        """Name the state of an interrupted seal."""
        return self.sealer.reconcile(run_id, output_dir)

    def close(self) -> None:
        self.store.close()
        self.journal.close()