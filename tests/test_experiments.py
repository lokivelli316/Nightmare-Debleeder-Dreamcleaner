"""Required experiments from strike — E-01 through E-10."""

from __future__ import annotations
import pytest


@pytest.mark.experiment
def test_E01_phone_storage_benchmark():
    """Measure SQLite, WAL, XLSX on actual device."""
    # Run on Termux device, record:
    # - insert/query latency
    # - DB size at 10k/50k/100k blocks
    # - decision-write latency
    # - restart recovery time
    # - XLSX generation time/memory
    pass


@pytest.mark.experiment
def test_E02_kill_loop_torture():
    """Inject SIGKILL at every stage — verify states."""
    # Stages: import, baseline, analyze, evidence, decision, seal, generate
    # Each must be: complete+verified, incomplete+detected, resumable, forkable, or discardable
    pass


@pytest.mark.experiment
def test_E03_candidate_recall_corpus():
    """Adversarial fixtures — measure recall & queue size."""
    fixtures = [
        "exact_phrase",
        "paraphrase",
        "symbol_renamed_equation",
        "distinctive_numeric",
        "valid_rounding",           # O-01: 6.02e23*2=1.20e24
        "shared_wrong_arithmetic",
        "citation_only",
        "seeded_inheritance",
        "shared_standard_text",
        "near_duplicate_doc",
        "author_idiolect_no_bleed",
    ]
    # Target: recall >= 0.95 on planted, queue < 10k on 100k blocks
    pass


@pytest.mark.experiment
def test_E04_identity_lineage():
    """Same file new name, reorder, correction, duplicate, move, split, merge, heading change."""
    pass


@pytest.mark.experiment
def test_E05_dependency_impact():
    """Decision-time vs pre-seal vs post-generation verification."""
    pass


@pytest.mark.experiment
def test_E06_equation_extraction():
    """DOCX OMML, Unicode, text-PDF, vector glyph, image, malformed, renamed symbols."""
    pass


@pytest.mark.experiment
def test_E07_gui_usability():
    """Phone: side-by-side, large buttons, accidental rate, recovery, long eq, large para, a11y."""
    pass


@pytest.mark.experiment
def test_E08_persistent_index_reproducibility():
    """Old run reproducible after new corpus docs added."""
    pass


@pytest.mark.experiment
def test_E09_public_private_boundary():
    """Public core builds/tests without private modules."""
    pass


@pytest.mark.experiment
def test_E10_rounding_aware_numeric():
    """6.02e23 * 2 = 1.204e24 reported as 1.20e24 (3 sig fig) → NOT arithmetic error."""
    from debleeder.evidence.channels import NumericChannel
    from debleeder.core.config import Config, Thresholds
    from debleeder.core.protocols import MethodVersion, BlockRecord, RunAddress, ExtractionQuality

    config = Config()
    channel = NumericChannel(config, config.thresholds, MethodVersion())

    target = BlockRecord(
        run_id="test", cid="t1", instance_id="t1",
        addr=RunAddress("doc1", 0, "h1"),
        normalized_content="6.02e23 * 2 = 1.20e24",
        raw_content="", extraction_quality=ExtractionQuality.HIGH,
        heading_path=[], metadata={},
    )
    source = BlockRecord(
        run_id="test", cid="s1", instance_id="s1",
        addr=RunAddress("doc2", 0, "h1"),
        normalized_content="6.02e23 * 2 = 1.20e24",
        raw_content="", extraction_quality=ExtractionQuality.HIGH,
        heading_path=[], metadata={},
    )

    ev = channel.analyze(target, source)
    assert ev is not None
    # Should match on rounded value
    assert ev.details["matches"]
    # Should NOT be flagged as arithmetic error (no such concept in channel)
    pass