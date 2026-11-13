"""Pytest wrapper for Phase 2c frontend contract & storage unit tests."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_phase2c_frontend_suite() -> None:
    node_bin = shutil.which("node")
    if node_bin is None:
        pytest.skip("node is not installed in this environment")
    repo_root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [node_bin, "--test", "tests/test_phase2c_frontend.mjs"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"node --test failed:\n{result.stdout}\n{result.stderr}"
