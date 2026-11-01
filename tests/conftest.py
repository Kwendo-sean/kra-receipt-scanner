import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

# Never pick up a real .env during tests
os.environ["RS_ENV_FILE"] = str(ROOT / "tests" / "nonexistent.env")


@pytest.fixture()
def tmp_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("RS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RS_SECRET_KEY", "test-secret")
    monkeypatch.setenv("RS_PUBLIC_HOSTS", "10.42.0.1,testserver")
    from app import config
    s = config.reload_settings()
    s.ensure_dirs()
    from app import db
    db.init_db()
    yield s
