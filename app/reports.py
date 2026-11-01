"""Report builders. Python computes every figure and the report skeleton; the
local model only writes short prose per section from a small fact sheet.

A report is a plain dict:
  {kind, title, subtitle, generated, sections: [{heading, prose, prose_status,
   kv: [[k, v]], tables: [{title, columns, rows, totals, money_cols}], images: [path]}],
   footer, prose_summary}
"""
from __future__ import annotations

import time
from collections import defaultdict
from datetime import date
from decimal import Decimal

from app import kra_rules as rules
from app import db, llm, receipts

KINDS = {
    "vat_schedule": "Monthly input-VAT schedule (VAT3 purchases)",
    "compliance": "Claimability & compliance report",
    "spending": "Spending summary",
    "receipt": "Single-receipt report",
    "narrative": "Explain my month",
}


def kes(v) -> str:
    if v is None:
        return "—"
    return f"{Decimal(str(v)):,.2f}"


def _d(v) -> Decimal:
    return Decimal(str(v)) if v is not None else Decimal("0")


def _period_label(params: dict) -> str:
    if params.get("period"):
        y, m = map(int, params["period"].split("-"))
        return date(y, m, 1).strftime("%B %Y")
    if params.get("ids"):
        return f"{len(params['ids'])} selected receipt(s)"
    a, b = params.get("date_from"), params.get("date_to")
    if a or b:
        return f"{a or '…'} to {b or '…'}"
    return "All receipts"


def select(params: dict, today: date) -> list[dict]:
    return receipts.query(period=params.get("period"), date_from=params.get("date_from"),
                          date_to=params.get("date_to"), supplier=params.get("supplier"),
                          ids=params.get("ids"), claimable=params.get("claimable"),
                          category=params.get("category"), today=today)


def _section(heading: str, facts: dict, task: str, fallback: str, use_llm: bool) -> dict:
    prose, status = llm.write_paragraph(task, facts, fallback, enabled=use_llm)
    return {"heading": heading, "prose": prose, "prose_status": status, "kv": [], "tables": [], "images": []}


def _vat_taxable(r: dict) -> Decimal:
    """Taxable value of VAT-bearing bands (rate > 0) as printed."""
    return sum((_d(b.get("taxable")) for b in r["fields"]["bands"]
                if b.get("rate") and b.get("taxable") is not None), Decimal("0"))


def _footer() -> str:
    return rules.DISCLAIMER


def _base(kind: str, params: dict, today: date) -> dict:
    return {"kind": kind, "title": KINDS[kind], "subtitle": _period_label(params),
            "generated": time.strftime("%Y-%m-%d %H:%M"), "today": today.isoformat(),
            "sections": [], "footer": _footer(), "params": params}


# ------------------------------------------------------------------ 1. VAT schedule


def vat_schedule(params: dict, today: date, use_llm: bool) -> dict:
    rep = _base("vat_schedule", params, today)
    rs = select(params, today)
    rows = []
    by_rate: dict[float, list[Decimal]] = defaultdict(lambda: [Decimal("0"), Decimal("0"), 0])
    tot_claim_vat = tot_claim_tx = Decimal("0")
    n_claim = 0
    for r in rs:
        f = r["fields"]
        c = r["claim"] or {"claimable": False, "reasons": ["Not processed"]}
        tx = _vat_taxable(r)
        vat = _d(r["vat"]) if r["vat"] is not None else None
        rows.append([f.get("receipt_date") or "", f.get("seller_name") or "", f.get("seller_pin") or "MISSING",
                     f.get("cu_invoice_no") or "MISSING", float(tx), None if vat is None else float(vat),
                     "Yes" if c["claimable"] else "No", "; ".join(c["reasons"] + c.get("warnings", [])) or "OK"])
        if c["claimable"]:
            n_claim += 1
            tot_claim_tx += tx
            tot_claim_vat += vat or 0
            for b in f["bands"]:
                if b.get("rate") is not None:
                    e = by_rate[float(b["rate"])]
                    e[0] += _d(b.get("taxable"))
                    e[1] += _d(b.get("vat"))
                    e[2] += 1
    due = None
    if params.get("period"):
        y, m = map(int, params["period"].split("-"))
        due = rules.vat3_due_date(y, m)
    facts = {
        "period": rep["subtitle"], "receipts": len(rs), "claimable receipts": n_claim,
        "not claimable": len(rs) - n_claim, "claimable taxable value KES": kes(tot_claim_tx),
        "claimable input VAT KES": kes(tot_claim_vat),
        "VAT3 due date": due.isoformat() if due else "depends on each month",
    }
    fb = (f"For {rep['subtitle']}, {n_claim} of {len(rs)} receipts are claimable, with input VAT of "
          f"KES {kes(tot_claim_vat)} on a taxable value of KES {kes(tot_claim_tx)}."
          + (f" The VAT3 return and payment are due by {due.isoformat()}." if due else ""))
    s = _section("Summary", facts, "Summarise this month's claimable input VAT for the VAT3 return in two sentences.",
                 fb, use_llm)
    s["kv"] = [[k, str(v)] for k, v in facts.items()]
    if due:
        s["kv"].append(["Note", rules.NIL_RETURN_NOTE])
    rep["sections"].append(s)
    rep["sections"].append({"heading": "Purchases schedule", "prose": "", "prose_status": None, "kv": [], "images": [],
                            "tables": [{"title": "One row per receipt",
                                        "columns": ["Date", "Supplier", "Supplier PIN", "CU invoice no.",
                                                    "Taxable (KES)", "VAT (KES)", "Claimable", "Reason"],
                                        "money_cols": [4, 5], "rows": rows,
                                        "totals": ["", "Claimable total", "", "", float(tot_claim_tx),
                                                   float(tot_claim_vat), str(n_claim), ""]}]})
    rate_rows = [[f"{rate:g}%", float(v[0]), float(v[1]), v[2]] for rate, v in sorted(by_rate.items(), reverse=True)]
    rep["sections"].append({"heading": "Claimable totals by rate (as printed)", "prose": "", "prose_status": None,
                            "kv": [], "images": [],
                            "tables": [{"title": "", "columns": ["Rate", "Taxable (KES)", "VAT (KES)", "Bands"],
                                        "money_cols": [1, 2], "rows": rate_rows, "totals": None}]})
    rep["sections"].append({"heading": "Things the app cannot verify", "prose": rules.RESIDUAL_RISK + " " +
                            rules.PENALTY_NOTE, "prose_status": "fixed", "kv": [], "tables": [], "images": []})
    rep["data"] = {"claimable_vat": float(tot_claim_vat), "claimable_taxable": float(tot_claim_tx),
                   "n": len(rs), "n_claimable": n_claim, "due": due.isoformat() if due else None,
                   "by_rate": {k: [float(v[0]), float(v[1])] for k, v in by_rate.items()}}
    return rep


# ------------------------------------------------------------------ 2. compliance


def compliance(params: dict, today: date, use_llm: bool) -> dict:
    rep = _base("compliance", params, today)
    rs = select(params, today)
    rows = []
    counts: dict[str, int] = defaultdict(int)
    soon = 0
    at_risk_vat = Decimal("0")
    for r in rs:
        c = r["claim"]
        if not c:
            continue
        issues = c["reasons"] + c["warnings"]
        if not issues:
            continue
        f = r["fields"]
        for reason in issues:
            key = reason.split(" on ")[0].split(":")[0]
            counts[key] += 1
            rows.append([f.get("receipt_date") or "", f.get("seller_name") or "", reason, rules.action_for(reason),
                         None if r["vat"] is None else float(r["vat"])])
        if any(w.startswith("Claim window closes") for w in c["warnings"]):
            soon += 1
        if not c["claimable"] and r["vat"]:
            at_risk_vat += _d(r["vat"])
    n_fail = len({(row[0], row[1]) for row in rows})
    facts = {"period": rep["subtitle"], "receipts checked": len(rs), "receipts with issues": n_fail,
             "claim windows closing within 30 days": soon,
             "VAT printed on non-claimable receipts KES": kes(at_risk_vat)}
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:5]:
        facts[f"issue: {k}"] = v
    fb = (f"{n_fail} of {len(rs)} receipts have issues. KES {kes(at_risk_vat)} of printed VAT cannot be claimed "
          f"as things stand, and {soon} claim window(s) close within 30 days.")
    s = _section("Summary", facts, "Explain the main compliance problems and what to do first, in two sentences.",
                 fb, use_llm)
    s["kv"] = [[k, str(v)] for k, v in facts.items()]
    rep["sections"].append(s)
    rep["sections"].append({"heading": "Receipts that need attention", "prose": "", "prose_status": None, "kv": [],
                            "images": [], "tables": [{"title": "", "columns": ["Date", "Supplier", "Issue",
                                                                              "What to do", "VAT (KES)"],
                                                      "money_cols": [4], "rows": rows, "totals": None}]})
    rep["sections"].append({"heading": "Rules applied", "prose_status": "fixed", "kv": [], "tables": [], "images": [],
                            "prose": " ".join([
                                "Input VAT is claimable only on an eTIMS-transmitted invoice showing the supplier's KRA PIN, "
                                f"within {rules.CLAIM_WINDOW_MONTHS} months after the end of the month of purchase.",
                                rules.RESIDUAL_RISK, rules.INCOME_TAX_NOTE, rules.PENALTY_NOTE])})
    rep["data"] = {"n": len(rs), "n_issues": n_fail, "closing_soon": soon, "at_risk_vat": float(at_risk_vat)}
    return rep


# ------------------------------------------------------------------ 3. spending


def spending(params: dict, today: date, use_llm: bool) -> dict:
    rep = _base("spending", params, today)
    rs = select(params, today)
    sup: dict[str, list] = defaultdict(lambda: [Decimal("0"), Decimal("0"), 0])
    cat: dict[str, list] = defaultdict(lambda: [Decimal("0"), Decimal("0"), 0])
    mon: dict[str, list] = defaultdict(lambda: [Decimal("0"), Decimal("0"), 0])
    total = vat = Decimal("0")
    missing_total = 0
    for r in rs:
        f = r["fields"]
        if f.get("total") is None:
            missing_total += 1
            continue
        t, v = _d(f["total"]), _d(r["vat"])
        total += t
        vat += v
        for bucket, key in ((sup, f.get("seller_name") or "(unknown supplier)"),
                            (cat, r.get("category") or "(uncategorised)"),
                            (mon, (f.get("receipt_date") or "")[:7] or "(no date)")):
            bucket[key][0] += t
            bucket[key][1] += v
            bucket[key][2] += 1

    def table(title, d, sort_by_amount=True):
        items = sorted(d.items(), key=(lambda kv: -kv[1][0]) if sort_by_amount else (lambda kv: kv[0]))
        return {"title": title, "columns": [title.split()[-1].capitalize(), "Spent (KES)", "VAT paid (KES)", "Receipts"],
                "money_cols": [1, 2], "rows": [[k, float(v[0]), float(v[1]), v[2]] for k, v in items],
                "totals": ["Total", float(total), float(vat), sum(v[2] for v in d.values())]}

    top = max(sup.items(), key=lambda kv: kv[1][0]) if sup else None
    facts = {"period": rep["subtitle"], "receipts": len(rs), "total spent KES": kes(total),
             "VAT paid KES": kes(vat), "suppliers": len(sup)}
    if top:
        facts["largest supplier"] = top[0]
        facts["largest supplier spend KES"] = kes(top[1][0])
    if missing_total:
        facts["receipts without a readable total"] = missing_total
    fb = (f"You spent KES {kes(total)} across {len(rs)} receipts, including KES {kes(vat)} of VAT."
          + (f" Your largest supplier was {top[0]} at KES {kes(top[1][0])}." if top else ""))
    s = _section("Summary", facts, "Summarise this spending in two sentences.", fb, use_llm)
    s["kv"] = [[k, str(v)] for k, v in facts.items()]
    rep["sections"].append(s)
    for t in (table("By supplier", sup), table("By category", cat), table("By month", mon, False)):
        rep["sections"].append({"heading": t["title"], "prose": "", "prose_status": None, "kv": [], "images": [],
                                "tables": [t]})
    rep["data"] = {"total": float(total), "vat": float(vat), "n": len(rs)}
    return rep


# ------------------------------------------------------------------ 4. single receipt


def single(params: dict, today: date, use_llm: bool) -> dict:
    rid = int(params["ids"][0])
    r = receipts.get(rid, today)
    if r is None:
        raise ValueError("Receipt not found")
    params = dict(params)
    rep = _base("receipt", params, today)
    f = r["fields"]
    rep["subtitle"] = f"{f.get('seller_name') or 'Receipt'} · {f.get('receipt_date') or 'no date'} · #{rid}"
    c = r["claim"] or {"claimable": False, "reasons": [], "warnings": [], "deadline": None}
    fields = [["Seller", f.get("seller_name")], ["Seller KRA PIN", f.get("seller_pin")],
              ["Buyer PIN", f.get("buyer_pin")], ["Receipt / invoice no.", f.get("receipt_no")],
              ["CU invoice no.", f.get("cu_invoice_no")], ["CU serial / SCU ID", f.get("cu_serial_no")],
              ["Receipt signature", f.get("receipt_signature")], ["KRA QR URL", f.get("kra_qr_url")],
              ["Date", f.get("receipt_date")], ["Time", f.get("receipt_time")],
              ["Total (KES)", kes(f.get("total")) if f.get("total") is not None else None],
              ["Total VAT printed (KES)", kes(f.get("total_vat")) if f.get("total_vat") is not None else None],
              ["Payment", f.get("payment_method")], ["M-PESA code", f.get("mpesa_code")],
              ["Category", r.get("category")],
              ["Claimable", "Yes" if c["claimable"] else "No"],
              ["Claim deadline", c.get("deadline")]]
    corrected = set(r["corrections"].keys())
    kv = [[k + (" (corrected)" if _field_key(k) in corrected else ""), v if v not in (None, "") else "MISSING"]
          for k, v in fields]
    facts = {"supplier": f.get("seller_name") or "unknown", "date": f.get("receipt_date") or "missing",
             "total KES": kes(f.get("total")), "VAT KES": kes(r["vat"]),
             "claimable": "yes" if c["claimable"] else "no",
             "reasons": c["reasons"] or ["none"], "warnings": c["warnings"] or ["none"]}
    fb = (f"Receipt from {facts['supplier']} dated {facts['date']} for KES {facts['total KES']}"
          f" with VAT of KES {facts['VAT KES']}. "
          + ("It is claimable." if c["claimable"] else "It is not claimable: " + "; ".join(c["reasons"]) + "."))
    s = _section("Summary", facts, "Describe this receipt and whether its VAT can be claimed, in two sentences.",
                 fb, use_llm)
    s["kv"] = kv
    rep["sections"].append(s)
    rep["sections"].append({"heading": "Line items", "prose": "", "prose_status": None, "kv": [], "images": [],
                            "tables": [{"title": "", "columns": ["Description", "Qty", "Unit (KES)", "Total (KES)", "Band"],
                                        "money_cols": [2, 3],
                                        "rows": [[i["description"], i.get("quantity"), i.get("unit_price"),
                                                  i["line_total"], i.get("band") or ""] for i in f["items"]],
                                        "totals": None}]})
    rep["sections"].append({"heading": "Tax bands (rate as printed)", "prose": "", "prose_status": None, "kv": [],
                            "images": [], "tables": [{"title": "", "columns": ["Band", "Rate", "Taxable (KES)", "VAT (KES)"],
                                                      "money_cols": [2, 3],
                                                      "rows": [[b.get("band") or "", f"{b['rate']:g}%" if b.get("rate") is not None else "?",
                                                                b.get("taxable"), b.get("vat")] for b in f["bands"]],
                                                      "totals": None}]})
    rep["sections"].append({"heading": "Checks", "prose": "", "prose_status": None, "kv": [], "images": [],
                            "tables": [{"title": "", "columns": ["Check", "Result", "Detail"], "money_cols": [],
                                        "rows": [[ch["label"], {True: "PASS", False: "FAIL", None: "n/a"}[ch["ok"]],
                                                  ch["detail"]] for ch in r["checks"]], "totals": None}]})
    imgs = []
    if params.get("include_images", True):
        row = db.one("SELECT stitched_path FROM receipts WHERE id=?", rid)
        if row and row["stitched_path"]:
            imgs.append(row["stitched_path"])
        else:
            imgs += [fr["path"] for fr in receipts.frames(rid)]
    rep["sections"].append({"heading": "Receipt image", "prose": "", "prose_status": None, "kv": [], "tables": [],
                            "images": imgs})
    if r["ocr_text"]:
        rep["sections"].append({"heading": "Raw OCR text", "prose": r["ocr_text"], "prose_status": "raw", "kv": [],
                                "tables": [], "images": []})
    rep["data"] = {"id": rid, "claimable": c["claimable"]}
    return rep


_LABEL_TO_FIELD = {"Seller": "seller_name", "Seller KRA PIN": "seller_pin", "Buyer PIN": "buyer_pin",
                   "Receipt / invoice no.": "receipt_no", "CU invoice no.": "cu_invoice_no",
                   "CU serial / SCU ID": "cu_serial_no", "Receipt signature": "receipt_signature",
                   "Date": "receipt_date", "Time": "receipt_time", "Total (KES)": "total",
                   "Total VAT printed (KES)": "total_vat", "Payment": "payment_method", "M-PESA code": "mpesa_code"}


def _field_key(label: str) -> str:
    return _LABEL_TO_FIELD.get(label, "")


# ------------------------------------------------------------------ 5. narrative


def narrative(params: dict, today: date, use_llm: bool) -> dict:
    rep = _base("narrative", params, today)
    vs = vat_schedule(params, today, use_llm=False)
    co = compliance(params, today, use_llm=False)
    sp = spending(params, today, use_llm=False)
    due = vs["data"]["due"]
    sections = [
        ("Your month in brief",
         {"period": rep["subtitle"], "receipts": sp["data"]["n"], "total spent KES": kes(sp["data"]["total"]),
          "VAT paid KES": kes(sp["data"]["vat"])},
         "Describe the month's spending in plain English in two sentences.",
         f"In {rep['subtitle']} you recorded {sp['data']['n']} receipts totalling KES {kes(sp['data']['total'])}, "
         f"of which KES {kes(sp['data']['vat'])} was VAT."),
        ("What you can claim",
         {"claimable receipts": vs["data"]["n_claimable"], "receipts": vs["data"]["n"],
          "claimable input VAT KES": kes(vs["data"]["claimable_vat"]), "VAT3 due date": due or "see each month"},
         "Explain how much input VAT can be claimed and when the return is due, in two sentences.",
         f"{vs['data']['n_claimable']} of {vs['data']['n']} receipts support an input-VAT claim of "
         f"KES {kes(vs['data']['claimable_vat'])}." + (f" File the VAT3 return by {due}." if due else "")),
        ("What needs fixing",
         {"receipts with issues": co["data"]["n_issues"], "claim windows closing within 30 days": co["data"]["closing_soon"],
          "VAT that cannot be claimed yet KES": kes(co["data"]["at_risk_vat"])},
         "Explain what needs attention and suggest asking suppliers for eTIMS invoices, in two sentences.",
         f"{co['data']['n_issues']} receipts have issues, leaving KES {kes(co['data']['at_risk_vat'])} of VAT "
         f"unclaimable for now. Ask those suppliers for eTIMS invoices showing their KRA PIN."),
    ]
    for heading, facts, task, fb in sections:
        s = _section(heading, facts, task, fb, use_llm)
        rep["sections"].append(s)
    rep["sections"].append({"heading": "Keep in mind", "prose_status": "fixed", "kv": [], "tables": [], "images": [],
                            "prose": rules.RESIDUAL_RISK + " " + rules.INCOME_TAX_NOTE})
    return rep


BUILDERS = {"vat_schedule": vat_schedule, "compliance": compliance, "spending": spending,
            "receipt": single, "narrative": narrative}


def build(kind: str, params: dict, today: date | None = None, use_llm: bool | None = None) -> dict:
    today = today or date.today()
    if use_llm is None:
        use_llm = llm.available()
    rep = BUILDERS[kind](params, today, use_llm)
    statuses = [s["prose_status"] for s in rep["sections"] if s.get("prose_status") in
                ("llm", "llm_retry", "template_rejected", "template_offline")]
    if not use_llm:
        rep["prose_summary"] = "Local language model unavailable: prose uses fixed templates."
    elif any(s == "template_rejected" for s in statuses):
        rep["prose_summary"] = "Some model paragraphs failed the number check and were replaced by templates."
    else:
        rep["prose_summary"] = "Prose written by the local model; every number checked against the facts."
    rep["prose_statuses"] = statuses
    return rep
