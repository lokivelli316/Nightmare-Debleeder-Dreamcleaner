"""Deterministic rebuild — clean report from originals + ledger only."""

from __future__ import annotations
from pathlib import Path
import json

from ..core.protocols import BlockRecord, DecisionRecord, Disposition, RunStore
from ..storage.sqlite_store import SQLiteStore


def generate_clean_report(run_id: str, store: RunStore, output_path: Path) -> None:
    """Generate clean Markdown report — deterministic, no timestamps."""
    # Sort by document first: ordinal alone interleaves blocks from different
    # sources and destroys document boundaries in the report.
    blocks = list(store.get_blocks(run_id))
    blocks.sort(key=lambda b: (b.addr.doc_version_hash, b.addr.ordinal))

    # Latest decision wins; full history stays in the store and the journal.
    decisions = {}
    for d in store.get_decisions(run_id):
        decisions[d.block_instance_id] = d

    lines = [
        "# Debleeder Clean Report",
        "",
        f"Run ID: {run_id}",
        "Generated: DETERMINISTIC_REBUILD",  # no timestamp
        "",
        "---",
        "",
    ]

    for block in blocks:
        decision = decisions.get(block.instance_id)
        disp = decision.disposition if decision else Disposition.KEEP

        # Heading path
        if block.heading_path:
            lines.append(f"{'#' * (len(block.heading_path) + 1)} {' > '.join(block.heading_path)}")
            lines.append("")

        # Disposition badge
        badge = _disposition_badge(disp, decision.reason if decision else "")
        lines.append(f"> **Disposition**: {badge}")
        lines.append("")

        # Content (or placeholder for removed)
        if disp in (Disposition.QUARANTINE, Disposition.SHRED, Disposition.PURGE):
            lines.append(f"> *[Block {block.cid[:8]} {disp.value.upper()}]*")
            lines.append("")
        elif disp == Disposition.REWRITE:
            lines.append(block.raw_content)
            lines.append("")
            lines.append("> *[Rewritten — see ledger for original]*")
            lines.append("")
        else:
            lines.append(block.raw_content)
            lines.append("")

    # Deterministic write — no trailing whitespace, consistent newlines
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _disposition_badge(disp: Disposition, reason: str) -> str:
    badges = {
        Disposition.KEEP: "✅ KEEP",
        Disposition.KEEP_SEEDED: "🌱 KEEP_SEEDED",
        Disposition.REWRITE: "✏️ REWRITE",
        Disposition.REVIEW: "👁️ REVIEW",
        Disposition.QUARANTINE: "🔒 QUARANTINE",
        Disposition.SHRED: "🗑️ SHRED",
        Disposition.PURGE: "💀 PURGE",
    }
    badge = badges.get(disp, disp.value.upper())
    if reason:
        badge += f" — {reason}"
    return badge


def _render_report(run_id: str, blocks: list, decisions: dict) -> str:
    """Shared renderer so bundle rebuild and live generation cannot drift."""
    lines = [
        "# Debleeder Clean Report",
        "",
        f"Run ID: {run_id}",
        "Generated: DETERMINISTIC_REBUILD",
        "",
        "---",
        "",
    ]
    for block in blocks:
        decision = decisions.get(block["instance_id"])
        disp = Disposition(decision["disposition"]) if decision else Disposition.KEEP
        reason = decision["reason"] if decision else ""
        if block["heading_path"]:
            lines.append(f"{'#' * (len(block['heading_path']) + 1)} {' > '.join(block['heading_path'])}")
            lines.append("")
        lines.append(f"> **Disposition**: {_disposition_badge(disp, reason)}")
        lines.append("")
        if disp in (Disposition.QUARANTINE, Disposition.SHRED, Disposition.PURGE):
            lines.append(f"> *[Block {block['cid'][:8]} {disp.value.upper()}]*")
            lines.append("")
        elif disp == Disposition.REWRITE:
            lines.append(block["raw_content"])
            lines.append("")
            lines.append("> *[Rewritten — see ledger for original]*")
            lines.append("")
        else:
            lines.append(block["raw_content"])
            lines.append("")
    return "\n".join(lines) + "\n"


def rebuild_from_bundle(bundle_path: Path, store: RunStore | None = None) -> bool:
    """Rebuild the clean report using ONLY the bundle's own contents.

    No live database access. This is what makes the bundle portable: if this
    returns True in a clean environment, the bundle is self-sufficient.
    """
    import hashlib

    bundle_path = Path(bundle_path)
    manifest_path = bundle_path / "manifest.json"
    blocks_path = bundle_path / "blocks.jsonl"
    decisions_path = bundle_path / "decisions.jsonl"
    if not (manifest_path.exists() and blocks_path.exists() and decisions_path.exists()):
        return False

    manifest = json.loads(manifest_path.read_text())

    # Every declared file must be present and hash-correct.
    for name, digest in manifest.get("files", {}).items():
        target = bundle_path / name
        if not target.exists():
            return False
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            return False

    blocks = [json.loads(l) for l in blocks_path.read_text().splitlines() if l.strip()]
    blocks.sort(key=lambda b: (b["doc_version_hash"], b["ordinal"]))
    decisions: dict[str, dict] = {}
    for line in decisions_path.read_text().splitlines():
        if line.strip():
            d = json.loads(line)
            decisions[d["block_instance_id"]] = d   # append-only file, last wins

    rebuilt = _render_report(manifest["run_id"], blocks, decisions)
    rebuilt_hash = hashlib.sha256(rebuilt.encode("utf-8")).hexdigest()
    return rebuilt_hash == manifest["clean_report_hash"]
