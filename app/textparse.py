"""Low-level token parsers: money, dates, times, KRA PINs, M-PESA codes.

Written for OCR quirks such as merged tokens: 'KES6,858.00',
'Date:21-10-202608:15', 'KRAPIN: P000611133Q'.
"""
from __future__ import annotations

import re
from datetime import date, time
from decimal import Decimal, InvalidOperation

# Money must carry decimals or thousands separators, so phone numbers,
# quantities and PINs are never read as money.
_MONEY = re.compile(
    # a minus counts only when it stands alone before the digits ("-50.00"),
    # not as a separator like "B-16%" / "E- 6,351.85"
    r"(?<![\d.,])(?:(?<![A-Za-z0-9%])(-)(?=\d))?(?:(?:KES|KSH|KSHS|SH|SHS)\.?\s?)?"
    r"(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+\.\d{2})(?![\d])",
    re.IGNORECASE,
)
# Allow the currency prefix to be glued on (KES6,858.00)
_MONEY_GLUED = re.compile(r"(?:KES|KSH|KSHS)(?=\d)", re.IGNORECASE)
# Two columns read as one: "43,645.696,983.31" -> "43,645.69 6,983.31" (an amount
# ends at its 2 decimals, so digits right after them start the next amount)
_AMOUNTS_GLUED = re.compile(r"(\.\d{2})(?=\d{1,3}(?:,\d{3})*\.\d{2}(?!\d))")


def money_spans(text: str) -> list[tuple[Decimal, int, int]]:
    """All money amounts in text with their (start, end) spans."""
    t = _MONEY_GLUED.sub(lambda m: m.group(0) + " ", text)
    t = _AMOUNTS_GLUED.sub(r"\1 ", t)
    out = []
    for m in _MONEY.finditer(t):
        raw = m.group(2).replace(",", "")
        try:
            v = Decimal(raw)
        except InvalidOperation:
            continue
        if m.group(1):
            v = -v
        out.append((v, m.start(2), m.end(2)))
    return out


def amounts(text: str) -> list[Decimal]:
    return [v for v, _, _ in money_spans(text)]


def parse_money(text: str) -> Decimal | None:
    a = amounts(text)
    return a[-1] if a else None


def q2(v: Decimal | float | int) -> Decimal:
    return Decimal(str(v)).quantize(Decimal("0.01"))


# ---------------------------------------------------------------- dates

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

# numeric, DAY FIRST (Kenyan receipts). Year may be glued to the time: 21-10-202608:15
# A 2-digit year must stand alone: "20:!6" / "20!6" are a broken 4-digit year.
_YY = r"\d{2}(?=[\s,.]|$)"
_DMY = re.compile(r"(?<!\d)(\d{1,2})\s?[/\-.]\s?(\d{1,2})\s?[/\-.]\s?(\d{4}(?=\d{1,2}:\d{2}|\D|$)|" + _YY + ")")
_YMD = re.compile(r"(?<!\d)(20\d{2})[/\-.](\d{1,2})[/\-.](\d{1,2})(?!\d)")
_DMONY = re.compile(r"(?<!\d)(\d{1,2})[\s\-/]?([A-Za-z]{3,9})[\s\-/,]*(\d{4}(?!\d)|" + _YY + ")")
# Only the unambiguous OCR slip is repaired: the letter O in a year ("2O26").
# Anything else ("20:!6": was the "!" a 1 or a 2?) is left unread, to be asked.
_O_YEAR = re.compile(r"(?<![\w])(2[0O][0-9O]{2})(?![\w:])")


def _fix_years(text: str) -> str:
    return _O_YEAR.sub(lambda m: m.group(1).replace("O", "0"), text)
_TIME = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AaPp][Mm])?(?!\d)")


def _year(y: str) -> int:
    n = int(y)
    return 2000 + n if n < 100 else n


def _mk(y: int, m: int, d: int) -> date | None:
    try:
        dt = date(y, m, d)
    except ValueError:
        return None
    return dt if 2000 <= dt.year <= 2100 else None


def find_date(text: str) -> date | None:
    text = _fix_years(text)
    for m in _YMD.finditer(text):
        dt = _mk(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if dt:
            return dt
    for m in _DMY.finditer(text):
        dt = _mk(_year(m.group(3)), int(m.group(2)), int(m.group(1)))
        if dt:
            return dt
    for m in _DMONY.finditer(text):
        mon = _MONTHS.get(m.group(2)[:3].lower())
        if mon:
            dt = _mk(_year(m.group(3)), mon, int(m.group(1)))
            if dt:
                return dt
    return None


def find_time(text: str) -> time | None:
    # Remove the date part first so '202608:15' yields 08:15, not 2608:15
    t = _DMY.sub(" ", text)
    t = _YMD.sub(" ", t)
    for m in _TIME.finditer(t):
        hh, mm = int(m.group(1)), int(m.group(2))
        ss = int(m.group(3) or 0)
        ap = (m.group(4) or "").lower()
        if ap == "pm" and hh < 12:
            hh += 12
        if ap == "am" and hh == 12:
            hh = 0
        if 0 <= hh < 24 and 0 <= mm < 60 and 0 <= ss < 60:
            return time(hh, mm, ss)
    return None


# ---------------------------------------------------------------- PINs

# Match on the text as printed - do NOT strip spaces first, or "PIN P05..."
# glues into one token. A = individual, P = non-individual.
PIN_RE = re.compile(r"(?<![A-Z0-9])([AP]\d{9}[A-Z])(?![A-Z0-9])")
# also accept a PIN glued to a preceding label, e.g. 'KRAPIN:P000611133Q' / 'PINP000611133Q'
PIN_GLUED_RE = re.compile(r"PIN[:.#\s]*([AP]\d{9}[A-Z])(?![A-Z0-9])", re.IGNORECASE)


def find_pins(text: str) -> list[str]:
    seen: list[str] = []
    for rx in (PIN_GLUED_RE, PIN_RE):
        for m in rx.finditer(text.upper()):
            p = m.group(1)
            if p not in seen:
                seen.append(p)
    return seen


# ---------------------------------------------------------------- M-PESA

MPESA_CODE_RE = re.compile(r"(?<![A-Z0-9])([A-Z][A-Z0-9]{9})(?![A-Z0-9])")


def find_mpesa_code(text: str) -> str | None:
    for m in MPESA_CODE_RE.finditer(text.upper()):
        c = m.group(1)
        if any(ch.isdigit() for ch in c) and sum(ch.isalpha() for ch in c) >= 3 and not PIN_RE.fullmatch(c):
            return c
    return None


# ---------------------------------------------------------------- rates

RATE_RE = re.compile(r"(\d{1,2}(?:\.\d{1,2})?)\s?%")


def find_rate(text: str) -> Decimal | None:
    m = RATE_RE.search(text)
    return Decimal(m.group(1)) if m else None


def unglue_amounts(text: str) -> str:
    """Put back the space OCR dropped between two adjacent amounts."""
    return _AMOUNTS_GLUED.sub(r"\1 ", text)
