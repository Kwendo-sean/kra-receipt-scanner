"""Kenyan VAT rules used by the app, in one place, with sources.

Researched Oct 2026. Every rule here is deterministic code; the language model
never decides any of these. Rates are a dated, editable table: the app trusts
the rate *printed on the receipt* and only uses this table to sanity-check it.

Sources:
  [KRA-VAT]   https://www.kra.go.ke/individual/filing-paying/types-of-taxes/value-added-tax
  [KRA-BRO]   KRA VAT brochure (kra.go.ke, "VAT" publications)
  [GT-PETRO]  Grant Thornton Kenya - temporary 8% VAT on petroleum products (LN 69 & 70 of 2026)
  [RISITI]    Risiti - eTIMS tax types / band letters
  [SALAMI]    Salami - eTIMS invoice requirements
  [FNJ]       FNJ & Associates - Kenya VAT guide
  [COMMENDA]  Commenda - Kenya VAT guide
"""
from __future__ import annotations

import calendar
import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from app import config

RULES_RESEARCHED = "2026-10"
DISCLAIMER = (
    "This report helps you prepare your VAT return. It is not tax advice; "
    "confirm with KRA or a tax professional before filing."
)

# ---------------------------------------------------------------- rates


@dataclass
class RateEntry:
    key: str
    rate: float  # percent
    label: str
    effective_from: str | None  # ISO date or None
    effective_to: str | None  # ISO date or None (None = open-ended / unknown)
    note: str
    source: str


DEFAULT_RATES: list[RateEntry] = [
    RateEntry("standard", 16.0, "Standard rate", None, None,
              "Standard VAT rate.", "KRA-VAT"),
    RateEntry("petroleum", 8.0, "Petroleum products (temporary)", "2026-04", None,
              "Temporary 8% on premium motor spirit, diesel and illuminating kerosene under "
              "Legal Notices 69 and 70 of 2026, extended at least once. Sources disagree on the "
              "expiry date, so none is hard-coded: the app trusts the rate printed on the receipt.",
              "GT-PETRO"),
    RateEntry("zero", 0.0, "Zero-rated", None, None,
              "Zero-rated supplies carry 0% VAT.", "KRA-VAT"),
    RateEntry("exempt", 0.0, "Exempt", None, None,
              "Exempt supplies carry no VAT and give no input-VAT credit.", "KRA-VAT"),
]


def _rates_file() -> Path:
    return config.settings.data_dir / "rates.json"


def load_rates() -> list[RateEntry]:
    """Rate table; user edits (data/rates.json) override the defaults."""
    p = _rates_file()
    if p.is_file():
        try:
            return [RateEntry(**r) for r in json.loads(p.read_text(encoding="utf-8"))]
        except (ValueError, TypeError):
            pass
    return list(DEFAULT_RATES)


def save_rates(rates: list[RateEntry]) -> None:
    config.settings.ensure_dirs()
    _rates_file().write_text(json.dumps([asdict(r) for r in rates], indent=2), encoding="utf-8")


def known_rates() -> set[float]:
    return {r.rate for r in load_rates()}


def is_known_rate(rate: float) -> bool:
    return any(abs(rate - r) < 0.01 for r in known_rates())


# eTIMS band letters. Sources agree on A and B only; C/D/E differ between
# sources (zero-rated / non-VAT / legacy 8%). We record letters but NEVER map a
# letter to a rate - the printed rate is used. [RISITI][SALAMI]
BAND_LETTERS_AGREED = {"A": "Exempt", "B": "16%"}
BAND_LETTERS_DISPUTED = {"C", "D", "E"}

# ---------------------------------------------------------------- tolerances
VAT_TOLERANCE_KES = 1.00  # taxable x rate vs printed VAT, per band
TOTAL_TOLERANCE_KES = 1.00

# ---------------------------------------------------------------- deadlines

VAT3_DUE_DAY = 20  # VAT3 return + payment due by the 20th of the following month [KRA-VAT]
CLAIM_WINDOW_MONTHS = 6  # input VAT claimable within 6 months after end of tax period [KRA-VAT][FNJ]
CLOSING_SOON_DAYS = 30


def add_months(year: int, month: int, n: int) -> tuple[int, int]:
    idx = year * 12 + (month - 1) + n
    return idx // 12, idx % 12 + 1


def last_day(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def vat3_due_date(year: int, month: int) -> date:
    """VAT3 for tax period (year, month) is due the 20th of the next month."""
    y, m = add_months(year, month, 1)
    return date(y, m, VAT3_DUE_DAY)


def claim_deadline(purchase: date) -> date:
    """Last day input VAT on this purchase can be claimed: end of the 6th month
    after the end of the month (tax period) the purchase was made in."""
    y, m = add_months(purchase.year, purchase.month, CLAIM_WINDOW_MONTHS)
    return last_day(y, m)


def claim_window_status(purchase: date, today: date) -> str:
    """'open' | 'closing_soon' | 'closed'."""
    dl = claim_deadline(purchase)
    if today > dl:
        return "closed"
    if (dl - today).days <= CLOSING_SOON_DAYS:
        return "closing_soon"
    return "open"


# ---------------------------------------------------------------- claimability


@dataclass
class Claimability:
    claimable: bool
    reasons: list[str]  # why not claimable (empty if claimable)
    warnings: list[str]  # claimable but needs attention
    deadline: date | None


RESIDUAL_RISK = (
    "KRA also checks that the supplier declared this sale in its own return; "
    "the app cannot verify that."
)
INCOME_TAX_NOTE = (
    "Since 2024, expenses not backed by an eTIMS invoice may not be deductible for income tax."
)
PENALTY_NOTE = (
    "Late filing / payment penalties and interest differ between sources "
    "(e.g. KES 10,000 or 5% of VAT due; 1% or 2% a month) - check with KRA."
)
NIL_RETURN_NOTE = "A nil VAT3 return is still required for a month with nothing to declare."


def assess(receipt: dict, today: date) -> Claimability:
    """Decide input-VAT claimability from stored (possibly user-corrected) fields.

    Rule: claimable only on an eTIMS-transmitted invoice showing the supplier's
    KRA PIN, with VAT shown, within the claim window. [KRA-VAT][SALAMI]
    """
    reasons: list[str] = []
    warnings: list[str] = []
    if not receipt.get("seller_pin"):
        reasons.append("Supplier KRA PIN not printed")
    if not (receipt.get("cu_invoice_no") or receipt.get("kra_qr_url")):
        reasons.append("No eTIMS / CU invoice number")
    vat = receipt.get("total_vat")
    if vat is None:
        band_vats = [b["vat"] for b in (receipt.get("bands") or []) if b.get("vat") is not None]
        vat = sum(band_vats) if band_vats else None
    if vat is None:
        reasons.append("No VAT shown")
    elif vat <= 0:
        reasons.append("No VAT charged (exempt / zero-rated only)")
    deadline = None
    d = receipt.get("receipt_date")
    if d:
        pd = date.fromisoformat(d) if isinstance(d, str) else d
        deadline = claim_deadline(pd)
        st = claim_window_status(pd, today)
        if st == "closed":
            reasons.append(f"Claim window closed on {deadline.isoformat()}")
        elif st == "closing_soon":
            warnings.append(f"Claim window closes on {deadline.isoformat()}")
    else:
        reasons.append("Receipt date missing")
    # presence flags are already covered by the reasons above; only arithmetic checks add warnings
    flags = {"pin_present", "etims_present", "vat_shown", "date_present", "total_present"}
    for c in receipt.get("checks", []) or []:
        if c.get("id") in flags:
            continue
        if c.get("ok") is False and c.get("severity") == "error":
            warnings.append(f"Check failed: {c.get('label')}")
    return Claimability(not reasons, reasons, warnings, deadline)


ACTIONS = {
    "Supplier KRA PIN not printed": "If the photo shows the supplier's KRA PIN, enter it; otherwise ask the "
                                    "supplier for an eTIMS invoice showing their PIN.",
    "No eTIMS / CU invoice number": "If the photo shows a CU invoice number, enter it; otherwise ask the supplier "
                                    "to issue an eTIMS invoice for this purchase.",
    "No VAT shown": "If VAT was charged, request a tax invoice that states the VAT; otherwise it cannot be claimed.",
    "No VAT charged (exempt / zero-rated only)": "Nothing to claim; keep as an expense record.",
    "Receipt date missing": "Enter the receipt date from the photo.",
}


def action_for(reason: str) -> str:
    if reason.startswith("Claim window closed"):
        return "Too late to claim this input VAT; keep it as an expense record."
    if reason.startswith("Claim window closes"):
        return "Include it in your next VAT3 return before the deadline."
    if reason.startswith("Duplicate of"):
        return "Delete this copy if it is the same receipt scanned twice; it is left out of the totals."
    if reason.startswith("Check failed"):
        return "Compare the figures with the photo and correct any misread field."
    return ACTIONS.get(reason, "Review this receipt against the photo.")


SOURCES = {
    "KRA-VAT": "https://www.kra.go.ke/individual/filing-paying/types-of-taxes/value-added-tax",
    "KRA-BRO": "KRA VAT brochure (kra.go.ke publications)",
    "GT-PETRO": "Grant Thornton Kenya: temporary VAT rate on petroleum products (LN 69/70 of 2026)",
    "RISITI": "Risiti: eTIMS tax types",
    "SALAMI": "Salami: eTIMS invoices",
    "FNJ": "FNJ & Associates: Kenya VAT guide",
    "COMMENDA": "Commenda: Kenya VAT guide",
}
