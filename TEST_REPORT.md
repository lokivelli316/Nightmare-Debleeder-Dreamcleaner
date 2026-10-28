# TEST_REPORT

Build: debleeder 0.2.1-alpha · Private alpha · 2026-08-05
Status vocabulary per `CLAUDE_MASTER_BUILD_DIRECTIVE.md` §9.

## v0.2.1 Reference Documents patch — raw output

```
$ python --version
Python 3.12.3

$ python -m compileall -q src tests
exit=0

$ python -m pytest -q -rs
52 passed, 10 skipped in 0.88s

$ python scripts/verify_build.py
19 passed            (doctrine invariants)
 9 passed,  5 skipped (safety doctrine)
 1 passed,  5 skipped (identity / lineage)
52 passed, 10 skipped (full suite)
BUILD VERIFICATION PASSED
exit=0
```

Tests were written before implementation. At that point the new file reported
`11 failed, 1 passed` — the single pass being
`test_same_material_without_declaration_still_escalates`, which asserts that
existing v0.2.0 behaviour is unchanged.

### v0.2.1 gate status

| Directive requirement | Status | Evidence |
|---|---|---|
| 1. Exact reference continuity visible, not escalated | BUILDER VERIFIED | `test_exact_reference_is_visible_but_does_not_escalate` |
| 2. Undeclared material still escalates | BUILDER VERIFIED | `test_same_material_without_declaration_still_escalates` |
| 3. Short exact block is still a candidate | BUILDER VERIFIED | `test_short_exact_block_is_still_a_candidate` |
| 4. Disagreement produces a surgical suggestion | BUILDER VERIFIED | `test_reference_disagreement_produces_surgical_suggestion` |
| 5. Structured conflict not suppressed | BUILDER VERIFIED | `test_structured_conflict_is_not_suppressed` |
| 6. Hash mismatch blocks application | BUILDER VERIFIED | `test_hash_mismatch_blocks_application`, `test_unapproved_suggestion_cannot_be_applied`, `test_approved_suggestion_applies_to_a_new_artifact_only` |
| 7. Reference evidence survives seal / bundle-only | BUILDER VERIFIED | `test_reference_evidence_survives_seal_and_bundle_only_inspection`, `test_reference_manifest_is_deterministic` |
| 8. Regression gates green | BUILDER VERIFIED | full suite above; `verify_build.py` exit 0 |
| Reworded canon line still found | BUILDER VERIFIED | `test_drifted_short_line_against_reference_is_still_found` |
| Role is explicit, never inferred | BUILDER VERIFIED | `test_reference_role_is_explicit_never_inferred`, `test_import_without_references_marks_everything_working` |
| Termux install / resource profile | DEFINED — NOT RUN | no target hardware |
| Private corpus seam tests | DEFINED — NOT RUN | private profile not built |
| DOCX tracked-change donor (`pablospe/docx-editor`) | ACKNOWLEDGED | not evaluated, not load-bearing, not imported |

Nothing in this patch is INDEPENDENTLY VERIFIED.

## v0.2.0 raw output

```
$ python --version
Python 3.12.3

$ python -m compileall -q src tests
exit=0

$ python -m pytest -q -rs
39 passed, 10 skipped in 0.73s

$ python scripts/verify_build.py
19 passed in 0.62s            (doctrine invariants)
 9 passed,  5 skipped         (safety doctrine)
 1 passed,  5 skipped         (identity / lineage)
39 passed, 10 skipped         (full suite)
BUILD VERIFICATION PASSED
```

Skips are printed with reasons by `pytest -rs` and are all of the form
`NOT RUN: <why>`. Nothing in this suite passes by being empty.

## What the v0.1 scaffold actually reported

For comparison, on the inherited scaffold in the same environment:

```
7 failed, 19 passed
pip install -e .  -> ERROR: No matching distribution found for minhash-lsh
```

All five `test_safety_doctrine` tests failed at fixture setup, before reaching an
assertion. The doctrine had never been exercised.

## Gates and their status

| Gate (directive §7) | Status | Evidence |
|---|---|---|
| 1. Parse / import | BUILDER VERIFIED | `test_same_content_different_position`, CLI end-to-end run |
| 2. Empty project startup | BUILDER VERIFIED | `debleeder init` smoke test in `verify_build.py` |
| 3. Duplicate block occurrence identity | BUILDER VERIFIED | `test_identical_content_in_two_documents_is_two_occurrences` |
| 4. Decision crash recovery | BUILDER VERIFIED | `test_decision_survives_crash_before_projection` |
| 5. Outbox idempotence | BUILDER VERIFIED | `test_outbox_projection_is_idempotent` |
| 6. Journal tamper / corruption recovery | BUILDER VERIFIED | `test_tamper_preserves_history_and_opens_recovery_branch`, `test_tampered_journal_refuses_further_appends` |
| 7. Seal crash recovery at every transition | PARTIAL — IMPLEMENTED, UNVERIFIED | `test_reconcile_names_an_interrupted_seal` covers state naming; per-boundary process kills are NOT yet injected |
| 8. Portable bundle-only rebuild | BUILDER VERIFIED | `test_bundle_rebuilds_without_the_live_database`, `test_bundle_detects_modified_artifact` |
| 9. Offline GUI / XSS / CSRF / traversal / body-limit / refresh | PARTIAL | escaping and single-pass substitution tested; CSRF, body limit and headers IMPLEMENTED — UNVERIFIED |
| 10. Private corpus expected-seam tests | DEFINED — NOT RUN | requires the private profile, not built |
| 11. Public / private clean-environment boundary | BUILDER VERIFIED (static) | `test_public_core_has_no_private_imports`; clean-venv install still to run |
| 12. Android / Termux install and resource profile | DEFINED — NOT RUN | no target hardware in this environment |

## Doctrine invariants now covered by tests

- Writer and verifier hash the same envelope; `verify_chain()` returns True on a journal the writer produced.
- Journal offsets are byte-accurate with non-ASCII content.
- Tampering preserves every byte, quarantines the damaged tail, opens a recorded recovery branch, and puts the journal into read-only.
- Decision + outbox commit in one SQLite transaction; journal projection is idempotent and replays after a crash.
- `verify_dual_write` executes and blocks sealing when it fails.
- A correction appends; it never overwrites. History and effective state are both retrievable.
- Identical content in two documents produces two occurrences and one real candidate pair.
- Rarity is a document-frequency IDF: content in one document scores rarer than content in all of them.
- Sealing is refused while any block is undecided, marked REVIEW, or REWRITE without an approved replacement.
- The run is marked SEALED only after the bundle is published and its published bytes re-verified.
- A sealed bundle rebuilds its clean report with no live database, and detects a modified artifact.
- Quarantined content is absent from the clean report and recoverable from the bundle.
- GUI escapes document-derived content and substitutes placeholders in a single pass.
- No guilt vocabulary anywhere in source or templates; core has no network imports; GUI binds loopback.

## Honest gaps

Nothing here is INDEPENDENTLY VERIFIED. Everything above is BUILDER VERIFIED at
most: it was run by the same party that wrote the repair. See
`KNOWN_LIMITATIONS.md` for what is deliberately absent.


## 2026-08-11 false-fire regression repair

Status ceiling: **BUILDER VERIFIED**. No human-device or independent verification claim.

### Failure preservation

The supplied false-fire package was applied to a clean `df96275` branch and tested before
additional repair. The new three-test regression file first failed **3/3** on the baseline.
After applying the supplied package unchanged, it still failed **2/3**:

- weak reference-overlap still created a `PROPOSED` correction;
- reference-gate values were declared in `Config.thresholds` but the candidate funnel still
  used hard-coded class constants.

The first full-suite run after integrating the missing authority/config wiring also exposed a
public/private-boundary failure because private corpus identifiers had been copied into public
core comments. The identifiers were removed; no runtime behavior changed.

### Fresh automated result

```text
python -m pytest -q -rs
55 passed, 10 skipped

python -m compileall -q src tests
exit=0

new false-fire regressions
3 passed

existing reference-document gates
13 passed
```

The ten skips remain the explicit deferred/cut gates already documented by the project; they
are not counted as passes.

### Real-corpus negative control

An unrelated technical working/reference pair was run in both directions. Before the repair,
each direction produced **2,256** proposed suggestions, including **1,931** suggestions whose
only detected channel was `reference_overlap`. After the repair, each direction produced
**46** suggestions, **0** reference-overlap-only suggestions, and **0** auto-applies. This is
a reduction of about **98%** in the actionable suggestion burden while preserving the raw
reference context for inspection.

### Known-parent lineage control

A synthesized technical document was compared with two declared parent references. The
pre-repair build produced **2,201** suggestions, including **1,949** reference-overlap-only
suggestions. The repaired build produced **58** suggestions and **0** reference-overlap-only
suggestions. The retained suggestions were supported by ordinary evidence: **45 numeric**,
**12 symbol**, and **1 numeric+symbol**; triage strength was **49 HIGH** and **9 MEDIUM**.
Both declared parent references remained represented. Auto-apply remained **0**.

This supports the narrow claim that the false-fire authority leak is repaired without simply
turning off structured/numeric disagreement detection. Broader corpus calibration remains
open.
