"""Arithmetic and compliance checks on a (possibly user-corrected) receipt.

Every result is stored, pass or fail. Nothing here estimates a missing value.
"""
from __future__ import annotations

from decimal import Decimal

from app import kra_rules as rules


def _d(v) -> Decimal | None:
    return None if v is None else Decimal(str(v))


def _check(cid: str, label: str, ok: bool | None, detail: str, severity: str = "error") -> dict:
    return {"id": cid, "label": label, "ok": ok, "detail": detail, "severity": severity}


def run_checks(r: dict) -> list[dict]:
    out: list[dict] = []
    tol = Decimal(str(rules.VAT_TOLERANCE_KES))
    bands = r.get("bands") or []

    # 1. per band: taxable x rate == VAT (within KES 1)
    for b in bands:
        name = f"{b.get('band') or ''} {b.get('rate')}%".strip()
        tx, vat, rate = _d(b.get("taxable")), _d(b.get("vat")), _d(b.get("rate"))
        if tx is None or vat is None or rate is None:
            out.append(_check(f"band_vat:{name}", f"Band {name}: taxable × rate = VAT", None,
                              "Taxable value or VAT not printed for this band", "warning"))
            continue
        expect = (tx * rate / 100).quantize(Decimal("0.01"))
        diff = abs(expect - vat)
        out.append(_check(f"band_vat:{name}", f"Band {name}: taxable × rate = VAT", diff <= tol,
                          f"{tx} × {rate}% = {expect}; printed VAT {vat} (diff {diff})"))
        if not rules.is_known_rate(float(rate)):
            out.append(_check(f"band_rate:{name}", f"Band {name}: rate is a known Kenyan VAT rate", False,
                              f"Printed rate {rate}% is not in the rate table", "warning"))

    # 2. bands add up to the total
    total = _d(r.get("total"))
    complete = [b for b in bands if b.get("taxable") is not None and b.get("vat") is not None]
    if bands and total is not None and len(complete) == len(bands):
        s = sum((_d(b["taxable"]) + _d(b["vat"]) for b in complete), Decimal("0"))
        diff = abs(s - total)
        out.append(_check("bands_total", "Tax bands add up to the total", diff <= Decimal(str(rules.TOTAL_TOLERANCE_KES)),
                          f"Sum of bands {s}; total {total} (diff {diff})"))
    else:
        out.append(_check("bands_total", "Tax bands add up to the total", None,
                          "Not enough band or total data to check", "warning"))

    # 3. stated total VAT == sum of band VAT
    stated = _d(r.get("total_vat"))
    band_vats = [_d(b["vat"]) for b in bands if b.get("vat") is not None]
    if stated is not None and band_vats:
        s = sum(band_vats, Decimal("0"))
        diff = abs(s - stated)
        out.append(_check("vat_sum", "Stated total VAT = sum of band VAT", diff <= tol,
                          f"Sum of band VAT {s}; stated {stated} (diff {diff})"))
    else:
        out.append(_check("vat_sum", "Stated total VAT = sum of band VAT", None,
                          "Total VAT or band VAT not printed", "warning"))

    # 4. items add up to the total (informational: discounts/rounding may differ)
    items = r.get("items") or []
    if items and total is not None:
        s = sum((_d(i["line_total"]) for i in items if i.get("line_total") is not None), Decimal("0"))
        diff = abs(s - total)
        out.append(_check("items_total", "Line items add up to the total", diff <= Decimal("1.00"),
                          f"Sum of items {s}; total {total} (diff {diff})", "warning"))

    # 5. flags
    out.append(_check("pin_present", "Supplier KRA PIN printed", bool(r.get("seller_pin")),
                      r.get("seller_pin") or "PIN missing"))
    out.append(_check("etims_present", "eTIMS / CU invoice number printed",
                      bool(r.get("cu_invoice_no") or r.get("kra_qr_url")),
                      r.get("cu_invoice_no") or ("KRA QR only" if r.get("kra_qr_url") else "No eTIMS number")))
    vat_shown = r.get("total_vat") is not None or any(b.get("vat") is not None for b in bands)
    out.append(_check("vat_shown", "VAT shown on receipt", vat_shown, "VAT printed" if vat_shown else "No VAT shown"))
    out.append(_check("date_present", "Receipt date printed", bool(r.get("receipt_date")),
                      r.get("receipt_date") or "Date missing"))
    out.append(_check("total_present", "Total printed", r.get("total") is not None,
                      str(r.get("total")) if r.get("total") is not None else "Total missing"))
    return out


def effective_vat(r: dict) -> Decimal | None:
    """VAT used in reports: stated total VAT if printed, else sum of printed band VAT.
    Both are printed figures; nothing is estimated. None if neither is printed."""
    if r.get("total_vat") is not None:
        return _d(r["total_vat"])
    vats = [_d(b["vat"]) for b in (r.get("bands") or []) if b.get("vat") is not None]
    return sum(vats, Decimal("0")) if vats else None


def effective_taxable(r: dict) -> Decimal | None:
    tx = [_d(b["taxable"]) for b in (r.get("bands") or []) if b.get("taxable") is not None]
    return sum(tx, Decimal("0")) if tx else None
