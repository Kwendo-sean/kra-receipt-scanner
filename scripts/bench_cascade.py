"""Faded receipts: single fast pass vs automatic second pass for hard receipts.

    python scripts/bench_cascade.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(ROOT / "scripts")]

import synth  # noqa: E402
from app import config  # noqa: E402
from app.pipeline import process_frames_arrays  # noqa: E402
from bench_models import n_fields  # noqa: E402
from eval_ocr import compare  # noqa: E402

LEVELS = (0.0, 0.55, 0.75, 0.85)


def main() -> None:
    data = []
    for lv in LEVELS:
        for fn in synth.ALL:
            s = fn()
            img = synth.photograph(synth.fade(synth.render(s), lv) if lv else synth.render(s), angle=2.0)
            data.append((lv, synth.split_sections(img, 4) if s.name == "long_multi" else [img], s.expect))
    for label, retry in (("fast pass only", ""), ("auto second pass", "medium")):
        config.settings.ocr_retry_rec = retry
        res = {lv: [0, 0, 0, 0] for lv in LEVELS}
        t = time.time()
        retried = 0
        for lv, frames, exp in data:
            r = process_frames_arrays(frames)
            retried += len(r["stitch"]["ocr_passes"]) > 1
            bad = compare(r["parsed"], exp)
            st = res[lv]
            st[0] += not bad; st[1] += 1; st[2] += n_fields(exp) - len(bad); st[3] += n_fields(exp)
        print(f"{label:18s} {time.time() - t:6.1f}s retried {retried:2d}/{len(data)} | " + "  ".join(
            f"fade {lv}: {s[0]}/{s[1]} {100 * s[2] / s[3]:.0f}%" for lv, s in res.items()), flush=True)


if __name__ == "__main__":
    main()
