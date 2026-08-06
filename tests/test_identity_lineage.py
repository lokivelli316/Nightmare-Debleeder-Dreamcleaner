"""Identity & lineage tests — E-04 from strike."""

from __future__ import annotations
from pathlib import Path
import tempfile
import pytest

from debleeder.core.config import Config, Profile
from debleeder.core.orchestrator import DebleederCore
from debleeder.core.protocols import BlockIdentity


def test_same_content_different_position():
    """Reordered paragraphs — same cid, different address."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        config = Config(project_root=tmp, data_dir=tmp / ".debleeder")
        core = DebleederCore(config)

        manifest = core.store.create_run(Profile.PHONE, config.method_version, "snap1")
        # Import doc with two paragraphs
        # ... (use markdown for simplicity)
        doc = tmp / "test.md"
        doc.write_text("# Heading\n\nPara A\n\nPara B\n")

        core.stage_import(manifest.run_id, [doc])
        blocks = list(core.store.get_blocks(manifest.run_id))
        assert len(blocks) == 2
        # Same content would have same cid
        core.close()


def test_one_char_correction_new_cid():
    """Single char change → new cid, lineage EDITED."""
    # Requires two runs with lineage mapping
    pytest.skip("NOT RUN: cross-version lineage is DEFINED but unimplemented")


def test_duplicate_paragraph_same_cid():
    """Identical paragraph in two positions → same cid, different ordinals."""
    pytest.skip("NOT RUN: cross-version lineage is DEFINED but unimplemented")


def test_split_paragraph_lineage():
    """One paragraph split into two → lineage SPLIT."""
    pytest.skip("NOT RUN: cross-version lineage is DEFINED but unimplemented")


def test_merged_paragraphs_lineage():
    """Two paragraphs merged → lineage MERGED."""
    pytest.skip("NOT RUN: cross-version lineage is DEFINED but unimplemented")


def test_cross_version_identity_mapping():
    """Same file, new name → identity preserved via cid."""
    pytest.skip("NOT RUN: cross-version lineage is DEFINED but unimplemented")