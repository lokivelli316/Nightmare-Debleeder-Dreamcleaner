"""Regression gates for the 2026-08-11 reference false-fire repair."""

from __future__ import annotations

from pathlib import Path
import tempfile

from debleeder.core.config import Config, Profile
from debleeder.core.orchestrator import DebleederCore
from debleeder.core.protocols import (
    Channel,
    ConfidenceBand,
    EvidenceRecord,
    ExtractionQuality,
    PairClassification,
    StrengthBand,
)
from debleeder.triage.engine import TriageEngine


GENERIC_REFERENCE = (
    "The model predicts energy density across stable boundary conditions for the "
    "calibrated reference manifold."
)
GENERIC_WORKING = (
    "The model predicts energy density across changing boundary assumptions for an "
    "unrelated exploratory system."
)


def _core(tmp: Path) -> DebleederCore:
    return DebleederCore(
        Config(project_root=tmp, data_dir=tmp / ".debleeder", profile=Profile.PHONE)
    )


def _write(tmp: Path, name: str, body: str) -> Path:
    p = tmp / name
    p.write_text(f"# {name}\n\n{body}\n", encoding="utf-8")
    return p


def test_reference_overlap_only_pair_does_not_create_correction_suggestion():
    """A shared academic 5-gram alone may be evidence, but not correction authority."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        core = _core(tmp)
        run = core.store.create_run(Profile.PHONE, core.config.method_version, "false-fire")
        working = _write(tmp, "working", GENERIC_WORKING)
        reference = _write(tmp, "reference", GENERIC_REFERENCE)

        core.stage_import(run.run_id, [working], reference_paths=[reference])
        core.stage_analyze(run.run_id)

        evidence = list(core.store.get_all_evidence(run.run_id))
        assert any(
            e.channel is Channel.SEED and e.details.get("match_type") == "reference_difference"
            for e in evidence
        ), "the weak seam should remain visible as reference context"
        assert not any(
            e.channel is not Channel.SEED and e.strength in (StrengthBand.MEDIUM, StrengthBand.HIGH)
            for e in evidence
        ), "fixture must remain a weak/no ordinary-evidence control"

        assert list(core.store.get_patch_suggestions(run.run_id)) == [], (
            "weak reference overlap must not become a PROPOSED correction"
        )
        core.close()


def test_low_strength_reference_difference_routes_as_ordinary():
    pair_id = "working:reference"
    evidence = [
        EvidenceRecord(
            run_id="r1",
            pair_id=pair_id,
            channel=Channel.PHRASE,
            raw_score=0.20,
            strength=StrengthBand.LOW,
            confidence=ConfidenceBand.HIGH,
            details={"jaccard": 0.20},
            method_version="phrase:v1.0",
            extraction_quality=ExtractionQuality.HIGH,
        ),
        EvidenceRecord(
            run_id="r1",
            pair_id=pair_id,
            channel=Channel.SEED,
            raw_score=0.50,
            strength=StrengthBand.NONE,
            confidence=ConfidenceBand.HIGH,
            details={"match_type": "reference_difference"},
            method_version="seed:v1.0",
            extraction_quality=ExtractionQuality.HIGH,
        ),
    ]

    result = TriageEngine(Config()).triage_pair(pair_id, evidence)
    assert result.classification == PairClassification.ORDINARY.value
    assert result.reference_disagreement is False
    assert result.raw_strength is StrengthBand.LOW


def test_reference_funnel_honors_versioned_config_thresholds():
    from debleeder.core.config import Thresholds
    from debleeder.core.protocols import BlockRecord, RunAddress
    from debleeder.evidence.candidate_funnel import CandidateFunnel

    thresholds = Thresholds(
        reference_overlap_min=0.95,
        reference_rare_token_min=2,
        reference_rare_df_max=3,
    )
    config = Config(thresholds=thresholds)
    funnel = CandidateFunnel(config, store=None, reference_instances={"ref-instance"})

    reference = BlockRecord(
        run_id="r1",
        cid="ref-cid",
        instance_id="ref-instance",
        addr=RunAddress("ref-doc", 0, "h1"),
        normalized_content="alpha beta gamma delta",
        raw_content="alpha beta gamma delta",
        extraction_quality=ExtractionQuality.HIGH,
        heading_path=[],
        metadata={},
    )
    working = BlockRecord(
        run_id="r1",
        cid="work-cid",
        instance_id="work-instance",
        addr=RunAddress("work-doc", 0, "h2"),
        normalized_content="alpha beta gamma epsilon",
        raw_content="alpha beta gamma epsilon",
        extraction_quality=ExtractionQuality.HIGH,
        heading_path=[],
        metadata={},
    )
    funnel._index_block(reference)
    funnel._index_block(working)

    assert list(funnel.generate_candidates("r1")) == [], (
        "reference gate must use Config.thresholds, not hard-coded class constants"
    )
