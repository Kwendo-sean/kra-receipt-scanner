"""Long-receipt support: stitch overlapping section frames into one tall image,
or (fallback) OCR each frame and drop the lines duplicated in the overlaps.

Frames are pre-processed grayscale images of (roughly) equal width, captured top
to bottom with ~20-30% overlap.
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

import cv2
import numpy as np


@dataclass
class Offset:
    dx: float
    dy: float
    method: str
    score: float


def same_width(frames: list[np.ndarray], max_w: int = 1200) -> list[np.ndarray]:
    w = min(max_w, int(np.median([f.shape[1] for f in frames])))
    out = []
    for f in frames:
        if f.shape[1] != w:
            h = int(round(f.shape[0] * w / f.shape[1]))
            f = cv2.resize(f, (w, h), interpolation=cv2.INTER_AREA)
        out.append(f)
    return out


def offset_orb(top: np.ndarray, bot: np.ndarray, max_overlap: float = 0.6) -> Offset | None:
    """ORB features + RANSAC homography between the bottom of `top` and the top
    of `bot`. Accept only a near-pure translation (receipts slide vertically)."""
    th, bh = top.shape[0], bot.shape[0]
    t0 = int(th * (1 - max_overlap))
    a = top[t0:]
    b = bot[: int(bh * max_overlap)]
    orb = cv2.ORB_create(nfeatures=3000, scaleFactor=1.2, nlevels=6, fastThreshold=10)
    ka, da = orb.detectAndCompute(a, None)
    kb, db = orb.detectAndCompute(b, None)
    if da is None or db is None or len(ka) < 12 or len(kb) < 12:
        return None
    bf = cv2.BFMatcher(cv2.NORM_HAMMING)
    pairs = bf.knnMatch(db, da, k=2)
    good = [m for m, *rest in (p for p in pairs if len(p) >= 1)
            if not rest or m.distance < 0.75 * rest[0].distance]
    if len(good) < 12:
        return None
    src = np.float32([kb[m.queryIdx].pt for m in good])
    dst = np.float32([ka[m.trainIdx].pt for m in good])
    H, mask = cv2.findHomography(src, dst, cv2.RANSAC, 4.0)
    if H is None:
        return None
    inl = int(mask.sum())
    if inl < 10:
        return None
    H = H / H[2, 2]
    # near-identity linear part + negligible perspective
    lin = H[:2, :2]
    if np.abs(lin - np.eye(2)).max() > 0.06 or np.abs(H[2, :2]).max() > 1e-4:
        return None
    dx, dy = float(H[0, 2]), float(H[1, 2]) + t0
    if not (0.15 * th < dy < th) or abs(dx) > 0.1 * top.shape[1]:
        return None
    return Offset(dx, dy, "orb", inl / max(1, len(good)))


def offset_template(top: np.ndarray, bot: np.ndarray, strip: float = 0.12, min_score: float = 0.55) -> Offset | None:
    """Vertical-only alignment: find where the top strip of `bot` appears in the
    lower part of `top` (normalised cross-correlation, small x tolerance)."""
    th, w = top.shape[:2]
    sh = max(24, int(bot.shape[0] * strip))
    margin = max(4, int(w * 0.04))
    tmpl = bot[:sh, margin:w - margin]
    if tmpl.std() < 5:  # blank strip: use a lower one with ink
        for y in range(sh, bot.shape[0] // 2, sh // 2):
            cand = bot[y:y + sh, margin:w - margin]
            if cand.std() >= 5:
                tmpl, y_off = cand, y
                break
        else:
            return None
    else:
        y_off = 0
    search_y0 = int(th * 0.3)
    region = top[search_y0:]
    if region.shape[0] < tmpl.shape[0]:
        return None
    res = cv2.matchTemplate(region, tmpl, cv2.TM_CCOEFF_NORMED)
    _, mx, _, loc = cv2.minMaxLoc(res)
    if mx < min_score:
        return None
    dy = search_y0 + loc[1] - y_off
    dx = loc[0] - margin
    if dy <= 0.15 * th:
        return None
    return Offset(float(dx), float(dy), "template", float(mx))


def stitch(frames: list[np.ndarray]) -> tuple[np.ndarray | None, dict]:
    """Stitch top-to-bottom frames. Returns (image or None, info)."""
    info: dict = {"method": "single" if len(frames) == 1 else None, "offsets": []}
    if len(frames) == 1:
        return frames[0], info
    frames = same_width(frames)
    # white side padding so horizontal alignment shifts never clip text
    pad = int(frames[0].shape[1] * 0.08)
    frames = [cv2.copyMakeBorder(f, 0, 0, pad, pad, cv2.BORDER_CONSTANT, value=255) for f in frames]
    w = frames[0].shape[1]
    strips = [frames[0]]
    strip_start = 0  # row in cur_bottom where strips[-1] begins
    cur_bottom = frames[0]
    methods = set()
    for i in range(1, len(frames)):
        bot = frames[i]
        off = offset_orb(cur_bottom, bot) or offset_template(cur_bottom, bot)
        if off is None:
            info["method"] = "failed"
            info["failed_at"] = i
            return None, info
        methods.add(off.method)
        info["offsets"].append({"i": i, "dx": round(off.dx, 1), "dy": round(off.dy, 1),
                                "method": off.method, "score": round(off.score, 3)})
        M = np.float32([[1, 0, off.dx], [0, 1, 0]])
        shifted = cv2.warpAffine(bot, M, (w, bot.shape[0]), borderValue=255)
        dy = int(round(off.dy))
        ov = max(0, min(cur_bottom.shape[0] - dy, bot.shape[0]))
        cut = _seam(cur_bottom, shifted, dy, ov)
        # trim the previous strip to end at the seam, then append the new part
        keep = dy + cut - strip_start
        strips[-1] = strips[-1][:max(0, keep)]
        strips.append(shifted[cut:])
        strip_start = cut
        cur_bottom = shifted
    info["method"] = "+".join(sorted(methods))
    out = np.vstack(strips)
    return out, info


def _seam(top: np.ndarray, bot: np.ndarray, dy: int, ov: int) -> int:
    """Pick the cut row (in `bot` coordinates) inside the overlap that crosses the
    least ink in both images, so the seam runs between text lines."""
    if ov <= 4:
        return ov
    lo, hi = int(ov * 0.2), max(int(ov * 0.8), int(ov * 0.2) + 1)
    ink_b = (255 - bot[lo:hi].astype(np.int32)).sum(axis=1)
    ink_t = (255 - top[dy + lo:dy + hi].astype(np.int32)).sum(axis=1)
    n = min(len(ink_b), len(ink_t))
    if n == 0:
        return ov // 2
    score = ink_b[:n] + ink_t[:n]
    # smooth so we land in the middle of a gap, not at a glyph's edge
    score = np.convolve(score, np.ones(5) / 5, mode="same")
    return lo + int(np.argmin(score))


# ------------------------------------------------------------ text de-dup


def _norm(s: str) -> str:
    return "".join(ch for ch in s.upper() if ch.isalnum())


def _similar(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    return na == nb or SequenceMatcher(None, na, nb).ratio() >= 0.85


def dedupe_overlap(prev: list[dict], nxt: list[dict], prev_h: float, next_h: float) -> tuple[int, int]:
    """Find the lines repeated in the overlap of two consecutive frames.

    Returns (drop_prev_tail, drop_next_head). A frame edge can cut a line in
    half, so prev may end with a partial line (whose full copy is in nxt) and
    nxt may start with a partial line (whose full copy is in prev); up to one
    such partial line is skipped at each edge. Only the bottom 60% of prev and
    the top 60% of nxt are considered, and the longest consecutive run wins.
    Lines are dicts with 'text', 'y0', 'y1'.
    """
    best = (0, 0, 0)  # (k, skip_prev_end, skip_next_start)
    for sp in (0, 1):
        end = len(prev) - sp
        for sn in (0, 1):
            for i in range(end):
                if prev[i]["y0"] < prev_h * 0.4:
                    continue
                k = 0
                while (i + k < end and sn + k < len(nxt) and nxt[sn + k]["y1"] <= next_h * 0.6
                       and _similar(prev[i + k]["text"], nxt[sn + k]["text"])):
                    k += 1
                if i + k != end or k == 0 or (sp or sn) and k < 2:
                    continue
                if k > best[0] or (k == best[0] and sp + sn < best[1] + best[2]):
                    best = (k, sp, sn)
    k, sp, sn = best
    if k == 0:
        return 0, 0
    return sp, sn + k


def merge_frame_lines(frames_lines: list[list[dict]], heights: list[float]) -> tuple[list[dict], list[int]]:
    """Concatenate per-frame lines, dropping overlap duplicates. Returns (lines, dropped_per_frame)."""
    out: list[dict] = []
    dropped = [0]
    y_base = 0.0
    for idx, lines in enumerate(frames_lines):
        if idx == 0:
            out.extend(dict(l) for l in lines)
            y_base = heights[0]
            continue
        drop_tail, drop_head = dedupe_overlap(frames_lines[idx - 1], lines, heights[idx - 1], heights[idx])
        if drop_tail:
            del out[len(out) - drop_tail:]
        dropped.append(drop_head + drop_tail)
        for l in lines[drop_head:]:
            d = dict(l)
            d["y0"] += y_base
            d["y1"] += y_base
            d["frame"] = idx
            out.append(d)
        y_base += heights[idx]
    return out, dropped
