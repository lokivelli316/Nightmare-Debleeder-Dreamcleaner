# Nightmare Debleeder Dreamcleaner

**Debleeder — Document Lineage Auditor**
Local-first, human-governed document debleeding and provenance.

> Status: **0.2.0-alpha — private alpha.** Not a release. See `KNOWN_LIMITATIONS.md`
> before using it on anything, and `TEST_REPORT.md` for what is actually verified.

## The nightmare

Your report is bleeding language, equations, parameters and assumptions from your
other work. You don't know where it came from. You don't know how much. You just
know something is wrong.

## The debleeder

Import the documents. Build the evidence spreadsheet. Analyze possible carryover.
Score every match with the reasoning exposed. Put every decision under human
control. No automatic deletion. No mysterious rewrites.

## The dreamcleaner

Log the complete before-state. Generate a clean report. Preserve quarantined
material separately. Retain enough evidence to reverse a bad decision. Your work
comes back *cleaner* — not mysteriously rewritten.

## Doctrine

Import -> baseline -> analyze -> evaluate -> **human chooses** -> log and verify
before omission -> clean report -> atlas and exports.

- Originals are never modified.
- Evidence is not a verdict. The tool does not assert plagiarism, theft, fraud or intent.
- A correction appends. History is never erased.
- Nothing is omitted until the ledger is durable and verified.
- Quarantined material stays recoverable.
- Nothing leaves the device.

## Quick start

```bash
pip install -e ".[dev]"
python -m debleeder init ~/debleeder_project
cp your_documents/* ~/debleeder_project/imports/
python -m debleeder run --project ~/debleeder_project --import ~/debleeder_project/imports
python -m debleeder verify ~/debleeder_project/exports/bundles/run_<id>
```

`python -m debleeder reconcile <run_id>` names the state of an interrupted seal.

Pure-Python dependencies only: no C extensions, so it installs under Termux.

## Verify the build

```bash
python scripts/verify_build.py
```

---

> *"Why the hell is my other report in this report?"*
> Debleed it. Get the dream back.
