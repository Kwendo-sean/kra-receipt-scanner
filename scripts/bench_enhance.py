"""Try image-enhancement variants for faded thermal print (small models).

    python scripts/bench_enhance.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

import synth  # noqa: E402
from app import preprocess  # noqa: E402
from app.pipeline import process_frames_arrays  # noqa: E402
from bench_models import n_fields  # noqa: E402
from eval_ocr import compare  # noqa: E402

ORIG = preprocess.even_lighting


def _norm(gray):
    k = max(31, (min(gray.shape[:2]) // 20) | 1)
    bg = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    return cv2.divide(gray, bg, scale=255)


def stretch(gray, gamma=2.0):
    """Background-divide, then stretch the ink range so faint print becomes dark."""
    n = _norm(gray).astype(np.float32)
    lo = np.percentile(n, 0.5)
    x = np.clip((n - lo) / max(1.0, 250 - lo), 0, 1)
    return (255 * x ** gamma).astype(np.uint8)


def stretch_clahe(gray):
    return cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(stretch(gray, 1.6))


def adaptive(gray):
    """Local ink darkening: subtract a blurred background, keep grey levels."""
    n = _norm(gray).astype(np.float32)
    dark = 255 - n
    gain = 255.0 / max(8.0, np.percentile(dark, 99.5))
    return np.clip(255 - dark * gain, 0, 255).astype(np.uint8)


VARIANTS = {"current (divide+CLAHE)": ORIG, "stretch g2.0": stretch,
            "stretch g1.6+CLAHE": stretch_clahe, "ink gain": adaptive}


def main() -> None:
    data = []
    for level in (0.55, 0.75, 0.85):
        for fn in synth.ALL:
            s = fn()
            img = synth.photograph(synth.fade(synth.render(s), level), angle=2.0)
            frames = synth.split_sections(img, 4) if s.name == "long_multi" else [img]
            data.append((level, frames, s.expect))
    for name, f in VARIANTS.items():
        preprocess.even_lighting = f
        res = {lv: [0, 0, 0, 0] for lv in (0.55, 0.75, 0.85)}
        t = time.time()
        for level, frames, exp in data:
            bad = compare(process_frames_arrays(frames)["parsed"], exp)
            st = res[level]
            st[0] += not bad; st[1] += 1; st[2] += n_fields(exp) - len(bad); st[3] += n_fields(exp)
        print(f"{name:24s} {time.time() - t:5.1f}s | " + "  ".join(
            f"fade {lv}: {s[0]}/{s[1]} exact {100 * s[2] / s[3]:.0f}%" for lv, s in res.items()), flush=True)
    preprocess.even_lighting = ORIG


if __name__ == "__main__":
    main()
