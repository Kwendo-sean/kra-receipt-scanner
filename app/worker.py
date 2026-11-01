"""Background worker: one process, one job at a time (OCR, reports, email
composition), plus the email outbox flush and the monthly auto-report.

    python -m app.worker
"""
from __future__ import annotations

import gc
import logging
import signal
import time
import traceback
from datetime import date
from pathlib import Path

from app import config, db, emailer, receipts, render, reports
from app import kra_rules as rules

log = logging.getLogger("worker")
_stop = False


def _stage(rid: int, stage: str) -> None:
    db.run("UPDATE receipts SET stage=?, updated_at=? WHERE id=?", stage, time.time(), rid)


# ------------------------------------------------------------------ OCR


def handle_ocr(payload: dict, engine_factory=None) -> dict:
    import cv2

    from app.ocr import OcrEngine, peak_rss_mb
    from app.pipeline import process_frames_arrays

    rid = int(payload["receipt_id"])
    row = db.one("SELECT ocr_mode FROM receipts WHERE id=? AND deleted=0", rid)
    if not row:
        return {"skipped": "deleted"}
    paths = [f["path"] for f in receipts.frames(rid)]
    db.run("UPDATE receipts SET status='processing', stage='preprocessing', error=NULL WHERE id=?", rid)
    res = process_frames_arrays(paths, mode=row["ocr_mode"], engine_factory=engine_factory or OcrEngine,
                                progress=lambda s: _stage(rid, s))
    stitched_path = None
    if res["stitched"] is not None:
        stitched_path = str(receipts.receipt_dir(rid) / "stitched.jpg")
        cv2.imwrite(stitched_path, res["stitched"], [cv2.IMWRITE_JPEG_QUALITY, 85])
    res.pop("stitched", None)
    db.run("UPDATE receipts SET status='done', stage=NULL, parsed_json=?, ocr_text=?, lines_json=?, stitch_json=?, "
           "prep_json=?, kra_qr=?, stitched_path=?, updated_at=? WHERE id=?",
           db.jdump(res["parsed"]), res["text"], db.jdump(res["lines"]), db.jdump(res["stitch"]),
           db.jdump(res["prep"]), res["kra_qr"], stitched_path, time.time(), rid)
    receipts.refresh_denorm(rid)
    db.audit(rid, "ocr_done", new={"stitch": res["stitch"].get("method")}, actor="worker")
    del res
    gc.collect()
    return {"receipt_id": rid, "peak_rss_mb": peak_rss_mb()}


# ------------------------------------------------------------------ reports


def write_report_files(rep: dict, out_dir: Path, formats: list[str]) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{rep['kind']}-{rep['today']}"
    files = {}
    import json
    (out_dir / "report.json").write_text(json.dumps(rep, default=str), encoding="utf-8")
    files["json"] = str(out_dir / "report.json")
    if "html" in formats:
        p = out_dir / f"{stem}.html"
        p.write_text(render.to_html(rep), encoding="utf-8")
        files["html"] = str(p)
    if "pdf" in formats:
        p = out_dir / f"{stem}.pdf"
        p.write_bytes(render.to_pdf(rep))
        files["pdf"] = str(p)
    if "xlsx" in formats:
        p = out_dir / f"{stem}.xlsx"
        p.write_bytes(render.to_xlsx(rep))
        files["xlsx"] = str(p)
    if "csv" in formats:
        p = out_dir / f"{stem}.csv"
        p.write_bytes(render.to_csv(rep))
        files["csv"] = str(p)
    return files


def handle_report(payload: dict, use_llm: bool | None = None) -> dict:
    rid = int(payload["report_id"])
    row = db.one("SELECT * FROM reports WHERE id=?", rid)
    params = db.jload(row["params_json"], {})
    db.run("UPDATE reports SET status='running' WHERE id=?", rid)
    rep = reports.build(row["kind"], params, use_llm=use_llm)
    files = write_report_files(rep, config.settings.reports_dir / f"rep{rid:05d}", ["pdf", "xlsx", "csv"])
    db.run("UPDATE reports SET status='done', title=?, files_json=?, prose_status=? WHERE id=?",
           f"{rep['title']} — {rep['subtitle']}", db.jdump(files), rep.get("prose_summary"), rid)
    return {"report_id": rid}


def create_report(kind: str, params: dict) -> int:
    if kind not in reports.KINDS:
        raise ValueError("Unknown report kind")
    rid = db.run("INSERT INTO reports(created_at, kind, title, params_json) VALUES (?,?,?,?)",
                 time.time(), kind, reports.KINDS[kind], db.jdump(params))
    db.enqueue("report", {"report_id": rid}, priority=5)
    return rid


# ------------------------------------------------------------------ email compose

INCLUDE_OPTIONS = ("pdf", "xlsx", "csv", "images", "stitched", "ocr_text")


def handle_email(payload: dict, use_llm: bool | None = None) -> dict:
    """Build the requested reports/attachments for a selection and queue the email."""
    recipient_id = int(payload["recipient_id"])
    emailer.confirmed_email(recipient_id)  # re-check at send-compose time
    params = payload.get("params", {})
    include = [i for i in payload.get("include", ["pdf"]) if i in INCLUDE_OPTIONS]
    kinds = [k for k in payload.get("report_kinds", []) if k in reports.KINDS]
    today = date.today()
    sel = reports.select(params, today)
    if not sel:
        raise ValueError("No receipts match this selection")
    single = len(sel) == 1 and params.get("ids")
    if not kinds:
        kinds = ["receipt"] if single else ["vat_schedule", "compliance"]
    out_dir = config.settings.outbox_dir / f"mail{int(time.time() * 1000)}"
    out_dir.mkdir(parents=True, exist_ok=True)
    atts: list[tuple[str, str]] = []
    fmts = [f for f in ("pdf", "xlsx", "csv") if f in include]
    summaries = []
    for k in kinds:
        p = dict(params)
        if k == "receipt":
            for r in sel:
                rep = reports.build("receipt", {"ids": [r["id"]], "include_images": "images" in include},
                                    today, use_llm)
                files = write_report_files(rep, out_dir / f"receipt{r['id']}", fmts)
                atts += [(Path(v).name.replace("receipt-", f"receipt{r['id']}-"), v)
                         for f, v in files.items() if f in fmts]
            summaries.append(f"Single-receipt report for {len(sel)} receipt(s)")
            continue
        rep = reports.build(k, p, today, use_llm)
        files = write_report_files(rep, out_dir / k, fmts)
        atts += [(Path(v).name, v) for f, v in files.items() if f in fmts]
        summaries.append(f"{rep['title']} — {rep['subtitle']}")
    if "ocr_text" in include:
        p = out_dir / "ocr-text.txt"
        p.write_text("\n\n".join(f"===== Receipt #{r['id']} {r['fields'].get('seller_name') or ''} "
                                 f"{r['fields'].get('receipt_date') or ''} =====\n{r['ocr_text'] or ''}" for r in sel),
                     encoding="utf-8")
        atts.append((p.name, str(p)))
    imgs: list[tuple[str, str]] = []
    for r in sel:
        row = db.one("SELECT stitched_path FROM receipts WHERE id=?", r["id"])
        if "stitched" in include and row and row["stitched_path"]:
            imgs.append((f"receipt{r['id']}-stitched.jpg", row["stitched_path"]))
        if "images" in include:
            imgs += [(f"receipt{r['id']}-section{f['idx'] + 1}.jpg", f["path"]) for f in receipts.frames(r["id"])]
    if imgs:
        atts += emailer.zip_images_in_chunks(imgs, out_dir, config.settings.email_max_bytes, "receipts")
    subject = payload.get("subject") or (f"Receipt #{sel[0]['id']}" if single else
                                         f"Receipts — {reports._period_label(params)}")
    body = "\n".join(["Attached from your receipt scanner:", ""] + [f"- {s}" for s in summaries] +
                     ["", f"{len(sel)} receipt(s) included.", "", rules.DISCLAIMER])
    ids = emailer.queue(recipient_id, subject, body, atts)
    return {"outbox_ids": ids, "attachments": [a[0] for a in atts]}


# ------------------------------------------------------------------ monthly auto email


def monthly_auto(today: date | None = None) -> int | None:
    """Between the 1st and 5th, queue last month's VAT schedule + compliance
    report (PDF + XLSX) to every confirmed recipient - once per month."""
    today = today or date.today()
    cfg = db.kv_get("auto_monthly", {"enabled": True})
    if not cfg.get("enabled", True) or not (1 <= today.day <= 5):
        return None
    y, m = rules.add_months(today.year, today.month, -1)
    period = f"{y:04d}-{m:02d}"
    key = f"auto_monthly_done:{period}"
    if db.kv_get(key):
        return None
    recips = [r for r in emailer.list_recipients() if r["confirmed"]]
    if not recips:
        return None
    n = 0
    for r in recips:
        db.enqueue("email", {"recipient_id": r["id"], "params": {"period": period},
                             "include": ["pdf", "xlsx"], "report_kinds": ["vat_schedule", "compliance"],
                             "subject": f"VAT records for {period} (VAT3 due {rules.vat3_due_date(y, m).isoformat()})"},
                   priority=6)
        n += 1
    db.kv_set(key, time.time())
    return n


# ------------------------------------------------------------------ loop

HANDLERS = {"ocr": handle_ocr, "report": handle_report, "email": handle_email}


def run_one() -> bool:
    job = db.claim_job()
    if not job:
        return False
    payload = db.jload(job["payload_json"], {})
    try:
        res = HANDLERS[job["kind"]](payload)
        db.finish_job(job["id"], res)
    except Exception as e:  # noqa: BLE001 - worker must survive any job failure
        log.error("job %s (%s) failed: %s", job["id"], job["kind"], e)
        log.debug(traceback.format_exc())
        msg = f"{type(e).__name__}: {e}"[:500]
        if job["kind"] == "ocr":
            db.run("UPDATE receipts SET status='failed', stage=NULL, error=? WHERE id=?", msg, payload.get("receipt_id"))
        if job["kind"] == "report":
            db.run("UPDATE reports SET status='failed', error=? WHERE id=?", msg, payload.get("report_id"))
        db.fail_job(job["id"], msg, retry_in=None)
    finally:
        gc.collect()
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config.settings.ensure_dirs()
    db.init_db()
    db.requeue_stale(0)
    emailer.ensure_owner_recipient()

    def _sig(*_):
        global _stop
        _stop = True
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    last_mail = last_monthly = 0.0
    log.info("worker started")
    while not _stop:
        did = run_one()
        now = time.time()
        if now - last_mail > 60:
            last_mail = now
            try:
                st = emailer.send_due()
                if st["sent"] or st["failed"]:
                    log.info("outbox: %s", st)
            except Exception as e:  # noqa: BLE001
                log.error("outbox error: %s", e)
        if now - last_monthly > 3600:
            last_monthly = now
            try:
                monthly_auto()
            except Exception as e:  # noqa: BLE001
                log.error("monthly auto error: %s", e)
        if not did:
            time.sleep(1.0)
    log.info("worker stopped")


if __name__ == "__main__":
    main()
