from datetime import date

from app import kra_rules as rules


def test_vat3_due_20th_of_following_month():
    assert rules.vat3_due_date(2026, 10) == date(2026, 11, 20)
    assert rules.vat3_due_date(2026, 12) == date(2027, 1, 20)


def test_claim_deadline_six_months_after_end_of_period():
    # Purchase in Jan 2026 -> period ends 31 Jan -> claim by 31 Jul 2026
    assert rules.claim_deadline(date(2026, 1, 15)) == date(2026, 7, 31)
    assert rules.claim_deadline(date(2026, 8, 31)) == date(2027, 2, 28)
    assert rules.claim_deadline(date(2027, 8, 1)) == date(2028, 2, 29)  # leap year


def test_claim_window_status():
    p = date(2026, 1, 15)
    assert rules.claim_window_status(p, date(2026, 3, 1)) == "open"
    assert rules.claim_window_status(p, date(2026, 7, 10)) == "closing_soon"
    assert rules.claim_window_status(p, date(2026, 8, 1)) == "closed"


def _r(**kw):
    base = dict(seller_pin="P051234567X", cu_invoice_no="0040123450000011111", total_vat=160.0,
                receipt_date="2026-10-01", bands=[{"band": "B", "rate": 16.0, "taxable": 1000.0, "vat": 160.0}])
    base.update(kw)
    return base


def test_claimable_needs_pin_etims_vat_and_window():
    today = date(2026, 11, 1)
    assert rules.assess(_r(), today).claimable
    c = rules.assess(_r(seller_pin=None), today)
    assert not c.claimable and "Supplier KRA PIN not printed" in c.reasons
    c = rules.assess(_r(cu_invoice_no=None), today)
    assert not c.claimable and "No eTIMS / CU invoice number" in c.reasons
    c = rules.assess(_r(total_vat=None, bands=[]), today)
    assert not c.claimable and "No VAT shown" in c.reasons
    c = rules.assess(_r(receipt_date="2026-01-10"), today)
    assert not c.claimable and c.reasons[0].startswith("Claim window closed")


def test_kra_qr_counts_as_etims_evidence():
    c = rules.assess(_r(cu_invoice_no=None, kra_qr_url="https://itax.kra.go.ke/x"), date(2026, 11, 1))
    assert c.claimable


def test_rates_are_dated_and_editable(tmp_settings):
    rates = rules.load_rates()
    assert {16.0, 8.0, 0.0} <= {r.rate for r in rates}
    petro = [r for r in rates if r.key == "petroleum"][0]
    assert petro.effective_to is None  # no hard-coded expiry
    petro.effective_to = "2027-06-30"
    rules.save_rates(rates)
    assert [r for r in rules.load_rates() if r.key == "petroleum"][0].effective_to == "2027-06-30"


def test_band_letters_never_map_to_rates():
    # only informational; there must be no letter->rate lookup used by the parser
    import app.parser as p
    assert not hasattr(p, "BAND_RATES")
