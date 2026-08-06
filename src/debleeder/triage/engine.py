"""Triage engine — two-axis (strength × confidence), no combined score."""

from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterator

from ..core.protocols import (
    EvidenceRecord, StrengthBand, ConfidenceBand, Channel,
    Disposition, Direction, PairClassification,
)
from ..core.config import Config


# Strength ordering
STRENGTH_ORDER = {
    StrengthBand.NONE: 0,
    StrengthBand.LOW: 1,
    StrengthBand.MEDIUM: 2,
    StrengthBand.HIGH: 3,
}

CONFIDENCE_ORDER = {
    ConfidenceBand.LOW: 0,
    ConfidenceBand.MEDIUM: 1,
    ConfidenceBand.HIGH: 2,
}


@dataclass(frozen=True, slots=True)
class TriageResult:
    """Two-axis triage — primary display."""
    pair_id: str
    strength: StrengthBand
    confidence: ConfidenceBand
    channel_evidence: dict[Channel, EvidenceRecord]
    direction: Direction
    modifiers: dict[str, any]  # lineage, reference, citation context
    classification: str = PairClassification.ORDINARY.value
    recommendation: str | None = None       # a suggestion, never a decision
    reference_disagreement: bool = False
    raw_strength: StrengthBand = StrengthBand.NONE   # pre-suppression, always kept


class TriageEngine:
    """Compute triage from decomposed evidence — deterministic, versioned."""

    def __init__(self, config: Config):
        self.config = config

    def triage_pair(
        self,
        pair_id: str,
        evidence: list[EvidenceRecord],
        lineage_map: dict[str, str] | None = None,
        seed_manifest: dict[str, str] | None = None,
    ) -> TriageResult:
        """Aggregate evidence → two-axis triage."""

        # Group by channel
        by_channel: dict[Channel, EvidenceRecord] = {}
        for e in evidence:
            by_channel[e.channel] = e

        # --- Strength: max of BLOCK-LEVEL channels ---
        block_channels = {
            Channel.PHRASE, Channel.NUMERIC, Channel.SYMBOL,
            Channel.EQUATION, Channel.CITATION,
        }
        block_evidence = [e for c, e in by_channel.items() if c in block_channels]

        if block_evidence:
            max_strength = max(block_evidence, key=lambda e: STRENGTH_ORDER[e.strength]).strength
        else:
            max_strength = StrengthBand.NONE

        # --- Confidence: min of block channels, modified by doc-level ---
        if block_evidence:
            min_confidence = min(block_evidence, key=lambda e: CONFIDENCE_ORDER[e.confidence]).confidence
        else:
            min_confidence = ConfidenceBand.LOW

        # Modifiers from document-level channels
        modifiers = {}
        direction = Direction.UNKNOWN

        # Lineage → direction hint + confidence boost
        lineage_ev = by_channel.get(Channel.LINEAGE)
        if lineage_ev and lineage_ev.strength != StrengthBand.NONE:
            modifiers["lineage"] = lineage_ev.details
            # Lineage establishes direction if first-observed recorded
            if lineage_ev.details.get("direction_hint") == "source_first":
                direction = Direction.SOURCE_TO_TARGET
            elif lineage_ev.details.get("direction_hint") == "target_first":
                direction = Direction.TARGET_TO_SOURCE
            # Confidence boost for lineage-supported pairs
            if min_confidence == ConfidenceBand.LOW:
                min_confidence = ConfidenceBand.MEDIUM

        # --- Reference Documents (v0.2.1) ---
        #
        # Reference status is CONTEXT, not evidence. It never lowers confidence
        # and never rewrites a channel. It changes how the pair is ROUTED.
        classification = PairClassification.ORDINARY.value
        recommendation = None
        reference_disagreement = False
        raw_strength = max_strength

        ref_ev = by_channel.get(Channel.SEED)
        if ref_ev:
            details = ref_ev.details or {}
            match_type = details.get("match_type")
            modifiers["reference"] = details

            if match_type == "exact_match":
                # Declared, exact, intentional. Suppress escalation only.
                classification = PairClassification.INTENTIONAL_CONTINUITY.value
                recommendation = Disposition.KEEP_SEEDED.value
                max_strength = StrengthBand.NONE
            elif match_type == "reference_difference":
                # A seam against declared canon. Nothing is suppressed.
                classification = PairClassification.REFERENCE_DISAGREEMENT.value
                reference_disagreement = True
            elif match_type == "reference_to_reference":
                classification = PairClassification.ORDINARY.value

        # Citation → context only, no strength/confidence change
        cite_ev = by_channel.get(Channel.CITATION)
        if cite_ev:
            modifiers["citation"] = cite_ev.details

        return TriageResult(
            pair_id=pair_id,
            strength=max_strength,
            confidence=min_confidence,
            channel_evidence=by_channel,
            direction=direction,
            modifiers=modifiers,
            classification=classification,
            recommendation=recommendation,
            reference_disagreement=reference_disagreement,
            raw_strength=raw_strength,
        )

    def triage_all(
        self,
        all_evidence: Iterator[EvidenceRecord],
        lineage_map: dict[str, str] | None = None,
        seed_manifest: dict[str, str] | None = None,
    ) -> dict[str, TriageResult]:
        """Triage all pairs — grouped by pair_id."""
        by_pair: dict[str, list[EvidenceRecord]] = defaultdict(list)
        for e in all_evidence:
            by_pair[e.pair_id].append(e)

        results = {}
        for pair_id, evidence in by_pair.items():
            results[pair_id] = self.triage_pair(
                pair_id, evidence, lineage_map, seed_manifest
            )
        return results