# KNOWN_LIMITATIONS

Private alpha. Read this before pointing the tool at anything you care about —
though note that originals are never modified, so the worst case is a wasted run,
not a lost document.

## Reference Documents (v0.2.1) — what is and is not true yet

**Works:** explicit `--reference` role at import, a deterministic hashable
reference index, exact-continuity classification with evidence retained and
escalation suppressed, unsuppressed reference disagreements, hash-anchored
correction suggestions with a PROPOSED → APPROVED/EDITED/REJECTED/STALE/APPLIED
lifecycle, refusal on hash mismatch, and reference/suggestion persistence into the
sealed bundle.

**Not yet true:**

| Item | Status | Why it matters |
|---|---|---|
| Reference candidate tier uses a fixed Jaccard token-overlap floor of 0.4 | PROVISIONAL, uncalibrated | A heavily reworded canon passage below that floor is not compared at all. The number is a guess. |
| Ordinary channels are not stored at `NONE` strength for reference pairs | By design, but it has a cost | A disagreement found only by reference overlap reports `detected_channels: ("reference_overlap",)` — truthful, but it means no per-channel numbers are shown for that pair. |
| `apply_suggestion` writes a per-block `corrected_<instance>.md` | MINIMAL | It is not a document rewrite. There is no DOCX/PDF round-trip and no tracked changes. |
| `EDITED` status exists but there is no editor | STUB | The GUI can mark a suggestion EDITED; it cannot yet capture edited replacement text. An EDITED suggestion applies the reference text as-is. |
| Reference-to-reference conflicts | NOT SURFACED | Detected and marked `reference_to_reference`, then excluded from ordinary triage. There is no reference-conflict view. |
| `pablospe/docx-editor` donor | ACKNOWLEDGED — NOT EVALUATED | License, Termux compatibility, determinism, and anchor-to-occurrence mapping are all unverified. Not imported, not load-bearing. |
| `_load_seed_manifest()` | DEPRECATED | Returns `{}` by design now. Reference Documents replaced it. Old callers do not break. |

## Deliberately not built

| Item | Status | Why |
|---|---|---|
| `purge` | CUT from alpha | The only operation that can destroy data. It earns nothing yet. Ships later, alone, with its own confirmation flow and tests. |
| Candidate funnel tier 4 (fuzzy MinHash/LSH) | NOT IMPLEMENTED | Absent rather than stubbed. Tiers 1–3 (numeric, citation, rare shingle) are the whole funnel today. Near-duplicates with no shared literal and no shared 5-gram will be missed. |
| Semantic channel | NOT BUILT | Heaviest dependency, weakest evidence, least explainable. |
| Cross-version lineage (SPLIT / MERGE / MOVED / EDITED) | DEFINED — NOT RUN | The lineage channel exists and the schema supports it, but no lineage is populated, so direction is almost always reported UNKNOWN. |
| Seed manifest loading | SUPERSEDED in v0.2.1 | Replaced by Reference Documents. `_load_seed_manifest()` is deprecated and returns `{}` deliberately. |
| Rewrite artifacts | MINIMAL | A REWRITE disposition blocks sealing unless `override_note` is filled. There is no editor and no replacement-text workflow. |
| Private profile | NOT BUILT | No `debleeder_private` package exists. The public core is clean of theory-specific data and that is currently the whole story. |

## Provisional and uncalibrated

Every threshold in `core/config.py` is a guess. Nothing has been calibrated
against a real corpus. In particular:

- Single-channel HIGH eligibility, corroboration rules, and pair aggregation are unvalidated.
- Strength bands are computed from raw scores whose distributions nobody has measured.
- On a single-author corpus the phrase channel will over-fire until rarity weighting is wired into scoring. `get_rarity()` is implemented and tested but **not yet consumed by the triage engine**.

Treat every band as "look at this", never as a finding.

## Known behaviour worth knowing

- **Direction is usually UNKNOWN.** Similarity is symmetric. Without lineage data the tool cannot say which document a shared block came from.
- **Clean reports interleave by document then ordinal.** A run over many documents produces one report containing all of them.
- **The clean report embeds the run id**, so two different runs over identical input hash differently by design. Determinism is asserted over report content.
- **XLSX timestamps are pinned** to make the workbook reproducible; the `created` date in the file properties is deliberately fake.
- **Normalization is lowercasing and punctuation folding.** Identity matching is case-insensitive. Output always renders `raw_content`.
- **The GUI requires a CSRF header** printed at startup. It is a single-user loopback tool, not a multi-user server. There are no sessions and no login.
- **Blocks are paragraphs.** No sentence-level or section-level segmentation.
- **PDF equation extraction is text-layer only.** Equations rendered as images arrive as nothing and the block is flagged `extraction_quality: low` or `failed`.

## Not yet run anywhere

- Termux / Android install and resource profile.
- Clean-virtualenv install from a built wheel.
- Any corpus larger than a handful of test documents. Performance is unmeasured.
- Process-kill injection at seal boundaries (state naming is tested; actual kills are not).
