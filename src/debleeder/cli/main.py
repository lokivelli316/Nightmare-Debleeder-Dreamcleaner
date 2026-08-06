"""CLI — fallback, automation, test interface. Same core as GUI."""

from __future__ import annotations
from pathlib import Path
import sys
import typer

from ..core.config import Config, Profile
from ..core.orchestrator import DebleederCore
from ..core.protocols import Disposition, MethodVersion
from ..gui.server_stdlib import run_gui_phone

app = typer.Typer(name="debleeder", help="Nightmare Debleeder Dreamcleaner")


@app.command()
def init(
    project_root: Path = typer.Argument(Path.cwd(), help="Project root directory"),
    profile: Profile = typer.Option(Profile.PHONE, help="Execution profile"),
):
    """Initialize project structure."""
    (project_root / "imports").mkdir(exist_ok=True)
    (project_root / "exports").mkdir(exist_ok=True)
    (project_root / "bundles").mkdir(exist_ok=True)
    (project_root / "seeds").mkdir(exist_ok=True)
    # v0.2.1: references/ exists but is NEVER imported silently. Declaring a
    # Reference Document is an explicit act: pass --reference at run time.
    (project_root / "references").mkdir(exist_ok=True)
    typer.echo(f"Initialized Debleeder project at {project_root}")
    typer.echo("references/ created. Nothing there is imported unless you pass "
               "--reference; a document becomes canon only when you say so.")


@app.command()
def run(
    project_root: Path = typer.Option(Path.cwd(), "--project", "-p"),
    profile: Profile = typer.Option(Profile.PHONE, "--profile"),
    gui: bool = typer.Option(False, "--gui", help="Launch GUI"),
    import_dir: Path = typer.Option(None, "--import", help="Directory of working documents to import"),
    reference_dir: Path = typer.Option(
        None, "--reference",
        help="Directory of Reference Documents (declared canon). Never inferred."),
):
    """Run full pipeline or launch GUI."""
    import dataclasses
    config = Config.load(project_root / "config.yaml")
    config = dataclasses.replace(
        config, profile=profile, project_root=project_root,
        data_dir=project_root / ".debleeder")

    core = DebleederCore(config)

    if gui:
        typer.echo("Starting GUI...")
        run_gui_phone(core, config.gui_host, config.gui_port)
        return

    # Headless pipeline (for automation/testing)
    if not import_dir:
        typer.echo("Error: --import required for headless run", err=True)
        raise typer.Exit(1)

    # Create run
    manifest = core.store.create_run(profile, config.method_version, "initial_snapshot")
    typer.echo(f"Created run: {manifest.run_id}")

    # Stage 1: Import
    exts = {".docx", ".pdf", ".md", ".txt"}
    source_files = [f for f in import_dir.glob("**/*")
                    if f.is_file() and f.suffix.lower() in exts]
    reference_files = []
    if reference_dir:
        reference_files = [f for f in reference_dir.glob("**/*")
                           if f.is_file() and f.suffix.lower() in exts]
    counts = core.stage_import(manifest.run_id, source_files,
                               reference_paths=reference_files or None)
    typer.echo(f"Imported {len(source_files)} working document(s)"
               + (f" and {len(reference_files)} reference document(s)" if reference_files else "")
               + f": {counts}")
    if reference_files:
        rm = core.reference_manifest(manifest.run_id)
        typer.echo(f"Reference index: {len(rm['entries'])} blocks, "
                   f"manifest {rm['manifest_hash'][:16]}...")

    # Stage 2: Baseline
    baseline_hash = core.stage_baseline(manifest.run_id, manifest, project_root / "exports")
    typer.echo(f"Baseline generated: {baseline_hash[:16]}...")

    # Stage 3: Analyze
    evidence_count = core.stage_analyze(manifest.run_id)
    typer.echo(f"Evidence records: {evidence_count}")

    # Stage 4: Evaluate
    triage = core.stage_evaluate(manifest.run_id)
    high_strength = sum(1 for t in triage.values() if t["strength"] == "high")
    continuity = sum(1 for t in triage.values()
                     if t["classification"] == "intentional_continuity")
    disagreements = sum(1 for t in triage.values() if t["reference_disagreement"])
    typer.echo(f"Triage complete. HIGH strength pairs: {high_strength}")
    if continuity:
        typer.echo(f"Intentional continuity (declared reference, exact): {continuity}")
    if disagreements:
        typer.echo(f"Reference disagreements: {disagreements}")

    suggestions = core.list_suggestions(manifest.run_id)
    if suggestions:
        typer.echo(f"\n{len(suggestions)} correction suggestion(s) proposed. "
                   "None applied. Review with: debleeder suggestions <run_id>")

    # Stage 5: Choose (interactive)
    typer.echo("\n--- INTERACTIVE REVIEW ---")
    while True:
        block = core.get_next_undecided(manifest.run_id)
        if not block:
            break
        typer.echo(f"\nBlock {block.instance_id[:8]} (content {block.cid[:8]}, ordinal {block.addr.ordinal})")
        typer.echo(block.raw_content[:500])
        disp = typer.prompt("Disposition", type=Disposition)
        reason = typer.prompt("Reason")
        core.record_decision(manifest.run_id, block.instance_id, disp, reason)

    # Stage 6: Seal
    typer.echo("\nSealing...")
    result = core.stage_seal(manifest.run_id, manifest, project_root / "exports")
    if result.success:
        typer.echo(f"Sealed: {result.seal_hash[:16]}... Bundle: {result.bundle_path}")
    else:
        typer.echo(f"Seal failed in state {result.state}: {result.error}", err=True)
        for blocker in result.blockers:
            typer.echo(f"  - {blocker}", err=True)
        raise typer.Exit(1)

    # Stage 7: Generate
    report = core.stage_generate(manifest.run_id, project_root / "exports")
    typer.echo(f"Clean report: {report}")

    core.close()


@app.command()
def verify(
    bundle_path: Path = typer.Argument(..., help="Path to sealed bundle"),
    project_root: Path = typer.Option(Path.cwd(), "--project", "-p"),
):
    """Verify deterministic rebuild from bundle."""
    core = None
    ok = False
    try:
        from ..rebuild.generator import rebuild_from_bundle
        ok = rebuild_from_bundle(bundle_path)   # bundle-only, no live database
    finally:
        pass
    if ok:
        typer.echo("Bundle verified — deterministic rebuild matches")
    else:
        typer.echo("Bundle verification FAILED", err=True)
        raise typer.Exit(1)


@app.command()
def suggestions(
    run_id: str = typer.Argument(..., help="Run to list correction suggestions for"),
    project_root: Path = typer.Option(Path.cwd(), "--project", "-p"),
):
    """List surgical correction suggestions. Proposals only — nothing is applied."""
    import dataclasses
    config = Config.load(project_root / "config.yaml")
    config = dataclasses.replace(config, project_root=project_root,
                                 data_dir=project_root / ".debleeder")
    core = DebleederCore(config)
    items = core.list_suggestions(run_id)
    if not items:
        typer.echo("No suggestions.")
    for s in items:
        typer.echo(f"\n[{s.status}] {s.suggestion_id}")
        typer.echo(f"  reference : {s.reference_source_name}")
        typer.echo(f"  channels  : {', '.join(s.detected_channels) or '(context only)'}")
        typer.echo(f"  before    : {s.before_hash[:16]}...")
        typer.echo(f"  current   : {s.current_text.strip()[:120]}")
        typer.echo(f"  proposed  : {s.proposed_text.strip()[:120]}")
        typer.echo("  auto_apply: False (human approval required)")
    core.close()


@app.command()
def approve(
    run_id: str = typer.Argument(...),
    suggestion_id: str = typer.Argument(...),
    note: str = typer.Option(..., "--note", help="Why you are approving this"),
    project_root: Path = typer.Option(Path.cwd(), "--project", "-p"),
):
    """Record explicit human approval for one suggestion. Does not apply it."""
    import dataclasses
    config = Config.load(project_root / "config.yaml")
    config = dataclasses.replace(config, project_root=project_root,
                                 data_dir=project_root / ".debleeder")
    core = DebleederCore(config)
    s = core.approve_suggestion(run_id, suggestion_id, note)
    typer.echo(f"{suggestion_id}: {s.status}" if s else "No such suggestion")
    core.close()


@app.command()
def apply(
    run_id: str = typer.Argument(...),
    suggestion_id: str = typer.Argument(...),
    project_root: Path = typer.Option(Path.cwd(), "--project", "-p"),
):
    """Apply an APPROVED suggestion to a NEW artifact. Refuses on hash mismatch."""
    import dataclasses
    config = Config.load(project_root / "config.yaml")
    config = dataclasses.replace(config, project_root=project_root,
                                 data_dir=project_root / ".debleeder")
    core = DebleederCore(config)
    result = core.apply_suggestion(run_id, suggestion_id, project_root / "exports" / "corrected")
    if result.applied:
        typer.echo(f"Applied to new artifact: {result.output_path}")
        typer.echo("The imported original is unchanged.")
    else:
        typer.echo(f"Refused: {result.error}", err=True)
        core.close()
        raise typer.Exit(1)
    core.close()


@app.command()
def reconcile(
    run_id: str = typer.Argument(..., help="Run to reconcile after an interrupted seal"),
    project_root: Path = typer.Option(Path.cwd(), "--project", "-p"),
):
    """Name the state of an interrupted seal. Never guesses, never auto-repairs."""
    import dataclasses
    config = Config.load(project_root / "config.yaml")
    config = dataclasses.replace(config, project_root=project_root,
                                 data_dir=project_root / ".debleeder")
    core = DebleederCore(config)
    report = core.reconcile_seal(run_id, project_root / "exports")
    for key, value in report.items():
        typer.echo(f"{key}: {value}")
    core.close()
    if report.get("trustworthy") is False:
        raise typer.Exit(2)


if __name__ == "__main__":
    app()