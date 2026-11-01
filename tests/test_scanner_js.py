"""Runs the browser-side scanner tests (overlap tracker, document box) under Node; skipped without Node."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("script", sorted((Path(__file__).parent / "js").glob("*.test.js")), ids=lambda p: p.name)
def test_browser_scanner_logic(script):
    r = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
