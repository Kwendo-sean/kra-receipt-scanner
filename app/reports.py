"""Report builders. Python computes every figure and the report skeleton; the
local model only writes short prose per section from a small fact sheet.

A report is a plain dict:
  {kind, title, subtitle, generated, sections: [{heading, prose, prose_status,
   kv: [[k, v]], stats: [[label, value, note]], bars: [{title, rows: [[label, value, shown]]}],
   bullets: [str], tables: [{title, columns, rows, totals, money_cols}], images: [path]}],
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

D0 = Decimal("0")
TOP_BARS = 8
MAX_STEPS = 8


def kes(v) -> str:
    if v is None:
        return "—"
    return f"{Decimal(str(v)):,.2f}"


def _d(v) -> Decimal:
    return Decimal(str(v)) if v is not None else D0


def _pct(part, whole) -> str:
    whole = _d(whole)
    if not whole:
        return "0%"
    return f"{(_d(part) * 100 / whole).quantize(Decimal('0.1'))}%"


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


def _prev_period(period: str) -> str:
    y, m = map(int, period.split("-"))
    y, m = rules.add_months(y, m, -1)
    return f"{y:04d}-{m:02d}"


def _days_note(days: int | None) -> str:
    if days is None:
        return ""
    if days < 0:
        return f"overdue by {-days} days"
    if days == 0:
        return "due today"
    return f"{days} days left"


def select(params: dict, today: date) -> list[dict]:
    return receipts.query(period=params.get("period"), date_from=params.get("date_from"),
                          date_to=params.get("date_to"), supplier=params.get("supplier"),
                          ids=params.get("ids"), claimable=params.get("claimable"),
                          category=params.get("category"), today=today)


def _sec(heading: str, **kw) -> dict:
    s = {"heading": heading, "prose": "", "prose_status": None, "kv": [], "stats": [], "bars": [],
         "bullets": [], "tables": [], "images": []}
    s.update(kw)
    return s


def _section(heading: str, facts: dict, task: str, fallback: str, use_llm: bool) -> dict:
    prose, status = llm.write_paragraph(task, facts, fallback, enabled=use_llm)
    return _sec(heading, prose=prose, prose_status=status)


def _bars(title: str, items: list[tuple[str, Decimal]], whole: Decimal | None = None) -> dict:
    """Horizontal bars; `shown` is the printed value (with share of `whole` if given)."""
    rows = []
    for label, v in items[:TOP_BARS]:
        shown = f"KES {kes(v)}" + (f" · {_pct(v, whole)}" if whole else "")
        rows.append([label, float(v), shown])
    return {"title": title, "rows": rows}


def _vat_taxable(r: dict) -> Decimal:
    """Taxable value of VAT-bearing bands (rate > 0) as printed."""
    return sum((_d(b.get("taxable")) for b in r["fields"]["bands"]
                if b.get("rate") and b.get("taxable") is not None), D0)


def _name(r: dict) -> str:
    return r["fields"].get("seller_name") or "(unknown supplier)"


def _footer() -> str:
    return rules.DISCLAIMER


def _base(kind: str, params: dict, today: date) -> dict:
    return {"kind": kind, "title": KINDS[kind], "subtitle": _period_label(params),
            "generated": time.strftime("%Y-%m-%d %H:%M"), "today": today.isoformat(),
            "sections": [], "footer": _footer(), "params": params}


def _due(params: dict, today: date) -> tuple[date | None, int | None]:
    if not params.get("period"):
        return None, None
    y, m = map(int, params["period"].split("-"))
    due = rules.vat3_due_date(y, m)
    return due, (due - today).days


# ------------------------------------------------------------------ shared: issues + next steps


_FIELD_LABELS = {"total": "total", "receipt_date": "date", "seller_name": "supplier name", "seller_pin": "supplier PIN"}


def _issues(rs: list[dict]) -> dict:
    """Group what stops receipts being claimed, per supplier, with the VAT involved."""
    by_sup: dict[str, dict] = {}
    closing, checks_failed, missing = [], [], []
    for r in rs:
        c = r["claim"]
        if not c:
            continue
        f = r["fields"]
        gaps = [lbl for k, lbl in _FIELD_LABELS.items() if f.get(k) in (None, "")]
        if gaps:
            missing.append((r, gaps))
        for w in c["warnings"]:
            if w.startswith("Claim window closes"):
                closing.append((r, c.get("deadline")))
            elif w.startswith("Check failed"):
                checks_failed.append((r, w.split(":", 1)[1].strip()))
        if c["claimable"]:
            continue
        name = _name(r)
        e = by_sup.setdefault(name, {"receipts": 0, "vat": D0, "reasons": defaultdict(int)})
        e["receipts"] += 1
        e["vat"] += _d(r["vat"])
        for reason in c["reasons"]:
            e["reasons"][reason.split(" on ")[0]] += 1
    rows = []
    for name, e in sorted(by_sup.items(), key=lambda kv: (-kv[1]["vat"], -kv[1]["receipts"])):
        main = max(e["reasons"].items(), key=lambda kv: kv[1])[0] if e["reasons"] else "Not claimable"
        rows.append({"supplier": name, "receipts": e["receipts"], "vat": e["vat"], "reason": main,
                     "action": rules.action_for(main)})
    return {"suppliers": rows, "closing": closing, "checks_failed": checks_failed, "missing": missing}


def _next_steps(iss: dict, due: date | None, days: int | None, period_label: str) -> list[str]:
    steps = []
    if due:
        if days is not None and days < 0:
            steps.append(f"The VAT3 return for {period_label} was due on {due.isoformat()}: file it now to "
                         "limit penalties.")
        else:
            steps.append(f"File the VAT3 return for {period_label} by {due.isoformat()} ({_days_note(days)}).")
    for r, dl in iss["closing"][:3]:
        steps.append(f"Claim receipt #{r['id']} from {_name(r)} before {dl}, or the VAT on it is lost.")
    for s in iss["suppliers"][:4]:
        if s["reason"].startswith(("Supplier KRA PIN", "No eTIMS")):
            vat = f", KES {kes(s['vat'])} of VAT" if s["vat"] else ""
            steps.append(f"Ask {s['supplier']} for an eTIMS invoice showing their KRA PIN "
                         f"({s['receipts']} receipt(s){vat}).")
        elif s["reason"] == "Receipt date missing":
            steps.append(f"Enter the date on the {s['supplier']} receipt(s) from the photo.")
    for r, label in iss["checks_failed"][:3]:
        steps.append(f"Check receipt #{r['id']} from {_name(r)} against the photo: \"{label}\" failed.")
    for r, gaps in iss["missing"][:3]:
        steps.append(f"Fill in the missing {', '.join(gaps)} on receipt #{r['id']}.")
    out = []
    for s in steps:  # a receipt can show up under two headings
        if s not in out:
            out.append(s)
    if len(out) == (1 if due else 0):
        out.append("Every receipt is complete: nothing to fix.")
    return out[:MAX_STEPS]


def _issues_table(iss: dict) -> dict:
    return {"title": "", "columns": ["Supplier", "Receipts", "VAT (KES)", "Main issue", "What to do"],
            "money_cols": [2],
            "rows": [[s["supplier"], s["receipts"], float(s["vat"]), s["reason"], s["action"]]
                     for s in iss["suppliers"]],
            "totals": None}


# ------------------------------------------------------------------ shared: spending figures


def _spend(rs: list[dict]) -> dict:
    sup: dict[str, list] = defaultdict(lambda: [D0, D0, 0])
    cat: dict[str, list] = defaultdict(lambda: [D0, D0, 0])
    mon: dict[str, list] = defaultdict(lambda: [D0, D0, 0])
    items: dict[str, list] = defaultdict(lambda: [D0, D0, 0, ""])
    total = vat = D0
    counted, missing_total = [], 0
    for r in rs:
        f = r["fields"]
        if f.get("total") is None:
            missing_total += 1
            continue
        t, v = _d(f["total"]), _d(r["vat"])
        total += t
        vat += v
        counted.append((r, t))
        for bucket, key in ((sup, _name(r)),
                            (cat, r.get("category") or "(uncategorised)"),
                            (mon, (f.get("receipt_date") or "")[:7] or "(no date)")):
            bucket[key][0] += t
            bucket[key][1] += v
            bucket[key][2] += 1
        for it in f.get("items") or []:
            desc = " ".join(str(it.get("description") or "").split())
            if not desc or it.get("line_total") is None:
                continue
            e = items[desc.upper()]
            e[0] += _d(it["line_total"])
            e[1] += _d(it.get("quantity") or 1)
            e[2] += 1
            e[3] = e[3] or desc
    n = len(counted)
    biggest = sorted(counted, key=lambda rt: -rt[1])[:5]
    return {"sup": sup, "cat": cat, "mon": mon, "items": items, "total": total, "vat": vat, "n": n,
            "missing_total": missing_total, "avg": (total / n).quantize(Decimal("0.01")) if n else D0,
            "biggest": biggest,
            "sup_sorted": sorted(sup.items(), key=lambda kv: -kv[1][0]),
            "cat_sorted": sorted(cat.items(), key=lambda kv: -kv[1][0]),
            "items_sorted": sorted(items.values(), key=lambda e: -e[0])}


def _compare(params: dict, today: date, total: Decimal) -> dict | None:
    """This period against the one before (only for a month report with receipts in both)."""
    if not params.get("period"):
        return None
    prev = _prev_period(params["period"])
    rs = receipts.query(period=prev, today=today)
    ptotal = sum((_d(r["fields"].get("total")) for r in rs if r["fields"].get("total") is not None), D0)
    if not rs or not ptotal:
        return None
    y, m = map(int, prev.split("-"))
    change = total - ptotal
    return {"label": date(y, m, 1).strftime("%B %Y"), "total": ptotal, "n": len(rs), "change": change,
            "pct": _pct(abs(change), ptotal), "up": change > 0}


def _compare_sentence(cmp: dict | None) -> str:
    if not cmp:
        return ""
    if not cmp["change"]:
        return f"That is the same as {cmp['label']}."
    return (f"That is KES {kes(abs(cmp['change']))} ({cmp['pct']}) {'more' if cmp['up'] else 'less'} than "
            f"{cmp['label']}, when you spent KES {kes(cmp['total'])}.")


# ------------------------------------------------------------------ 1. VAT schedule


def vat_schedule(params: dict, today: date, use_llm: bool) -> dict:
    rep = _base("vat_schedule", params, today)
    rs = select(params, today)
    rows, blocked = [], []
    by_rate: dict[float, list] = defaultdict(lambda: [D0, D0, 0])
    by_sup: dict[str, Decimal] = defaultdict(lambda: D0)
    tot_claim_vat = tot_claim_tx = D0
    n_claim = 0
    for r in rs:
        f = r["fields"]
        c = r["claim"] or {"claimable": False, "reasons": ["Not processed"], "warnings": []}
        tx = _vat_taxable(r)
        vat = _d(r["vat"]) if r["vat"] is not None else None
        rows.append([f.get("receipt_date") or "", f.get("seller_name") or "", f.get("seller_pin") or "MISSING",
                     f.get("cu_invoice_no") or "MISSING", float(tx), None if vat is None else float(vat),
                     "Yes" if c["claimable"] else "No", "; ".join(c["reasons"] + c.get("warnings", [])) or "OK"])
        if c["claimable"]:
            n_claim += 1
            tot_claim_tx += tx
            tot_claim_vat += vat or 0
            by_sup[_name(r)] += vat or 0
            for b in f["bands"]:
                if b.get("rate") is not None:
                    e = by_rate[float(b["rate"])]
                    e[0] += _d(b.get("taxable"))
                    e[1] += _d(b.get("vat"))
                    e[2] += 1
        elif vat:
            blocked.append((r, vat, c["reasons"]))
    unclaim_vat = sum((v for _, v, _ in blocked), D0)
    due, days = _due(params, today)
    iss = _issues(rs)
    top = sorted(by_sup.items(), key=lambda kv: -kv[1])
    main_reason = iss["suppliers"][0]["reason"] if iss["suppliers"] else None

    facts = {
        "period": rep["subtitle"], "receipts": len(rs), "claimable receipts": n_claim,
        "not claimable": len(rs) - n_claim, "claimable taxable value KES": kes(tot_claim_tx),
        "claimable input VAT KES": kes(tot_claim_vat),
        "VAT printed on receipts that cannot be claimed yet KES": kes(unclaim_vat),
        "main reason receipts are not claimable": main_reason or "none",
        "suppliers with the most claimable VAT": "; ".join(f"{k} KES {kes(v)}" for k, v in top[:3]) or "none",
        "VAT3 due date": due.isoformat() if due else "depends on each month",
        "time left to file": _days_note(days) or "see each month",
    }
    fb = (f"For {rep['subtitle']}, {n_claim} of {len(rs)} receipts support an input-VAT claim of "
          f"KES {kes(tot_claim_vat)} on taxable purchases of KES {kes(tot_claim_tx)}."
          + (f" The largest share comes from {top[0][0]} (KES {kes(top[0][1])})." if top else ""))
    if blocked:
        fb += (f" Another KES {kes(unclaim_vat)} of VAT is printed on {len(blocked)} receipt(s) that cannot be "
               f"claimed as they stand, mostly because: {main_reason.lower()}.")
    if due:
        fb += (f"\n\nThe VAT3 return and payment are due by {due.isoformat()} ({_days_note(days)}). "
               "The schedule below lists each purchase with the supplier PIN and CU invoice number the return asks for.")
    s = _section("Summary", facts,
                 "Write two short paragraphs for the owner. First: how much input VAT can be claimed this month "
                 "and which suppliers it comes from. Second: how much VAT cannot be claimed yet and why, and "
                 "when the VAT3 return is due.", fb, use_llm)
    s["stats"] = [["Claimable input VAT", f"KES {kes(tot_claim_vat)}", f"{n_claim} of {len(rs)} receipts"],
                  ["Taxable purchases", f"KES {kes(tot_claim_tx)}", "claimable receipts"],
                  ["VAT not claimable yet", f"KES {kes(unclaim_vat)}", f"{len(blocked)} receipt(s)"],
                  ["VAT3 due", due.isoformat() if due else "—", _days_note(days)]]
    if due:
        s["kv"] = [["Note", rules.NIL_RETURN_NOTE]]
    rep["sections"].append(s)
    if top:
        rep["sections"].append(_sec("Where the claimable VAT comes from",
                                    bars=[_bars("Input VAT by supplier", top, tot_claim_vat)]))
    rep["sections"].append(_sec("Purchases schedule", tables=[{
        "title": "One row per receipt",
        "columns": ["Date", "Supplier", "Supplier PIN", "CU invoice no.", "Taxable (KES)", "VAT (KES)",
                    "Claimable", "Reason"],
        "money_cols": [4, 5], "rows": rows,
        "totals": ["", "Claimable total", "", "", float(tot_claim_tx), float(tot_claim_vat), str(n_claim), ""]}]))
    if blocked:
        rep["sections"].append(_sec("Fix before you file", tables=[{
            "title": "VAT printed on these receipts cannot be claimed yet",
            "columns": ["Date", "Supplier", "VAT (KES)", "Why", "What to do"], "money_cols": [2],
            "rows": [[r["fields"].get("receipt_date") or "", _name(r), float(v), "; ".join(why),
                      rules.action_for(why[0]) if why else ""] for r, v, why in blocked],
            "totals": ["", "Total", float(unclaim_vat), "", ""]}]))
    rate_rows = [[f"{rate:g}%", float(v[0]), float(v[1]), v[2]] for rate, v in sorted(by_rate.items(), reverse=True)]
    rep["sections"].append(_sec("Claimable totals by rate (as printed)", tables=[{
        "title": "", "columns": ["Rate", "Taxable (KES)", "VAT (KES)", "Bands"],
        "money_cols": [1, 2], "rows": rate_rows, "totals": None}]))
    rep["sections"].append(_sec("Next steps", bullets=_next_steps(iss, due, days, rep["subtitle"])))
    rep["sections"].append(_sec("Things the app cannot verify", prose_status="fixed",
                                prose=rules.RESIDUAL_RISK + " " + rules.PENALTY_NOTE))
    rep["data"] = {"claimable_vat": float(tot_claim_vat), "claimable_taxable": float(tot_claim_tx),
                   "n": len(rs), "n_claimable": n_claim, "due": due.isoformat() if due else None,
                   "days_left": days, "unclaimable_vat": float(unclaim_vat),
                   "top_suppliers": [[k, float(v)] for k, v in top[:5]],
                   "by_rate": {k: [float(v[0]), float(v[1])] for k, v in by_rate.items()}}
    return rep


# ------------------------------------------------------------------ 2. compliance


def compliance(params: dict, today: date, use_llm: bool) -> dict:
    rep = _base("compliance", params, today)
    rs = select(params, today)
    rows = []
    counts: dict[str, int] = defaultdict(int)
    soon = 0
    at_risk_vat = D0
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
    iss = _issues(rs)
    due, days = _due(params, today)
    worst = iss["suppliers"][0] if iss["suppliers"] else None
    common = sorted(counts.items(), key=lambda kv: -kv[1])
    facts = {"period": rep["subtitle"], "receipts checked": len(rs), "receipts with issues": n_fail,
             "receipts with no issues": len(rs) - n_fail,
             "claim windows closing within 30 days": soon,
             "VAT printed on non-claimable receipts KES": kes(at_risk_vat)}
    if worst:
        facts["supplier with the most VAT at risk"] = f"{worst['supplier']} KES {kes(worst['vat'])}, {worst['reason']}"
    for k, v in common[:4]:
        facts[f"issue: {k}"] = f"{v} receipt(s)"
    fb = (f"{n_fail} of {len(rs)} receipts have issues, and KES {kes(at_risk_vat)} of printed VAT cannot be "
          f"claimed as things stand.")
    if common:
        fb += f" The most common problem is \"{common[0][0]}\" ({common[0][1]} receipt(s))."
    if worst:
        fb += (f"\n\nStart with {worst['supplier']}: "
               + (f"it holds KES {kes(worst['vat'])} of that VAT. " if worst["vat"] else "")
               + worst["action"])
    if soon:
        fb += f" {soon} claim window(s) close within 30 days, so claim those in your next VAT3 return."
    s = _section("Summary", facts,
                 "Write two short paragraphs. First: what the main compliance problems are and how much VAT they "
                 "put at risk. Second: which supplier to deal with first and what to ask them for.", fb, use_llm)
    s["stats"] = [["Receipts with issues", f"{n_fail} of {len(rs)}", "need attention"],
                  ["VAT at risk", f"KES {kes(at_risk_vat)}", "printed but not claimable"],
                  ["Closing within 30 days", str(soon), "claim windows"],
                  ["Clean receipts", str(len(rs) - n_fail), "nothing to do"]]
    rep["sections"].append(s)
    if common:
        rep["sections"].append(_sec("Most common problems", tables=[{
            "title": "", "columns": ["Problem", "Receipts", "What to do"], "money_cols": [],
            "rows": [[k, v, rules.action_for(k)] for k, v in common], "totals": None}]))
    if iss["suppliers"]:
        rep["sections"].append(_sec("Who to contact", tables=[_issues_table(iss)]))
    rep["sections"].append(_sec("Receipts that need attention", tables=[{
        "title": "", "columns": ["Date", "Supplier", "Issue", "What to do", "VAT (KES)"],
        "money_cols": [4], "rows": rows, "totals": None}]))
    rep["sections"].append(_sec("Next steps", bullets=_next_steps(iss, due, days, rep["subtitle"])))
    rep["sections"].append(_sec("Rules applied", prose_status="fixed", prose=" ".join([
        "Input VAT is claimable only on an eTIMS-transmitted invoice showing the supplier's KRA PIN, "
        f"within {rules.CLAIM_WINDOW_MONTHS} months after the end of the month of purchase.",
        rules.RESIDUAL_RISK, rules.INCOME_TAX_NOTE, rules.PENALTY_NOTE])))
    rep["data"] = {"n": len(rs), "n_issues": n_fail, "closing_soon": soon, "at_risk_vat": float(at_risk_vat),
                   "worst": None if not worst else [worst["supplier"], float(worst["vat"]), worst["reason"]]}
    return rep


# ------------------------------------------------------------------ 3. spending


def spending(params: dict, today: date, use_llm: bool) -> dict:
    rep = _base("spending", params, today)
    rs = select(params, today)
    sp = _spend(rs)
    total, vat = sp["total"], sp["vat"]
    cmp = _compare(params, today, total)

    def table(title, d, sort_by_amount=True):
        items = sorted(d.items(), key=(lambda kv: -kv[1][0]) if sort_by_amount else (lambda kv: kv[0]))
        return {"title": "", "columns": [title.split()[-1].capitalize(), "Spent (KES)", "VAT paid (KES)",
                                         "Receipts", "Share"],
                "money_cols": [1, 2],
                "rows": [[k, float(v[0]), float(v[1]), v[2], _pct(v[0], total)] for k, v in items],
                "totals": ["Total", float(total), float(vat), sum(v[2] for v in d.values()), ""]}

    top = sp["sup_sorted"][:3]
    big = sp["biggest"][0] if sp["biggest"] else None
    facts = {"period": rep["subtitle"], "receipts": len(rs), "total spent KES": kes(total),
             "VAT paid KES": kes(vat), "VAT share of spending": _pct(vat, total),
             "average receipt KES": kes(sp["avg"]), "suppliers": len(sp["sup"]),
             "top suppliers": "; ".join(f"{k} KES {kes(v[0])} ({_pct(v[0], total)})" for k, v in top) or "none"}
    if sp["cat_sorted"]:
        k, v = sp["cat_sorted"][0]
        facts["largest category"] = f"{k} KES {kes(v[0])} ({_pct(v[0], total)})"
    if big:
        facts["largest single receipt"] = f"{_name(big[0])} KES {kes(big[1])} on {big[0]['fields'].get('receipt_date') or 'no date'}"
    if sp["items_sorted"]:
        e = sp["items_sorted"][0]
        facts["item with the most spend"] = f"{e[3]} KES {kes(e[0])}"
    if cmp:
        facts["previous month"] = f"{cmp['label']} KES {kes(cmp['total'])}"
        facts["change from previous month"] = f"{'up' if cmp['up'] else 'down'} KES {kes(abs(cmp['change']))} ({cmp['pct']})"
    if sp["missing_total"]:
        facts["receipts without a readable total"] = sp["missing_total"]

    fb = f"You spent KES {kes(total)} across {sp['n']} receipts, including KES {kes(vat)} of VAT."
    if cmp:
        fb += " " + _compare_sentence(cmp)
    if top:
        fb += (f"\n\n{top[0][0]} was your largest supplier at KES {kes(top[0][1][0])}, "
               f"{_pct(top[0][1][0], total)} of the total.")
        if len(top) > 1:
            fb += " After that: " + ", ".join(f"{k} (KES {kes(v[0])})" for k, v in top[1:]) + "."
    if big:
        fb += f" The biggest single receipt was KES {kes(big[1])} from {_name(big[0])}."
    if sp["missing_total"]:
        fb += f" {sp['missing_total']} receipt(s) have no readable total and are left out: fill them in to include them."
    s = _section("Summary", facts,
                 "Write two short paragraphs. First: how much was spent and how that compares with the previous "
                 "month. Second: where the money went (top suppliers, largest category, biggest receipt).",
                 fb, use_llm)
    s["stats"] = [["Total spent", f"KES {kes(total)}", f"{sp['n']} receipts"],
                  ["VAT paid", f"KES {kes(vat)}", _pct(vat, total) + " of spending"],
                  ["Average receipt", f"KES {kes(sp['avg'])}", f"{len(sp['sup'])} suppliers"],
                  (["vs " + cmp["label"], f"{'+' if cmp['up'] else '−'}KES {kes(abs(cmp['change']))}",
                    f"{cmp['pct']} {'more' if cmp['up'] else 'less'}"]
                   if cmp else ["Largest receipt", f"KES {kes(big[1]) if big else '—'}", _name(big[0]) if big else ""])]
    rep["sections"].append(s)
    if sp["sup_sorted"]:
        bars = [_bars("By supplier", [(k, v[0]) for k, v in sp["sup_sorted"]], total)]
        if len(sp["cat"]) > 1:
            bars.append(_bars("By category", [(k, v[0]) for k, v in sp["cat_sorted"]], total))
        rep["sections"].append(_sec("Where the money went", bars=bars))
    if sp["biggest"]:
        rep["sections"].append(_sec("Biggest purchases", tables=[{
            "title": "", "columns": ["Date", "Supplier", "Total (KES)", "VAT (KES)", "Receipt"],
            "money_cols": [2, 3],
            "rows": [[r["fields"].get("receipt_date") or "", _name(r), float(t),
                      None if r["vat"] is None else float(r["vat"]), f"#{r['id']}"] for r, t in sp["biggest"]],
            "totals": None}]))
    if sp["items_sorted"]:
        rep["sections"].append(_sec("What you bought most", tables=[{
            "title": "Items by amount spent (from the receipt lines)",
            "columns": ["Item", "Times bought", "Quantity", "Spent (KES)"], "money_cols": [3],
            "rows": [[e[3], e[2], float(e[1]), float(e[0])] for e in sp["items_sorted"][:12]], "totals": None}]))
    for heading, d, by_amount in (("By supplier", sp["sup"], True), ("By category", sp["cat"], True),
                                  ("By month", sp["mon"], False)):
        rep["sections"].append(_sec(heading, tables=[table(heading, d, by_amount)]))
    rep["data"] = {"total": float(total), "vat": float(vat), "n": len(rs), "avg": float(sp["avg"]),
                   "compare": None if not cmp else {"label": cmp["label"], "total": float(cmp["total"]),
                                                    "change": float(cmp["change"]), "pct": cmp["pct"]}}
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
    items = f["items"]
    top_item = max(items, key=lambda i: _d(i.get("line_total"))) if items else None
    # only what a claim or the books depend on; buyer PIN, signature and QR are optional
    needed = ("Seller", "Seller KRA PIN", "CU invoice no.", "Date", "Total (KES)")
    missing = [k for k, v in fields if k in needed and v in (None, "")]
    failed = [ch["label"] for ch in r["checks"] if ch["ok"] is False]
    facts = {"supplier": f.get("seller_name") or "unknown", "date": f.get("receipt_date") or "missing",
             "total KES": kes(f.get("total")), "VAT KES": kes(r["vat"]), "items": len(items),
             "claimable": "yes" if c["claimable"] else "no",
             "reasons": c["reasons"] or ["none"], "warnings": c["warnings"] or ["none"],
             "claim deadline": c.get("deadline") or "unknown",
             "missing fields": missing or ["none"], "failed checks": failed or ["none"]}
    if top_item:
        facts["largest item"] = f"{top_item['description']} KES {kes(top_item['line_total'])}"
    fb = (f"Receipt from {facts['supplier']} dated {facts['date']} for KES {facts['total KES']}"
          f" with VAT of KES {facts['VAT KES']}, covering {len(items)} item line(s)."
          + (f" The largest is {top_item['description']} at KES {kes(top_item['line_total'])}." if top_item else ""))
    if c["claimable"]:
        fb += f"\n\nIts VAT is claimable" + (f" until {c['deadline']}." if c.get("deadline") else ".")
    else:
        fb += "\n\nIts VAT is not claimable as it stands: " + "; ".join(c["reasons"]).lower() + "."
    if missing:
        fb += f" Missing from the scan: {', '.join(missing).lower()}."
    s = _section("Summary", facts,
                 "Write two short paragraphs. First: describe this receipt (supplier, date, total, VAT, largest "
                 "item). Second: whether its VAT can be claimed, and what is missing or needs checking.",
                 fb, use_llm)
    s["stats"] = [["Total", f"KES {kes(f.get('total'))}", f.get("payment_method") or ""],
                  ["VAT", f"KES {kes(r['vat'])}", ""],
                  ["Items", str(len(items)), "lines read"],
                  ["Claimable", "Yes" if c["claimable"] else "No", c.get("deadline") and f"until {c['deadline']}" or ""]]
    s["kv"] = kv
    rep["sections"].append(s)
    steps = [f"{reason}: {rules.action_for(reason)}" for reason in c["reasons"] + c["warnings"]]
    steps += [f"Fill in the {k.lower()} from the photo." for k in missing
              if not any(k.lower() in st.lower() for st in steps)]
    rep["sections"].append(_sec("What to do", bullets=steps[:MAX_STEPS] or
                                ["Nothing to do: this receipt is complete and claimable."]))
    rep["sections"].append(_sec("Line items", tables=[{
        "title": "", "columns": ["Description", "Qty", "Unit (KES)", "Total (KES)", "Band"], "money_cols": [2, 3],
        "rows": [[i["description"], i.get("quantity"), i.get("unit_price"), i["line_total"], i.get("band") or ""]
                 for i in items],
        "totals": ["Items total", "", "", float(sum((_d(i["line_total"]) for i in items), D0)), ""] if items else None}]))
    rep["sections"].append(_sec("Tax bands (rate as printed)", tables=[{
        "title": "", "columns": ["Band", "Rate", "Taxable (KES)", "VAT (KES)"], "money_cols": [2, 3],
        "rows": [[b.get("band") or "", f"{b['rate']:g}%" if b.get("rate") is not None else "?",
                  b.get("taxable"), b.get("vat")] for b in f["bands"]],
        "totals": None}]))
    rep["sections"].append(_sec("Checks", tables=[{
        "title": "", "columns": ["Check", "Result", "Detail"], "money_cols": [],
        "rows": [[ch["label"], {True: "PASS", False: "FAIL", None: "n/a"}[ch["ok"]], ch["detail"]]
                 for ch in r["checks"]], "totals": None}]))
    imgs = []
    if params.get("include_images", True):
        row = db.one("SELECT stitched_path FROM receipts WHERE id=?", rid)
        if receipts.scan_path(rid).is_file():
            imgs.append(str(receipts.scan_path(rid)))
        elif row and row["stitched_path"]:
            imgs.append(row["stitched_path"])
        else:
            imgs += [fr["path"] for fr in receipts.frames(rid)]
    rep["sections"].append(_sec("Receipt image", images=imgs))
    if r["ocr_text"]:
        rep["sections"].append(_sec("Raw OCR text", prose=r["ocr_text"], prose_status="raw"))
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
    rs = select(params, today)
    sp = _spend(rs)
    vs = vat_schedule(params, today, use_llm=False)["data"]
    co = compliance(params, today, use_llm=False)["data"]
    iss = _issues(rs)
    due, days = _due(params, today)
    cmp = _compare(params, today, sp["total"])
    total = sp["total"]
    top = sp["sup_sorted"][:3]
    big = sp["biggest"][0] if sp["biggest"] else None
    label = rep["subtitle"]

    # 1. in brief
    facts = {"period": label, "receipts": len(rs), "total spent KES": kes(total), "VAT paid KES": kes(sp["vat"]),
             "average receipt KES": kes(sp["avg"]), "claimable input VAT KES": kes(vs["claimable_vat"]),
             "receipts with issues": co["n_issues"]}
    if cmp:
        facts["previous month"] = f"{cmp['label']} KES {kes(cmp['total'])}"
        facts["change"] = f"{'up' if cmp['up'] else 'down'} KES {kes(abs(cmp['change']))} ({cmp['pct']})"
    if big:
        facts["largest single receipt"] = f"{_name(big[0])} KES {kes(big[1])}"
    fb = (f"In {label} you recorded {len(rs)} receipts totalling KES {kes(total)}, of which KES {kes(sp['vat'])} "
          f"was VAT. The average receipt was KES {kes(sp['avg'])}.")
    if cmp:
        fb += " " + _compare_sentence(cmp)
    fb += (f"\n\nYou can claim KES {kes(vs['claimable_vat'])} of input VAT"
           + (f", and {co['n_issues']} receipt(s) need attention before you file." if co["n_issues"]
              else ", and every receipt is in order."))
    s = _section("Your month in brief", facts,
                 "Write two short paragraphs in plain English for a small-business owner. First: how much was "
                 "spent and how it compares with the previous month. Second: how much VAT can be claimed and how "
                 "many receipts need attention.", fb, use_llm)
    s["stats"] = [["Spent", f"KES {kes(total)}", f"{len(rs)} receipts"],
                  ["VAT paid", f"KES {kes(sp['vat'])}", _pct(sp["vat"], total) + " of spending"],
                  ["Claimable VAT", f"KES {kes(vs['claimable_vat'])}", f"{vs['n_claimable']} receipts"],
                  ["Need attention", str(co["n_issues"]), f"KES {kes(co['at_risk_vat'])} VAT at risk"]]
    rep["sections"].append(s)

    # 2. where the money went
    if top:
        facts = {"total spent KES": kes(total),
                 "top suppliers": "; ".join(f"{k} KES {kes(v[0])} ({_pct(v[0], total)})" for k, v in top),
                 "suppliers": len(sp["sup"])}
        if sp["cat_sorted"]:
            facts["categories"] = "; ".join(f"{k} KES {kes(v[0])} ({_pct(v[0], total)})"
                                            for k, v in sp["cat_sorted"][:3])
        if sp["items_sorted"]:
            facts["items with the most spend"] = "; ".join(f"{e[3]} KES {kes(e[0])}" for e in sp["items_sorted"][:3])
        fb = (f"{top[0][0]} took the biggest share of your spending: KES {kes(top[0][1][0])}, "
              f"{_pct(top[0][1][0], total)} of the total.")
        if len(top) > 1:
            fb += " Next came " + " and ".join(f"{k} (KES {kes(v[0])})" for k, v in top[1:]) + "."
        if sp["items_sorted"]:
            e = sp["items_sorted"][0]
            fb += f" The single item you spent most on was {e[3]} (KES {kes(e[0])})."
        s = _section("Where the money went", facts,
                     "In one or two short paragraphs, explain where the money went: the top suppliers with their "
                     "shares, the main categories, and the items with the most spend.", fb, use_llm)
        s["bars"] = [_bars("By supplier", [(k, v[0]) for k, v in sp["sup_sorted"]], total)]
        if len(sp["cat"]) > 1:
            s["bars"].append(_bars("By category", [(k, v[0]) for k, v in sp["cat_sorted"]], total))
        rep["sections"].append(s)
    if sp["biggest"]:
        rep["sections"].append(_sec("Biggest purchases", tables=[{
            "title": "", "columns": ["Date", "Supplier", "Total (KES)", "VAT (KES)", "Receipt"], "money_cols": [2, 3],
            "rows": [[r["fields"].get("receipt_date") or "", _name(r), float(t),
                      None if r["vat"] is None else float(r["vat"]), f"#{r['id']}"] for r, t in sp["biggest"]],
            "totals": None}]))

    # 3. what you can claim
    facts = {"claimable receipts": vs["n_claimable"], "receipts": vs["n"],
             "claimable input VAT KES": kes(vs["claimable_vat"]),
             "taxable purchases KES": kes(vs["claimable_taxable"]),
             "suppliers with the most claimable VAT": "; ".join(f"{k} KES {kes(v)}" for k, v in vs["top_suppliers"][:3])
             or "none",
             "VAT3 due date": due.isoformat() if due else "see each month",
             "time left": _days_note(days) or "see each month"}
    fb = (f"{vs['n_claimable']} of {vs['n']} receipts support an input-VAT claim of KES {kes(vs['claimable_vat'])} "
          f"on taxable purchases of KES {kes(vs['claimable_taxable'])}.")
    if vs["top_suppliers"]:
        fb += f" Most of it comes from {vs['top_suppliers'][0][0]} (KES {kes(vs['top_suppliers'][0][1])})."
    if due:
        fb += f" File the VAT3 return by {due.isoformat()} ({_days_note(days)})."
    rep["sections"].append(_section("What you can claim", facts,
                                    "In two or three sentences, explain how much input VAT can be claimed, where "
                                    "it comes from, and when the return is due.", fb, use_llm))

    # 4. what needs fixing
    worst = iss["suppliers"][0] if iss["suppliers"] else None
    facts = {"receipts with issues": co["n_issues"], "claim windows closing within 30 days": co["closing_soon"],
             "VAT that cannot be claimed yet KES": kes(co["at_risk_vat"])}
    if worst:
        facts["supplier with the most VAT at risk"] = f"{worst['supplier']} KES {kes(worst['vat'])}, {worst['reason']}"
        facts["what to ask for"] = worst["action"]
    if not co["n_issues"]:
        fb = "Nothing needs fixing: every receipt has the supplier PIN, an eTIMS invoice number and readable figures."
    else:
        fb = (f"{co['n_issues']} receipt(s) have issues, leaving KES {kes(co['at_risk_vat'])} of VAT unclaimable "
              "for now.")
        if worst:
            fb += (f" Start with {worst['supplier']}" + (f" (KES {kes(worst['vat'])})" if worst["vat"] else "")
                   + f": {worst['action']}")
        if co["closing_soon"]:
            fb += f" {co['closing_soon']} claim window(s) close within 30 days."
    s = _section("What needs fixing", facts,
                 "In two or three sentences, explain what needs attention, which supplier to contact first and "
                 "what to ask them for.", fb, use_llm)
    if iss["suppliers"]:
        s["tables"] = [_issues_table(iss)]
    rep["sections"].append(s)

    rep["sections"].append(_sec("Next steps", bullets=_next_steps(iss, due, days, label)))
    rep["sections"].append(_sec("Keep in mind", prose_status="fixed",
                                prose=rules.RESIDUAL_RISK + " " + rules.INCOME_TAX_NOTE))
    rep["data"] = {"n": len(rs), "total": float(total), "claimable_vat": vs["claimable_vat"]}
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
