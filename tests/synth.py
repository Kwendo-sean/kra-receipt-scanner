"""Synthetic Kenyan receipts with exact ground truth, as text and as rendered images.

Used to build the test set before tuning. Real-photo fixtures go in
tests/fixtures/real/ (git-ignored) with a matching .json of expected fields.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Synth:
    name: str
    lines: list[str]
    expect: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _m(v: float) -> str:
    return f"{v:,.2f}"


def supermarket_16_exempt() -> Synth:
    items = [("WHOLE MILK 500ML", 2, 65.00, "A"), ("BREAD WHITE 400G", 1, 70.00, "A"),
             ("COOKING OIL 1L", 1, 420.00, "B"), ("SUGAR 2KG", 1, 380.00, "B"),
             ("DETERGENT 1KG", 2, 300.00, "B")]
    lines = ["NAIVAS SUPERMARKET LTD", "MOI AVENUE, NAIROBI", "PIN: P051234567X", "TEL 0722 123 456",
             "TAX INVOICE", "Receipt No: 0012-3456", "Date: 21/10/2026  Time: 08:15", "CASHIER: JANE"]
    total = 0.0
    for d, q, u, b in items:
        lt = q * u
        total += lt
        lines.append(f"{d}  {q} x {_m(u)}  {_m(lt)} {b}")
    # A exempt 200.00; B gross 1400.00 incl 16% -> taxable 1206.90, vat 193.10
    lines += [
        f"TOTAL  {_m(total)}",
        "RATE    TAXABLE    VAT",
        "A-EX  0%  200.00  0.00",
        "B-16%  16%  1,206.90  193.10",
        "TOTAL VAT  193.10",
        f"M-PESA  {_m(total)}",
        "M-PESA REF: SJK7ABC123",
        "CU Serial No: KRAMW017202207061234",
        "CU Invoice No: 0170612340000012345",
        "Thank you for shopping",
    ]
    expect = dict(seller_name="NAIVAS SUPERMARKET LTD", seller_pin="P051234567X", receipt_no="0012-3456",
                  receipt_date="2026-10-21", receipt_time="08:15:00", total=1600.00, total_vat=193.10,
                  cu_invoice_no="0170612340000012345", cu_serial_no="KRAMW017202207061234",
                  payment_method="M-PESA", mpesa_code="SJK7ABC123", n_items=5,
                  bands={("A", 0.0): (200.00, 0.00), ("B", 16.0): (1206.90, 193.10)})
    return Synth("supermarket_16_exempt", lines, expect)


def fuel_8() -> Synth:
    lines = ["RUBIS ENERGY KENYA", "RUBIS WESTLANDS SERVICE STATION", "KRA PIN: P000611133Q",
             "INVOICE NO: WL-889122", "21-10-2026 17:42",
             "DIESEL  40.000 L x 171.50  6,860.00 E",
             "TOTAL KES 6,860.00",
             "E-8%  8%  6,351.85  508.15",
             "TOTAL TAX  508.15",
             "PAID BY VISA CARD **** 4421",
             "CU INVOICE NO. KRACU0300001234/887",
             "CU SERIAL NO: KRACU0300001234"]
    expect = dict(seller_name="RUBIS ENERGY KENYA", seller_pin="P000611133Q", receipt_no="WL-889122",
                  receipt_date="2026-10-21", receipt_time="17:42:00", total=6860.00, total_vat=508.15,
                  cu_invoice_no="KRACU0300001234/887", payment_method="Card", n_items=1,
                  bands={("E", 8.0): (6351.85, 508.15)})
    return Synth("fuel_8", lines, expect)


def kiosk_no_vat() -> Synth:
    lines = ["MAMA WANJIKU KIOSK", "KAWANGWARE STAGE", "Date 03/11/2026",
             "SUKUMA WIKI  50.00", "TOMATOES  120.00", "ONIONS  80.00",
             "TOTAL  250.00", "CASH  300.00", "CHANGE  50.00", "Tel 0711000111"]
    expect = dict(seller_name="MAMA WANJIKU KIOSK", seller_pin=None, receipt_date="2026-11-03",
                  total=250.00, total_vat=None, cu_invoice_no=None, payment_method="Cash", n_items=3, bands={})
    return Synth("kiosk_no_vat", lines, expect)


def etims_gross_style() -> Synth:
    """eTIMS 'TOTAL B-16.00%' (gross) + 'TOTAL TAX B' layout with merged OCR tokens."""
    lines = ["QUICKMART LIMITED", "KRAPIN: P051098765K", "Buyer PIN: A012345678Z",
             "Date:05-10-202614:03", "Trans No: QM-77812",
             "RICE PISHORI 2KG  1 x 520.00  520.00 B", "MAIZE FLOUR 2KG  2 x 180.00  360.00 A",
             "TOTAL  KES880.00",
             "TOTAL A-EX  360.00", "TOTAL B-16.00%  520.00", "TOTAL TAX A  0.00", "TOTAL TAX B  71.72",
             "TOTAL TAX  71.72",
             "CASH  1,000.00", "CU INV NO: 0040123450000098765", "SCU ID: KRACU0400012345"]
    expect = dict(seller_name="QUICKMART LIMITED", seller_pin="P051098765K", buyer_pin="A012345678Z",
                  receipt_date="2026-10-05", receipt_time="14:03:00", receipt_no="QM-77812",
                  total=880.00, total_vat=71.72, cu_invoice_no="0040123450000098765",
                  cu_serial_no="KRACU0400012345", payment_method="Cash", n_items=2,
                  bands={("A", 0.0): (360.00, 0.00), ("B", 16.0): (448.28, 71.72)})
    return Synth("etims_gross_style", lines, expect)


def long_multi(n_items: int = 60, seed: int = 7) -> Synth:
    rnd = random.Random(seed)
    names = ["PANADOL 500MG", "AMOXIL 250MG", "CETRIZINE TABS", "VITAMIN C 1000", "ZINC SULPHATE",
             "ORS SACHETS", "GAUZE ROLL", "SURGICAL SPIRIT", "COTTON WOOL", "PLASTER STRIP",
             "IBUPROFEN 400", "MULTIVIT SYRUP", "HAND SANITIZER", "FACE MASK BOX", "THERMOMETER"]
    lines = ["GOODLIFE PHARMACY LTD", "SARIT CENTRE, NAIROBI", "PIN P052233445M", "Receipt No: GL-55120",
             "Date: 30/09/2026 19:20"]
    tot_a = tot_b = 0.0
    for i in range(n_items):
        nm = f"{names[i % len(names)]} #{i + 1:02d}"
        q = rnd.randint(1, 3)
        u = rnd.choice([45.0, 120.0, 250.0, 380.0, 999.0, 1450.0])
        band = "B" if i % 3 else "A"
        lt = q * u
        if band == "A":
            tot_a += lt
        else:
            tot_b += lt
        lines.append(f"{nm}  {q} x {_m(u)}  {_m(lt)} {band}")
    total = tot_a + tot_b
    tx_b = round(tot_b / 1.16, 2)
    vat_b = round(tot_b - tx_b, 2)
    lines += [f"TOTAL  {_m(total)}", f"A-EX  0%  {_m(tot_a)}  0.00", f"B-16%  16%  {_m(tx_b)}  {_m(vat_b)}",
              f"TOTAL VAT  {_m(vat_b)}", "CARD  " + _m(total), "CU Invoice No: 0090011220000033445"]
    expect = dict(seller_name="GOODLIFE PHARMACY LTD", seller_pin="P052233445M", receipt_no="GL-55120",
                  receipt_date="2026-09-30", total=round(total, 2), total_vat=vat_b, n_items=n_items,
                  payment_method="Card", cu_invoice_no="0090011220000033445",
                  bands={("A", 0.0): (round(tot_a, 2), 0.0), ("B", 16.0): (tx_b, vat_b)})
    return Synth("long_multi", lines, expect)


ALL = [supermarket_16_exempt, fuel_8, kiosk_no_vat, etims_gross_style, long_multi]


# ------------------------------------------------------------------ rendering


def _font(size: int):
    from PIL import ImageFont
    for name in ("DejaVuSansMono.ttf", "consola.ttf", "cour.ttf", "LiberationMono-Regular.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", "C:/Windows/Fonts/consola.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render(s: Synth, width: int = 900, font_size: int = 26, margin: int = 40, line_gap: int = 14):
    """Render receipt text to a white 'paper' image (numpy BGR)."""
    import numpy as np
    from PIL import Image, ImageDraw

    f = _font(font_size)
    lh = font_size + line_gap
    h = margin * 2 + lh * len(s.lines)
    img = Image.new("L", (width, h), 255)
    dr = ImageDraw.Draw(img)
    for i, ln in enumerate(s.lines):
        dr.text((margin, margin + i * lh), ln, fill=20, font=f)
    arr = np.array(img)
    return np.dstack([arr, arr, arr])


def photograph(img, seed: int = 1, angle: float = 2.0, fade: bool = True):
    """Simulate a phone photo: place on a dark table, rotate, uneven light, noise."""
    import cv2
    import numpy as np

    rnd = np.random.default_rng(seed)
    h, w = img.shape[:2]
    pad = 80
    canvas = np.full((h + 2 * pad, w + 2 * pad, 3), 60, np.uint8)
    canvas[pad:pad + h, pad:pad + w] = img
    M = cv2.getRotationMatrix2D((canvas.shape[1] / 2, canvas.shape[0] / 2), angle, 1.0)
    canvas = cv2.warpAffine(canvas, M, (canvas.shape[1], canvas.shape[0]), borderValue=(60, 60, 60))
    if fade:
        yy, xx = np.mgrid[0:canvas.shape[0], 0:canvas.shape[1]]
        grad = 0.75 + 0.25 * (xx / canvas.shape[1])
        canvas = np.clip(canvas.astype(np.float32) * grad[..., None], 0, 255)
        canvas = canvas.astype(np.uint8)
    noise = rnd.normal(0, 6, canvas.shape)
    return np.clip(canvas.astype(np.float32) + noise, 0, 255).astype(np.uint8)


def split_sections(img, n: int, overlap: float = 0.25):
    """Cut a tall image into n overlapping vertical frames (like guided multi-shot)."""
    h = img.shape[0]
    step = h / (n - (n - 1) * overlap)
    frames = []
    for i in range(n):
        y0 = int(round(i * step * (1 - overlap)))
        y1 = int(round(min(h, y0 + step)))
        frames.append(img[y0:y1].copy())
    return frames


def write_fixtures(out_dir: Path) -> None:
    import cv2
    out_dir.mkdir(parents=True, exist_ok=True)
    for fn in ALL:
        s = fn()
        cv2.imwrite(str(out_dir / f"{s.name}.jpg"), photograph(render(s)))


if __name__ == "__main__":
    write_fixtures(Path(__file__).parent / "fixtures" / "generated")
