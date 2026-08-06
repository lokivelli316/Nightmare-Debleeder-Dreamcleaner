#!/usr/bin/env python3
"""Build verification — run before any release."""

from __future__ import annotations
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str], cwd: Path | None = None) -> bool:
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return result.returncode == 0


def main():
    root = Path(__file__).parent.parent
    ok = True

    # 1. Compile everything
    ok &= run([sys.executable, "-m", "compileall", "-q", "src", "tests"], root)

    # 2. Doctrine invariants (MUST PASS — these are the safety gates)
    ok &= run([sys.executable, "-m", "pytest",
               "tests/test_doctrine_invariants.py", "-q"], root)

    # 3. Safety doctrine tests (MUST PASS)
    ok &= run([sys.executable, "-m", "pytest", "tests/test_safety_doctrine.py", "-q"], root)

    # 4. Identity/lineage tests
    ok &= run([sys.executable, "-m", "pytest", "tests/test_identity_lineage.py", "-q"], root)

    # 5. Whole suite
    ok &= run([sys.executable, "-m", "pytest", "-q"], root)

    # 6. CLI smoke test
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        ok &= run([sys.executable, "-m", "debleeder", "init", tmp], root)

    # Lint and type check are OPTIONAL here: they are not installed in every
    # environment and their absence must not be reported as a doctrine failure.
    run([sys.executable, "-m", "ruff", "check", "src", "tests"], root)

    if ok:
        print("\nBUILD VERIFICATION PASSED")
        return 0
    else:
        print("\nBUILD VERIFICATION FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(main())