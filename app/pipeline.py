"""Frames -> pre-processed -> stitched (or per-frame + de-dup) -> OCR -> lines -> parsed + checks."""
from __future__ import annotations

import gc
from pathlib import Path
from typing import Callable, Iterable

import numpy as np

from app import config
from app.checks import run_checks
from app.lines import lines_to_text, rebuild_lines
from app.ocr import OcrEngine
from app.parser import parse_lines
from app.preprocess import detect_kra_qr, limit_side, load, prepare
from app.stitch import merge_frame_lines, stitch


def _prep_frames(sources: Iterable, max_side: int) -> tuple[list[np.ndarray], list[dict], str | None]:
    """Load + pre-process frames one at a time; the full-res original is freed
    before the next one is loaded."""
    grays, infos, qr = [], [], None
    sources = list(sources)
    # Sections of a long receipt are partial views: a per-frame paper crop would
    # cut them inconsistently, so only single shots are perspective-cropped.
    crop = len(sources) == 1
    for src in sources:
        img = load(src) if isinstance(src, (str, Path)) else src
        if qr is None:
            qr = detect_kra_qr(limit_side(img, 1600))
        g, info = prepare(img, max_side=max_side, crop=crop)
        del img
        grays.append(g)
        infos.append(info)
        gc.collect()
    return grays, infos, qr


def _ocr_lines(eng: OcrEngine, img: np.ndarray) -> list[dict]:
    return [l.to_dict() for l in rebuild_lines(eng.read(img))]


def process_frames_arrays(sources: list, mode: str = "auto", engine_factory: Callable = OcrEngine,
                          progress: Callable[[str], None] | None = None) -> dict:
    """mode: 'auto' (stitch, fall back to de-dup), 'stitch', or 'dedupe'."""
    say = progress or (lambda m: None)
    max_side = config.settings.ocr_max_side
    say("preprocessing")
    grays, infos, qr = _prep_frames(sources, max_side)
    stitched, sinfo = (None, {"method": "skipped"})
    if mode in ("auto", "stitch"):
        say("stitching" if len(grays) > 1 else "ocr")
        stitched, sinfo = stitch(grays)
    with engine_factory() as eng:
        if stitched is not None:
            say("ocr")
            lines = _ocr_lines(eng, stitched)
            dropped = None
        else:
            say("ocr per frame")
            per = []
            for g in grays:
                per.append(_ocr_lines(eng, g))
            lines, dropped = merge_frame_lines(per, [g.shape[0] for g in grays])
            sinfo = dict(sinfo, fallback="dedupe", dropped_lines=dropped)
    texts = [l["text"] for l in lines]
    say("parsing")
    parsed = parse_lines(texts, kra_qr_url=qr).to_dict()
    checks = run_checks(parsed)
    out = {
        "text": "\n".join(texts),
        "lines": lines,
        "parsed": parsed,
        "checks": checks,
        "stitch": sinfo,
        "prep": infos,
        "kra_qr": qr,
        "stitched": stitched if stitched is not None and len(grays) > 1 else None,
    }
    del grays
    gc.collect()
    return out


__all__ = ["process_frames_arrays", "lines_to_text"]
