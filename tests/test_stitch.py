import cv2
import numpy as np

import synth
from app.lines import Fragment, rebuild_lines
from app.stitch import merge_frame_lines, offset_orb, offset_template, stitch


def _gray(img):
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def test_rebuild_lines_joins_label_and_amounts():
    frags = [Fragment("B-16%", 10, 100, 80, 130), Fragment("16%", 120, 102, 160, 131),
             Fragment("5,000.00", 300, 98, 400, 128), Fragment("800.00", 450, 103, 520, 133),
             Fragment("TOTAL", 10, 150, 80, 180), Fragment("5,800.00", 300, 151, 400, 181)]
    lines = [ln.text for ln in rebuild_lines(frags)]
    assert lines == ["B-16%  16%  5,000.00  800.00", "TOTAL  5,800.00"]


def test_rebuild_lines_tolerates_skew_and_slight_box_overlap():
    # right-hand amount sits 12px lower (skew) and its box overlaps by 10px
    frags = [Fragment("WHOLE MILK 2 x 65.00", 30, 354, 455, 385), Fragment("130.00 A", 443, 366, 572, 395),
             Fragment("BREAD", 30, 405, 120, 435)]
    lines = [ln.text for ln in rebuild_lines(frags)]
    assert lines[0] == "WHOLE MILK 2 x 65.00 130.00 A"
    assert len(lines) == 2


def _long_frames(n=4):
    s = synth.long_multi(40)
    img = _gray(synth.render(s))
    return img, synth.split_sections(img, n, overlap=0.25)


def test_orb_and_template_offsets_agree_with_truth():
    img, frames = _long_frames(3)
    step = img.shape[0] / (3 - 2 * 0.25)
    true_dy = round(step * 0.75)
    o1 = offset_orb(frames[0], frames[1])
    o2 = offset_template(frames[0], frames[1])
    assert o1 is not None and abs(o1.dy - true_dy) <= 3
    assert o2 is not None and abs(o2.dy - true_dy) <= 3


def test_stitch_reconstructs_tall_image():
    img, frames = _long_frames(4)
    out, info = stitch(frames)
    assert out is not None and info["method"] in ("orb", "template", "orb+template")
    pad = int(img.shape[1] * 0.08)
    assert abs(out.shape[0] - img.shape[0]) <= 6
    core = out[:, pad:pad + img.shape[1]]
    h = min(core.shape[0], img.shape[0])
    diff = np.abs(core[:h].astype(int) - img[:h].astype(int)).mean()
    assert diff < 6, diff


def test_stitch_fails_cleanly_on_unrelated_frames():
    a = _gray(synth.render(synth.fuel_8()))
    b = np.full_like(a, 255)
    out, info = stitch([a, b])
    assert out is None and info["method"] == "failed"


def _L(texts, h=1000, top=0):
    step = h / (len(texts) + 1)
    return [{"text": t, "y0": top + (i + 1) * step - 10, "y1": top + (i + 1) * step + 10} for i, t in enumerate(texts)]


def test_dedupe_removes_overlap_lines():
    f1 = _L(["HEADER", "ITEM A 10.00", "ITEM B 20.00", "ITEM C 30.00", "ITEM D 40.00"])
    f2 = _L(["ITEM C 30.00", "ITEM D 40.00", "ITEM E 50.00", "TOTAL 150.00"])
    lines, dropped = merge_frame_lines([f1, f2], [1000, 1000])
    assert [l["text"] for l in lines] == ["HEADER", "ITEM A 10.00", "ITEM B 20.00", "ITEM C 30.00",
                                          "ITEM D 40.00", "ITEM E 50.00", "TOTAL 150.00"]
    assert dropped == [0, 2]


def test_dedupe_handles_lines_cut_at_frame_edges():
    # prev ends with a half line; next starts with a half line
    f1 = _L(["HEADER", "ITEM A 10.00", "ITEM B 20.00", "ITEM C 30.00", "ITEM D 40.00", "1TFM F 5"])
    f2 = _L(["IT B 2O", "ITEM C 30.00", "ITEM D 40.00", "ITEM E 50.00", "TOTAL 150.00"])
    lines, _ = merge_frame_lines([f1, f2], [1000, 1000])
    assert [l["text"] for l in lines] == ["HEADER", "ITEM A 10.00", "ITEM B 20.00", "ITEM C 30.00",
                                          "ITEM D 40.00", "ITEM E 50.00", "TOTAL 150.00"]


def test_dedupe_tolerates_ocr_noise_in_overlap():
    f1 = _L(["A", "ITEM C 30.00", "ITEM D 40.00"])
    f2 = _L(["ITEM C 3O.00", "ITEM D 40.00", "ITEM E 50.00"])
    lines, _ = merge_frame_lines([f1, f2], [1000, 1000])
    assert [l["text"] for l in lines] == ["A", "ITEM C 30.00", "ITEM D 40.00", "ITEM E 50.00"]


def test_dedupe_keeps_everything_without_overlap():
    f1 = _L(["A 1.00", "B 2.00"])
    f2 = _L(["C 3.00", "D 4.00"])
    lines, dropped = merge_frame_lines([f1, f2], [1000, 1000])
    assert len(lines) == 4 and dropped == [0, 0]
