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
from app.parser import parse_and_derive
from app.preprocess import detect_kra_qr, limit_side, load, prepare
from app.stitch import compose, merge_sections, stitch


def _prep_frames(sources: Iterable, max_side: int,
                 quads: list | None = None) -> tuple[list[np.ndarray], list[dict], str | None]:
    """Load + pre-process frames one at a time; the full-res original is freed
    before the next one is loaded."""
    grays, infos, qr = [], [], None
    sources = list(sources)
    # Single shots get a perspective crop to the whole receipt. Sections of a long
    # receipt are partial views: each is cut to its paper strip, turned upright and
    # scaled to one width, so the stitcher only has to find a vertical slide.
    crop = len(sources) == 1
    quads = quads or [None] * len(sources)
    for src, quad in zip(sources, quads):
        img = load(src) if isinstance(src, (str, Path)) else src
        if qr is None:
            qr = detect_kra_qr(limit_side(img, 1600))
        g, info = prepare(img, max_side=max_side, crop=crop, quad=quad if crop else None, section=not crop)
        del img
        grays.append(g)
        infos.append(info)
        gc.collect()
    return grays, infos, qr


def _ocr_lines(eng: OcrEngine, img: np.ndarray) -> list[dict]:
    return [l.to_dict() for l in rebuild_lines(eng.read(img))]


CRITICAL = ("seller_pin", "total", "receipt_date")
IDENTIFIERS = ("seller_name", "seller_pin", "buyer_pin", "receipt_no", "cu_invoice_no", "cu_serial_no",
               "receipt_signature", "receipt_date", "receipt_time", "mpesa_code", "payment_method")


def quality(parsed: dict, checks: list[dict]) -> tuple[int, int, int]:
    """Higher is better: (no failed arithmetic, critical fields present, checks passed)."""
    failed = sum(1 for c in checks if c["ok"] is False and c["severity"] == "error"
                 and not c["id"].endswith("_present") and c["id"] != "vat_shown")
    present = sum(1 for k in CRITICAL if parsed.get(k) is not None)
    passed = sum(1 for c in checks if c["ok"] is True)
    return (-failed, present, passed)


def needs_retry(parsed: dict, checks: list[dict]) -> bool:
    q = quality(parsed, checks)
    return q[0] < 0 or q[1] < len(CRITICAL)


def _retry_factory():
    """Medium recogniser if it was fetched; None when unavailable or disabled."""
    from app.ocr import model_paths
    rec = config.settings.ocr_retry_rec
    if not rec or rec == config.settings.ocr_rec:
        return None
    try:
        model_paths(rec=rec)
    except (FileNotFoundError, ValueError):
        return None
    return lambda: OcrEngine(rec=rec)


def _read(engine_factory, stitched, grays, mode: str = "auto") -> tuple[list[dict], dict | None]:
    """OCR the stitched image, or each section and join them where the same
    printed lines appear in both (strict line de-dup only for a join with no
    common text). Returns (lines, join info)."""
    with engine_factory() as eng:
        if stitched is not None:
            return _ocr_lines(eng, stitched), None
        per = [_ocr_lines(eng, g) for g in grays]
    lines, info = merge_sections(per, [g.shape[0] for g in grays])
    return lines, info


def process_frames_arrays(sources: list, mode: str = "auto", engine_factory: Callable = OcrEngine,
                          progress: Callable[[str], None] | None = None,
                          retry_factory: Callable | None = None, quads: list | None = None) -> dict:
    """mode: 'auto' (long receipts: read each section, join them where the same
    printed lines appear in both), 'stitch' (image stitch first), or 'dedupe'
    (same as auto; kept for receipts saved with that setting).
    Hard receipts get a second OCR pass with a more accurate recogniser
    (retry_factory; by default the medium model when the default engine is used)."""
    say = progress or (lambda m: None)
    max_side = config.settings.ocr_max_side
    say("preprocessing")
    grays, infos, qr = _prep_frames(sources, max_side, quads)
    stitched, sinfo = (None, {"method": "skipped"})
    if len(grays) == 1:
        stitched, sinfo = stitch(grays)
    elif mode == "stitch":
        say("stitching")
        stitched, sinfo = stitch(grays)
    say("ocr" if stitched is not None else "ocr per section")
    lines, join = _read(engine_factory, stitched, grays, mode)
    if join is not None:
        sinfo = dict(sinfo, **join) if sinfo.get("method") == "failed" else dict(join)
        if join.get("joins") is not None:
            stitched = compose(grays, join["joins"])
    texts = [l["text"] for l in lines]
    say("parsing")
    parsed = parse_and_derive(texts, kra_qr_url=qr)
    checks = run_checks(parsed)
    passes = [{"engine": "default", "quality": list(quality(parsed, checks))}]

    if retry_factory is None and engine_factory is OcrEngine:
        retry_factory = _retry_factory()
    if retry_factory is not None and needs_retry(parsed, checks):
        say("re-reading faded print")
        gc.collect()
        lines2, _ = _read(retry_factory, stitched if len(grays) == 1 else None, grays, mode)
        parsed2 = parse_and_derive([l["text"] for l in lines2], kra_qr_url=qr)
        checks2 = run_checks(parsed2)
        passes.append({"engine": "retry", "quality": list(quality(parsed2, checks2))})
        if quality(parsed2, checks2) > quality(parsed, checks):
            parsed, lines, other = parsed2, lines2, parsed
            passes[-1]["chosen"] = True
        else:
            other = parsed2
        # identifiers don't affect the arithmetic: take any the chosen pass missed
        for k in IDENTIFIERS:
            if parsed.get(k) is None and other.get(k) is not None:
                parsed[k] = other[k]
                parsed.setdefault("notes", []).append(f"{k} taken from the second OCR pass")
        checks = run_checks(parsed)
        texts = [l["text"] for l in lines]

    out = {
        "text": "\n".join(texts),
        "lines": lines,
        "parsed": parsed,
        "checks": checks,
        "stitch": dict(sinfo, ocr_passes=passes),
        "prep": infos,
        "kra_qr": qr,
        "stitched": stitched if stitched is not None and len(grays) > 1 else None,
        # clean white-background scan for display (stitched tall image, or the single shot)
        "scan": stitched if stitched is not None else (grays[0] if len(grays) == 1 else None),
    }
    del grays
    gc.collect()
    return out


__all__ = ["process_frames_arrays", "lines_to_text"]
