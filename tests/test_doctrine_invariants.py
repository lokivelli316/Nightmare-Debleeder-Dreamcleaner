"""Doctrine invariants — the gates that must hold before this touches real work.

Each test here corresponds to a defect found by executing the v0.1 scaffold.
They exist so those defects cannot come back silently.
"""

from __future__ import annotations

from pathlib import Path
import json
import tempfile

import pytest

from debleeder.core.config import Config, Profile
from debleeder.core.orchestrator import DebleederCore
from debleeder.core.protocols import Disposition
from debleeder.storage.audit_journal import JSONLAuditJournal, JournalIntegrityError


# ---------- journal ----------

def test_verify_chain_accepts_what_append_wrote():
    """Writer and verifier must hash the SAME envelope."""
    with tempfile.TemporaryDirectory() as tmp:
        j = JSONLAuditJournal(Path(tmp) / "audit.jsonl")
        for i in range(5):
            j.append({"type": "DECISION", "n": i, "note": "parameter \u03b5 and \u03ba"})
        assert j.verify_chain() is True
        j.close()

        reopened = JSONLAuditJournal(Path(tmp) / "audit.jsonl")
        assert reopened.verify_chain() is True
        reopened.close()


def test_offsets_are_byte_accurate_with_non_ascii():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "audit.jsonl"
        j = JSONLAuditJournal(path)
        j.append({"type": "T", "text": "\u03b5 \u2014 \u03ba \u2192 \u221e"})
        j.append({"type": "T", "text": "plain"})
        assert j.offset == path.stat().st_size
        j.close()


def test_tamper_preserves_history_and_opens_recovery_branch():
    """Detection must NEVER cause deletion. This is the doctrine's cornerstone."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "audit.jsonl"
        j = JSONLAuditJournal(path)
        for i in range(3):
            j.append({"type": "DECISION", "n": i, "reason": "original"})
        j.close()

        size_before = path.stat().st_size
        lines = path.read_bytes().split(b"\n")
        lines[1] = lines[1].replace(b'"original"', b'"TAMPERED"')
        path.write_bytes(b"\n".join(lines))
        size_tampered = path.stat().st_size

        with pytest.raises(JournalIntegrityError) as excinfo:
            JSONLAuditJournal(path)

        err = excinfo.value
        assert path.stat().st_size == size_tampered, "journal bytes must not shrink"
        assert size_tampered == size_before
        assert err.quarantine_path is not None and err.quarantine_path.exists()
        assert err.recovery_branch is not None and err.recovery_branch.exists()

        branch = [json.loads(l) for l in err.recovery_branch.read_text().splitlines() if l.strip()]
        assert branch[0]["type"] == "RECOVERY_BRANCH_OPENED"
        assert branch[0]["damaged_journal"] == str(path)


def test_tampered_journal_refuses_further_appends():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "audit.jsonl"
        j = JSONLAuditJournal(path)
        j.append({"type": "A"})
        j.append({"type": "B"})
        j.close()
        raw = path.read_bytes().replace(b'"A"', b'"X"')
        path.write_bytes(raw)
        with pytest.raises(JournalIntegrityError):
            JSONLAuditJournal(path)


# ---------- decisions / outbox ----------

def _core(tmp: Path) -> DebleederCore:
    return DebleederCore(Config(project_root=tmp, data_dir=tmp / ".debleeder",
                                profile=Profile.PHONE))


def _seed_run(core: DebleederCore, tmp: Path, text: str = "A paragraph to decide about.\n"):
    manifest = core.store.create_run(Profile.PHONE, core.config.method_version, "snap1")
    doc = tmp / "doc.md"
    doc.write_text(f"# H\n\n{text}")
    core.stage_import(manifest.run_id, [doc])
    return manifest


def test_outbox_projection_is_idempotent():
    """Replaying projection must not duplicate journal records."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        core = _core(tmp)
        m = _seed_run(core, tmp)
        block = next(iter(core.store.get_blocks(m.run_id)))
        core.record_decision(m.run_id, block.instance_id, Disposition.KEEP, "keep it")

        before = sum(1 for _o, r in core.journal.read_range(0, None)
                     if r.get("type") == "DECISION")
        assert core.persister.project_pending() == 0
        assert core.persister.project_pending() == 0
        after = sum(1 for _o, r in core.journal.read_range(0, None)
                    if r.get("type") == "DECISION")
        assert before == after == 1
        core.close()


def test_decision_survives_crash_before_projection():
    """Committed decision + unprojected outbox must reconcile on next start."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        core = _core(tmp)
        m = _seed_run(core, tmp)
        block = next(iter(core.store.get_blocks(m.run_id)))

        # Commit atomically, then simulate the process dying before projection.
        from debleeder.core.protocols import DecisionRecord
        from datetime import datetime, timezone
        draft = DecisionRecord(
            run_id=m.run_id, block_cid=block.cid, block_instance_id=block.instance_id,
            disposition=Disposition.QUARANTINE, reason="crash test",
            override_note=None, timestamp=datetime.now(timezone.utc),
            sqlite_txn_id=0, jsonl_offset=-1,
        )
        core.store.commit_decision_with_outbox(draft, "evt-crash", {
            "run_id": m.run_id, "block_cid": block.cid,
            "block_instance_id": block.instance_id,
            "disposition": "quarantine", "reason": "crash test",
            "override_note": None, "timestamp": draft.timestamp.isoformat(),
        })
        assert len(core.store.unprojected_outbox()) == 1
        ok, problems = core.persister.verify_dual_write(m.run_id)
        assert not ok and any("not yet projected" in p for p in problems)
        core.close()

        recovered = _core(tmp)   # startup runs project_pending()
        assert recovered.store.unprojected_outbox() == []
        ok, problems = recovered.persister.verify_dual_write(m.run_id)
        assert ok, problems
        recovered.close()


def test_verify_dual_write_actually_runs():
    """It raised ValueError on its first line in v0.1 and had no callers."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        core = _core(tmp)
        m = _seed_run(core, tmp)
        block = next(iter(core.store.get_blocks(m.run_id)))
        core.record_decision(m.run_id, block.instance_id, Disposition.KEEP, "fine")
        ok, problems = core.persister.verify_dual_write(m.run_id)
        assert ok, problems
        core.close()


# ---------- identity ----------

def test_identical_content_in_two_documents_is_two_occurrences():
    """The primary bleed case. v0.1 collapsed it to one row and could not see it."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        core = _core(tmp)
        m = core.store.create_run(Profile.PHONE, core.config.method_version, "snap1")
        shared = "The coupling constant is 3.847e-11 across every regime we examined.\n"
        (tmp / "a.md").write_text(f"# A\n\n{shared}")
        (tmp / "b.md").write_text(f"# B\n\n{shared}")
        core.stage_import(m.run_id, [tmp / "a.md", tmp / "b.md"])

        blocks = list(core.store.get_blocks(m.run_id))
        cids = [b.cid for b in blocks]
        instances = {b.instance_id for b in blocks}
        assert len(blocks) == 2
        assert cids[0] == cids[1], "same content -> same content id"
        assert len(instances) == 2, "same content in two documents -> two occurrences"

        core.stage_analyze(m.run_id)
        pairs = {e.pair_id for e in core.store.get_all_evidence(m.run_id)}
        assert pairs, "an exact copy across documents must produce a candidate pair"
        for pid in pairs:
            left, _, right = pid.partition(":")
            assert left != right, "a pair must never be an occurrence with itself"
        core.close()


def test_rarity_is_inverse_document_frequency():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        core = _core(tmp)
        m = core.store.create_run(Profile.PHONE, core.config.method_version, "snap1")
        common = "This sentence appears in every single document of the corpus.\n"
        for i in range(4):
            (tmp / f"d{i}.md").write_text(f"# D{i}\n\n{common}\n\nUnique line number {i} here.\n")
        core.stage_import(m.run_id, [tmp / f"d{i}.md" for i in range(4)])

        blocks = list(core.store.get_blocks(m.run_id))
        common_cid = next(b.cid for b in blocks if "every single document" in b.normalized_content)
        unique_cid = next(b.cid for b in blocks if "unique line number 0" in b.normalized_content)
        assert core.store.get_rarity(unique_cid) > core.store.get_rarity(common_cid), \
            "content in one document must score rarer than content in all of them"
        core.close()


# ---------- seal ----------

def test_seal_refuses_with_undecided_blocks():
    """No implicit decisions (directive §4.6)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        core = _core(tmp)
        m = _seed_run(core, tmp)
        result = core.stage_seal(m.run_id, m, tmp / "exports")
        assert result.success is False
        assert any("no decision" in b for b in result.blockers)
        assert core.store.get_run(m.run_id).status != "SEALED"
        core.close()


def test_seal_refuses_while_review_outstanding():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        core = _core(tmp)
        m = _seed_run(core, tmp)
        for b in core.store.get_blocks(m.run_id):
            core.record_decision(m.run_id, b.instance_id, Disposition.REVIEW, "unsure")
        result = core.stage_seal(m.run_id, m, tmp / "exports")
        assert result.success is False
        assert any("REVIEW" in b for b in result.blockers)
        core.close()


def _sealed_run(core: DebleederCore, tmp: Path):
    m = core.store.create_run(Profile.PHONE, core.config.method_version, "snap1")
    (tmp / "a.md").write_text("# A\n\nKeep this paragraph.\n\nQuarantine this other paragraph.\n")
    core.stage_import(m.run_id, [tmp / "a.md"])
    for b in core.store.get_blocks(m.run_id):
        disp = (Disposition.QUARANTINE
                if "quarantine" in b.normalized_content.lower() else Disposition.KEEP)
        core.record_decision(m.run_id, b.instance_id, disp, "test decision")
    return m, core.stage_seal(m.run_id, m, tmp / "exports")


def test_seal_publishes_before_marking_sealed():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        core = _core(tmp)
        m, result = _sealed_run(core, tmp)
        assert result.success, result.error
        assert result.state == "SEALED"
        assert result.bundle_path.exists()
        assert core.store.get_run(m.run_id).status == "SEALED"
        # No staging directory may survive a successful publish.
        assert not (tmp / "exports" / "bundles" / f".staging_run_{m.run_id}").exists()
        core.close()


def test_bundle_rebuilds_without_the_live_database():
    """Portable bundle (directive §4.5): no live operational state."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        core = _core(tmp)
        m, result = _sealed_run(core, tmp)
        assert result.success, result.error
        bundle = result.bundle_path
        core.close()

        from debleeder.rebuild.generator import rebuild_from_bundle
        assert rebuild_from_bundle(bundle) is True

        for required in ("manifest.json", "blocks.jsonl", "decisions.jsonl",
                         "clean_report.md", "quarantine.jsonl", "LIMITATIONS.md"):
            assert (bundle / required).exists(), f"bundle missing {required}"


def test_bundle_detects_modified_artifact():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        core = _core(tmp)
        _m, result = _sealed_run(core, tmp)
        core.close()
        report = result.bundle_path / "clean_report.md"
        report.write_text(report.read_text() + "\nsneaky edit\n")
        from debleeder.rebuild.generator import rebuild_from_bundle
        assert rebuild_from_bundle(result.bundle_path) is False


def test_quarantined_content_is_recoverable_from_bundle():
    """Quarantine before purge: removal must stay reversible."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        core = _core(tmp)
        _m, result = _sealed_run(core, tmp)
        core.close()
        clean = (result.bundle_path / "clean_report.md").read_text()
        assert "quarantine this other paragraph" not in clean.lower()
        quarantined = [json.loads(l) for l in
                       (result.bundle_path / "quarantine.jsonl").read_text().splitlines() if l.strip()]
        assert quarantined, "quarantined blocks must be retained"
        assert any("quarantine this other paragraph" in q["normalized_content"].lower()
                   for q in quarantined)


def test_reconcile_names_an_interrupted_seal():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        core = _core(tmp)
        m = _seed_run(core, tmp)
        core.store.update_run_status(m.run_id, "OUTPUTS_WRITTEN")
        report = core.reconcile_seal(m.run_id, tmp / "exports")
        assert report["state"] == "OUTPUTS_WRITTEN"
        assert report["trustworthy"] is False
        assert "re-seal" in report["action"]

        core.store.update_run_status(m.run_id, "SEALED")
        report = core.reconcile_seal(m.run_id, tmp / "exports")
        assert report["trustworthy"] is False
        assert "INCONSISTENT" in report["action"]
        core.close()


def test_clean_report_is_deterministic():
    """Identical inputs and decisions must render identical report content.

    The run id is deliberately in the header, so two different runs cannot hash
    identically; determinism is asserted over everything else.
    """
    def body(text: str) -> str:
        return "\n".join(l for l in text.splitlines() if not l.startswith("Run ID:"))

    reports = []
    for _ in range(2):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            core = _core(tmp)
            _m, result = _sealed_run(core, tmp)
            assert result.success, result.error
            reports.append(body((result.bundle_path / "clean_report.md").read_text()))
            core.close()

    assert reports[0] == reports[1]


# ---------- GUI ----------

def test_gui_escapes_document_content():
    """Input is untrusted documents. Unescaped block content is stored XSS."""
    from debleeder.gui.server_stdlib import HTMXHandler

    class Probe(HTMXHandler):
        def __init__(self):
            pass

    probe = Probe()
    probe.templates_dir = Path(__file__).resolve().parents[1] / "src" / "debleeder" / "gui" / "templates"
    payload = '<script>alert("xss")</script>'
    rendered = probe._render_template("base.html", {
        "title": payload, "run_id": payload, "stage": payload, "content": "<p>safe</p>",
    })
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


def test_gui_template_substitution_is_single_pass():
    """A value containing another field's placeholder must not be re-substituted."""
    from debleeder.gui.server_stdlib import HTMXHandler

    class Probe(HTMXHandler):
        def __init__(self):
            pass

    probe = Probe()
    probe.templates_dir = Path(__file__).resolve().parents[1] / "src" / "debleeder" / "gui" / "templates"
    rendered = probe._render_template("base.html", {
        "title": "{{ stage }}", "run_id": "r", "stage": "SECRET", "content": "x",
    })
    assert "SECRET" not in rendered.split("</title>")[0]
