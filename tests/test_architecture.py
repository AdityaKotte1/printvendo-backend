import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _find_lint_imports() -> str:
    """Locate the `lint-imports` console script.

    import-linter has no `python -m importlinter` entry point, so the script has
    to be found on disk. Looking next to sys.executable first matters: pytest
    runs under the venv's interpreter but not necessarily with the venv's
    Scripts directory on PATH, so shutil.which alone misses it.
    """
    scripts_dir = Path(sys.executable).parent
    for name in ("lint-imports.exe", "lint-imports"):
        candidate = scripts_dir / name
        if candidate.exists():
            return str(candidate)

    found = shutil.which("lint-imports")
    if found is None:
        pytest.fail('lint-imports not found. Run `pip install -e ".[dev]"` first.')
    return found


def _lint_imports(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_find_lint_imports(), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def test_import_contracts_hold():
    result = _lint_imports()
    assert result.returncode == 0, result.stdout + result.stderr


def test_all_three_contracts_are_actually_evaluated():
    """Guard against a config that silently checks nothing.

    An .importlinter with an unreadable root_package, or with every contract
    typo'd out of existence, still exits 0 -- a green build that proves nothing.
    Asserting the reported count keeps that from passing unnoticed.
    """
    result = _lint_imports()
    assert "Contracts: 3 kept, 0 broken." in result.stdout, result.stdout
