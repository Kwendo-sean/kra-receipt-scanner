"""End-to-end OCR accuracy check: synthetic receipts (rendered + 'photographed')
and any real photos in tests/fixtures/real/<name>.jpg with <name>.json expected fields.

    python scripts/eval_ocr.py [--show]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import synth  # noqa: E402
from app.pipeline import process_frames_arrays  # noqa: E402
from app.preprocess import load  # noqa: E402


def compare(parsed: dict, exp: dict) -> list[str]:
    bad = []
    for k, v in exp.items():
        if k == "n_items":
            if len(parsed["items"]) != v:
                bad.append(f"n_items {len(parsed['items'])} != {v}")
        elif k == "bands":
            got = {(b["band"], b["rate"]): (b["taxable"], b["vat"]) for b in parsed["bands"]}
            want = {tuple(json.loads(kk)) if isinstance(kk, str) else kk: tuple(vv) for kk, vv in v.items()}
            if got != want:
                bad.append(f"bands {got} != {want}")
        elif parsed.get(k) != v:
            bad.append(f"{k}: {parsed.get(k)!r} != {v!r}")
    return bad


def main() -> int:
    show = "--show" in sys.argv
    cases = []
    for fn in synth.ALL:
        s = fn()
        img = synth.photograph(synth.render(s), angle=2.0)
        frames = synth.split_sections(img, 4) if s.name == "long_multi" else [img]
        cases.append((s.name, frames, s.expect))
    real = ROOT / "tests" / "fixtures" / "real"
    for jp in sorted(real.glob("*.json")) if real.is_dir() else []:
        exp = json.loads(jp.read_text(encoding="utf-8"))
        imgs = sorted(real.glob(jp.stem + "*.jpg"))
        cases.append((jp.stem, [load(p) for p in imgs], exp))

    fails = 0
    for name, frames, exp in cases:
        t = time.time()
        res = process_frames_arrays(frames)
        bad = compare(res["parsed"], exp)
        fails += bool(bad)
        print(f"{'FAIL' if bad else 'ok  '} {name:24s} {time.time() - t:5.1f}s  stitch={res['stitch']['method']}")
        for b in bad:
            print("      ", b)
        if show and bad:
            print("      " + "\n       ".join(res["text"].splitlines()))
    print(f"{len(cases) - fails}/{len(cases)} receipts exact")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
