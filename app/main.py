"""FastAPI app: pairing, scanner/receipt/report/email APIs, auth-protected media.

    python -m app.main          (HTTPS on 0.0.0.0:8090 using certs/server.{crt,key})
"""
from __future__ import annotations

import io
from contextlib import asynccontextmanager
import json
import os
import time
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app import __version__, auth, config, db, emailer, llm, receipts, render, reports, worker
from app import kra_rules as rules

APP_DIR = Path(__file__).parent
templates = Environment(loader=FileSystemLoader(APP_DIR / "templates"), autoescape=select_autoescape(["html"]))

@asynccontextmanager
async def lifespan(_app):
    config.settings.ensure_dirs()
    db.init_db()
    emailer.ensure_owner_recipient()
    yield


app = FastAPI(title="KRA receipt scanner", version=__version__, docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)
# Static = app code (JS/CSS/icons) only. Receipt media is NEVER served statically.
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")

CSP = ("default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; script-src 'self'; "
       "style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _allowed_hosts() -> set[str]:
    hs = set(config.settings.public_hosts) | {"localhost", "127.0.0.1"}
    return {h.lower() for h in hs}


@app.middleware("http")
async def security(request: Request, call_next):
    # DNS-rebinding guard: only answer to our own host names / IPs
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
    if host not in _allowed_hosts():
        return JSONResponse({"detail": "Unknown host"}, status_code=421)
    # Cross-site request guard for state-changing calls (in addition to CSRF token)
    if request.method not in SAFE_METHODS:
        origin = request.headers.get("origin")
        if origin:
            o_host = origin.split("://", 1)[-1].rsplit(":", 1)[0].strip("[]").lower()
            if o_host not in _allowed_hosts():
                return JSONResponse({"detail": "Bad origin"}, status_code=403)
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = CSP
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"  # pairing tokens are in URLs
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=()"
    if request.url.scheme == "https":
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    if not request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ------------------------------------------------------------------ auth deps


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _is_local(request: Request) -> bool:
    return _client_ip(request) in ("127.0.0.1", "::1")


def optional_session(request: Request) -> dict | None:
    return auth.session_from_token(request.cookies.get(auth.COOKIE))


def require_session(request: Request) -> dict:
    s = optional_session(request)
    if not s:
        raise HTTPException(401, "Not paired")
    if request.method not in SAFE_METHODS:
        if not auth.csrf_ok(s["id"], request.headers.get("x-csrf-token")):
            raise HTTPException(403, "CSRF check failed")
    return s


def can_show_pairing(request: Request, sess: dict | None) -> bool:
    return bool(sess) or _is_local(request) or config.settings.pairing_from_network


# ------------------------------------------------------------------ pages


def _page(name: str, **ctx) -> HTMLResponse:
    return HTMLResponse(templates.get_template(name).render(version=__version__, **ctx))


@app.get("/", response_class=HTMLResponse)
def index(request: Request, sess: dict | None = Depends(optional_session)):
    if sess:
        return _page("app.html", csrf=auth.csrf_token(sess["id"]), device=sess.get("label") or "")
    return _page("pair.html", show_qr=can_show_pairing(request, None),
                 wifi=bool(config.settings.wifi_ssid), ttl=config.settings.pair_token_ttl)


@app.get("/pair-screen", response_class=HTMLResponse)
def pair_screen(request: Request, sess: dict | None = Depends(optional_session)):
    """Full-screen pairing QR for the Pi's attached display (kiosk on localhost)."""
    if not can_show_pairing(request, sess):
        raise HTTPException(403, "Pairing screen is only shown on the Pi's own display")
    return _page("pair.html", show_qr=True, kiosk=True, wifi=bool(config.settings.wifi_ssid),
                 ttl=config.settings.pair_token_ttl)


def _qr_svg(data: str) -> Response:
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=12, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


def _base_url(request: Request) -> str:
    host = request.headers.get("host") or f"{config.settings.public_hosts[0]}:{config.settings.port}"
    if _is_local(request) and host.split(":")[0] in ("localhost", "127.0.0.1"):
        host = f"{config.settings.public_hosts[0]}:{config.settings.port}"  # phone can't reach localhost
    return f"https://{host}"


@app.get("/api/pair/new")
def pair_new(request: Request, sess: dict | None = Depends(optional_session)):
    if not can_show_pairing(request, sess):
        raise HTTPException(403, "Pairing not allowed from here")
    tok = auth.new_pair_token()
    return {"url": f"{_base_url(request)}/pair/{tok}", "token": tok, "ttl": config.settings.pair_token_ttl}


@app.get("/api/pair/qr.svg")
def pair_qr(request: Request, token: str, sess: dict | None = Depends(optional_session)):
    if not can_show_pairing(request, sess):
        raise HTTPException(403, "Pairing not allowed from here")
    if not token.replace("-", "").replace("_", "").isalnum() or len(token) > 64:
        raise HTTPException(400, "Bad token")
    return _qr_svg(f"{_base_url(request)}/pair/{token}")


def _wifi_escape(s: str) -> str:
    for ch in "\\;,:\"":
        s = s.replace(ch, "\\" + ch)
    return s


@app.get("/api/wifi/qr.svg")
def wifi_qr(request: Request, sess: dict | None = Depends(optional_session)):
    s = config.settings
    if not can_show_pairing(request, sess) or not s.wifi_ssid:
        raise HTTPException(404)
    return _qr_svg(f"WIFI:T:WPA;S:{_wifi_escape(s.wifi_ssid)};P:{_wifi_escape(s.wifi_password)};;")


@app.get("/pair/{token}")
def pair(token: str, request: Request):
    if auth.pair_rate_limited(_client_ip(request)):
        raise HTTPException(429, "Too many pairing attempts; wait a few minutes")
    sess = auth.redeem_pair_token(token, request.headers.get("user-agent", ""))
    if not sess:
        return _page("pair.html", show_qr=False, error="This pairing code is invalid, used or expired. "
                     "Show a fresh QR code on the Pi and scan again.", wifi=False, ttl=0)
    resp = RedirectResponse("/?paired=1", status_code=303)
    resp.set_cookie(auth.COOKIE, sess, max_age=config.settings.session_days * 86400, httponly=True,
                    secure=request.url.scheme == "https", samesite="lax", path="/")
    return resp


@app.get("/ca.crt")
def ca_cert():
    """The local CA *certificate* (public) so the phone can trust the Pi once.
    The CA private key never leaves certs/ on the Pi."""
    p = Path(config.settings.ca_file)
    if not p.is_file():
        raise HTTPException(404, "No local CA generated yet (run scripts/make_certs.sh)")
    return FileResponse(p, media_type="application/x-x509-ca-cert", filename="receipt-scanner-ca.crt")


@app.post("/api/logout")
def logout(sess: dict = Depends(require_session)):
    auth.revoke(sess["id"])
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


# ------------------------------------------------------------------ receipts API


def _bad(e: Exception) -> HTTPException:
    return HTTPException(400, str(e))


@app.post("/api/receipts")
async def create_receipt(request: Request, sess: dict = Depends(require_session)):
    body = await _json(request)
    return {"id": receipts.create(body.get("mode", "single"))}


async def _json(request: Request) -> dict:
    try:
        b = await request.json()
    except (ValueError, json.JSONDecodeError):
        return {}
    return b if isinstance(b, dict) else {}


@app.post("/api/receipts/{rid}/frames")
async def upload_frame(rid: int, file: UploadFile = File(...), idx: int | None = Form(None),
                       sharpness: float | None = Form(None), sess: dict = Depends(require_session)):
    data = await file.read(receipts.MAX_FRAME_BYTES + 1)
    try:
        i = receipts.add_frame(rid, data, idx, sharpness)
    except receipts.ValidationError as e:
        raise _bad(e)
    return {"idx": i}


@app.post("/api/receipts/import")
async def import_receipts(files: list[UploadFile] = File(...), one_receipt: bool = Form(False),
                          sess: dict = Depends(require_session)):
    """Photos or PDFs (e.g. from the iPhone's built-in document scanner)."""
    if len(files) > 40:
        raise HTTPException(400, "Too many files")
    loaded = []
    for f in files:
        data = await f.read(receipts.MAX_IMPORT_BYTES + 1)
        loaded.append((f.filename or "", data))
    try:
        ids = receipts.import_files(loaded, one_receipt)
    except receipts.ValidationError as e:
        raise _bad(e)
    return {"ids": ids}


@app.delete("/api/receipts/{rid}/frames/{idx}")
def delete_frame(rid: int, idx: int, sess: dict = Depends(require_session)):
    receipts.delete_frame(rid, idx)
    return {"ok": True}


@app.post("/api/receipts/{rid}/submit")
async def submit_receipt(rid: int, request: Request, sess: dict = Depends(require_session)):
    body = await _json(request)
    try:
        job = receipts.submit(rid, body.get("ocr_mode", "auto"))
    except receipts.ValidationError as e:
        raise _bad(e)
    return {"job": job}


@app.post("/api/receipts/{rid}/reprocess")
async def reprocess(rid: int, request: Request, sess: dict = Depends(require_session)):
    body = await _json(request)
    try:
        receipts.submit(rid, body.get("ocr_mode", "auto"))
    except receipts.ValidationError as e:
        raise _bad(e)
    return {"ok": True}


@app.get("/api/receipts")
def list_receipts(limit: int = 100, sess: dict = Depends(require_session)):
    return {"receipts": receipts.list_recent(min(max(1, limit), 500))}


@app.get("/api/receipts/{rid}")
def get_receipt(rid: int, sess: dict = Depends(require_session)):
    r = receipts.get(rid)
    if not r:
        raise HTTPException(404)
    return r


@app.patch("/api/receipts/{rid}")
async def patch_receipt(rid: int, request: Request, sess: dict = Depends(require_session)):
    body = await _json(request)
    try:
        return receipts.correct(rid, body.get("changes") or {}, body.get("revert") or [])
    except receipts.ValidationError as e:
        raise _bad(e)


@app.delete("/api/receipts/{rid}")
def delete_receipt(rid: int, sess: dict = Depends(require_session)):
    receipts.soft_delete(rid)
    return {"ok": True}


@app.get("/api/receipts/{rid}/audit")
def receipt_audit(rid: int, sess: dict = Depends(require_session)):
    rows = db.all_("SELECT at, actor, action, field, old, new FROM audit WHERE receipt_id=? ORDER BY id", rid)
    return {"audit": [dict(r) for r in rows]}


@app.get("/api/queue")
def queue_status(sess: dict = Depends(require_session)):
    rows = db.all_("SELECT id, status, stage, error, seller_name, total, created_at FROM receipts "
                   "WHERE deleted=0 AND (status IN ('queued','processing','capturing','failed') OR updated_at > ?) "
                   "ORDER BY id DESC LIMIT 30", time.time() - 3600)
    pending = db.one("SELECT COUNT(*) AS n FROM jobs WHERE status IN ('queued','running')")["n"]
    return {"receipts": [dict(r) for r in rows], "jobs_pending": pending}


# ------------------------------------------------------------------ media (auth only)


def _send_image(path: str | None, w: int | None):
    if not path or not os.path.isfile(path):
        raise HTTPException(404)
    root = config.settings.media_dir.resolve()
    rp = Path(path).resolve()
    if root not in rp.parents:
        raise HTTPException(404)
    if w:
        import cv2
        img = cv2.imread(str(rp), cv2.IMREAD_COLOR)
        if img is None:
            raise HTTPException(404)
        w = max(64, min(int(w), 1600))
        if img.shape[1] > w:
            img = cv2.resize(img, (w, int(img.shape[0] * w / img.shape[1])), interpolation=cv2.INTER_AREA)
        ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return Response(enc.tobytes(), media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})
    return FileResponse(rp, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})


@app.get("/media/receipts/{rid}/frame/{idx}.jpg")
def media_frame(rid: int, idx: int, w: int | None = None, sess: dict = Depends(require_session)):
    f = db.one("SELECT path FROM frames WHERE receipt_id=? AND idx=?", rid, idx)
    return _send_image(f["path"] if f else None, w)


@app.get("/media/receipts/{rid}/stitched.jpg")
def media_stitched(rid: int, w: int | None = None, sess: dict = Depends(require_session)):
    r = db.one("SELECT stitched_path FROM receipts WHERE id=? AND deleted=0", rid)
    return _send_image(r["stitched_path"] if r else None, w)


@app.get("/media/receipts/{rid}/scan.jpg")
def media_scan(rid: int, w: int | None = None, sess: dict = Depends(require_session)):
    r = db.one("SELECT id FROM receipts WHERE id=? AND deleted=0", rid)
    p = receipts.scan_path(rid)
    return _send_image(str(p) if r and p.is_file() else None, w)


def _image_url_for(path: str) -> str:
    p = Path(path)
    rid = int(p.parent.name.lstrip("r"))
    if p.name == "stitched.jpg":
        return f"/media/receipts/{rid}/stitched.jpg?w=900"
    if p.name == "scan.jpg":
        return f"/media/receipts/{rid}/scan.jpg?w=900"
    idx = int(p.stem.split("_")[1])
    return f"/media/receipts/{rid}/frame/{idx}.jpg?w=900"


# ------------------------------------------------------------------ reports


def _clean_params(p: dict) -> dict:
    out = {}
    for k in ("period", "date_from", "date_to", "supplier", "category"):
        if p.get(k):
            out[k] = str(p[k])[:60]
    if p.get("ids"):
        out["ids"] = [int(i) for i in p["ids"]][:2000]
    if p.get("claimable") in (True, False):
        out["claimable"] = p["claimable"]
    if "include_images" in p:
        out["include_images"] = bool(p["include_images"])
    return out


@app.post("/api/reports")
async def new_report(request: Request, sess: dict = Depends(require_session)):
    body = await _json(request)
    kind = body.get("kind")
    params = _clean_params(body.get("params") or {})
    if kind not in reports.KINDS:
        raise HTTPException(400, "Unknown report kind")
    if kind == "receipt" and len(params.get("ids", [])) != 1:
        raise HTTPException(400, "Choose exactly one receipt")
    try:
        if params.get("period"):
            receipts.query(period=params["period"], status=None)  # validate period format
    except receipts.ValidationError as e:
        raise _bad(e)
    return {"id": worker.create_report(kind, params)}


@app.get("/api/reports")
def list_reports(sess: dict = Depends(require_session)):
    rows = db.all_("SELECT id, created_at, kind, title, status, prose_status, error FROM reports ORDER BY id DESC LIMIT 50")
    return {"reports": [dict(r) for r in rows], "kinds": reports.KINDS}


def _report_row(rid: int):
    r = db.one("SELECT * FROM reports WHERE id=?", rid)
    if not r:
        raise HTTPException(404)
    return r


@app.get("/api/reports/{rid}")
def get_report(rid: int, sess: dict = Depends(require_session)):
    r = dict(_report_row(rid))
    r["files"] = [k for k in db.jload(r.pop("files_json"), {}) if k != "json"]
    return r


@app.get("/reports/{rid}/view", response_class=HTMLResponse)
def view_report(rid: int, sess: dict = Depends(require_session)):
    r = _report_row(rid)
    files = db.jload(r["files_json"], {})
    if r["status"] != "done" or "json" not in files:
        raise HTTPException(409, "Report not ready")
    rep = json.loads(Path(files["json"]).read_text(encoding="utf-8"))
    return HTMLResponse(render.to_html(rep, image_url=_image_url_for))


@app.get("/reports/{rid}/download/{fmt}")
def download_report(rid: int, fmt: str, sess: dict = Depends(require_session)):
    r = _report_row(rid)
    files = db.jload(r["files_json"], {})
    if fmt not in ("pdf", "xlsx", "csv") or fmt not in files:
        raise HTTPException(404)
    p = Path(files[fmt])
    if config.settings.reports_dir.resolve() not in p.resolve().parents:
        raise HTTPException(404)
    media = {"pdf": "application/pdf", "csv": "text/csv",
             "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}[fmt]
    return FileResponse(p, media_type=media, filename=p.name)


# ------------------------------------------------------------------ email


@app.post("/api/email")
async def send_email(request: Request, sess: dict = Depends(require_session)):
    body = await _json(request)
    try:
        rid = int(body.get("recipient_id"))
        emailer.confirmed_email(rid)
    except (TypeError, ValueError, emailer.EmailError):
        raise HTTPException(400, "Choose a saved, confirmed recipient")
    include = [i for i in body.get("include", []) if i in worker.INCLUDE_OPTIONS]
    if not include:
        raise HTTPException(400, "Choose at least one thing to include")
    params = _clean_params(body.get("params") or {})
    if not receipts.query(**{k: v for k, v in params.items() if k != "include_images"}):
        raise HTTPException(400, "No processed receipts match this selection")
    kinds = [k for k in body.get("report_kinds", []) if k in reports.KINDS]
    subject = str(body.get("subject") or "")[:150] or None
    job = db.enqueue("email", {"recipient_id": rid, "params": params, "include": include,
                               "report_kinds": kinds, "subject": subject}, priority=6)
    return {"job": job}


@app.post("/api/email/test")
def email_test(sess: dict = Depends(require_session)):
    """Send one test email to the configured account right now and report exactly what failed."""
    import smtplib
    from email.message import EmailMessage
    info = emailer.smtp_info()
    if info["missing"]:
        raise HTTPException(400, "SMTP not configured: set " + ", ".join(info["missing"]) +
                            " in .env on the Pi, then restart receipt-app and receipt-worker")
    if not emailer.online():
        raise HTTPException(503, f"Cannot reach {info['host']}:{info['port']}; is the Pi online?")
    m = EmailMessage()
    m["From"] = info["from"]
    m["To"] = info["from"]
    m["Subject"] = "Receipt scanner: test email"
    m.set_content("SMTP works. Queued reports will now be delivered automatically.")
    try:
        emailer._smtp_send(m)
    except smtplib.SMTPAuthenticationError as e:
        raise HTTPException(400, emailer._scrub(
            f"Login refused ({e.smtp_code}). For Gmail use a 16-character App Password, not your normal password."))
    except (OSError, smtplib.SMTPException) as e:
        raise HTTPException(502, emailer._scrub(f"{type(e).__name__}: {e}"))
    emailer.ensure_owner_recipient()
    return {"ok": True, "sent_to": info["from"]}


@app.get("/api/overview")
def overview(period: str | None = None, sess: dict = Depends(require_session)):
    """Desktop dashboard figures for one month - all computed, no model."""
    period = period or date.today().strftime("%Y-%m")
    try:
        receipts.query(period=period, status=None)
    except receipts.ValidationError as e:
        raise _bad(e)
    today = date.today()
    vs = reports.vat_schedule({"period": period}, today, use_llm=False)
    co = reports.compliance({"period": period}, today, use_llm=False)
    sp = reports.spending({"period": period}, today, use_llm=False)
    sup = next(s for s in sp["sections"] if s["heading"] == "By supplier")["tables"][0]["rows"][:5]
    issues = next(s for s in co["sections"] if s["heading"] == "Receipts that need attention")["tables"][0]["rows"][:8]
    due = vs["data"]["due"]
    return {
        "period": period, "label": vs["subtitle"],
        "receipts": sp["data"]["n"], "spent": sp["data"]["total"], "vat_paid": sp["data"]["vat"],
        "claimable_vat": vs["data"]["claimable_vat"], "claimable": vs["data"]["n_claimable"],
        "issues": co["data"]["n_issues"], "closing_soon": co["data"]["closing_soon"],
        "at_risk_vat": co["data"]["at_risk_vat"], "due": due,
        "days_to_due": (date.fromisoformat(due) - today).days if due else None,
        "by_rate": vs["data"]["by_rate"], "top_suppliers": sup, "issue_rows": issues,
    }


@app.get("/api/outbox")
def get_outbox(sess: dict = Depends(require_session)):
    jobs = db.all_("SELECT id, status, error, created_at FROM jobs WHERE kind='email' ORDER BY id DESC LIMIT 10")
    return {"outbox": emailer.outbox(), "compose_jobs": [dict(j) for j in jobs]}


@app.post("/api/outbox/{oid}/retry")
def outbox_retry(oid: int, sess: dict = Depends(require_session)):
    emailer.retry(oid)
    return {"ok": True}


@app.delete("/api/outbox/{oid}")
def outbox_cancel(oid: int, sess: dict = Depends(require_session)):
    emailer.cancel(oid)
    return {"ok": True}


@app.get("/api/recipients")
def get_recipients(sess: dict = Depends(require_session)):
    return {"recipients": emailer.list_recipients()}


@app.post("/api/recipients")
async def add_recipient(request: Request, sess: dict = Depends(require_session)):
    body = await _json(request)
    try:
        emailer.add_recipient(str(body.get("email", "")))
    except emailer.EmailError as e:
        raise _bad(e)
    return {"ok": True}


@app.post("/api/recipients/confirm")
async def confirm_recipient(request: Request, sess: dict = Depends(require_session)):
    body = await _json(request)
    if auth.pair_rate_limited("confirm:" + sess["id"]):
        raise HTTPException(429, "Too many attempts")
    if not emailer.confirm_recipient(str(body.get("email", "")), str(body.get("code", ""))):
        raise HTTPException(400, "Wrong or expired code")
    return {"ok": True}


@app.delete("/api/recipients/{rid}")
def delete_recipient(rid: int, sess: dict = Depends(require_session)):
    emailer.remove_recipient(rid)
    return {"ok": True}


# ------------------------------------------------------------------ settings / status


@app.get("/api/status")
def status(sess: dict = Depends(require_session)):
    s = config.settings
    return {
        "llm": llm.available(timeout=1.5),
        "smtp_configured": bool(s.smtp_user and s.smtp_password),
        "smtp": emailer.smtp_info(),
        "jobs_pending": db.one("SELECT COUNT(*) AS n FROM jobs WHERE status IN ('queued','running')")["n"],
        "outbox_queued": db.one("SELECT COUNT(*) AS n FROM outbox WHERE status IN ('queued','sending')")["n"],
        "today": date.today().isoformat(),
        "version": __version__,
    }


@app.get("/api/settings")
def get_settings(sess: dict = Depends(require_session)):
    return {
        "auto_monthly": db.kv_get("auto_monthly", {"enabled": True}),
        "categories": db.kv_get("categories", ["Stock", "Fuel", "Office", "Utilities", "Meals", "Travel",
                                               "Repairs", "Professional fees", "Other"]),
        "rates": [r.__dict__ for r in rules.load_rates()],
        "rules": {"vat3_due_day": rules.VAT3_DUE_DAY, "claim_window_months": rules.CLAIM_WINDOW_MONTHS,
                  "researched": rules.RULES_RESEARCHED, "sources": rules.SOURCES,
                  "notes": [rules.RESIDUAL_RISK, rules.INCOME_TAX_NOTE, rules.PENALTY_NOTE, rules.NIL_RETURN_NOTE]},
        "sessions": [dict(s, current=(s["id"] == sess["id"])) for s in auth.list_sessions()],
        "smtp_from": config.settings.smtp_from or config.settings.smtp_user,
    }


@app.post("/api/settings")
async def set_settings(request: Request, sess: dict = Depends(require_session)):
    body = await _json(request)
    if "auto_monthly" in body:
        db.kv_set("auto_monthly", {"enabled": bool((body["auto_monthly"] or {}).get("enabled"))})
    if "categories" in body and isinstance(body["categories"], list):
        cats = [str(c).strip()[:40] for c in body["categories"] if str(c).strip()][:50]
        db.kv_set("categories", cats)
    if "rates" in body and isinstance(body["rates"], list):
        try:
            rules.save_rates([rules.RateEntry(**{k: r.get(k) for k in rules.RateEntry.__dataclass_fields__})
                              for r in body["rates"]])
        except TypeError as e:
            raise _bad(e)
    return {"ok": True}


@app.delete("/api/sessions/{sid}")
def revoke_session(sid: str, sess: dict = Depends(require_session)):
    auth.revoke(sid)
    return {"ok": True}


# ------------------------------------------------------------------ entry


def main() -> None:
    import uvicorn
    s = config.settings
    kw = {}
    if os.path.isfile(s.cert_file) and os.path.isfile(s.key_file):
        kw = {"ssl_certfile": s.cert_file, "ssl_keyfile": s.key_file}
    else:
        print("WARNING: no TLS certificate found; serving plain HTTP (phone camera will NOT work). "
              "Run scripts/make_certs.sh")
    uvicorn.run(app, host=s.host, port=s.port, workers=1, proxy_headers=False, server_header=False, **kw)


if __name__ == "__main__":
    main()
