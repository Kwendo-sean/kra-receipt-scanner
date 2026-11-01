"""Fill faded figures from the receipt's own arithmetic - never estimates.

A value is filled only when (a) it follows exactly from other *printed* numbers
and (b) an independent printed number confirms it (within KES 1). Otherwise the
field stays missing. Every filled value is recorded in `derived` with its working.
"""
from __future__ import annotations

from decimal import Decimal

TOL = Decimal("1.00")


def _d(v) -> Decimal | None:
    return None if v is None else Decimal(str(v))


def _f(v: Decimal) -> float:
    return float(v.quantize(Decimal("0.01")))


def derive(p: dict) -> dict:
    """Mutates and returns the parsed dict; adds p['derived'] = [{field, value, how}]."""
    out = p.setdefault("derived", [])
    bands = p.get("bands") or []
    total = _d(p.get("total"))
    stated_vat = _d(p.get("total_vat"))

    def note(field, value, how):
        out.append({"field": field, "value": _f(value), "how": how})

    # 1. taxable missing, gross + VAT printed on the band line: taxable = gross - VAT,
    #    confirmed by taxable x printed rate = printed VAT
    for b in bands:
        tx, vat, gross, rate = _d(b.get("taxable")), _d(b.get("vat")), _d(b.get("gross")), _d(b.get("rate"))
        if tx is None and vat is not None and gross is not None and rate is not None:
            cand = gross - vat
            if abs(cand * rate / 100 - vat) <= TOL:
                b["taxable"] = _f(cand)
                b["source"] = (b.get("source") or "") + " +derived taxable"
                note(f"band {b.get('band') or ''} {b['rate']:g}% taxable", cand,
                     f"gross {gross} - VAT {vat}; check {cand} x {rate}% = VAT")

    # 1b. a band line printed one amount only (taxable or gross?): accept the
    #     reading the receipt total proves, confirmed by rate / stated VAT
    if total is not None:
        for b in bands:
            x, rate = _d(b.get("gross")), _d(b.get("rate"))
            if x is None or rate is None or b.get("taxable") is not None or b.get("vat") is not None:
                continue
            others = [o for o in bands if o is not b]
            if any(o.get("taxable") is None or o.get("vat") is None for o in others):
                continue
            known = sum(_d(o["taxable"]) + _d(o["vat"]) for o in others)
            vat_c = total - known - x            # reading 1: X is the taxable value
            if vat_c >= 0 and abs(x * rate / 100 - vat_c) <= TOL:
                b["taxable"], b["vat"], b["gross"] = _f(x), _f(vat_c), _f(x + vat_c)
                b["source"] = (b.get("source") or "") + " +derived vat"
                note(f"band {b.get('band') or ''} {b['rate']:g}% VAT", vat_c,
                     f"total {total} - other bands {known} - taxable {x}; check {x} x {rate}%")
                continue
            if abs(known + x - total) <= TOL and stated_vat is not None:   # reading 2: X is gross
                vat_c = stated_vat - sum(_d(o["vat"]) for o in others)
                if abs((x - vat_c) * rate / 100 - vat_c) <= TOL:
                    b["taxable"], b["vat"] = _f(x - vat_c), _f(vat_c)
                    b["source"] = (b.get("source") or "") + " +derived taxable/vat"
                    note(f"band {b.get('band') or ''} {b['rate']:g}% taxable/VAT", x - vat_c,
                         f"gross {x} - VAT {vat_c} (stated VAT less other bands); check x {rate}%")

    # 2. exactly one band figure missing: the receipt total fixes it, and the
    #    printed rate (or stated total VAT) must confirm it
    if total is not None and bands:
        missing = [(b, k) for b in bands for k in ("taxable", "vat")
                   if b.get(k) is None and b.get("rate") is not None]
        if len(missing) == 1:
            b, k = missing[0]
            known = sum((_d(x.get("taxable")) or 0) + (_d(x.get("vat")) or 0) for x in bands)
            cand = total - known
            rate = _d(b["rate"])
            other = _d(b.get("vat" if k == "taxable" else "taxable"))
            ok = False
            if cand >= 0 and other is not None:
                ok = (abs(cand * rate / 100 - other) <= TOL) if k == "taxable" else (abs(other * rate / 100 - cand) <= TOL)
            if not ok and k == "vat" and stated_vat is not None:
                others = sum(_d(x.get("vat")) or 0 for x in bands if x is not b)
                ok = cand >= 0 and abs(others + cand - stated_vat) <= TOL
            if ok:
                b[k] = _f(cand)
                b["source"] = (b.get("source") or "") + f" +derived {k}"
                note(f"band {b.get('band') or ''} {b['rate']:g}% {k}", cand,
                     f"total {total} - other printed band figures {known}; confirmed by the printed rate/VAT")

    # 3. single band whose amounts are both gone: total and stated VAT give them,
    #    confirmed by the printed rate
    if total is not None and stated_vat is not None and len(bands) == 1:
        b = bands[0]
        rate = _d(b.get("rate"))
        if b.get("taxable") is None and b.get("vat") is None and rate:
            cand = total - stated_vat
            if abs(cand * rate / 100 - stated_vat) <= TOL:
                b["taxable"], b["vat"] = _f(cand), _f(stated_vat)
                b["source"] = (b.get("source") or "") + " +derived from totals"
                note(f"band {b.get('band') or ''} {b['rate']:g}% taxable/VAT", cand,
                     f"total {total} - total VAT {stated_vat}; check x {rate}% = VAT")

    # 4. total missing: complete bands sum to it, confirmed by the line items
    if total is None and bands and all(b.get("taxable") is not None and b.get("vat") is not None for b in bands):
        cand = sum(_d(b["taxable"]) + _d(b["vat"]) for b in bands)
        items = [i for i in p.get("items") or [] if i.get("line_total") is not None]
        if items and abs(sum(_d(i["line_total"]) for i in items) - cand) <= TOL:
            p["total"] = _f(cand)
            note("total", cand, "sum of printed tax bands; confirmed by the sum of line items")
    return p
