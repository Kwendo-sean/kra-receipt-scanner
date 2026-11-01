from datetime import date, time
from decimal import Decimal

import pytest

import synth
from app import textparse as tp
from app.checks import run_checks
from app.parser import parse_lines, parse_text


# ---------------------------------------------------------------- tokens

def test_money_requires_decimals_or_separators():
    assert tp.amounts("Tel 0722123456 qty 3 PIN P051234567X") == []
    assert tp.amounts("5,000") == [Decimal("5000")]
    assert tp.amounts("KES6,858.00") == [Decimal("6858.00")]
    assert tp.amounts("B-16%  16%  5,000.00  800.00") == [Decimal("5000.00"), Decimal("800.00")]
    assert tp.amounts("DISCOUNT -50.00") == [Decimal("-50.00")]


def test_dates_are_day_first_and_handle_glued_time():
    assert tp.find_date("Date:21-10-202608:15") == date(2026, 10, 21)
    assert tp.find_time("Date:21-10-202608:15") == time(8, 15)
    assert tp.find_date("03/04/2026") == date(2026, 4, 3)  # 3 April, not March 4
    assert tp.find_date("2026-10-21 10:00") == date(2026, 10, 21)
    assert tp.find_date("21 Oct 2026") == date(2026, 10, 21)
    assert tp.find_date("13/13/2026") is None
    assert tp.find_time("02:30 PM") == time(14, 30)


def test_pin_regex_and_glued_labels():
    assert tp.find_pins("KRAPIN: P000611133Q") == ["P000611133Q"]
    assert tp.find_pins("KRAPIN:P000611133Q") == ["P000611133Q"]
    assert tp.find_pins("PIN P051234567X  BUYER A012345678Z") == ["P051234567X", "A012345678Z"]
    assert tp.find_pins("P05123456X") == []  # too short
    assert tp.find_pins("X051234567X") == []  # wrong prefix


def test_mpesa_code():
    assert tp.find_mpesa_code("M-PESA REF: SJK7ABC123") == "SJK7ABC123"
    assert tp.find_mpesa_code("PIN P051234567X") is None


# ---------------------------------------------------------------- receipts

def _assert_expected(r, exp):
    d = r.to_dict()
    for k, v in exp.items():
        if k == "n_items":
            assert len(d["items"]) == v, [i["description"] for i in d["items"]]
        elif k == "bands":
            got = {(b["band"], b["rate"]): (b["taxable"], b["vat"]) for b in d["bands"]}
            assert got == v, d["bands"]
        else:
            assert d[k] == v, (k, d[k], v)


@pytest.mark.parametrize("fn", synth.ALL, ids=lambda f: f.__name__)
def test_synthetic_receipts_parse_exactly(fn):
    s = fn()
    _assert_expected(parse_text(s.text), s.expect)


def test_supermarket_checks_all_pass():
    r = parse_text(synth.supermarket_16_exempt().text).to_dict()
    checks = {c["id"]: c for c in run_checks(r)}
    assert checks["band_vat:B 16.0%"]["ok"] is True
    assert checks["band_vat:A 0.0%"]["ok"] is True
    assert checks["bands_total"]["ok"] is True
    assert checks["vat_sum"]["ok"] is True
    assert checks["pin_present"]["ok"] and checks["etims_present"]["ok"]


def test_kiosk_flags_missing_pin_etims_vat():
    r = parse_text(synth.kiosk_no_vat().text).to_dict()
    checks = {c["id"]: c for c in run_checks(r)}
    assert checks["pin_present"]["ok"] is False
    assert checks["etims_present"]["ok"] is False
    assert checks["vat_shown"]["ok"] is False


def test_bad_vat_is_flagged_not_fixed():
    s = synth.supermarket_16_exempt()
    text = s.text.replace("1,206.90  193.10", "1,206.90  200.00")
    r = parse_text(text).to_dict()
    b = [b for b in r["bands"] if b["band"] == "B"][0]
    assert b["vat"] == 200.00  # stored as printed
    checks = {c["id"]: c for c in run_checks(r)}
    assert checks["band_vat:B 16.0%"]["ok"] is False


def test_rate_read_from_receipt_not_letter():
    # letter B but printed 8% -> 8% is used
    r = parse_lines(["SHOP", "PIN P051234567X", "TOTAL 108.00", "B-8%  8%  100.00  8.00"]).to_dict()
    assert r["bands"][0]["rate"] == 8.0


def test_vatable_plus_vat_lines():
    r = parse_lines(["CAFE JAVA", "PIN P051234567X", "Date 01/10/2026", "LATTE  580.00",
                     "TOTAL 580.00", "VATABLE AMT  500.00", "VAT 16%  80.00"]).to_dict()
    assert r["bands"] == [{"band": None, "rate": 16.0, "taxable": 500.0, "vat": 80.0, "gross": None,
                           "source": "VATABLE + VAT lines"}]


def test_kra_qr_supplies_invoice_number():
    r = parse_lines(["SHOP", "TOTAL 100.00"],
                    kra_qr_url="https://itax.kra.go.ke/KRA-Portal/invoiceChk.htm?actionCode=loadPage&invoiceNo=0040123450000011111")
    assert r.cu_invoice_no == "0040123450000011111"


def test_faded_cents_are_not_trusted():
    s = synth.supermarket_16_exempt()
    r = parse_text(s.text.replace("TOTAL VAT  193.10", "TOTAL VAT  1,193")).to_dict()
    assert r["total_vat"] is None                       # unread, not "1193.00"
    assert any("without cents" in n for n in r["notes"])
    # receipts that genuinely print whole shillings are unaffected
    r = parse_lines(["DUKA", "BREAD  1,200", "MILK  2,300", "TOTAL  3,500"]).to_dict()
    assert r["total"] == 3500.0


# ---------------------------------------------------------------- arithmetic gap-filling

def _derived(text):
    from app.parser import parse_and_derive
    return parse_and_derive(text.splitlines())


def test_faded_band_vat_filled_from_total_and_confirmed_by_rate():
    s = synth.supermarket_16_exempt()
    r = _derived(s.text.replace("B-16%  16%  1,206.90  193.10", "B-16%  16%  1,206.90"))
    b = [b for b in r["bands"] if b["band"] == "B"][0]
    assert b["vat"] == 193.10 and any("vat" in d["field"].lower() for d in r["derived"])
    assert run_checks(r) and all(c["ok"] is not False for c in run_checks(r) if c["severity"] == "error")


def test_single_band_rebuilt_from_total_and_stated_vat():
    s = synth.fuel_8()
    r = _derived(s.text.replace("E-8%  8%  6,351.85  508.15", "E-8%  8%"))
    assert r["bands"][0]["taxable"] == 6351.85 and r["bands"][0]["vat"] == 508.15


def test_no_fill_without_independent_confirmation():
    # VAT faded AND the printed rate disagrees with what the total implies -> stays missing
    s = synth.supermarket_16_exempt()
    text = s.text.replace("B-16%  16%  1,206.90  193.10", "B-16%  16%  1,206.90").replace("TOTAL  1,600.00", "TOTAL  1,650.00")
    r = _derived(text.replace("TOTAL VAT  193.10", ""))
    b = [b for b in r["bands"] if b["band"] == "B"][0]
    assert b["vat"] is None and not r["derived"]


def test_total_filled_only_when_items_confirm():
    s = synth.supermarket_16_exempt()
    r = _derived(s.text.replace("TOTAL  1,600.00", "TOTAL"))
    assert r["total"] == 1600.0 and r["derived"][0]["field"] == "total"


def test_mangled_year_is_left_unread_not_guessed():
    from app.textparse import find_date
    # real receipt: "2026" came out as "20:!6" - must not become 2020 (or 2016)
    assert find_date("DATETIME:07 Jun 20:!6 14:55:52") is None
    assert find_date("DATETIME : 07 Jun 20!6 14:55:52") is None
    assert find_date("07 Jun 20:16 14:55") is None
    # the one safe repair: letter O for zero
    assert str(find_date("07 Jun 2O26 14:55")) == "2026-06-07"
    assert str(find_date("07 Jun 26")) == "2026-06-07"
