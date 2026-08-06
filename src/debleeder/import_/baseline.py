"""Baseline spreadsheet generation — deterministic XLSX, hashed into ledger."""

from __future__ import annotations
from pathlib import Path
import hashlib
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

from ..core.protocols import BlockRecord, RunManifest
from ..storage.sqlite_store import SQLiteStore


def generate_baseline_xlsx(
    run_id: str,
    store: SQLiteStore,
    output_path: Path,
    manifest: RunManifest,
) -> str:
    """Generate deterministic baseline workbook — returns SHA-256 hash."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Baseline"

    # Headers
    headers = [
        "CID", "Ordinal", "Heading Path", "Extraction Quality",
        "Character Count", "Word Count", "Equation Count",
        "Normalized Content (truncated)", "Raw Content (truncated)"
    ]
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = Font(bold=True)
        cell.fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
        cell.font = Font(color="FFFFFF", bold=True)

    # Data rows — deterministic order by ordinal
    blocks = list(store.get_blocks(run_id))
    blocks.sort(key=lambda b: (b.addr.doc_version_hash, b.addr.ordinal))

    for row_idx, block in enumerate(blocks, 2):
        ws.cell(row=row_idx, column=1, value=block.cid)
        ws.cell(row=row_idx, column=2, value=block.addr.ordinal)
        ws.cell(row=row_idx, column=3, value=" > ".join(block.heading_path))
        ws.cell(row=row_idx, column=4, value=block.extraction_quality.value)
        ws.cell(row=row_idx, column=5, value=len(block.normalized_content))
        ws.cell(row=row_idx, column=6, value=len(block.normalized_content.split()))
        ws.cell(row=row_idx, column=7, value=block.metadata.get("equation_count", 0))
        ws.cell(row=row_idx, column=8, value=block.normalized_content[:500])
        ws.cell(row=row_idx, column=9, value=block.raw_content[:500])

    # Auto-fit columns (deterministic widths)
    for col in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(col)].width = min(50, max(12, len(headers[col-1]) + 2))

    # Manifest sheet
    ms = wb.create_sheet("Manifest")
    ms.cell(row=1, column=1, value="Run ID").font = Font(bold=True)
    ms.cell(row=1, column=2, value=manifest.run_id)
    ms.cell(row=2, column=1, value="Profile").font = Font(bold=True)
    ms.cell(row=2, column=2, value=manifest.profile.value)
    ms.cell(row=3, column=1, value="Method Version").font = Font(bold=True)
    ms.cell(row=3, column=2, value=str(manifest.method_version))
    ms.cell(row=4, column=1, value="Corpus Snapshot").font = Font(bold=True)
    ms.cell(row=4, column=2, value=manifest.corpus_snapshot_hash)
    ms.cell(row=5, column=1, value="Block Count").font = Font(bold=True)
    ms.cell(row=5, column=2, value=len(blocks))

    # Deterministic save: openpyxl stamps docProps/core.xml with the current time,
    # which would make two identical runs hash differently. Pin it.
    from datetime import datetime as _dt
    _epoch = _dt(2020, 1, 1, 0, 0, 0)
    wb.properties.created = _epoch
    wb.properties.modified = _epoch
    wb.properties.creator = "debleeder"
    wb.properties.lastModifiedBy = "debleeder"
    wb.save(output_path)

    # Hash the file
    return hashlib.sha256(output_path.read_bytes()).hexdigest()