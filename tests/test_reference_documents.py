"""Reference Documents — v0.2.1 patch gates.

Written BEFORE implementation, per the patch directive. Each test maps to a
numbered requirement in Debleeder_v0_2_1_Reference_Documents_Patch_Directive.md.
"""

from __future__ import annotations

from pathlib import Path
import hashlib
import json
import tempfile

import pytest

from debleeder.core.config import Config, Profile
from debleeder.core.orchestrator import DebleederCore
from debleeder.core.protocols import Channel, Disposition, StrengthBand


SHARED = "The ladder coefficient is fixed at 3.847e-11 throughout this analysis."
DRIFTED = "The ladder coefficient is fixed at 4.212e-09 throughout this analysis."


def _core(tmp: Path) -> DebleederCore:
    return DebleederCore(Config(project_root=tmp, data_dir=tmp / ".debleeder",
                                profile=Profile.PHONE))


def _write(tmp: Path, name: str, body: str) -> Path:
    p = tmp / name
    p.write_text(body, encoding="utf-8")
    return p


def _ref_run(core, tmp, working_body: str, reference_body: str):
    m = core.store.create_run(Profile.PHONE, core.config.method_version, "snap1")
    work = _write(tmp, "working.md", f"# Working\n\n{working_body}\n")
    ref = _write(tmp, "canon.md", f"# Canon\n\n{reference_body}\n")
    core.stage_import(m.run_id, [work], reference_paths=[ref])
    core.stage_analyze(m.run_id)
    return m, work, ref


# --- 1. exact declared reference is visible but does not escalate -------------

def test_exact_reference_is_visible_but_does_not_escalate():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        core = _core(tmp)
        m, _work, _ref = _ref_run(core, tmp, SHARED, SHARED)

        evidence = list(core.store.get_all_evidence(m.run_id))
        channels = {e.channel for e in evidence}

        assert Channel.SEED in channels, "reference context must be emitted"
        assert channels & {Channel.PHRASE, Channel.NUMERIC}, \
            "ordinary channel evidence must be RETAINED, not replaced"

        ref_ev = [e for e in evidence if e.channel is Channel.SEED]
        assert any(e.details.get("match_type") == "exact_match" for e in ref_ev)
        assert any(e.details.get("reference_source_name") for e in ref_ev)
        assert any(e.details.get("reference_doc_hash") for e in ref_ev)
        assert any(e.details.get("reference_instance_id") for e in ref_ev)

        triage = core.stage_evaluate(m.run_id)
        assert triage, "an exact cross-document match must produce a pair"
        continuity = [r for r in triage.values()
                      if r.get("classification") == "intentional_continuity"]
        assert continuity, "exact declared continuity must be classified"
        for r in continuity:
            assert r["strength"] == StrengthBand.NONE.value, \
                "displayed strength must be suppressed, not escalated"
            assert r["channels"], "per-channel evidence must remain inspectable"
            assert r["recommendation"] == Disposition.KEEP_SEEDED.value

        # Nothing may be decided on the user's behalf.
        assert list(core.store.get_decisions(m.run_id)) == []
        assert list(core.store.get_undecided_blocks(m.run_id)), \
            "blocks must still await a human decision"
        core.close()


# --- 2. same material without declaration still escalates --------------------

def test_same_material_without_declaration_still_escalates():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        core = _core(tmp)
        m = core.store.create_run(Profile.PHONE, core.config.method_version, "snap1")
        a = _write(tmp, "a.md", f"# A\n\n{SHARED}\n")
        b = _write(tmp, "b.md", f"# B\n\n{SHARED}\n")
        core.stage_import(m.run_id, [a, b])          # no reference_paths
        core.stage_analyze(m.run_id)

        triage = core.stage_evaluate(m.run_id)
        assert triage
        assert not any(r.get("classification") == "intentional_continuity"
                       for r in triage.values()), \
            "undeclared material must not be suppressed"
        assert any(r["strength"] != StrengthBand.NONE.value for r in triage.values()), \
            "undeclared shared material must still escalate"
        core.close()


# --- 3. short exact block is still a candidate -------------------------------

def test_short_exact_block_is_still_a_candidate():
    """Under five words, no number, no citation — the old funnel missed these."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        core = _core(tmp)
        m = core.store.create_run(Profile.PHONE, core.config.method_version, "snap1")
        a = _write(tmp, "a.md", "# A\n\nFederate not fuse.\n")
        b = _write(tmp, "b.md", "# B\n\nFederate not fuse.\n")
        core.stage_import(m.run_id, [a, b])
        core.stage_analyze(m.run_id)

        pairs = {e.pair_id for e in core.store.get_all_evidence(m.run_id)}
        assert pairs, "an exact short block must still become a candidate pair"
        for pid in pairs:
            left, _, right = pid.partition(":")
            assert left != right
        core.close()


# --- 4. reference disagreement produces a surgical suggestion ----------------

def test_reference_disagreement_produces_surgical_suggestion():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        core = _core(tmp)
        before_bytes = None
        m, work, _ref = _ref_run(core, tmp, DRIFTED, SHARED)
        before_bytes = work.read_bytes()

        suggestions = list(core.store.get_patch_suggestions(m.run_id))
        assert suggestions, "a reference disagreement must produce a suggestion"
        s = suggestions[0]

        assert s.working_instance_id and s.reference_instance_id
        assert s.working_instance_id != s.reference_instance_id
        assert s.current_text.strip() == DRIFTED
        assert s.proposed_text.strip() == SHARED
        assert s.before_hash == hashlib.sha256(s.current_text.encode()).hexdigest()
        assert s.diff, "a compact human-readable diff is required"
        assert s.detected_channels, "the channels that found the seam must be named"
        assert s.requires_human_approval is True
        assert s.auto_apply is False
        assert s.status == "PROPOSED"

        # The original file is never touched.
        assert work.read_bytes() == before_bytes
        core.close()


# --- 5. structured conflict is not suppressed --------------------------------

def test_structured_conflict_is_not_suppressed():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        core = _core(tmp)
        m, _work, _ref = _ref_run(core, tmp, DRIFTED, SHARED)

        evidence = list(core.store.get_all_evidence(m.run_id))
        strengths = {e.channel: e.strength for e in evidence}
        assert any(s != StrengthBand.NONE for c, s in strengths.items()
                   if c in (Channel.PHRASE, Channel.NUMERIC)), \
            "a changed value inside reference wording must stay visible"

        triage = core.stage_evaluate(m.run_id)
        assert not any(r.get("classification") == "intentional_continuity"
                       for r in triage.values()), \
            "a disagreement is not exact continuity"
        for r in triage.values():
            assert r.get("reference_disagreement") is True
        core.close()


# --- 6. hash mismatch blocks application ------------------------------------

def test_hash_mismatch_blocks_application():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        core = _core(tmp)
        m, _work, _ref = _ref_run(core, tmp, DRIFTED, SHARED)
        s = next(iter(core.store.get_patch_suggestions(m.run_id)))

        core.approve_suggestion(m.run_id, s.suggestion_id, "approved for test")
        approved = core.get_suggestion(m.run_id, s.suggestion_id)
        assert approved.status == "APPROVED"

        # Someone changes the working block underneath the approval.
        core.store.replace_block_content(
            m.run_id, s.working_instance_id, "Something else entirely now.")

        out_dir = tmp / "applied"
        result = core.apply_suggestion(m.run_id, s.suggestion_id, out_dir)
        assert result.applied is False
        assert "hash" in (result.error or "").lower()

        stale = core.get_suggestion(m.run_id, s.suggestion_id)
        assert stale.status == "STALE"
        assert not list(out_dir.glob("**/*")) if out_dir.exists() else True, \
            "no partial output may be published on refusal"
        core.close()


def test_approved_suggestion_applies_to_a_new_artifact_only():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        core = _core(tmp)
        m, work, _ref = _ref_run(core, tmp, DRIFTED, SHARED)
        original = work.read_bytes()
        s = next(iter(core.store.get_patch_suggestions(m.run_id)))

        core.approve_suggestion(m.run_id, s.suggestion_id, "approved")
        result = core.apply_suggestion(m.run_id, s.suggestion_id, tmp / "applied")
        assert result.applied is True, result.error
        assert result.output_path.exists()
        assert SHARED in result.output_path.read_text()
        assert work.read_bytes() == original, "the original must be byte-identical"
        assert core.get_suggestion(m.run_id, s.suggestion_id).status == "APPLIED"
        core.close()


def test_unapproved_suggestion_cannot_be_applied():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        core = _core(tmp)
        m, _work, _ref = _ref_run(core, tmp, DRIFTED, SHARED)
        s = next(iter(core.store.get_patch_suggestions(m.run_id)))
        result = core.apply_suggestion(m.run_id, s.suggestion_id, tmp / "applied")
        assert result.applied is False
        assert "approv" in (result.error or "").lower()
        core.close()


# --- 7. reference evidence survives seal and bundle-only inspection ----------

def _decide_all(core, run_id, disposition=Disposition.KEEP):
    for b in list(core.store.get_undecided_blocks(run_id)):
        core.record_decision(run_id, b.instance_id, disposition, "test decision")


def test_reference_evidence_survives_seal_and_bundle_only_inspection():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        core = _core(tmp)
        m, _work, _ref = _ref_run(core, tmp, DRIFTED, SHARED)
        _decide_all(core, m.run_id)
        result = core.stage_seal(m.run_id, m, tmp / "exports")
        assert result.success, result.error
        bundle = result.bundle_path
        core.close()   # live database gone

        manifest = json.loads((bundle / "manifest.json").read_text())
        assert "reference_manifest.json" in manifest["files"]
        assert "patch_suggestions.jsonl" in manifest["files"]

        refs = json.loads((bundle / "reference_manifest.json").read_text())
        assert refs["entries"], "reference index must be persisted"
        entry = refs["entries"][0]
        for field in ("doc_hash", "source_name", "instance_id", "cid",
                      "normalized_hash", "raw_hash", "ordinal", "extraction_quality"):
            assert field in entry, f"reference entry missing {field}"
        assert refs["manifest_hash"]

        suggestions = [json.loads(l) for l in
                       (bundle / "patch_suggestions.jsonl").read_text().splitlines() if l.strip()]
        assert suggestions
        assert suggestions[0]["auto_apply"] is False
        assert suggestions[0]["before_hash"]

        from debleeder.rebuild.generator import rebuild_from_bundle
        assert rebuild_from_bundle(bundle) is True


def test_reference_manifest_is_deterministic():
    hashes = []
    for _ in range(2):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            core = _core(tmp)
            m, _w, _r = _ref_run(core, tmp, SHARED, SHARED)
            hashes.append(core.reference_manifest(m.run_id)["manifest_hash"])
            core.close()
    assert hashes[0] == hashes[1]


# --- role plumbing -----------------------------------------------------------

def test_reference_role_is_explicit_never_inferred():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        core = _core(tmp)
        m, _w, _r = _ref_run(core, tmp, SHARED, SHARED)
        roles = {b.metadata.get("document_role") for b in core.store.get_blocks(m.run_id)}
        assert roles == {"working", "reference"}
        for b in core.store.get_blocks(m.run_id):
            assert b.metadata.get("source_name")
            assert b.metadata.get("document_role") in ("working", "reference")
        core.close()


def test_import_without_references_marks_everything_working():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        core = _core(tmp)
        m = core.store.create_run(Profile.PHONE, core.config.method_version, "snap1")
        a = _write(tmp, "a.md", f"# A\n\n{SHARED}\n")
        core.stage_import(m.run_id, [a])
        assert {b.metadata.get("document_role") for b in core.store.get_blocks(m.run_id)} == {"working"}
        assert core.reference_manifest(m.run_id)["entries"] == []
        core.close()


def test_drifted_short_line_against_reference_is_still_found():
    """A reworded short line shares no 5-gram, no number, no citation.

    The generic tiers cannot see it. Declaring a reference is exactly the act
    that should make it visible.
    """
    canon = "Underpromise. Overbuild. Fix forever. Federate, do not fuse."
    drifted = "Underpromise. Overbuild. Fix for now. Federate, do not fuse."
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        core = _core(tmp)
        m, _w, _r = _ref_run(core, tmp, drifted, canon)

        suggestions = list(core.store.get_patch_suggestions(m.run_id))
        assert suggestions, "a reworded line must still be compared against canon"
        s = suggestions[0]
        assert s.current_text.strip() == drifted
        assert s.proposed_text.strip() == canon

        triage = core.stage_evaluate(m.run_id)
        assert any(r["reference_disagreement"] for r in triage.values())
        core.close()
