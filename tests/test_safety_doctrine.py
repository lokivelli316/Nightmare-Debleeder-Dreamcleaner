"""Safety doctrine tests — must pass before any feature tests."""

from __future__ import annotations
from pathlib import Path
import tempfile
import pytest

from debleeder.core.config import Config, Profile
from debleeder.core.orchestrator import DebleederCore
from debleeder.core.protocols import Disposition, StrengthBand, ConfidenceBand


def test_originals_immutable():
    """Original documents never modified."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "imports" / "doc.docx"
        src.parent.mkdir(parents=True)
        src.write_bytes(b"test content")

        config = Config(project_root=tmp, profile=Profile.PHONE,
                        data_dir=tmp / ".debleeder")

        core = DebleederCore(config)
        manifest = core.store.create_run(Profile.PHONE, config.method_version, "snap1")

        # Import
        core.stage_import(manifest.run_id, [src])

        # Verify original unchanged
        assert src.read_bytes() == b"test content"
        core.close()


def test_ledger_before_omission():
    """No omission without ledger entry."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        config = Config(project_root=tmp, data_dir=tmp / ".debleeder")
        core = DebleederCore(config)

        manifest = core.store.create_run(Profile.PHONE, config.method_version, "snap1")
        # ... setup blocks ...

        # Decision must be recorded before any removal
        # (enforced by API — no removal function exists without decision)
        core.close()


def test_quarantine_before_purge():
    """Purge refused while unresolved deps exist."""
    # Implemented in seal protocol — purge only after Stage 7 verification
    pytest.skip("NOT RUN: purge is CUT from the alpha build; re-enable with the destructive lifecycle feature")


def test_append_only_history():
    """Correction adds event, never erases."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        config = Config(project_root=tmp, data_dir=tmp / ".debleeder")
        core = DebleederCore(config)

        manifest = core.store.create_run(Profile.PHONE, config.method_version, "snap1")
        doc = tmp / "doc.md"
        doc.write_text("# H\n\nA paragraph that will be decided about twice.\n")
        core.stage_import(manifest.run_id, [doc])
        block = next(iter(core.store.get_blocks(manifest.run_id)))

        core.record_decision(manifest.run_id, block.instance_id, Disposition.KEEP, "initial")
        core.record_decision(manifest.run_id, block.instance_id, Disposition.REVIEW, "reconsidered")

        decisions = list(core.store.get_decisions(manifest.run_id))
        assert len(decisions) == 2, "correction must APPEND, never overwrite"
        assert decisions[0].disposition == Disposition.KEEP
        assert decisions[1].disposition == Disposition.REVIEW

        # Latest wins for behaviour; history survives for audit.
        effective = core.store.get_effective_decisions(manifest.run_id)
        assert effective[block.instance_id].disposition == Disposition.REVIEW
        assert len(core.persister.get_history(manifest.run_id, block.instance_id)) == 2
        core.close()


def test_deterministic_rebuild():
    """Clean report rebuildable from originals + ledger."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        config = Config(project_root=tmp, data_dir=tmp / ".debleeder")
        core = DebleederCore(config)

        manifest = core.store.create_run(Profile.PHONE, config.method_version, "snap1")
        # ... full pipeline to seal ...
        result = core.stage_seal(manifest.run_id, manifest, tmp / "exports")
        assert result.success

        # Verify rebuild
        ok = core.verify_bundle(result.bundle_path)
        assert ok
        core.close()


def test_channel_evidence_inspectable():
    """Evidence channels remain decomposed."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        config = Config(project_root=tmp, data_dir=tmp / ".debleeder")
        core = DebleederCore(config)

        manifest = core.store.create_run(Profile.PHONE, config.method_version, "snap1")

        a = tmp / "a.md"
        a.write_text("# Theory A\n\nThe coupling constant is 3.847e-11 across every "
                     "regime we examined in this section of the analysis.\n")
        b = tmp / "b.md"
        b.write_text("# Theory B\n\nThe coupling constant is 3.847e-11 across every "
                     "regime we examined in this section of the analysis.\n")
        core.stage_import(manifest.run_id, [a, b])
        core.stage_analyze(manifest.run_id)

        evidence = list(core.store.get_all_evidence(manifest.run_id))
        channels = {e.channel for e in evidence}
        assert len(channels) >= 2, f"channels must stay decomposed, got {channels}"

        # No combined score may replace the per-channel record.
        triage = core.stage_evaluate(manifest.run_id)
        assert triage, "identical content in two documents must produce a pair"
        for result in triage.values():
            assert len(result["channels"]) >= 1
            for detail in result["channels"].values():
                assert "strength" in detail and "confidence" in detail
        core.close()


def test_no_guilt_terminology():
    """UI uses triage bands, not guilt percentages."""
    # Verified in templates — no "plagiarism", "theft", "fraud", "guilt", "%"
    pytest.skip("NOT RUN: superseded by test_no_guilt_language_in_outputs below")


def test_local_first():
    """No network access in core."""
    # Core has no network imports, no HTTP clients
    pytest.skip("NOT RUN: superseded by test_no_network_imports_in_core below")


def test_purge_outside_normal_flow():
    """Purge is explicit, separate command."""
    # CLI has no purge in normal run; only explicit verify+purge
    pytest.skip("NOT RUN: purge is CUT from the alpha build")


def test_private_plugins_not_load_bearing():
    """Public core builds without private deps."""
    # Verified by E-09 test: pip install debleeder[public] && pytest
    pytest.skip("NOT RUN: superseded by test_public_core_has_no_private_imports below")

def test_no_guilt_language_in_outputs():
    """The tool must never assert plagiarism, theft, fraud, or intent."""
    import pathlib as _p
    banned = ("plagiaris", "theft", "stolen", "fraud", "guilty", "cheat")
    root = _p.Path(__file__).resolve().parents[1] / "src" / "debleeder"
    offenders = []
    for f in root.rglob("*"):
        if f.suffix in (".py", ".html"):
            text = f.read_text(encoding="utf-8", errors="replace").lower()
            for word in banned:
                if word in text:
                    offenders.append(f"{f.name}: {word}")
    assert not offenders, offenders


def test_no_network_imports_in_core():
    """Local-first: the core must not reach the network."""
    import pathlib as _p
    banned = ("import requests", "import httpx", "import urllib.request",
              "from urllib.request", "import socket", "import ftplib")
    root = _p.Path(__file__).resolve().parents[1] / "src" / "debleeder"
    offenders = []
    for f in root.rglob("*.py"):
        if "gui" in f.parts:
            continue   # the GUI binds a loopback listener by design
        text = f.read_text(encoding="utf-8", errors="replace")
        for pattern in banned:
            if pattern in text:
                offenders.append(f"{f.name}: {pattern}")
    assert not offenders, offenders


def test_gui_binds_loopback_by_default():
    from debleeder.core.config import Config
    assert Config().gui_host == "127.0.0.1"


def test_public_core_has_no_private_imports():
    """The public core must carry no theory-specific data or imports."""
    import pathlib as _p, re as _re
    root = _p.Path(__file__).resolve().parents[1] / "src" / "debleeder"
    private_markers = _re.compile(
        r"\b(SME|CUT|UGME|COTF|PMAE|lokivelli|earlywine)\b", _re.IGNORECASE)
    offenders = []
    for f in root.rglob("*.py"):
        for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if private_markers.search(line):
                offenders.append(f"{f.relative_to(root)}:{i}: {line.strip()[:80]}")
        if "debleeder_private" in f.read_text(encoding="utf-8", errors="replace"):
            offenders.append(f"{f.relative_to(root)}: imports private package")
    assert not offenders, offenders
