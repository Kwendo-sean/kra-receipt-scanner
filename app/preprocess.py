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


def prepare(img: np.ndarray, max_side: int = 1600, crop: bool = True,
            quad: list | None = None) -> tuple[np.ndarray, dict]:
    """Full pipeline -> grayscale image ready for OCR, plus a debug info dict.
    `quad`: corners confirmed on the phone (fractions of the photo) - used for the
    perspective flatten instead of detecting the paper again."""
    info: dict = {"in_size": list(img.shape[:2])}
    if quad:
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
