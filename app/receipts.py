"""Receipt service: capture, frames, effective (parsed + user-corrected) view, corrections, queries."""
from __future__ import annotations

import os
import re
import time
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app import config, db
from app import kra_rules as rules
from app.checks import effective_taxable, effective_vat, run_checks
from app.textparse import PIN_RE

SCALAR_FIELDS = {
    "seller_name": "text", "seller_pin": "pin", "buyer_pin": "pin", "receipt_no": "text",
    "cu_invoice_no": "text", "cu_serial_no": "text", "receipt_signature": "text",
    "receipt_date": "date", "receipt_time": "time", "total": "money", "total_vat": "money",
    "payment_method": "text", "mpesa_code": "text",
}
LIST_FIELDS = {"bands", "items"}
MAX_FRAMES = 40
MAX_FRAME_BYTES = 15 * 1024 * 1024


class ValidationError(ValueError):
    pass


def receipt_dir(rid: int) -> Path:
    d = config.settings.media_dir / f"r{rid:06d}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def create(mode: str = "single") -> int:
    now = time.time()
    rid = db.run("INSERT INTO receipts(created_at, updated_at, status, mode) VALUES (?,?,?,?)",
                 now, now, "capturing", "multi" if mode == "multi" else "single")
    db.audit(rid, "created", new={"mode": mode})
    return rid


def _is_jpeg(data: bytes) -> bool:
    return data[:3] == b"\xff\xd8\xff"


def add_frame(rid: int, data: bytes, idx: int | None = None, sharpness: float | None = None) -> int:
    """Store one JPEG still. Re-using an idx replaces that section (retake)."""
    if not _is_jpeg(data):
        raise ValidationError("Frame must be a JPEG image")
    if len(data) > MAX_FRAME_BYTES:
        raise ValidationError("Frame too large")
    r = db.one("SELECT status FROM receipts WHERE id=? AND deleted=0", rid)
    if not r:
        raise ValidationError("Receipt not found")
    n = db.one("SELECT COUNT(*) AS n, COALESCE(MAX(idx), -1) AS mx FROM frames WHERE receipt_id=?", rid)
    if idx is None:
        idx = n["mx"] + 1
    if idx < 0 or idx >= MAX_FRAMES:
        raise ValidationError("Too many sections")
    path = receipt_dir(rid) / f"frame_{idx:02d}.jpg"
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    w = h = None
    try:
        import cv2
        import numpy as np
        im = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_REDUCED_GRAYSCALE_8)
        if im is None:
            raise ValidationError("Unreadable JPEG")
        h, w = im.shape[0] * 8, im.shape[1] * 8
    except ImportError:
        pass
    db.run("INSERT INTO frames(receipt_id, idx, path, created_at, sharpness, width, height) VALUES (?,?,?,?,?,?,?) "
           "ON CONFLICT(receipt_id, idx) DO UPDATE SET path=excluded.path, created_at=excluded.created_at, "
           "sharpness=excluded.sharpness, width=excluded.width, height=excluded.height",
           rid, idx, str(path), time.time(), sharpness, w, h)
    db.audit(rid, "frame_saved", field=f"frame {idx}", actor="phone")
    return idx


def delete_frame(rid: int, idx: int) -> None:
    f = db.one("SELECT path FROM frames WHERE receipt_id=? AND idx=?", rid, idx)
    if f:
        try:
            os.remove(f["path"])
        except OSError:
            pass
        db.run("DELETE FROM frames WHERE receipt_id=? AND idx=?", rid, idx)
        db.audit(rid, "frame_deleted", field=f"frame {idx}")


def frames(rid: int) -> list[dict]:
    return [dict(r) for r in db.all_("SELECT * FROM frames WHERE receipt_id=? ORDER BY idx", rid)]


def submit(rid: int, ocr_mode: str = "auto") -> int:
    if not frames(rid):
        raise ValidationError("No frames captured")
    ocr_mode = ocr_mode if ocr_mode in ("auto", "stitch", "dedupe") else "auto"
    db.run("UPDATE receipts SET status='queued', stage=NULL, error=NULL, ocr_mode=?, updated_at=? WHERE id=?",
           ocr_mode, time.time(), rid)
    db.audit(rid, "submitted", new={"ocr_mode": ocr_mode}, actor="phone")
    return db.enqueue("ocr", {"receipt_id": rid}, priority=1)


# ------------------------------------------------------------------ effective view


def _row(rid: int):
    return db.one("SELECT * FROM receipts WHERE id=? AND deleted=0", rid)


def effective(row) -> dict:
    """Parsed fields with the user's corrections applied on top."""
    parsed = db.jload(row["parsed_json"], {}) or {}
    corr = db.jload(row["corrections_json"], {}) or {}
    eff = dict(parsed)
    for k, v in corr.items():
        eff[k] = v
    eff.setdefault("items", [])
    eff.setdefault("bands", [])
    eff["kra_qr_url"] = eff.get("kra_qr_url") or row["kra_qr"]
    return eff


def get(rid: int, today: date | None = None) -> dict | None:
    row = _row(rid)
    if not row:
        return None
    eff = effective(row)
    checks = run_checks(eff) if row["parsed_json"] else []
    eff["checks"] = checks
    claim = rules.assess(eff, today or date.today()) if row["parsed_json"] else None
    vat = effective_vat(eff)
    taxable = effective_taxable(eff)
    return {
        "id": row["id"],
        "status": row["status"],
        "stage": row["stage"],
        "error": row["error"],
        "mode": row["mode"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "category": row["category"],
        "fields": eff,
        "parsed": db.jload(row["parsed_json"], {}),
        "corrections": db.jload(row["corrections_json"], {}),
        "checks": checks,
        "claim": None if claim is None else {
            "claimable": claim.claimable, "reasons": claim.reasons, "warnings": claim.warnings,
            "deadline": claim.deadline.isoformat() if claim.deadline else None,
        },
        "vat": None if vat is None else float(vat),
        "taxable": None if taxable is None else float(taxable),
        "ocr_text": row["ocr_text"],
        "stitch": db.jload(row["stitch_json"], {}),
        "frames": [{"idx": f["idx"], "sharpness": f["sharpness"]} for f in frames(rid)],
        "has_stitched": bool(row["stitched_path"]),
    }


def refresh_denorm(rid: int) -> None:
    row = _row(rid)
    if not row:
        return
    eff = effective(row)
    vat = effective_vat(eff)
    db.run("UPDATE receipts SET receipt_date=?, seller_name=?, seller_pin=?, total=?, vat=?, checks_json=?, "
           "updated_at=? WHERE id=?",
           eff.get("receipt_date"), eff.get("seller_name"), eff.get("seller_pin"), eff.get("total"),
           None if vat is None else float(vat), db.jdump(run_checks(eff)), time.time(), rid)


# ------------------------------------------------------------------ corrections


def _money(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        d = Decimal(str(v).replace(",", "").strip())
    except InvalidOperation:
        raise ValidationError(f"Not a money amount: {v!r}")
    return float(d.quantize(Decimal("0.01")))


def _validate(field: str, value):
    kind = SCALAR_FIELDS.get(field)
    if value is None or value == "":
        return None
    if kind == "text":
        v = re.sub(r"\s+", " ", str(value)).strip()
        if len(v) > 200:
            raise ValidationError("Too long")
        return v
    if kind == "pin":
        v = str(value).strip().upper()
        if not PIN_RE.fullmatch(v):
            raise ValidationError("KRA PIN must look like P051234567X")
        return v
    if kind == "date":
        try:
            return date.fromisoformat(str(value)).isoformat()
        except ValueError:
            raise ValidationError("Date must be YYYY-MM-DD")
    if kind == "time":
        m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", str(value).strip())
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ValidationError("Time must be HH:MM")
        return f"{int(m.group(1)):02d}:{m.group(2)}:{m.group(3) or '00'}"
    if kind == "money":
        return _money(value)
    raise ValidationError(f"Unknown field {field}")


def _validate_bands(bands) -> list[dict]:
    out = []
    for b in bands or []:
        letter = (str(b.get("band") or "").strip().upper() or None)
        if letter and not re.fullmatch(r"[A-Z]", letter):
            raise ValidationError("Band must be a single letter")
        rate = _money(b.get("rate"))
        out.append({"band": letter, "rate": rate, "taxable": _money(b.get("taxable")),
                    "vat": _money(b.get("vat")), "gross": _money(b.get("gross")), "source": "user"})
    return out


def _validate_items(items) -> list[dict]:
    out = []
    for i in items or []:
        desc = re.sub(r"\s+", " ", str(i.get("description") or "")).strip()[:200]
        lt = _money(i.get("line_total"))
        if lt is None:
            raise ValidationError("Each item needs a line total")
        q = i.get("quantity")
        out.append({"description": desc or "(item)", "quantity": None if q in (None, "") else float(q),
                    "unit_price": _money(i.get("unit_price")), "line_total": lt,
                    "band": (str(i.get("band") or "").strip().upper() or None)})
    return out


def correct(rid: int, changes: dict, revert: list[str] | None = None) -> dict:
    """Apply user corrections. They are kept across re-OCR and audited."""
    row = _row(rid)
    if not row:
        raise ValidationError("Receipt not found")
    corr = db.jload(row["corrections_json"], {}) or {}
    parsed = db.jload(row["parsed_json"], {}) or {}
    for field in revert or []:
        if field in corr:
            db.audit(rid, "correction_reverted", field, corr[field], parsed.get(field))
            corr.pop(field)
    for field, value in (changes or {}).items():
        if field == "category":
            cat = (str(value).strip()[:60] or None) if value is not None else None
            db.audit(rid, "category", "category", row["category"], cat)
            db.run("UPDATE receipts SET category=? WHERE id=?", cat, rid)
            continue
        if field == "bands":
            v = _validate_bands(value)
        elif field == "items":
            v = _validate_items(value)
        elif field in SCALAR_FIELDS:
            v = _validate(field, value)
        else:
            raise ValidationError(f"Field {field} cannot be edited")
        old = corr.get(field, parsed.get(field))
        if v == parsed.get(field) and field in corr:
            corr.pop(field)
        elif v != old:
            corr[field] = v
        db.audit(rid, "corrected", field, old, v)
    db.run("UPDATE receipts SET corrections_json=?, updated_at=? WHERE id=?", db.jdump(corr), time.time(), rid)
    refresh_denorm(rid)
    return get(rid)


def soft_delete(rid: int) -> None:
    db.run("UPDATE receipts SET deleted=1, updated_at=? WHERE id=?", time.time(), rid)
    db.audit(rid, "deleted")


# ------------------------------------------------------------------ queries


def query(period: str | None = None, date_from: str | None = None, date_to: str | None = None,
          supplier: str | None = None, ids: list[int] | None = None, claimable: bool | None = None,
          category: str | None = None, status: str | None = "done", today: date | None = None) -> list[dict]:
    """Return effective receipt dicts matching a filter. period = 'YYYY-MM'."""
    q = "SELECT id FROM receipts WHERE deleted=0"
    args: list = []
    if status:
        q += " AND status=?"
        args.append(status)
    if ids:
        q += " AND id IN (%s)" % ",".join("?" * len(ids))
        args += [int(i) for i in ids]
    if period:
        if not re.fullmatch(r"\d{4}-\d{2}", period):
            raise ValidationError("Period must be YYYY-MM")
        q += " AND receipt_date LIKE ?"
        args.append(period + "-%")
    if date_from:
        q += " AND receipt_date >= ?"
        args.append(date_from)
    if date_to:
        q += " AND receipt_date <= ?"
        args.append(date_to)
    if supplier:
        q += " AND (seller_name LIKE ? OR seller_pin = ?)"
        args += [f"%{supplier}%", supplier.upper()]
    if category:
        q += " AND category = ?"
        args.append(category)
    q += " ORDER BY receipt_date, id"
    out = []
    for r in db.all_(q, *args):
        g = get(r["id"], today)
        if g is None:
            continue
        if claimable is not None and g["claim"] and g["claim"]["claimable"] != claimable:
            continue
        out.append(g)
    return out


def list_recent(limit: int = 50, today: date | None = None) -> list[dict]:
    rows = db.all_("SELECT id, status, stage, error, receipt_date, seller_name, seller_pin, total, vat, created_at, "
                   "category, mode, parsed_json, corrections_json, kra_qr FROM receipts WHERE deleted=0 "
                   "ORDER BY id DESC LIMIT ?", limit)
    out = []
    for r in rows:
        d = {k: r[k] for k in r.keys() if k not in ("parsed_json", "corrections_json", "kra_qr")}
        d["claimable"] = None
        d["issues"] = []
        if r["parsed_json"]:
            c = rules.assess(effective(r), today or date.today())
            d["claimable"], d["issues"] = c.claimable, c.reasons + c.warnings
        out.append(d)
    return out
