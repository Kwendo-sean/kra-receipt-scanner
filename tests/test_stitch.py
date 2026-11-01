import cv2
import numpy as np

import synth
from app.lines import Fragment, rebuild_lines
from app.preprocess import find_paper_strip, prepare_section, straighten_rows
from app.stitch import align_by_text, compose, merge_frame_lines, merge_sections, offset_orb, offset_template, stitch


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


def test_rebuild_lines_merges_double_detected_word():
    # the detector boxed "3" twice: once at the end of the name, once with the price
    frags = [Fragment("THERMOMETER #15 3", 30, 890, 330, 919), Fragment("3 x 45.00 135.00 B", 312, 890, 640, 919)]
    assert [ln.text for ln in rebuild_lines(frags)] == ["THERMOMETER #15 3 x 45.00 135.00 B"]


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


# ------------------------------------------------------------ real-photo style sections

def _receipt_lines(n=60):
    out = []
    for i in range(n):
        if i % 7 == 3:
            out.append("1 x 120.00 A")                      # repeated text (same item price)
        elif i % 2:
            out.append(f"20{36000000 + i * 7919:011d}")      # item code
        else:
            out.append(f"ITEM {chr(65 + i % 26)}{i} PACK {i * 3}G  {i * 11 + 50}.00")
    return out


def _section(texts, first, last, s=1.0, d=0.0, pitch=40.0, noise=None):
    """OCR lines of a section showing receipt lines first..last-1; y_section = (40*i - d) / s."""
    out = []
    for i in range(first, last):
        t = texts[i]
        if noise and i in noise:
            t = noise[i]
        y = (pitch * i - d) / s
        out.append({"text": t, "y0": y, "y1": y + 26 / s})
    return out


def test_text_alignment_finds_shift_and_stretch():
    texts = _receipt_lines()
    a = _section(texts, 0, 36)
    # next photo starts at line 20, 4% stretched, with OCR slips in the overlap
    b = _section(texts, 20, 60, s=1 / 1.04, d=800, noise={22: "2036I74238", 25: "1TEM Z24 PACK 72G 314.00"})
    r = align_by_text(a, b)
    assert r and abs(r["s"] - 1 / 1.04) < 0.01 and abs(r["d"] - 800) < 6 and r["inliers"] >= 8


def test_merge_sections_keeps_every_line_once_in_order():
    texts = _receipt_lines()
    secs = [_section(texts, 0, 30), _section(texts, 18, 45, s=1.03, d=720), _section(texts, 40, 60, d=1600)]
    lines, info = merge_sections(secs, [1200, 1200, 1200])
    assert [l["text"] for l in lines] == texts
    assert info["method"] == "text" and len(info["joins"]) == 2


def test_merge_sections_handles_a_repeated_photo():
    # the same part photographed twice (r000012 had this) adds nothing
    texts = _receipt_lines()
    secs = [_section(texts, 0, 30), _section(texts, 1, 30, d=40), _section(texts, 25, 60, d=1000)]
    lines, _ = merge_sections(secs, [1200, 1200, 1400])
    assert [l["text"] for l in lines] == texts


def test_merge_sections_falls_back_without_common_text():
    a = [{"text": f"AAA LINE {i}", "y0": i * 40, "y1": i * 40 + 26} for i in range(20)]
    b = [{"text": f"BBB ROW {i * 13}", "y0": i * 40, "y1": i * 40 + 26} for i in range(20)]
    lines, info = merge_sections([a, b], [800, 800])
    assert len(lines) == 40 and info["joins"][0]["method"] == "dedupe"


def test_compose_cuts_sections_at_the_join():
    # 3 sections of one tall gradient image, aligned by known offsets
    tall = np.tile(np.arange(3000, dtype=np.float32)[:, None] / 3000 * 255, (1, 300)).astype(np.uint8)
    secs = [tall[0:1200], tall[900:2100], tall[1800:3000]]
    joins = [{"s": 1.0, "d": 900.0, "cut": 1050.0}, {"s": 1.0, "d": 900.0, "cut": 1050.0}]
    out = compose(secs, joins)
    assert out.shape[0] == 3000 and np.abs(out.astype(int) - tall.astype(int)).max() <= 1


def _photo_of_strip(angle):
    """Dark table, white paper strip running off the top and bottom, tilted, with print."""
    h, w = 4032 // 4, 3024 // 4
    img = np.full((h, w, 3), (40, 60, 90), np.uint8)
    paper = np.full((h * 2, 260), 240, np.uint8)
    for y in range(20, paper.shape[0] - 20, 24):
        cv2.putText(paper, "ITEM 1 x 120.00", (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, 30, 1)
    M = cv2.getRotationMatrix2D((130, h), angle, 1.0)
    M[0, 2] += w / 2 - 130
    M[1, 2] += h / 2 - h
    rot = cv2.warpAffine(paper, M, (w, h), borderValue=0)
    m = cv2.warpAffine(np.full_like(paper, 255), M, (w, h), borderValue=0)
    img[m > 0] = cv2.cvtColor(rot, cv2.COLOR_GRAY2BGR)[m > 0]
    return img


def test_section_is_cut_to_the_upright_paper_strip():
    for angle in (-6, 0, 4):
        img = _photo_of_strip(angle)
        st = find_paper_strip(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
        assert st and abs(st["angle"] - angle) < 1.0, (angle, st)
        g, info = prepare_section(img)
        assert info["cropped"] == "paper strip" and g.shape[1] == 1200
        # nearly all of the crop is paper (no table down the sides)
        assert (g > 150).mean() > 0.9


def test_straighten_rows_levels_curled_paper():
    # labels on the left, amounts on the right; down the page the right side
    # sags until it is ~0.9 of a line lower than its label (r000010's totals)
    h, w, pitch = 2400, 1200, 50
    flat = np.full((h, w), 255, np.uint8)
    rng = np.random.default_rng(3)
    for y in range(40, h - 60, pitch):
        x = 30
        while x < w - 60:
            ln = int(rng.integers(20, 90))
            flat[y:y + 22, x:min(w - 30, x + ln)] = 30
            x += ln + int(rng.integers(15, 60))
    sag = lambda x, y: 0.9 * pitch * (y / h) * (x / w)
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    curled = cv2.remap(flat, xs, ys - sag(xs, ys), cv2.INTER_LINEAR, borderValue=255)

    def misalign(img):
        # row offset between the right and left quarters, bottom third of the page
        a = (img[1600:2300, :300] < 128).sum(axis=1).astype(float)
        b = (img[1600:2300, -300:] < 128).sum(axis=1).astype(float)
        best = max(range(-30, 50), key=lambda s: np.dot(a[30:-50], b[30 + s:len(b) - 50 + s]))
        return abs(best)

    assert misalign(curled) > 25
    out, info = straighten_rows(curled)
    assert info["straighten"]["max_shift"] > 10        # measured from the middle strip
    assert misalign(out) <= 6
