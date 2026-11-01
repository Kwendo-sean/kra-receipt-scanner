"""API, auth, worker, reports and outbox - runs on a laptop with OCR and the LLM mocked."""
from datetime import date

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

import synth
from app.lines import Fragment


class FakeEngine:
    """Stands in for RapidOCR: returns one fragment per line of the given text."""
    text = ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def read(self, img):
        return [Fragment(t, 10, 40 * i, 10 + 12 * len(t), 40 * i + 30) for i, t in enumerate(self.text.splitlines())]


def _jpeg(h=400, w=300) -> bytes:
    img = np.full((h, w, 3), 255, np.uint8)
    cv2.putText(img, "RECEIPT", (20, 60), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 2)
    return cv2.imencode(".jpg", img)[1].tobytes()


@pytest.fixture()
def client(tmp_settings, monkeypatch):
    from app import llm, main
    monkeypatch.setattr(llm, "available", lambda timeout=3.0: False)
    with TestClient(main.app, base_url="https://testserver") as c:
        yield c


def _pair(client) -> str:
    """Pair via a token (as the Pi's screen would) and return the CSRF token."""
    from app import auth
    tok = auth.new_pair_token()
    r = client.get(f"/pair/{tok}", follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/").text
    return page.split('name="csrf" content="')[1].split('"')[0]


def _scan(client, csrf, text, frames=1):
    from app import worker
    rid = client.post("/api/receipts", json={"mode": "single" if frames == 1 else "multi"},
                      headers={"X-CSRF-Token": csrf}).json()["id"]
    for i in range(frames):
        r = client.post(f"/api/receipts/{rid}/frames", files={"file": ("f.jpg", _jpeg(), "image/jpeg")},
                        data={"idx": str(i)}, headers={"X-CSRF-Token": csrf})
        assert r.status_code == 200, r.text
    assert client.post(f"/api/receipts/{rid}/submit", json={"ocr_mode": "dedupe"},
                       headers={"X-CSRF-Token": csrf}).status_code == 200
    FakeEngine.text = text
    from app import db
    job = db.claim_job(["ocr"])
    worker.handle_ocr(db.jload(job["payload_json"]), engine_factory=FakeEngine)
    db.finish_job(job["id"])
    return rid


# ------------------------------------------------------------------ auth


def test_everything_requires_pairing(client):
    for url in ["/api/receipts", "/api/receipts/1", "/media/receipts/1/frame/0.jpg", "/media/receipts/1/stitched.jpg",
                "/api/reports", "/reports/1/view", "/reports/1/download/pdf", "/api/outbox", "/api/settings",
                "/api/status", "/api/recipients"]:
        assert client.get(url).status_code == 401, url
    assert client.post("/api/receipts", json={}).status_code == 401
    # unpaired root shows the pairing help, not the app
    assert 'name="csrf"' not in client.get("/").text


def test_media_dir_not_served_statically(client, tmp_settings):
    p = tmp_settings.media_dir / "secret.jpg"
    p.write_bytes(_jpeg())
    for url in ["/static/../data/media/secret.jpg", "/media/secret.jpg", "/data/media/secret.jpg"]:
        assert client.get(url).status_code in (401, 404)


def test_pair_token_single_use_and_expiry(client, tmp_settings, monkeypatch):
    from app import auth
    tok = auth.new_pair_token()
    assert client.get(f"/pair/{tok}", follow_redirects=False).status_code == 303
    c2 = TestClient(client.app, base_url="https://testserver")
    assert "invalid, used or expired" in c2.get(f"/pair/{tok}").text  # reuse fails
    import time as _t
    tok2 = auth.new_pair_token()
    real = _t.time
    monkeypatch.setattr(auth.time, "time", lambda: real() + tmp_settings.pair_token_ttl + 5)
    assert "invalid, used or expired" in c2.get(f"/pair/{tok2}").text


def test_pairing_rate_limited(client):
    for _ in range(10):
        client.get("/pair/nope")
    assert client.get("/pair/nope").status_code == 429


def test_pairing_qr_not_offered_to_unpaired_network_browser(client):
    # TestClient's peer is not localhost -> pairing endpoints refuse
    assert client.get("/api/pair/new").status_code == 403


def test_csrf_required_for_writes(client):
    csrf = _pair(client)
    assert client.post("/api/receipts", json={}).status_code == 403
    assert client.post("/api/receipts", json={}, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post("/api/receipts", json={}, headers={"X-CSRF-Token": csrf}).status_code == 200


def test_unknown_host_rejected(client):
    assert client.get("/", headers={"host": "evil.example"}).status_code == 421


def test_revoked_session_loses_access(client):
    csrf = _pair(client)
    assert client.get("/api/status").status_code == 200
    client.post("/api/logout", headers={"X-CSRF-Token": csrf})
    assert client.get("/api/status").status_code == 401


def test_security_headers(client):
    r = client.get("/")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["referrer-policy"] == "no-referrer"


# ------------------------------------------------------------------ scan -> parse -> correct


def test_scan_flow_and_media_auth(client):
    csrf = _pair(client)
    rid = _scan(client, csrf, synth.supermarket_16_exempt().text)
    r = client.get(f"/api/receipts/{rid}").json()
    assert r["status"] == "done"
    assert r["fields"]["seller_pin"] == "P051234567X" and r["fields"]["total"] == 1600.0
    assert r["claim"]["claimable"] is True
    img = client.get(f"/media/receipts/{rid}/frame/0.jpg")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    # another (unpaired) client can't see it
    other = TestClient(client.app, base_url="https://testserver")
    assert other.get(f"/media/receipts/{rid}/frame/0.jpg").status_code == 401


def test_non_jpeg_upload_rejected(client):
    csrf = _pair(client)
    rid = client.post("/api/receipts", json={}, headers={"X-CSRF-Token": csrf}).json()["id"]
    r = client.post(f"/api/receipts/{rid}/frames", files={"file": ("x.jpg", b"<html>", "image/jpeg")},
                    headers={"X-CSRF-Token": csrf})
    assert r.status_code == 400


def test_corrections_kept_across_reocr_and_audited(client):
    from app import db, worker
    csrf = _pair(client)
    rid = _scan(client, csrf, synth.kiosk_no_vat().text)
    r = client.patch(f"/api/receipts/{rid}", json={"changes": {"seller_pin": "p051234567x", "category": "Meals"}},
                     headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and r.json()["fields"]["seller_pin"] == "P051234567X"
    bad = client.patch(f"/api/receipts/{rid}", json={"changes": {"seller_pin": "123"}}, headers={"X-CSRF-Token": csrf})
    assert bad.status_code == 400
    # re-read the photo: user's correction survives
    client.post(f"/api/receipts/{rid}/reprocess", json={}, headers={"X-CSRF-Token": csrf})
    job = db.claim_job(["ocr"])
    worker.handle_ocr(db.jload(job["payload_json"]), engine_factory=FakeEngine)
    r = client.get(f"/api/receipts/{rid}").json()
    assert r["fields"]["seller_pin"] == "P051234567X" and r["parsed"]["seller_pin"] is None
    actions = [a["action"] for a in client.get(f"/api/receipts/{rid}/audit").json()["audit"]]
    assert "corrected" in actions and actions.count("ocr_done") == 2


def test_multi_section_receipt_dedupes(client):
    csrf = _pair(client)
    rid = _scan(client, csrf, synth.fuel_8().text, frames=3)
    r = client.get(f"/api/receipts/{rid}").json()
    # FakeEngine returns the same full text for each frame: overlap de-dup must collapse it
    assert r["fields"]["total"] == 6860.0 and len(r["fields"]["items"]) == 1


# ------------------------------------------------------------------ reports


def _three_receipts(client, csrf):
    return [_scan(client, csrf, fn().text) for fn in (synth.supermarket_16_exempt, synth.etims_gross_style,
                                                       synth.kiosk_no_vat)]


def test_report_figures_are_computed_not_modelled(client):
    from app import reports
    csrf = _pair(client)
    _three_receipts(client, csrf)
    rep = reports.build("vat_schedule", {"period": "2026-10"}, today=date(2026, 11, 1), use_llm=False)
    # Oct 2026: supermarket (193.10) + quickmart (71.72) are claimable
    assert rep["data"]["n"] == 2 and rep["data"]["n_claimable"] == 2
    assert rep["data"]["claimable_vat"] == pytest.approx(264.82)
    assert rep["data"]["claimable_taxable"] == pytest.approx(1206.90 + 448.28)
    assert rep["data"]["due"] == "2026-11-20"
    assert rep["data"]["by_rate"][16.0] == pytest.approx([1655.18, 264.82])
    comp = reports.build("compliance", {"period": "2026-11"}, today=date(2026, 11, 5), use_llm=False)
    assert comp["data"]["n_issues"] == 1  # the kiosk receipt
    sp = reports.build("spending", {}, today=date(2026, 11, 5), use_llm=False)
    assert sp["data"]["total"] == pytest.approx(1600 + 880 + 250)


def test_reports_render_all_formats_with_llm_down(client, tmp_settings):
    from app import db, worker
    csrf = _pair(client)
    ids = _three_receipts(client, csrf)
    for kind, params in [("vat_schedule", {"period": "2026-10"}), ("compliance", {"period": "2026-10"}),
                         ("spending", {"period": "2026-10"}), ("narrative", {"period": "2026-10"}),
                         ("receipt", {"ids": [ids[0]]})]:
        r = client.post("/api/reports", json={"kind": kind, "params": params}, headers={"X-CSRF-Token": csrf})
        assert r.status_code == 200, r.text
        job = db.claim_job(["report"])
        worker.handle_report(db.jload(job["payload_json"]), use_llm=False)
        rid = r.json()["id"]
        view = client.get(f"/reports/{rid}/view")
        assert view.status_code == 200 and "not tax advice" in view.text and "model unavailable" in view.text
        for fmt, magic in (("pdf", b"%PDF"), ("xlsx", b"PK"), ("csv", b"\xef\xbb\xbf")):
            d = client.get(f"/reports/{rid}/download/{fmt}")
            assert d.status_code == 200 and d.content.startswith(magic), (kind, fmt)


def test_report_html_escapes_model_and_receipt_text(client):
    from app import render, reports
    csrf = _pair(client)
    rid = _scan(client, csrf, synth.kiosk_no_vat().text)
    client.patch(f"/api/receipts/{rid}", json={"changes": {"seller_name": "<img src=x onerror=alert(1)>"}},
                 headers={"X-CSRF-Token": csrf})
    rep = reports.build("receipt", {"ids": [rid]}, use_llm=False)
    html = render.to_html(rep)
    assert "<img src=x" not in html and "&lt;img src=x" in html


def test_llm_prose_used_when_numbers_check(client, monkeypatch):
    from app import llm, reports
    csrf = _pair(client)
    _three_receipts(client, csrf)
    monkeypatch.setattr(llm, "chat", lambda s, u, max_tokens=None: "Two receipts are claimable, worth KES 264.82.")
    rep = reports.build("vat_schedule", {"period": "2026-10"}, today=date(2026, 11, 1), use_llm=True)
    assert rep["sections"][0]["prose_status"] == "llm"
    monkeypatch.setattr(llm, "chat", lambda s, u, max_tokens=None: "You can claim KES 300.00.")
    rep = reports.build("vat_schedule", {"period": "2026-10"}, today=date(2026, 11, 1), use_llm=True)
    assert rep["sections"][0]["prose_status"] == "template_rejected"
    assert "264.82" in rep["sections"][0]["prose"]


# ------------------------------------------------------------------ email outbox


def _owner(tmp_settings, monkeypatch):
    from app import emailer
    monkeypatch.setattr(tmp_settings, "smtp_from", "owner@example.com")
    emailer.ensure_owner_recipient()
    return [r for r in emailer.list_recipients() if r["email"] == "owner@example.com"][0]["id"]


def test_email_only_to_confirmed_recipients(client, tmp_settings, monkeypatch):
    from app import emailer
    csrf = _pair(client)
    rid = _scan(client, csrf, synth.supermarket_16_exempt().text)
    client.post("/api/recipients", json={"email": "stranger@example.com"}, headers={"X-CSRF-Token": csrf})
    stranger = [r for r in emailer.list_recipients() if r["email"] == "stranger@example.com"][0]
    r = client.post("/api/email", json={"recipient_id": stranger["id"], "params": {"ids": [rid]}, "include": ["pdf"]},
                    headers={"X-CSRF-Token": csrf})
    assert r.status_code == 400
    # arbitrary address can't be smuggled in
    r = client.post("/api/email", json={"recipient": "x@evil.com", "params": {"ids": [rid]}, "include": ["pdf"]},
                    headers={"X-CSRF-Token": csrf})
    assert r.status_code == 400
    # the confirmation email holds a code; confirming enables the address
    row = [o for o in emailer.outbox() if o["recipient"] == "stranger@example.com"][0]
    from app import db
    body = db.one("SELECT body FROM outbox WHERE id=?", row["id"])["body"]
    code = body.split("code is ")[1][:6]
    assert client.post("/api/recipients/confirm", json={"email": "stranger@example.com", "code": "000000" if code != "000000" else "111111"},
                       headers={"X-CSRF-Token": csrf}).status_code == 400
    assert client.post("/api/recipients/confirm", json={"email": "stranger@example.com", "code": code},
                       headers={"X-CSRF-Token": csrf}).status_code == 200


def test_email_single_and_monthly_queue_offline_then_send(client, tmp_settings, monkeypatch):
    from app import db, emailer, worker
    csrf = _pair(client)
    owner = _owner(tmp_settings, monkeypatch)
    ids = _three_receipts(client, csrf)
    # single receipt with images + OCR text
    r = client.post("/api/email", json={"recipient_id": owner, "params": {"ids": [ids[0]]},
                                        "include": ["pdf", "images", "ocr_text"], "report_kinds": ["receipt"]},
                    headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    # monthly collection
    r = client.post("/api/email", json={"recipient_id": owner, "params": {"period": "2026-10"},
                                        "include": ["pdf", "xlsx", "csv"], "report_kinds": ["vat_schedule", "compliance"]},
                    headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    for _ in range(2):
        job = db.claim_job(["email"])
        res = worker.handle_email(db.jload(job["payload_json"]), use_llm=False)
        db.finish_job(job["id"], res)
    ob = client.get("/api/outbox").json()["outbox"]
    assert len(ob) == 2 and all(o["status"] == "queued" for o in ob)
    names = sorted(sum((o["attachments"] for o in ob), []))
    assert any(n.endswith(".pdf") for n in names) and "receipts-images.zip" in names and "ocr-text.txt" in names
    assert any(n.endswith(".xlsx") for n in names) and any(n.endswith(".csv") for n in names)
    # offline: nothing sent, nothing lost
    st = emailer.send_due(sender=lambda m: None, is_online=lambda: False)
    assert st["offline"] and all(o["status"] == "queued" for o in emailer.outbox())
    # online but SMTP flaky: retried with backoff
    def flaky(m):
        raise OSError("network unreachable")
    st = emailer.send_due(sender=flaky, is_online=lambda: True)
    assert st["deferred"] == 2
    assert all(o["next_attempt"] > 0 and o["status"] == "queued" for o in emailer.outbox())
    # later: sends
    sent = []
    import time as _t
    st = emailer.send_due(sender=lambda m: sent.append(m), is_online=lambda: True, now=_t.time() + 10_000)
    assert st["sent"] == 2 and len(sent) == 2
    assert all(o["status"] == "sent" for o in emailer.outbox())
    assert sent[0]["To"] == "owner@example.com"


def test_large_collections_split_under_limit(tmp_settings, tmp_path):
    from app import emailer
    files = []
    for i in range(5):
        p = tmp_path / f"f{i}.bin"
        p.write_bytes(b"x" * 3_000_000)
        files.append((p.name, str(p)))
    parts = emailer.split_attachments(files, 10_000_000)
    assert len(parts) == 3 and sum(len(p) for p in parts) == 5
    for p in parts:
        assert sum(3_000_000 * emailer.B64_OVERHEAD for _ in p) <= 10_000_000


def test_password_never_stored_in_errors(tmp_settings, monkeypatch):
    from app import emailer
    monkeypatch.setattr(tmp_settings, "smtp_password", "hunter2pass")
    assert "hunter2pass" not in emailer._scrub("login failed for hunter2pass")


def test_monthly_auto_email_once(client, tmp_settings, monkeypatch):
    from app import db, worker
    _owner(tmp_settings, monkeypatch)
    assert worker.monthly_auto(date(2026, 11, 10)) is None  # outside 1st-5th
    assert worker.monthly_auto(date(2026, 11, 2)) == 1
    assert worker.monthly_auto(date(2026, 11, 3)) is None  # already done this month
    job = db.one("SELECT payload_json FROM jobs WHERE kind='email'")
    assert db.jload(job["payload_json"])["params"] == {"period": "2026-10"}


# ------------------------------------------------------------------ desktop dashboard / SMTP diagnostics


def test_overview_and_list_claimable(client):
    csrf = _pair(client)
    _three_receipts(client, csrf)
    o = client.get("/api/overview?period=2026-10").json()
    assert o["receipts"] == 2 and o["claimable"] == 2
    assert o["claimable_vat"] == pytest.approx(264.82) and o["due"] == "2026-11-20"
    assert client.get("/api/overview?period=bad").status_code == 400
    rows = client.get("/api/receipts").json()["receipts"]
    kiosk = [r for r in rows if r["seller_name"] == "MAMA WANJIKU KIOSK"][0]
    assert kiosk["claimable"] is False and "Supplier KRA PIN not printed" in kiosk["issues"]
    assert "parsed_json" not in kiosk


def test_smtp_status_and_test_endpoint(client, tmp_settings, monkeypatch):
    from app import emailer
    csrf = _pair(client)
    st = client.get("/api/status").json()["smtp"]
    assert st["missing"] == ["RS_SMTP_USER", "RS_SMTP_PASSWORD"] and "password" not in st
    r = client.post("/api/email/test", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 400 and "RS_SMTP_USER" in r.json()["detail"]
    monkeypatch.setattr(tmp_settings, "smtp_user", "me@gmail.com")
    monkeypatch.setattr(tmp_settings, "smtp_password", "abcdefghijklmnop")
    monkeypatch.setattr(emailer, "online", lambda timeout=4.0: True)
    sent = []
    monkeypatch.setattr(emailer, "_smtp_send", lambda m: sent.append(m))
    r = client.post("/api/email/test", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and sent[0]["To"] == "me@gmail.com"
    assert any(x["email"] == "me@gmail.com" and x["confirmed"] for x in emailer.list_recipients())


def test_gmail_app_password_spaces_removed(monkeypatch):
    from app import config
    monkeypatch.setenv("RS_SMTP_PASSWORD", "abcd efgh ijkl mnop")
    assert config.Settings().smtp_password == "abcdefghijklmnop"


def test_outbox_waits_for_smtp_config_then_recovers(client, tmp_settings, monkeypatch):
    from app import db, emailer
    _owner(tmp_settings, monkeypatch)
    owner = [r for r in emailer.list_recipients() if r["confirmed"]][0]["id"]
    emailer.queue(owner, "hello", "body", [])
    # not configured: stays queued (not failed) with an explanatory note
    st = emailer.send_due()
    assert st["not_configured"] and emailer.outbox()[0]["status"] == "queued"
    assert "SMTP not configured" in emailer.outbox()[0]["last_error"]
    # an old-style permanent failure is re-queued once SMTP is set
    db.run("UPDATE outbox SET status='failed', last_error=?",
           "SMTP not configured (set RS_SMTP_USER / RS_SMTP_PASSWORD in .env)")
    assert emailer.requeue_config_failures() == 0  # still not configured
    monkeypatch.setattr(tmp_settings, "smtp_user", "owner@example.com")
    monkeypatch.setattr(tmp_settings, "smtp_password", "abcdefghijklmnop")
    assert emailer.requeue_config_failures() == 1
    sent = []
    st = emailer.send_due(sender=lambda m: sent.append(m), is_online=lambda: True)
    assert st["sent"] == 1 and emailer.outbox()[0]["status"] == "sent"


# ------------------------------------------------------------------ clean scan / import


def test_white_background_removes_table():
    from app.preprocess import paper_mask, white_background
    img = synth.photograph(synth.render(synth.fuel_8()), angle=0)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    m = paper_mask(gray)
    assert m is not None
    out = white_background(gray, m)
    assert out[5, 5] == 255 and out[-5, -5] == 255          # table corners now white
    h, w = gray.shape
    assert (out[h // 2, w // 2] == gray[h // 2, w // 2])     # paper untouched
    # paper filling the whole frame: no mask, nothing changed
    full = cv2.cvtColor(synth.render(synth.fuel_8()), cv2.COLOR_BGR2GRAY)
    assert paper_mask(full) is None


def _pdf_with_receipt() -> bytes:
    import io
    from PIL import Image
    img = synth.render(synth.fuel_8())
    buf = io.BytesIO()
    Image.fromarray(img[..., ::-1]).save(buf, format="PDF", resolution=150)
    return buf.getvalue()


def test_import_pdf_and_png(client):
    from app import db, receipts, worker
    csrf = _pair(client)
    png = cv2.imencode(".png", synth.render(synth.kiosk_no_vat()))[1].tobytes()
    r = client.post("/api/receipts/import", headers={"X-CSRF-Token": csrf},
                    files=[("files", ("scan.pdf", _pdf_with_receipt(), "application/pdf")),
                           ("files", ("kiosk.png", png, "image/png"))])
    assert r.status_code == 200, r.text
    ids = r.json()["ids"]
    assert len(ids) == 2
    for rid in ids:
        fr = receipts.frames(rid)
        assert len(fr) == 1 and open(fr[0]["path"], "rb").read(3) == b"\xff\xd8\xff"
    # both queued for OCR; process one and check the clean scan is saved and served
    FakeEngine.text = synth.fuel_8().text
    job = db.claim_job(["ocr"])
    worker.handle_ocr(db.jload(job["payload_json"]), engine_factory=FakeEngine)
    rid = db.jload(job["payload_json"])["receipt_id"]
    assert client.get(f"/api/receipts/{rid}").json()["has_scan"] is True
    s = client.get(f"/media/receipts/{rid}/scan.jpg")
    assert s.status_code == 200 and s.headers["content-type"] == "image/jpeg"
    assert TestClient(client.app, base_url="https://testserver").get(f"/media/receipts/{rid}/scan.jpg").status_code == 401


def test_import_as_one_long_receipt_and_rejects_junk(client):
    from app import receipts
    csrf = _pair(client)
    jpg = _jpeg()
    r = client.post("/api/receipts/import", headers={"X-CSRF-Token": csrf}, data={"one_receipt": "true"},
                    files=[("files", ("a.jpg", jpg, "image/jpeg")), ("files", ("b.jpg", jpg, "image/jpeg"))])
    ids = r.json()["ids"]
    assert len(ids) == 1 and len(receipts.frames(ids[0])) == 2
    bad = client.post("/api/receipts/import", headers={"X-CSRF-Token": csrf},
                      files=[("files", ("x.txt", b"hello", "text/plain"))])
    assert bad.status_code == 400


def test_ocr_model_size_setting(tmp_settings, monkeypatch):
    from app import ocr
    monkeypatch.setattr(tmp_settings, "ocr_extra_dir", str(tmp_settings.data_dir / "nowhere"))
    with pytest.raises(ValueError):
        ocr.model_paths("huge", "small")
    with pytest.raises(FileNotFoundError, match="fetch_models"):
        ocr.model_paths("small", "medium")


# ------------------------------------------------------------------ iPhone Shortcut (Apple document scanner)


def test_shortcut_upload_with_upload_only_key(client):
    from app import receipts
    csrf = _pair(client)
    j = client.post("/api/apikeys", json={"name": "Sean's iPhone"}, headers={"X-CSRF-Token": csrf}).json()
    key, url = j["key"], j["upload_url"]
    assert key.startswith("rsk_") and url.endswith("/api/shortcut/upload")
    phone = TestClient(client.app, base_url="https://testserver")   # no session cookie, like Shortcuts
    auth_h = {"Authorization": f"Bearer {key}"}
    # raw body (Shortcuts: Request Body = File)
    r = phone.post("/api/shortcut/upload", content=_pdf_with_receipt(), headers=auth_h)
    assert r.status_code == 200, r.text
    body = r.json()
    rid = body["ids"][0]
    assert "sent to the Pi" in body["message"] and body["url"].endswith(f"#receipt-{rid}")
    assert receipts.get(rid)["status"] == "queued"
    # multipart also works
    r = phone.post("/api/shortcut/upload", files={"file": ("scan.jpg", _jpeg(), "image/jpeg")}, headers=auth_h)
    assert r.status_code == 200
    # the key cannot read anything
    for u in ["/api/receipts", f"/api/receipts/{rid}", f"/media/receipts/{rid}/frame/0.jpg", "/api/apikeys"]:
        assert phone.get(u, headers=auth_h).status_code == 401, u
    # bad / missing / revoked keys
    assert phone.post("/api/shortcut/upload", content=b"x", headers={"Authorization": "Bearer rsk_nope"}).status_code == 401
    assert phone.post("/api/shortcut/upload", content=b"x").status_code == 401
    kid = client.get("/api/apikeys").json()["keys"][0]["id"]
    client.delete(f"/api/apikeys/{kid}", headers={"X-CSRF-Token": csrf})
    assert phone.post("/api/shortcut/upload", content=_jpeg(), headers=auth_h).status_code == 401


def test_shortcut_upload_rejects_empty_and_junk(client):
    csrf = _pair(client)
    key = client.post("/api/apikeys", json={}, headers={"X-CSRF-Token": csrf}).json()["key"]
    phone = TestClient(client.app, base_url="https://testserver")
    h = {"Authorization": f"Bearer {key}"}
    assert phone.post("/api/shortcut/upload", content=b"", headers=h).status_code == 400
    assert phone.post("/api/shortcut/upload", content=b"not an image", headers=h).status_code == 400
