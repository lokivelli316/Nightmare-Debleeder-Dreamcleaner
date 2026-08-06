"""
Nightmare Debleeder Dreamcleaner — Core Package

Forensic document bleed detection with human-in-the-loop safety.
Architecture: SQLite operational + JSONL audit ledger, dual-write persistence,
deterministic rebuild from sealed bundles.
"""

__version__ = "0.1.0"
__all__ = [
    "core",
    "storage",
    "import_",
    "evidence",
    "triage",
    "decisions",
    "seal",
    "rebuild",
    "gui",
    "cli",
]