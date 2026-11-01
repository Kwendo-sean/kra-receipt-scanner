"""Compare EasyOCR (the recogniser used by Easy-Yolo-OCR) with the current
RapidOCR engine on the synthetic receipts, through the same pipeline.

Run in an environment that has easyocr + torch installed:
    python scripts/bench_easyocr.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

import synth  # noqa: E402
from app.lines import Fragment  # noqa: E402
from app.pipeline import process_frames_arrays  # noqa: E402
from eval_ocr import compare  # noqa: E402


def rss_mb() -> float:
    try:
        import psutil
        return psutil.Process(os.getpid()).memory_info().rss / 1048576
    except ImportError:
        return float("nan")


class EasyEngine:
    reader = None

    def __enter__(self):
        import easyocr
        import torch
        torch.set_num_threads(2)  # same budget as RapidOCR on the Pi
        if EasyEngine.reader is None:
            EasyEngine.reader = easyocr.Reader(["en"], gpu=False, verbose=False)
        return self

    def __exit__(self, *a):
        pass

    def read(self, img):
        out = []
        for box, text, conf in EasyEngine.reader.readtext(img, paragraph=False):
            out.append(Fragment.from_box(box, text, conf))
        return out


def main() -> None:
    base = rss_mb()
    total = 0.0
    ok = 0
    for fn in synth.ALL:
        s = fn()
        img = synth.photograph(synth.render(s), angle=2.0)
        frames = synth.split_sections(img, 4) if s.name == "long_multi" else [img]
        t = time.time()
        res = process_frames_arrays(frames, engine_factory=EasyEngine)
        dt = time.time() - t
        total += dt
        bad = compare(res["parsed"], s.expect)
        ok += not bad
        print(f"{'ok  ' if not bad else 'FAIL'} {s.name:24s} {dt:5.1f}s")
        for b in bad[:6]:
            print("      ", b)
    print(f"EasyOCR: {ok}/{len(synth.ALL)} exact · {total:.1f}s total · RSS {rss_mb():.0f} MB (start {base:.0f} MB)")


if __name__ == "__main__":
    main()
