"""One-time download of the larger PP-OCRv6 'medium' models into models/
(SHA256-verified). At runtime the app only ever loads explicit local paths.

    python scripts/fetch_models.py            # det + rec medium
    python scripts/fetch_models.py rec        # only the recogniser
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "models"
BASE = "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx/PP-OCRv6"
MODELS = {
    "det": ("PP-OCRv6_det_medium.onnx", f"{BASE}/det/PP-OCRv6_det_medium.onnx",
            "92078b7355007ccfffcd4c8cd441a3afd4538904d06881b29a155e1e679907c2"),
    "rec": ("PP-OCRv6_rec_medium.onnx", f"{BASE}/rec/PP-OCRv6_rec_medium.onnx",
            "eef444829dbbe18d7fea59a3f6eb75647518d2b3a9568d27c92e42940204894b"),
}


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(kind: str) -> Path:
    name, url, digest = MODELS[kind]
    DEST.mkdir(exist_ok=True)
    out = DEST / name
    if out.is_file() and sha256(out) == digest:
        print(f"{name}: already present")
        return out
    tmp = out.with_suffix(".part")
    print(f"{name}: downloading…")
    with httpx.stream("GET", url, follow_redirects=True, timeout=120) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    got = sha256(tmp)
    if got != digest:
        tmp.unlink()
        raise SystemExit(f"{name}: SHA256 mismatch ({got}); refusing to use it")
    tmp.replace(out)
    print(f"{name}: OK ({out.stat().st_size / 1e6:.1f} MB)")
    return out


if __name__ == "__main__":
    kinds = [k for k in sys.argv[1:] if k in MODELS] or list(MODELS)
    for k in kinds:
        fetch(k)
