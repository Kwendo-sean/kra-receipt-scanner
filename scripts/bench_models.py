"""Compare PP-OCRv6 small vs medium (detector / recogniser) on fresh and faded
synthetic receipts, plus any real photos in tests/fixtures/real/.

    python scripts/fetch_models.py      # once
    python scripts/bench_models.py [--quick]
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
from app.ocr import OcrEngine  # noqa: E402
from app.pipeline import process_frames_arrays  # noqa: E402
from eval_ocr import compare  # noqa: E402

CONFIGS = [("small", "small"), ("small", "medium"), ("medium", "small"), ("medium", "medium")]
FADES = [0.0, 0.55, 0.75, 0.85]


def n_fields(exp: dict) -> int:
    return sum(len(v) if k == "bands" else 1 for k, v in exp.items())


def cases(quick: bool):
    fns = synth.ALL[:4] if quick else synth.ALL
    for level in FADES:
        for fn in fns:
            s = fn()
            img = synth.photograph(synth.fade(synth.render(s), level) if level else synth.render(s), angle=2.0)
            frames = synth.split_sections(img, 4) if s.name == "long_multi" else [img]
            yield f"{s.name}@{level:.2f}", level, frames, s.expect


def main() -> None:
    quick = "--quick" in sys.argv
    data = list(cases(quick))
    print(f"{len(data)} receipts x {len(CONFIGS)} configs")
    for det, rec in CONFIGS:
        factory = lambda d=det, r=rec: OcrEngine(det=d, rec=r)  # noqa: E731
        per_level: dict[float, list] = {lv: [0, 0, 0, 0] for lv in FADES}  # exact, receipts, fields_ok, fields
        t0 = time.time()
        for name, level, frames, exp in data:
            res = process_frames_arrays(frames, engine_factory=factory)
            bad = compare(res["parsed"], exp)
            tot = n_fields(exp)
            st = per_level[level]
            st[0] += not bad
            st[1] += 1
            st[2] += tot - len(bad)
            st[3] += tot
        dt = time.time() - t0
        cols = "  ".join(f"fade {lv:.2f}: {st[0]}/{st[1]} exact, {100 * st[2] / st[3]:.0f}% fields"
                         for lv, st in per_level.items())
        print(f"det={det:6s} rec={rec:6s} {dt:6.1f}s | {cols}", flush=True)


if __name__ == "__main__":
    main()
