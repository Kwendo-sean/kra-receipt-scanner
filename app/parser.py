"""Deterministic receipt parser: rebuilt OCR lines -> structured KRA/VAT fields.

Nothing is guessed. A field that is not printed stays None and is flagged by the
checks. Rates come from the receipt, never from band letters.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal

from app import textparse as tp

KEY_TOTAL_STRONG = re.compile(r"\b(GRAND\s*TOTAL|TOTAL\s*(?:AMOUNT|KES|KSH|DUE|PAYABLE)|AMOUNT\s*(?:DUE|PAYABLE))\b", re.I)
KEY_TOTAL = re.compile(r"(?<![A-Z])TOTAL(?![A-Z])", re.I)
KEY_NOT_TOTAL = re.compile(r"SUB\s*-?\s*TOTAL|TOTAL\s*(?:TAX|VAT|ITEMS?|QTY|LINES?|DISCOUNT|SAVINGS?)|"
                           r"TOTAL\s*(?:BEFORE|AFTER)\s*DISC|TOTAL\s+[A-E]\s*[-=]|TAX\s*TOTAL|VAT\s*TOTAL|NO\.?\s*OF\s*ITEMS", re.I)
KEY_TOTAL_VAT = re.compile(r"TOTAL\s*(?:TAX|VAT)(?!\s*[A-E]\b)(?!\s*[A-E]\s*[-:])|(?:TAX|VAT)\s*TOTAL|VAT\s*AMOUNT|TOTAL\s*VAT\s*AMOUNT"
                           # a bare "VAT  1,451.72" line in the totals block
                           r"|^\s*(?:VAT|TAX)\s*:?\s*(?:KES|KSH)?\s*-?\d[\d,]*\.\d{2}\s*$", re.I)
KEY_ITEMS_END = re.compile(r"SUB\s*-?\s*TOTAL|(?<![A-Z])TOTAL(?![A-Z])|TAXABLE|VATABLE|TAX\s*(?:CODE|SUMMARY|RATE|DETAILS)|VAT\s*(?:SUMMARY|ANALYSIS|RATE|CODE|%)|^\s*RATE\b|AMOUNT\s*DUE", re.I)
KEY_SKIP_ITEM = re.compile(r"CASH|CHANGE|M-?PESA|CARD|VISA|MASTER|TENDER|BALANCE|PIN\b|TEL|PHONE|DATE|TIME|TILL|CASHIER|RECEIPT|INVOICE|SERIAL|SIGNATURE|THANK|WELCOME|ROUNDING|DISCOUNT", re.I)
KEY_HEADER_SKIP = re.compile(r"PIN|TEL|PHONE|P\.?O\.?\s*BOX|BOX\s*\d|EMAIL|@|WWW\.|TAX\s*INVOICE|RECEIPT|INVOICE|DATE|TIME|CASHIER|TILL|VAT\s*NO|KRA|^\W*$|ROAD|STREET|\bRD\b|\bST\b|BRANCH|BUILDING|FLOOR|AVENUE|HOUSE|WELCOME", re.I)
KEY_BUYER = re.compile(r"BUYER|CUSTOMER|CLIENT|PURCHASER|BILL\s*TO", re.I)

LBL_RECEIPT_NO = re.compile(r"(?:RECEIPT|RCPT|INVOICE|INV|BILL|TRANS(?:ACTION)?|TXN|DOC(?:UMENT)?)\s*(?:NO|NUMBER|NUM|#)\.?\s*[:#.\-]?\s*([A-Z0-9][A-Z0-9/\-]{2,})", re.I)
LBL_CU_INV = re.compile(r"(?:CU\s*INV(?:OICE)?\s*(?:NO|NUMBER|NUM)?|CUIN|CONTROL\s*UNIT\s*INVOICE\s*(?:NO|NUMBER)?|TRADER\s*INVOICE\s*NO)\.?\s*[:#.\-]?\s*([A-Z0-9][A-Z0-9/\-]{5,})", re.I)
LBL_CU_SERIAL = re.compile(r"(?:CU\s*(?:SERIAL|S/?N)\s*(?:NO|NUMBER)?|SCU\s*(?:ID|NO)?|CONTROL\s*UNIT\s*(?:SERIAL|ID)\s*(?:NO|NUMBER)?|CU\s*ID)\.?\s*[:#.\-]?\s*([A-Z0-9][A-Z0-9/\-]{5,})", re.I)
LBL_SIGNATURE = re.compile(r"(?:RECEIPT\s*SIGNATURE|SIGNATURE|INTERNAL\s*DATA)\s*[:#.\-]?\s*([A-Z0-9][A-Z0-9\-/+=]{5,})", re.I)

ITEM_QTY = re.compile(
    r"^(?P<desc>.*?)\s*(?P<qty>\d+(?:\.\d{1,3})?)\s*(?:PCS|PC|EA|KG|G|L|LT|LTR|LTRS|LITRES?)?\s*[xX@*×]\s*"
    r"(?P<unit>\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+\.\d{2})\s*(?:=\s*)?"
    r"(?P<total>-?\d{1,3}(?:,\d{3})*\.\d{2}|-?\d+\.\d{2})\s*(?P<band>[A-E])?\s*$",
    re.I,
)
ITEM_SIMPLE = re.compile(r"^(?P<desc>.*?[A-Za-z].*?)\s+(?P<total>-?\d{1,3}(?:,\d{3})*\.\d{2}|-?\d+\.\d{2})\s*(?P<band>[A-E])?\s*$")
BAND_TAG = re.compile(r"(?<![A-Z])([A-E])\s*[-=:]?\s*(?:(\d{1,2}(?:\.\d{1,2})?)\s?%|EX(?:EMPT(?:ED)?)?|Z(?:ERO)?|NON)", re.I)
EXEMPT_WORD = re.compile(r"\bEX(?:EMPT(?:ED)?)?\b|\bA\s*-\s*EX\b", re.I)
ZERO_WORD = re.compile(r"ZERO\s*-?\s*RATED", re.I)


def D(v) -> float | None:
    return None if v is None else float(tp.q2(v))


@dataclass
class Item:
    description: str
    quantity: float | None
    unit_price: float | None
    line_total: float
    band: str | None


@dataclass
class Band:
    band: str | None
    rate: float | None  # percent, as printed
    taxable: float | None
    vat: float | None
    gross: float | None = None
    source: str = ""  # which printed line(s) it came from


@dataclass
class ParsedReceipt:
    seller_name: str | None = None
    seller_pin: str | None = None
    buyer_pin: str | None = None
    receipt_no: str | None = None
    cu_invoice_no: str | None = None
    cu_serial_no: str | None = None
    receipt_signature: str | None = None
    kra_qr_url: str | None = None
    receipt_date: str | None = None
    receipt_time: str | None = None
    items: list[Item] = field(default_factory=list)
    bands: list[Band] = field(default_factory=list)
    total: float | None = None
    total_vat: float | None = None  # as printed (None if not printed)
    payment_method: str | None = None
    mpesa_code: str | None = None
    notes: list[str] = field(default_factory=list)
    derived: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ----------------------------------------------------------------------------


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip(" :-.#")


def _find_label(lines: list[str], rx: re.Pattern) -> str | None:
    for i, ln in enumerate(lines):
        m = rx.search(ln)
        if m:
            return m.group(1).strip().upper().rstrip(".")
        # label alone on a line, value on the next
        lbl = rx.pattern.split(r"\.?\s*[:#.\-]?\s*(")[0]
        if re.search(lbl + r"\.?\s*[:#.\-]?\s*$", ln, re.I) and i + 1 < len(lines):
            m2 = re.match(r"\s*([A-Z0-9][A-Z0-9/\-]{5,})\s*$", lines[i + 1], re.I)
            if m2:
                return m2.group(1).upper()
    return None


def _seller_name(lines: list[str]) -> str | None:
    for ln in lines[:8]:
        s = ln.strip()
        if sum(c.isalpha() for c in s) < 3:
            continue
        if KEY_HEADER_SKIP.search(s) or tp.amounts(s) or tp.find_date(s):
            continue
        return _clean(s)
    return None


def _pins(lines: list[str]) -> tuple[str | None, str | None, list[str]]:
    seller = buyer = None
    notes = []
    for i, ln in enumerate(lines):
        pins = tp.find_pins(ln)
        if not pins:
            continue
        ctx = ln if KEY_BUYER.search(ln) else ""
        if not ctx and i > 0 and KEY_BUYER.search(lines[i - 1]) and not tp.find_pins(lines[i - 1]):
            ctx = lines[i - 1]
        for p in pins:
            if ctx:
                buyer = buyer or p
            elif seller is None:
                seller = p
            elif p != seller and buyer is None and KEY_BUYER.search(" ".join(lines[max(0, i - 1): i + 1])):
                buyer = p
    if seller and buyer and seller == buyer:
        notes.append("Seller and buyer PIN identical")
    return seller, buyer, notes


def _date_time(lines: list[str]) -> tuple[str | None, str | None]:
    labelled = [ln for ln in lines if re.search(r"DATE", ln, re.I)]
    for group in (labelled, lines):
        for ln in group:
            d = tp.find_date(ln)
            if d:
                t = tp.find_time(ln)
                if t is None:
                    for ln2 in lines:
                        if re.search(r"TIME", ln2, re.I):
                            t = tp.find_time(ln2)
                            if t:
                                break
                return d.isoformat(), (t.strftime("%H:%M:%S") if t else None)
    return None, None


def _payment(lines: list[str]) -> tuple[str | None, str | None]:
    method = code = None
    for i, ln in enumerate(lines):
        u = ln.upper()
        if re.search(r"M-?\s?PESA|MPESA|LIPA\s*NA", u):
            method = "M-PESA"
            code = tp.find_mpesa_code(re.sub(r"M-?\s?PESA", " ", u))
            if not code and i + 1 < len(lines):
                code = tp.find_mpesa_code(lines[i + 1])
            break
        if re.search(r"(?<![A-Z])(VISA|MASTERCARD|MASTER\s*CARD|CARD|DEBIT|CREDIT\s*CARD|PDQ)(?![A-Z])", u):
            method = method or "Card"
        elif re.search(r"(?<![A-Z])CASH(?![A-Z])", u) and method is None:
            method = "Cash"
    return method, code


def _total(lines: list[str]) -> float | None:
    strong, plain = [], []
    for ln in lines:
        if KEY_NOT_TOTAL.search(ln):
            continue
        a = tp.amounts(ln)
        if not a:
            continue
        if KEY_TOTAL_STRONG.search(ln):
            strong.append(a[-1])
        elif KEY_TOTAL.search(ln):
            plain.append(a[-1])
    if strong:
        return D(strong[0])
    if plain:
        return D(plain[0])
    return None


def _total_vat(lines: list[str]) -> float | None:
    for ln in lines:
        if KEY_TOTAL_VAT.search(ln):
            a = tp.amounts(ln)
            if a:
                return D(a[-1])
    return None


def _items_region(lines: list[str]) -> tuple[int, int]:
    """Items sit between the header (after last date/PIN/receipt line near top)
    and the first totals/tax-summary line."""
    end = len(lines)
    for i, ln in enumerate(lines):
        if ITEM_QTY.match(ln.strip()):
            continue
        # a totals/tax line ends the items even if its amount faded away
        if KEY_ITEMS_END.search(ln) or (BAND_TAG.search(ln) and tp.find_rate(ln) is not None):
            end = i
            break
    start = 0
    for i, ln in enumerate(lines[:end]):
        if re.search(r"PIN|DATE|TIME|RECEIPT|INVOICE|CASHIER|TILL|DESCRIPTION|ITEM|QTY", ln, re.I) and not ITEM_QTY.match(ln):
            start = i + 1
    return start, end


def _items(lines: list[str]) -> list[Item]:
    start, end = _items_region(lines)
    items: list[Item] = []
    pending_desc: str | None = None
    for ln in lines[start:end]:
        s = ln.strip()
        if not s:
            continue
        m = ITEM_QTY.match(s)
        if m:
            desc = _clean(m.group("desc")) or (pending_desc or "")
            if not re.search(r"[A-Za-z]", desc) and pending_desc:
                desc = pending_desc
            items.append(Item(desc or "(item)", float(m.group("qty")),
                              D(tp.parse_money(m.group("unit") + (".00" if "." not in m.group("unit") else ""))),
                              D(tp.parse_money(m.group("total"))), (m.group("band") or None)))
            pending_desc = None
            continue
        if KEY_SKIP_ITEM.search(s):
            pending_desc = None
            continue
        m = ITEM_SIMPLE.match(s)
        if m:
            items.append(Item(_clean(m.group("desc")), None, None,
                              D(tp.parse_money(m.group("total"))), (m.group("band") or None)))
            pending_desc = None
            continue
        if re.search(r"[A-Za-z]{2,}", s) and not tp.amounts(s):
            pending_desc = _clean(s)
    return items


def _infer_band_amounts(rate: Decimal | None, vals: list[Decimal]) -> tuple:
    """Assign printed amounts on a band line to (taxable, vat, gross) by checking
    which reading is arithmetically consistent with the printed rate. Returns
    (taxable, vat, gross, how)."""
    tol = Decimal("1.00")
    if rate is None:
        return None, None, None, "no rate"
    r = rate / 100
    if len(vals) >= 3:
        a, b, c = vals[-3], vals[-2], vals[-1]
        if abs(a * r - b) <= tol and abs(a + b - c) <= tol:
            return a, b, c, "taxable,vat,gross"
        if abs(b * r - c) <= tol:
            return b, c, None, "…,taxable,vat"
        return a, b, c, "taxable,vat,gross (unchecked)"
    if len(vals) == 2:
        a, b = vals
        if rate == 0:
            if b == 0:
                return a, b, None, "taxable,vat"
            if a == 0:
                return b, a, None, "vat,taxable"
            if a == b:
                # some tills repeat the amount in the VAT column of a 0% band;
                # 0% of anything is 0, so that is not a VAT figure
                return a, Decimal("0"), None, "taxable (0% band; VAT column repeats the amount)"
        if abs(a * r - b) <= tol:
            return a, b, None, "taxable,vat"
        if r and abs(a * r / (1 + r) - b) <= tol:
            return a - b, b, a, "gross,vat"
        if abs(b * r - a) <= tol:
            return b, a, None, "vat,taxable"
        return a, b, None, "taxable,vat (unchecked)"
    if len(vals) == 1:
        if rate == 0:
            return vals[0], Decimal("0"), None, "exempt/zero amount"
        return None, None, vals[0], "single amount"
    return None, None, None, "no amounts"


def _bands(lines: list[str], items_end: int) -> tuple[list[Band], list[str]]:
    notes: list[str] = []
    bands: dict[tuple, Band] = {}
    tax_by_letter: dict[str, Decimal] = {}
    pending_single: dict[str, tuple[Decimal | None, Decimal]] = {}
    region = lines[max(0, items_end - 1):]

    # eTIMS style: "TOTAL TAX B 800.00" / "TAX B 800.00"
    for ln in region:
        m = re.search(r"(?:TOTAL\s*)?(?:TAX|VAT)\s*([A-E])(?![A-Z])", ln, re.I)
        if m and not tp.find_rate(ln):
            a = tp.amounts(ln)
            if a:
                tax_by_letter[m.group(1).upper()] = a[-1]

    for ln in region:
        if re.search(r"(?:TOTAL\s*)?(?:TAX|VAT)\s*[A-E](?![A-Z])", ln, re.I) and not tp.find_rate(ln):
            continue
        if KEY_TOTAL_VAT.search(ln) and not tp.find_rate(ln):
            continue
        letter = None
        rate = None
        mt = BAND_TAG.search(ln)
        if mt:
            letter = mt.group(1).upper()
            if mt.group(2):
                rate = Decimal(mt.group(2))
            elif re.search(r"EX|Z|NON", mt.group(0)[1:], re.I):
                rate = Decimal("0")
        if rate is None:
            rate = tp.find_rate(ln)
        if rate is None and (EXEMPT_WORD.search(ln) or ZERO_WORD.search(ln)):
            rate = Decimal("0")
        if rate is None:
            continue
        # amounts excluding the rate itself (rate has %; money requires decimals)
        txt = tp.RATE_RE.sub(" ", ln)
        vals = tp.amounts(txt)
        if not vals:
            # band tag + printed rate but the amounts faded: keep the band so the
            # receipt's arithmetic can still recover it (derive.py)
            if mt and (letter, float(rate)) not in bands:
                bands[(letter, float(rate))] = Band(letter, float(rate), None, None, None, "amounts unreadable")
            continue
        if KEY_TOTAL_STRONG.search(ln) and not mt:
            continue
        taxable, vat, gross, how = _infer_band_amounts(rate, vals)
        if how == "single amount":
            pending_single[letter or str(rate)] = (rate, vals[0])
            continue
        if taxable is None and vat is None:
            continue
        key = (letter, float(rate))
        if key in bands:
            continue
        bands[key] = Band(letter, float(rate), D(taxable), D(vat), D(gross), how)
        if "unchecked" in how:
            notes.append(f"Band {letter or ''} {rate}%: amounts did not reconcile; stored as printed order")

    # single-amount band lines paired with "TAX <letter>" (eTIMS)
    for k, (rate, amt) in pending_single.items():
        letter = k if len(k) == 1 and k.isalpha() else None
        tax = tax_by_letter.get(letter) if letter else None
        if tax is None:
            # Unpaired single amount: treat as taxable only if VAT line elsewhere matches
            bands[(letter, float(rate))] = Band(letter, float(rate), None, None, D(amt), "single amount (gross?)")
            notes.append(f"Band {letter or ''} {rate}%: only one amount printed")
            continue
        r = rate / 100
        if r and abs(amt * r / (1 + r) - tax) <= Decimal("1.00"):
            bands[(letter, float(rate))] = Band(letter, float(rate), D(amt - tax), D(tax), D(amt), "gross + TAX line")
        elif abs(amt * r - tax) <= Decimal("1.00"):
            bands[(letter, float(rate))] = Band(letter, float(rate), D(amt), D(tax), D(amt + tax), "taxable + TAX line")
        else:
            bands[(letter, float(rate))] = Band(letter, float(rate), None, D(tax), D(amt), "unreconciled TAX line")
            notes.append(f"Band {letter} {rate}%: amount and TAX line do not reconcile")

    # "VATABLE AMT 4,310.34" + "VAT 16% 689.66" printed on separate lines
    if not any(b.taxable is not None and b.vat is not None for b in bands.values()):
        taxable_amt = None
        for ln in region:
            if re.search(r"VATABLE|TAXABLE|NET\s*AMOUNT", ln, re.I) and not tp.find_rate(ln):
                a = tp.amounts(ln)
                if a:
                    taxable_amt = a[-1]
        if taxable_amt is not None:
            for key, b in list(bands.items()):
                if b.rate and b.taxable is not None and b.vat is not None and "unchecked" in b.source:
                    pass
            # find a VAT line with rate and a single amount
            for ln in region:
                rate = tp.find_rate(ln)
                if rate and re.search(r"VAT|TAX", ln, re.I):
                    a = tp.amounts(tp.RATE_RE.sub(" ", ln))
                    if len(a) == 1:
                        bands = {(None, float(rate)): Band(None, float(rate), D(taxable_amt), D(a[0]), None, "VATABLE + VAT lines")}
                        break
    return list(bands.values()), notes


_NO_CENTS = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+)(?![\d.,])")


def _mark_cut_amounts(lines: list[str]) -> tuple[list[str], int]:
    """On a receipt that prints cents, '6,983' is almost certainly '6,983.31' with
    faded cents. Make such tokens unreadable (field stays missing and is flagged)
    instead of storing a wrong figure."""
    spans = [m for ln in lines for m in tp._MONEY.finditer(tp._MONEY_GLUED.sub(lambda x: x.group(0) + " ", ln))]
    if len(spans) < 3:
        return lines, 0
    with_cents = sum(1 for m in spans if "." in m.group(2))
    if with_cents / len(spans) < 0.8:
        return lines, 0
    n = 0
    out = []
    for ln in lines:
        new, k = _NO_CENTS.subn(lambda m: m.group(1).replace(",", " ") + "?", ln)
        n += k
        out.append(new)
    return out, n


def parse_lines(lines: list[str], kra_qr_url: str | None = None) -> ParsedReceipt:
    lines = [tp.unglue_amounts(ln) for ln in (l.strip() for l in lines) if ln]
    lines, cut = _mark_cut_amounts(lines)
    r = ParsedReceipt()
    if cut:
        r.notes.append(f"{cut} amount(s) printed without cents look faded; left unread - check the photo")
    r.seller_name = _seller_name(lines)
    r.seller_pin, r.buyer_pin, notes = _pins(lines)
    r.notes.extend(notes)
    r.cu_invoice_no = _find_label(lines, LBL_CU_INV)
    r.cu_serial_no = _find_label(lines, LBL_CU_SERIAL)
    r.receipt_signature = _find_label(lines, LBL_SIGNATURE)
    rn = None
    for ln in lines:
        if re.search(r"CU\s*INV|CONTROL\s*UNIT|TRADER\s*INVOICE", ln, re.I):
            continue
        m = LBL_RECEIPT_NO.search(ln)
        if m:
            rn = m.group(1).upper()
            break
    r.receipt_no = rn
    r.receipt_date, r.receipt_time = _date_time(lines)
    r.items = _items(lines)
    _, items_end = _items_region(lines)
    r.bands, notes = _bands(lines, items_end)
    r.notes.extend(notes)
    r.total = _total(lines)
    r.total_vat = _total_vat(lines)
    r.payment_method, r.mpesa_code = _payment(lines)
    r.kra_qr_url = kra_qr_url
    if kra_qr_url and not r.cu_invoice_no:
        m = re.search(r"(?:invoiceNo|invNo|cuin|invoice)=([A-Za-z0-9/\-]+)", kra_qr_url, re.I)
        if m:
            r.cu_invoice_no = m.group(1).upper()
            r.notes.append("CU invoice number taken from KRA QR code")
    return r


def parse_and_derive(lines: list[str], kra_qr_url: str | None = None) -> dict:
    """Parse, then fill faded figures that the receipt's own arithmetic proves."""
    from app.derive import derive
    return derive(parse_lines(lines, kra_qr_url).to_dict())


def parse_text(text: str, kra_qr_url: str | None = None) -> ParsedReceipt:
    return parse_lines(text.splitlines(), kra_qr_url)
