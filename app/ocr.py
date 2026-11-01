"""RapidOCR (ONNX Runtime, CPU) wrapper with explicit, bundled model paths.

The engine is created per job and freed afterwards, so its memory goes back to
llama-server. Long images are processed in overlapping horizontal tiles so peak
memory is bounded regardless of receipt length.
"""
from __future__ import annotations

import gc
import os
from pathlib import Path

import numpy as np

from app import config
from app.lines import Fragment

MODEL_FILES = {
    "det": "PP-OCRv6_det_{size}.onnx",
    "rec": "PP-OCRv6_rec_{size}.onnx",
    "cls": "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
}
SIZES = ("tiny", "small", "medium")


def model_dir() -> Path:
    if config.settings.ocr_model_dir:
        return Path(config.settings.ocr_model_dir)
    import rapidocr  # noqa: WPS433 (import only to locate the wheel)
    return Path(rapidocr.__file__).parent / "models"


def model_paths(det: str | None = None, rec: str | None = None) -> dict[str, str]:
    """Explicit local paths only - RapidOCR is never allowed to download."""
    s = config.settings
    det, rec = det or s.ocr_det, rec or s.ocr_rec
    for v in (det, rec):
        if v not in SIZES:
            raise ValueError(f"OCR model size must be one of {SIZES}, got {v!r}")
    bundled, extra = model_dir(), Path(s.ocr_extra_dir)

    def find(name: str) -> str:
        for d in (bundled, extra):
            if (d / name).is_file():
                return str(d / name)
        return str(bundled / name)

    paths = {"det": find(MODEL_FILES["det"].format(size=det)),
             "rec": find(MODEL_FILES["rec"].format(size=rec)),
             "cls": find(MODEL_FILES["cls"])}
    missing = [p for p in paths.values() if not os.path.isfile(p)]
    if missing:
        raise FileNotFoundError(f"OCR model files missing (will not download): {missing}. "
                                "For medium models run: python scripts/fetch_models.py")
    return paths


class OcrEngine:
    """Context manager: `with OcrEngine() as eng: frags = eng.read(img)`."""

    def __init__(self, threads: int | None = None, det: str | None = None, rec: str | None = None):
        self.threads = threads or config.settings.ocr_threads
        self.det, self.rec = det, rec
        self._eng = None

    def __enter__(self) -> "OcrEngine":
        from rapidocr import RapidOCR

        p = model_paths(self.det, self.rec)
        params = {
            "Global.log_level": "error",
            "Global.max_side_len": 4000,
            "Det.model_path": p["det"],
            "Rec.model_path": p["rec"],
            "Cls.model_path": p["cls"],
            "EngineConfig.onnxruntime.intra_op_num_threads": self.threads,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        }
        self._eng = RapidOCR(params=params)
        return self

    def __exit__(self, *exc) -> None:
        self._eng = None
        gc.collect()

    def _run(self, img: np.ndarray) -> list[Fragment]:
        res = self._eng(img)
        boxes = getattr(res, "boxes", None)
        txts = getattr(res, "txts", None)
        scores = getattr(res, "scores", None)
        if boxes is None or txts is None:
            return []
        scores = scores if scores is not None else [1.0] * len(txts)
        return [Fragment.from_box(b, t, s) for b, t, s in zip(boxes, txts, scores)]

    def read(self, img: np.ndarray, tile_h: int = 1400, overlap: int = 120) -> list[Fragment]:
        """OCR an image; tall images are tiled with overlap, and fragments from
        the overlap are kept only once (the tile whose centre region they fall in)."""
        h = img.shape[0]
        if h <= tile_h + overlap:
            return self._run(img)
        out: list[Fragment] = []
        y = 0
        while y < h:
            y1 = min(h, y + tile_h + overlap)
            frags = self._run(np.ascontiguousarray(img[y:y1]))
            keep_lo = 0 if y == 0 else overlap / 2
            keep_hi = (y1 - y) if y1 >= h else tile_h + overlap / 2
            for f in frags:
                if keep_lo <= f.yc < keep_hi:
                    f.y0 += y
                    f.y1 += y
                    out.append(f)
            if y1 >= h:
                break
            y += tile_h
        return out


def peak_rss_mb() -> float | None:
    try:
        import resource
        r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return r / 1024.0  # Linux: KiB
    except ImportError:
        return None
