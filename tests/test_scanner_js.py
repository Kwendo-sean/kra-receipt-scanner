"""Runs the browser-side long-receipt overlap tracker test under Node (skipped if Node is absent)."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_live_overlap_tracker():
    script = Path(__file__).parent / "js" / "align.test.js"
    r = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
