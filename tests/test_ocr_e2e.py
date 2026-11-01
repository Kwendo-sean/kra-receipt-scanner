"""Real RapidOCR on rendered + 'photographed' synthetic receipts (skipped if
RapidOCR isn't installed). Real-photo fixtures are checked by scripts/eval_ocr.py."""
import pytest

import synth

pytest.importorskip("rapidocr")
pytest.importorskip("onnxruntime")

from app.pipeline import process_frames_arrays  # noqa: E402


def _check(parsed, exp):
    for k, v in exp.items():
        if k == "n_items":
            assert len(parsed["items"]) == v
        elif k == "bands":
            assert {(b["band"], b["rate"]): (b["taxable"], b["vat"]) for b in parsed["bands"]} == v
        else:
            assert parsed[k] == v, k


def test_ocr_ocr_model_paths_are_bundled_and_explicit():
    from app.ocr import model_paths
    p = model_paths()
    assert p["det"].endswith("PP-OCRv6_det_small.onnx") and p["rec"].endswith("PP-OCRv6_rec_small.onnx")


@pytest.mark.parametrize("fn", [synth.supermarket_16_exempt, synth.etims_gross_style], ids=lambda f: f.__name__)
def test_short_receipt_photo(fn):
    s = fn()
    res = process_frames_arrays([synth.photograph(synth.render(s), angle=2.5)])
    _check(res["parsed"], s.expect)


@pytest.mark.parametrize("mode", ["auto", "dedupe"])
def test_long_multi_section_photo(mode):
    s = synth.long_multi()
    frames = synth.split_sections(synth.photograph(synth.render(s), angle=1.5), 4)
    res = process_frames_arrays(frames, mode=mode)
    _check(res["parsed"], s.expect)
