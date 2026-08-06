#!/usr/bin/env python3
"""Build Termux-compatible package — no APK, just installable wheel + entry points."""

from __future__ import annotations
import subprocess
import sys
from pathlib import Path


def main():
    root = Path(__file__).parent.parent

    # Build wheel
    subprocess.run([sys.executable, "-m", "build", "--wheel"], cwd=root, check=True)

    # Verify wheel installs in clean Termux-like environment
    # (This would run in CI on aarch64)
    print("Wheel built. Test on Termux with:")
    print("  pip install dist/debleeder-*.whl")
    print("  debleeder init ~/debleeder_project")
    print("  debleeder run --gui --import ~/debleeder_project/imports")


if __name__ == "__main__":
    main()