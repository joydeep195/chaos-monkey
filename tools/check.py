"""Run every check in one go: lint, formatting, types, tests and dependency audit.

Usage: uv run python tools/check.py

Each step prints its own output. The script exits with 1 if any step fails.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def uv_executable() -> str:
    # uv sets UV to its own path when it runs a command.
    found = os.environ.get("UV") or shutil.which("uv")
    if not found:
        raise SystemExit("Cannot find uv. Run this script with: uv run python tools/check.py")
    return found


def run(title: str, command: list[str]) -> bool:
    print(f"\n=== {title} ===", flush=True)
    result = subprocess.run(command, cwd=REPO_ROOT, check=False)
    print(f"--- {title}: {'passed' if result.returncode == 0 else 'FAILED'}", flush=True)
    return result.returncode == 0


def audit_dependencies() -> bool:
    """Check every locked package, including dev tools, against known vulnerabilities."""
    with tempfile.TemporaryDirectory() as tmp:
        requirements = Path(tmp) / "requirements.txt"
        exported = subprocess.run(
            [
                uv_executable(),
                "export",
                "--format",
                "requirements-txt",
                "--all-groups",
                "--all-extras",
                "--no-emit-project",
                "--quiet",
                "--output-file",
                str(requirements),
            ],
            cwd=REPO_ROOT,
            check=False,
        )
        if exported.returncode != 0:
            print("--- Dependency audit: FAILED (could not export uv.lock)")
            return False
        return run(
            "Dependency audit (pip-audit)",
            [
                PYTHON,
                "-m",
                "pip_audit",
                "--requirement",
                str(requirements),
                "--require-hashes",
                "--disable-pip",
                "--progress-spinner",
                "off",
            ],
        )


def main() -> int:
    results = {
        "Lint and security rules (ruff)": run(
            "Lint and security rules (ruff)", [PYTHON, "-m", "ruff", "check", "."]
        ),
        "Formatting (ruff)": run(
            "Formatting (ruff)", [PYTHON, "-m", "ruff", "format", "--check", "."]
        ),
        "Types (mypy)": run("Types (mypy)", [PYTHON, "-m", "mypy"]),
        "Tests (pytest)": run("Tests (pytest)", [PYTHON, "-m", "pytest"]),
        "Dependency audit (pip-audit)": audit_dependencies(),
    }
    print("\n=== Summary ===")
    for title, passed in results.items():
        print(f"{'PASS' if passed else 'FAIL'}  {title}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
