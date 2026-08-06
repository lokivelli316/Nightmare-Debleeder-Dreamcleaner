"""Candidate reduction funnel — inverted indexes over numerics, citations, shingles.

Tier 4 (fuzzy MinHash/LSH) is NOT implemented. stdlib only; no C extensions.
"""

from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import hashlib
import re
from typing import Iterator

from ..core.protocols import BlockRecord, Channel
from ..core.config import Config
from ..storage.sqlite_store import SQLiteStore


@dataclass(slots=True)
class CandidatePair:
    """Candidate pair of block OCCURRENCES for detailed evidence analysis."""
    target_instance: str
    source_instance: str
    target_cid: str
    source_cid: str
    target_ordinal: int
    source_ordinal: int
    trigger_channels: list[Channel]  # which channels triggered this pair
    rarity_score: float  # combined rarity of shared features


class CandidateFunnel:
    """Multi-stage candidate reduction — bounded queue, streaming friendly."""

    #: Jaccard token overlap above which a working block is compared against a
    #: declared reference block. Provisional and uncalibrated.
    REFERENCE_OVERLAP_MIN = 0.4

    def __init__(self, config: Config, store: SQLiteStore,
                 reference_instances: set[str] | None = None):
        self.config = config
        self.store = store
        # v0.2.1: declared Reference Documents get their own tier. A working
        # block that DRIFTED from canon may share no 5-gram, no number and no
        # citation with it — the generic tiers would never surface the pair,
        # which is exactly the case a reference is declared to catch.
        self.reference_instances = set(reference_instances or ())
        self.profile = config.for_profile()
        self.queue_limit = self.profile.candidate_queue_limit

        # Stage 1: Exact numeric literal buckets
        self.numeric_buckets: dict[str, list[str]] = defaultdict(list)  # literal -> [cid]

        # Stage 2: Rare shingle index (5-grams)
        self.shingle_index: dict[str, list[str]] = defaultdict(list)  # shingle -> [cid]

        # Stage 3: Symbol / citation buckets
        self.symbol_buckets: dict[str, list[str]] = defaultdict(list)
        self.citation_buckets: dict[str, list[str]] = defaultdict(list)

        # Stage 4: fuzzy phrase tier — NOT IMPLEMENTED (see KNOWN_LIMITATIONS.md)
        self.minhash_index = None

        self._cid_of: dict[str, str] = {}
        self._doc_of: dict[str, str] = {}
        # Tier 0: exact content-id buckets. An identical short block with no
        # number and no citation used to fall through every tier and vanish.
        self.exact_cid_buckets: dict[str, list[str]] = defaultdict(list)
        self._words: dict[str, set[str]] = {}

    def build_indexes(self, run_id: str) -> None:
        """Build all indexes from blocks — streaming, checkpointed."""
        blocks = list(self.store.get_blocks(run_id))
        for block in blocks:
            self._index_block(block)

    def _index_block(self, block: BlockRecord) -> None:
        # Index by OCCURRENCE. Indexing by content id would make an exact copy
        # across two documents collapse into a single entry — i.e. the primary
        # bleed case would be structurally invisible.
        cid = block.instance_id
        self._cid_of[block.instance_id] = block.cid
        self._doc_of[block.instance_id] = block.addr.doc_version_hash
        self.exact_cid_buckets[block.cid].append(block.instance_id)
        self._words[block.instance_id] = set(block.normalized_content.split())
        content = block.normalized_content

        # Numeric literals — exact match bucket
        for num in re.findall(r"\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b", content):
            self.numeric_buckets[num].append(cid)

        # Shingles (5-grams, word-level)
        words = content.split()
        for i in range(len(words) - 4):
            shingle = " ".join(words[i:i+5])
            self.shingle_index[shingle].append(cid)

        # Symbols (Greek, math operators, sub/superscript markers)
        for sym in re.findall(r"[α-ωΑ-Ω∑∏∫∂∇±×÷≤≥≠≈∞∈∉⊂⊃∪∩∧∨¬→↔]", content):
            self.symbol_buckets[sym].append(cid)

        # Citations — [1], (Author, Year), doi:..., arxiv:...
        for cite in re.findall(r"\[\d+\]|\(\w+(?: et al\.)?, \d{4}\)|doi:\S+|arxiv:\S+", content, re.IGNORECASE):
            self.citation_buckets[cite.lower()].append(cid)

    def generate_candidates(self, run_id: str) -> Iterator[CandidatePair]:
        """Generate candidate pairs — yield in priority order, respect queue limit."""
        emitted = 0
        seen_pairs: set[tuple[str, str]] = set()

        # Priority 0: exact content match across different documents.
        for content_id, instances in self.exact_cid_buckets.items():
            if len(instances) < 2:
                continue
            for i, ia in enumerate(instances):
                for ib in instances[i + 1:]:
                    if self._doc_of.get(ia) == self._doc_of.get(ib):
                        continue   # same document: repetition, not carryover
                    pair = tuple(sorted([ia, ib]))
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    cp = self._make_pair(pair, [Channel.PHRASE], 1.0)
                    if cp is None:
                        continue
                    yield cp
                    emitted += 1
                    if emitted >= self.queue_limit:
                        return

        # Priority 0b: every working block against every declared reference block
        # with meaningful token overlap. Reference sets are small and declared, so
        # this stays bounded.
        if self.reference_instances:
            working = [i for i in self._words if i not in self.reference_instances]
            for ref_id in sorted(self.reference_instances):
                ref_words = self._words.get(ref_id, set())
                if not ref_words:
                    continue
                for work_id in working:
                    work_words = self._words.get(work_id, set())
                    if not work_words:
                        continue
                    overlap = len(ref_words & work_words)
                    union = len(ref_words | work_words)
                    if union == 0 or overlap / union < self.REFERENCE_OVERLAP_MIN:
                        continue
                    pair = tuple(sorted([ref_id, work_id]))
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    cp = self._make_pair(pair, [Channel.PHRASE], overlap / union)
                    if cp is None:
                        continue
                    yield cp
                    emitted += 1
                    if emitted >= self.queue_limit:
                        return

        # Priority 1: Shared numeric literals (high precision)
        for literal, cids in self.numeric_buckets.items():
            if len(cids) < 2:
                continue
            rarity = self._rarity_of_numeric(literal)
            for i, cid_a in enumerate(cids):
                for cid_b in cids[i+1:]:
                    pair = tuple(sorted([cid_a, cid_b]))
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    cp = self._make_pair(pair, [Channel.NUMERIC], rarity)
                    if cp is None:
                        continue
                    yield cp
                    emitted += 1
                    if emitted >= self.queue_limit:
                        return

        # Priority 2: Shared citations
        for cite, cids in self.citation_buckets.items():
            if len(cids) < 2:
                continue
            rarity = 1.0 / len(cids)
            for i, cid_a in enumerate(cids):
                for cid_b in cids[i+1:]:
                    pair = tuple(sorted([cid_a, cid_b]))
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    cp = self._make_pair(pair, [Channel.CITATION], rarity)
                    if cp is None:
                        continue
                    yield cp
                    emitted += 1
                    if emitted >= self.queue_limit:
                        return

        # Priority 3: Rare shingles (IDF-weighted)
        shingle_scores: list[tuple[float, str, list[str]]] = []
        for shingle, cids in self.shingle_index.items():
            if len(cids) < 2 or len(cids) > 100:  # too common or too rare
                continue
            # IDF approximation
            idf = 1.0 / len(cids)
            shingle_scores.append((idf, shingle, cids))

        shingle_scores.sort(reverse=True)  # rarest first
        for _, shingle, cids in shingle_scores:
            for i, cid_a in enumerate(cids):
                for cid_b in cids[i+1:]:
                    pair = tuple(sorted([cid_a, cid_b]))
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    cp = self._make_pair(pair, [Channel.PHRASE], 1.0 / len(cids))
                    if cp is None:
                        continue
                    yield cp
                    emitted += 1
                    if emitted >= self.queue_limit:
                        return

        # Priority 4: fuzzy phrase via banded stdlib MinHash.
        # NOT IMPLEMENTED. Deliberately absent rather than stubbed: see
        # KNOWN_LIMITATIONS.md. Tiers 1-3 above are the whole funnel today.

    def _make_pair(self, pair, channels, rarity) -> CandidatePair | None:
        """Build a candidate from two occurrence ids, or None if degenerate."""
        a, b = pair
        if a == b:
            return None
        return CandidatePair(
            target_instance=a, source_instance=b,
            target_cid=self._cid_of.get(a, a), source_cid=self._cid_of.get(b, b),
            target_ordinal=0, source_ordinal=0,
            trigger_channels=list(channels),
            rarity_score=rarity,
        )

    def _rarity_of_numeric(self, literal: str) -> float:
        """Rarity of numeric literal — exact match is high signal."""
        count = len(self.numeric_buckets.get(literal, []))
        return 1.0 / max(1, count)