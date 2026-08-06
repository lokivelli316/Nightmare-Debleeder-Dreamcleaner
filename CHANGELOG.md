# CHANGELOG

## 0.2.1-alpha — 2026-08-05 — Reference Documents

Focused patch. No architecture rewrite. Every v0.2.0 safety contract and every
green gate preserved. Tests were written first and shown failing (11 failed,
1 passed) before implementation.

### Root defect closed

`keep_seeded` existed in the doctrine but was unreachable: `_load_seed_manifest()`
returned `{}`, and contextual evidence was discarded whenever its strength was
`NONE` — so even a populated manifest could not have reached triage.

### Added

- `DocumentRole` (`working` / `reference`). Declared explicitly at import; never inferred.
- `stage_import(run_id, source_paths, reference_paths=None)` — existing callers unchanged.
- Deterministic, hashable reference index (`reference_manifest()`), persisted in SQLite and projected to `reference_manifest.json` in the bundle.
- `ReferenceChannel` (alias `SeedChannel`): emits contextual evidence even at `NONE`, distinguishes `exact_match` / `reference_difference` / `reference_to_reference`, and orients working → reference.
- Triage classification: `intentional_continuity` (displayed strength suppressed, `raw_strength` retained, `keep_seeded` recommended — never auto-decided) and `reference_disagreement` (nothing suppressed).
- `PatchSuggestion` with lifecycle `PROPOSED → APPROVED | EDITED | REJECTED | STALE | APPLIED`, carrying before-hash, exact current and proposed text, compact unified diff, detecting channels, `requires_human_approval: true`, `auto_apply: false`.
- `approve_suggestion` / `reject_suggestion` / `apply_suggestion`. Apply refuses unless approved, re-hashes the working block immediately before applying, refuses and marks `STALE` on mismatch, writes ledger intent before output, applies an exact anchored replacement only, and writes to a new artifact.
- Candidate funnel tier 0: exact content-id occurrences across different documents — a short identical block with no number and no citation used to fall through every tier.
- Candidate funnel tier 0b: every working block against every declared reference block above a token-overlap floor. A reworded canon line shares no 5-gram and would otherwise never be compared.
- CLI: `--reference`, `suggestions`, `approve`, `apply`; `init` creates `references/` and states plainly that nothing there is imported unless declared.
- GUI: `Intentional continuity` label, current-beside-reference display, compact diff, and CSRF-protected Approve / Edit / Reject forms. No apply control.
- Bundle: `reference_manifest.json` and `patch_suggestions.jsonl`.

### Changed

- Block occurrence ids no longer include the run id, so the same document yields the same occurrence ids across runs and the reference manifest hash is reproducible.
- GUI CSRF accepts a form field as well as a header, because the CSP forbids scripts and a plain form cannot set a custom header.

### Deprecated

- `_load_seed_manifest()` — retained so older callers do not break, returns `{}` by design.


## 0.2.0-alpha — 2026-08-05

Repair pass over the inherited scaffold. The scaffold compiled; its safety layer
had never been executed. Every change below closes a defect that was reproduced by
running the code.

### Load-bearing

- **Journal no longer destroys history on tamper detection.** `_rebuild_chain` used to catch its own hash-mismatch exception and call `truncate()` — three records in, one survived. Now: bytes preserved, damaged tail quarantined, recovery branch recorded, journal read-only, `JournalIntegrityError` raised.
- **`verify_chain()` works.** Writer and verifier now share one `canonical_envelope()`. Previously they hashed different byte strings, so the check returned False on a journal the writer had just produced and could never pass.
- **`verify_dual_write()` works and is wired.** It previously raised `ValueError` on its first loop and had zero callers. It now runs and blocks sealing when it fails.
- **Real outbox.** Decision row and outbox event commit in one SQLite transaction; the JSONL journal is an idempotent projection replayed on startup. The previous "atomic dual-write" wrote JSONL first, then SQLite, with nothing spanning them.
- **Decisions are append-only.** The `decisions` primary key was `(run_id, block_cid)`, so a correction silently replaced the original. Now every decision inserts a new row with a monotonic `seq`; effective state is the latest, history is intact.
- **Block occurrence identity.** `blocks` was keyed on `(run_id, cid)`, so identical content in two documents collapsed into one row and the funnel produced a degenerate self-pair. Exact copies — the primary bleed case — were structurally invisible. Added `instance_id`; evidence, decisions and quarantine now reference occurrences.
- **Seal is ordered correctly.** Was: mark SEALED, then copy files, then verify the *temp* copies. Now: build in a same-filesystem sibling, fsync, verify, atomic `rename`, fsync parent, verify the **published** bytes, then mark SEALED. Explicit persisted state machine plus `reconcile()`.
- **Portable bundle.** The bundle now carries `blocks.jsonl`, `decisions.jsonl`, `quarantine.jsonl`, the audit projection and `LIMITATIONS.md`, and `rebuild_from_bundle()` uses only the bundle's contents. It previously rebuilt from the live database.
- **No implicit decisions.** Sealing is refused while any block is undecided, marked REVIEW, or REWRITE without an approved replacement.
- **GUI escaping.** Document content was interpolated raw into HTML — stored XSS in a tool whose input is untrusted documents. Added escaping, single-pass substitution, CSRF header, body limit, security headers.

### Installability and platform

- Removed `minhash-lsh` (does not exist on PyPI; `pip install -e .` could not succeed, so `verify_build.py` failed at step 1 and had never printed PASSED).
- Removed `mmh3` (C extension, no aarch64 wheel for Termux) — it was imported and never used.
- Declared what is actually imported; dropped `jinja2`, `htpy`, `pydantic` which were declared and unused.
- Added `[project.scripts]`, src-layout packaging, package data for templates, `__init__.py` for every subpackage.

### Correctness

- `MethodVersion.__str__` read `__dict__` on a slots dataclass — raised on the seal path. Added `parse()` for a real round trip.
- `SQLiteStore._txn_counter` was a dataclass default that never applied because `__init__` is hand-written; every transaction raised `AttributeError`.
- `get_all_evidence` unpacked columns off by one from `confidence` onward — the triage stage could not run.
- Blocks were reconstructed with a fabricated anonymous class instead of `RunAddress`.
- `CorpusSnapshot` was constructed with an `index_path` the dataclass did not have.
- Corpus index counted block occurrences as documents; `get_rarity` is now a real document-frequency IDF.
- Channel evaluation was gated on which funnel tier discovered the pair, so a pair found by a shared number never reported its phrase overlap. All channels now run on every candidate.
- `normalize_text` was defined and never applied; identity was computed over raw text. Now normalized for matching, `raw_content` for output.
- Extraction failures no longer abort a run; they yield a `FAILED`-quality block.
- Clean reports sort by document then ordinal instead of interleaving documents.
- XLSX properties pinned so the workbook is reproducible.
- `datetime.utcnow()` replaced with timezone-aware UTC throughout.
- GUI index route double-sent and crashed on `None.encode()`; `{next_cid}` shipped unformatted in user-facing HTML.
- CLI mutated a frozen `Config`.

### Tests

- `tests/test_doctrine_invariants.py` — 19 new tests, one per repaired defect.
- Safety-doctrine tests now construct `Config` correctly and actually assert; two that were empty of content now import documents, analyze, and check decomposition.
- Stub tests that passed by containing `pass` are now explicit skips with reasons.
- Added public/private boundary, no-guilt-language, no-network, and loopback-binding tests.
- `verify_build.py` no longer fails on absent lint/type tooling and runs the doctrine gates first.
