"""Configuration — deterministic, profile-aware, no secrets."""

from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import json
import os

from .protocols import MethodVersion, Profile


@dataclass(frozen=True, slots=True)
class PhoneConfig:
    """Phone profile bounds — aggressive checkpointing, bounded memory."""
    max_corpus_docs: int = 500
    max_blocks_per_run: int = 50_000
    candidate_queue_limit: int = 10_000
    checkpoint_interval_decisions: int = 5
    stream_chunk_size: int = 1000
    equation_extraction: str = "stdlib_only"  # OMML + text-PDF
    gui_server: str = "http.server"  # stdlib only


@dataclass(frozen=True, slots=True)
class DesktopConfig:
    """Desktop profile — richer capabilities, same core contracts."""
    max_corpus_docs: int = 100_000
    max_blocks_per_run: int = 1_000_000
    candidate_queue_limit: int = 100_000
    checkpoint_interval_decisions: int = 50
    stream_chunk_size: int = 5000
    equation_extraction: str = "full"  # + MathML, LaTeX, vector glyphs
    gui_server: str = "fastapi"
    semantic_channel_enabled: bool = False  # opt-in only


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Calibrated thresholds per channel — versioned."""
    phrase: dict[str, float] = field(default_factory=lambda: {
        "low": 0.15, "medium": 0.35, "high": 0.65
    })
    numeric: dict[str, float] = field(default_factory=lambda: {
        "low": 0.5, "medium": 0.8, "high": 0.95
    })
    symbol: dict[str, float] = field(default_factory=lambda: {
        "low": 0.3, "medium": 0.6, "high": 0.85
    })
    citation: dict[str, float] = field(default_factory=lambda: {
        "low": 0.4, "medium": 0.7, "high": 0.9
    })
    equation: dict[str, float] = field(default_factory=lambda: {
        "low": 0.4, "medium": 0.7, "high": 0.9
    })
    # lineage and seed are modifiers, not direct strength


@dataclass(frozen=True, slots=True)
class Config:
    """Root configuration — loaded from YAML, overridden by env."""
    profile: Profile = Profile.PHONE
    data_dir: Path = Path.home() / ".local" / "share" / "debleeder"
    project_root: Path = Path.cwd()
    method_version: MethodVersion = field(default_factory=MethodVersion)
    thresholds: Thresholds = field(default_factory=Thresholds)
    phone: PhoneConfig = field(default_factory=PhoneConfig)
    desktop: DesktopConfig = field(default_factory=DesktopConfig)

    # GUI
    gui_host: str = "127.0.0.1"
    gui_port: int = 8080

    # Safety
    require_acknowledgment_for_shred: bool = True
    refuse_purge_with_unresolved_deps: bool = True

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        """Load from YAML with env overrides."""
        data: dict[str, Any] = {}
        if path and path.exists():
            import yaml
            data = yaml.safe_load(path.read_text()) or {}

        # Env overrides (DEBLEEDER_*)
        for key, value in os.environ.items():
            if key.startswith("DEBLEEDER_"):
                cfg_key = key[10:].lower()
                # Simple nested set
                parts = cfg_key.split("__")
                target = data
                for p in parts[:-1]:
                    target = target.setdefault(p, {})
                target[parts[-1]] = json.loads(value) if value.startswith(("{", "[")) else value

        return cls(**data)  # type: ignore[arg-type]

    def for_profile(self) -> PhoneConfig | DesktopConfig:
        return self.phone if self.profile == Profile.PHONE else self.desktop