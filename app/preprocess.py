"""Image pre-processing before OCR: receipt edge detection + perspective crop,
deskew, uneven-lighting correction (CLAHE), and long-side limiting.

All functions take/return numpy BGR or grayscale arrays and never keep more than
one full-resolution image alive at a time.
"""
from __future__ import annotations

import cv2
import numpy as np


def load(path) -> np.ndarray:
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"Cannot read image {path}")
    return img


def limit_side(img: np.ndarray, max_side: int) -> np.ndarray:
    h, w = img.shape[:2]
    s = max(h, w)
    if s <= max_side:
        return img
    f = max_side / s
    return cv2.resize(img, (int(w * f), int(h * f)), interpolation=cv2.INTER_AREA)


def limit_width(img: np.ndarray, max_w: int) -> np.ndarray:
    h, w = img.shape[:2]
    if w <= max_w:
        return img
    f = max_w / w
    return cv2.resize(img, (max_w, int(h * f)), interpolation=cv2.INTER_AREA)


def sharpness(gray: np.ndarray) -> float:
    """Variance of the Laplacian on a downscaled frame (same metric as the browser)."""
    small = limit_side(gray, 480)
    return float(cv2.Laplacian(small, cv2.CV_64F).var())


def _order(pts: np.ndarray) -> np.ndarray:
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]], np.float32)


def find_receipt_quad(img: np.ndarray) -> np.ndarray | None:
    """Find the paper (bright region) quadrilateral. Returns 4 points or None."""
    small = limit_side(img, 800)
    f = img.shape[0] / small.shape[0]
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if small.ndim == 3 else small
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    _, th = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    cnts, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    c = max(cnts, key=cv2.contourArea)
    area = cv2.contourArea(c)
    if area < 0.15 * gray.shape[0] * gray.shape[1]:
        return None
    if area > 0.97 * gray.shape[0] * gray.shape[1]:
        return None  # paper fills the frame already
    peri = cv2.arcLength(c, True)
    approx = cv2.approxPolyDP(c, 0.02 * peri, True)
    if len(approx) == 4:
        pts = approx.reshape(4, 2).astype(np.float32)
    else:
        pts = cv2.boxPoints(cv2.minAreaRect(c)).astype(np.float32)
    return _order(pts * f)


def perspective_crop(img: np.ndarray, quad: np.ndarray) -> np.ndarray:
    tl, tr, br, bl = quad
    w = int(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl)))
    h = int(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr)))
    if w < 50 or h < 50:
        return img
    dst = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], np.float32)
    M = cv2.getPerspectiveTransform(quad, dst)
    return cv2.warpPerspective(img, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def deskew_angle(gray: np.ndarray) -> float:
    """Estimate small text skew (degrees) from the min-area rect of ink pixels."""
    small = limit_side(gray, 1000)
    th = cv2.adaptiveThreshold(small, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 25, 15)
    # join characters into line blobs, then measure line angles
    th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (25, 3)))
    # RETR_LIST: the paper edge can form one big outer ring around every line
    cnts, _ = cv2.findContours(th, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    angles = []
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        if w < 60 or h > 0.1 * small.shape[0] or cv2.contourArea(c) < 0.3 * w * max(3, h * 0.3):
            continue
        rw, rh = cv2.minAreaRect(c)[1]
        if max(rw, rh) < 5 * min(rw, rh):
            continue
        box = cv2.boxPoints(cv2.minAreaRect(c))
        # angle of the rect's longest edge, in (-45, 45]
        edges = [box[(i + 1) % 4] - box[i] for i in range(4)]
        e = max(edges, key=lambda v: float(np.hypot(v[0], v[1])))
        a = float(np.degrees(np.arctan2(e[1], e[0])))
        while a > 45:
            a -= 180 if a > 135 else 90
        while a <= -45:
            a += 180 if a <= -135 else 90
        angles.append(a)
    if len(angles) < 3:
        return 0.0
    return float(np.median(angles))


def rotate(img: np.ndarray, angle: float) -> np.ndarray:
    if abs(angle) < 0.2:
        return img
    h, w = img.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    cos, sin = abs(M[0, 0]), abs(M[0, 1])
    nw, nh = int(h * sin + w * cos), int(h * cos + w * sin)
    M[0, 2] += nw / 2 - w / 2
    M[1, 2] += nh / 2 - h / 2
    border = 255 if img.ndim == 2 else (255, 255, 255)
    return cv2.warpAffine(img, M, (nw, nh), flags=cv2.INTER_LINEAR, borderValue=border)


def even_lighting(gray: np.ndarray) -> np.ndarray:
    """Flatten uneven light / faded thermal paper: divide by background, then CLAHE."""
    k = max(31, (min(gray.shape[:2]) // 20) | 1)
    bg = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    norm = cv2.divide(gray, bg, scale=255)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return clahe.apply(norm)


def paper_mask(gray: np.ndarray) -> np.ndarray | None:
    """Mask of the receipt paper, found by its *edges* rather than brightness:
    a table/paper boundary is a sharp step, while uneven light or faded print is
    smooth after blurring, so shadowed paper is never mistaken for background.
    Returns None when there is no clear paper boundary (e.g. paper fills frame)."""
    small = limit_side(gray, 600)
    sh, sw = small.shape[:2]
    blur = cv2.GaussianBlur(small, (0, 0), 3.0)          # erase the print itself
    edges = cv2.Canny(blur, 25, 70)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=2)
    free = (edges == 0).astype(np.uint8)
    n, lab = cv2.connectedComponents(free, connectivity=4)
    if n < 2:
        return None
    # the paper is the edge-free region at the centre (receipts are framed centrally)
    cy, cx = sh // 2, sw // 2
    win = lab[max(0, cy - 15):cy + 16, max(0, cx - 15):cx + 16].ravel()
    win = win[win > 0]
    if win.size == 0:
        return None
    paper = int(np.bincount(win).argmax())
    m = (lab == paper).astype(np.uint8) * 255
    # fill any holes, then grow back over the dilated edge band
    cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    m = np.zeros_like(m)
    cv2.drawContours(m, cnts, -1, 255, thickness=cv2.FILLED)
    m = cv2.dilate(m, np.ones((3, 3), np.uint8), iterations=2)
    frac = m.mean() / 255
    if frac > 0.97 or frac < 0.12:
        return None
    # only whiten when the outside really is darker than the paper (a table)
    inside, outside = small[m > 0], small[m == 0]
    if outside.size == 0 or float(np.median(inside)) - float(np.median(outside)) < 35:
        return None
    m = cv2.erode(m, np.ones((3, 3), np.uint8))  # stay just inside the paper edge
    return cv2.resize(m, (gray.shape[1], gray.shape[0]), interpolation=cv2.INTER_NEAREST)


def white_background(gray: np.ndarray, mask: np.ndarray | None) -> np.ndarray:
    """Everything that isn't paper becomes pure white (a clean 'scan')."""
    if mask is None:
        return gray
    out = gray.copy()
    out[mask == 0] = 255
    return out


def _best_shift(a: np.ndarray, b: np.ndarray, centre: int, r: int) -> tuple[int, float]:
    """Shift s in centre±r maximising corr(a[y], b[y+s]) (1-D row-ink profiles)."""
    n = len(a)
    best, bs = -2.0, centre
    for s in range(centre - r, centre + r + 1):
        lo, hi = max(0, -s), min(n, n - s)
        if hi - lo < n * 0.6:
            continue
        x, y = a[lo:hi], b[lo + s:hi + s]
        x, y = x - x.mean(), y - y.mean()
        den = float(np.sqrt((x * x).sum() * (y * y).sum()))
        if den <= 1e-6:
            continue
        c = float((x * y).sum()) / den
        if c > best:
            best, bs = c, s
    return bs, best


def straighten_rows(gray: np.ndarray, strips: int = 6, win: int = 360, step: int = 120) -> tuple[np.ndarray, dict]:
    """Wavy / curled paper: down the page, the right-hand amounts drift up or down
    against their labels until OCR pairs a label with the next line's amount.
    Measure, in overlapping windows, how far each vertical strip's text lines sit
    from the middle strip's, follow that drift smoothly down the page (so a drift
    of nearly a whole line is not mistaken for the next line), and remap the image
    so every printed line is level again."""
    h, w = gray.shape[:2]
    info: dict = {"straighten": None}
    if h < win + step or w < 300:
        return gray, info
    ink = (gray < 150).astype(np.float32)
    sw = w // strips
    prof = [cv2.GaussianBlur(ink[:, k * sw:(k + 1) * sw].sum(axis=1).reshape(-1, 1), (1, 0), 2.5).ravel()
            for k in range(strips)]
    c = strips // 2
    centres = list(range(win // 2, h - win // 2, step))
    rel = np.zeros((len(centres), strips))          # shift of strip k vs strip k-1 (k > c) or k+1 (k < c)
    prev = np.zeros(strips, int)
    found = 0
    for i, yc in enumerate(centres):
        y0, y1 = yc - win // 2, yc + win // 2
        for k in list(range(c + 1, strips)) + list(range(c - 1, -1, -1)):
            ref = k - 1 if k > c else k + 1
            a, b = prof[ref][y0:y1], prof[k][y0:y1]
            if a.std() < 0.5 or b.std() < 0.5:        # no print here: keep following the last drift
                rel[i, k] = prev[k]
                continue
            r = 14 if i == 0 or not found else 6
            s, score = _best_shift(a, b, int(prev[k]), r)
            if score < 0.35:
                s = prev[k]
            else:
                found += 1
            rel[i, k] = prev[k] = s
    # smooth along the page, then accumulate outwards from the middle strip
    for k in range(strips):
        rel[:, k] = cv2.GaussianBlur(rel[:, k].reshape(-1, 1).astype(np.float32), (1, 0), 1.5).ravel()
    tot = np.zeros_like(rel)
    for k in range(c + 1, strips):
        tot[:, k] = tot[:, k - 1] + rel[:, k]
    for k in range(c - 1, -1, -1):
        tot[:, k] = tot[:, k + 1] + rel[:, k]
    span = float(np.abs(tot).max())
    if span < 2.0:
        info["straighten"] = {"max_shift": round(span, 1)}
        return gray, info
    # displacement field: rows by interpolation between window centres, columns between strip centres
    ys = np.arange(h, dtype=np.float32)
    xs_c = (np.arange(strips) + 0.5) * sw
    per_strip = np.stack([np.interp(ys, centres, tot[:, k]) for k in range(strips)], axis=1)   # h x strips
    xs = np.arange(w, dtype=np.float32)
    disp = np.empty((h, w), np.float32)
    for y in range(0, h, 8):                        # coarse rows, then upsample
        disp[y] = np.interp(xs, xs_c, per_strip[y])
    rows = np.arange(0, h, 8)
    disp = cv2.resize(disp[rows], (w, h), interpolation=cv2.INTER_LINEAR)
    map_x = np.tile(xs, (h, 1))
    map_y = (ys[:, None] + disp).astype(np.float32)
    out = cv2.remap(gray, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=255)
    info["straighten"] = {"max_shift": round(span, 1)}
    return out, info


SECTION_W = 1200  # paper width every long-receipt section is scaled to


def _edge_line(ys: np.ndarray, xs: np.ndarray) -> tuple[float, float] | None:
    """Robust x = a*y + b through one side of the paper (drops bumps from hands/tears)."""
    if len(ys) < 20:
        return None
    keep = np.ones(len(ys), bool)
    for _ in range(3):
        a, b = np.polyfit(ys[keep], xs[keep], 1)
        r = np.abs(xs - (a * ys + b))
        thr = max(2.0, 2.5 * float(np.median(r[keep])))
        keep = r <= thr
        if keep.sum() < 20:
            return None
    return float(a), float(b)


def find_paper_strip(gray: np.ndarray) -> dict | None:
    """A section of a long receipt: the paper is a bright strip crossing the
    photo (usually running off the top and/or bottom). Finds its two long
    edges as straight lines. Returns {angle, x0, x1, y0, y1} in full-res
    pixels of the image *after* rotating by `angle`, or None."""
    small = limit_side(gray, 800)
    f = gray.shape[0] / small.shape[0]
    blur = cv2.GaussianBlur(small, (0, 0), 2.0)
    _, th = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(th, connectivity=4)
    if n < 2:
        return None
    k = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    m = lab == k
    sh, sw = m.shape
    if m.mean() < 0.06 or m.mean() > 0.9:
        return None
    cols = np.arange(sw)
    rows = np.where(m.any(axis=1))[0]
    lx = np.array([cols[m[y]].min() for y in rows], float)
    rx = np.array([cols[m[y]].max() for y in rows], float)
    wid = rx - lx
    ok = wid > 0.6 * np.median(wid)
    # edges touching the photo border are not paper edges
    okl, okr = ok & (lx > 1), ok & (rx < sw - 2)
    L, R = _edge_line(rows[okl].astype(float), lx[okl]), _edge_line(rows[okr].astype(float), rx[okr])
    if L is None or R is None:
        return None
    slope = (L[0] + R[0]) / 2
    if abs(np.degrees(np.arctan(slope))) > 20:
        return None
    angle = float(np.degrees(np.arctan(slope)))
    # paper edges after rotating the image by `angle` (same centre/size rule as rotate())
    M = cv2.getRotationMatrix2D((sw / 2, sh / 2), -angle, 1.0)
    cos, sin = abs(M[0, 0]), abs(M[0, 1])
    nw, nh = int(sh * sin + sw * cos), int(sh * cos + sw * sin)
    M[0, 2] += nw / 2 - sw / 2
    M[1, 2] += nh / 2 - sh / 2
    mr = cv2.warpAffine(m.astype(np.uint8) * 255, M, (nw, nh), flags=cv2.INTER_NEAREST)
    cov = (mr > 0).sum(axis=0)
    xs = np.where(cov > 0.5 * cov.max())[0]
    if len(xs) < 10:
        return None
    x0, x1 = xs.min(), xs.max() + 1
    inside = (mr[:, x0:x1] > 0).mean(axis=1)
    ys = np.where(inside > 0.6)[0]
    if len(ys) < 10:
        return None
    return {"angle": angle, "x0": int(x0 * f), "x1": int(x1 * f), "y0": int(ys.min() * f), "y1": int((ys.max() + 1) * f)}


def prepare_section(img: np.ndarray) -> tuple[np.ndarray, dict]:
    """One section of a long receipt -> upright paper strip at SECTION_W wide.
    Every section ends up at the same scale and orientation, so neighbouring
    sections differ only by a vertical slide (what the stitcher expects)."""
    info: dict = {"in_size": list(img.shape[:2])}
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    strip = find_paper_strip(gray)
    if strip is None:
        info["section"] = "no paper strip"
        return gray, info
    # rotate() turns by -angle for a positive slope; keep the same sign convention
    g = rotate(gray, -strip["angle"])
    g = g[strip["y0"]:strip["y1"], strip["x0"]:strip["x1"]]
    if g.shape[1] < 50 or g.shape[0] < 50:
        info["section"] = "strip too small"
        return gray, info
    g = cv2.resize(g, (SECTION_W, int(round(g.shape[0] * SECTION_W / g.shape[1]))),
                   interpolation=cv2.INTER_AREA if g.shape[1] > SECTION_W else cv2.INTER_CUBIC)
    info["section"] = {"angle": round(strip["angle"], 2), "paper_w": strip["x1"] - strip["x0"]}
    info["cropped"] = "paper strip"
    return g, info


def prepare(img: np.ndarray, max_side: int = 1600, crop: bool = True,
            quad: list | None = None, section: bool = False) -> tuple[np.ndarray, dict]:
    """Full pipeline -> grayscale image ready for OCR, plus a debug info dict.
    `quad`: corners confirmed on the phone (fractions of the photo) - used for the
    perspective flatten instead of detecting the paper again."""
    info: dict = {"in_size": list(img.shape[:2])}
    if section and not quad:
        img, info = prepare_section(img)
    elif quad:
        h, w = img.shape[:2]
        pts = np.array([[x * w, y * h] for x, y in quad], np.float32)
        img = perspective_crop(img, _order(pts))
        info["cropped"] = "phone corners"
    elif crop:
        quad = find_receipt_quad(img)
        if quad is not None:
            img = perspective_crop(img, quad)
            info["cropped"] = True
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    del img
    ang = deskew_angle(gray)
    info["deskew_deg"] = round(ang, 2)
    gray = rotate(gray, ang)
    mask = paper_mask(gray)
    info["white_bg"] = mask is not None
    gray = white_background(even_lighting(gray), mask)
    del mask
    gray, sinfo = straighten_rows(gray)
    info.update(sinfo)
    # Receipts are narrow: cap the width (keeps text size sane) rather than the
    # long side, so a 2 m roll is not shrunk to illegibility.
    gray = limit_width(gray, max_side) if gray.shape[0] > 2 * gray.shape[1] else limit_side(gray, max_side)
    info["out_size"] = list(gray.shape[:2])
    info["sharpness"] = round(sharpness(gray), 1)
    return gray, info


def detect_kra_qr(img: np.ndarray) -> str | None:
    """Decode a KRA eTIMS QR code if present. The URL is stored as evidence only."""
    det = cv2.QRCodeDetector()
    for candidate in (img, limit_side(img, 1200)):
        try:
            data, pts, _ = det.detectAndDecode(candidate)
        except cv2.error:
            data = ""
        if data:
            return data.strip()
    return None
