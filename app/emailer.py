"""Email outbox: queue, split by size, send over SMTP when online, retry with backoff.

Only addresses saved *and confirmed* in settings can receive mail. The SMTP
password is read from config only inside `_smtp_send` and is scrubbed from any
stored error text.
"""
from __future__ import annotations

import hashlib
import hmac
import mimetypes
import os
import re
import secrets
import smtplib
import socket
import ssl
import time
import uuid
import zipfile
from email.message import EmailMessage
from pathlib import Path

from app import config, db

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
MAX_ATTEMPTS = 12
B64_OVERHEAD = 1.37


class EmailError(ValueError):
    pass


def _hash_code(code: str) -> str:
    return hmac.new(config.settings.get_secret_key(), code.encode(), hashlib.sha256).hexdigest()


# ------------------------------------------------------------------ recipients


def ensure_owner_recipient() -> None:
    """The sending account configured on the Pi (.env) is the owner's own address:
    it is saved as confirmed. Any other address needs a confirmation code."""
    s = config.settings
    addr = (s.smtp_from or s.smtp_user or "").strip().lower()
    if addr and EMAIL_RE.match(addr):
        db.run("INSERT INTO recipients(email, confirmed, created_at, confirmed_at) VALUES (?,1,?,?) "
               "ON CONFLICT(email) DO UPDATE SET confirmed=1", addr, time.time(), time.time())


def list_recipients() -> list[dict]:
    return [dict(r) for r in db.all_("SELECT id, email, confirmed, created_at, confirmed_at FROM recipients ORDER BY email")]


def add_recipient(email: str) -> None:
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        raise EmailError("Invalid email address")
    code = f"{secrets.randbelow(10**6):06d}"
    existing = db.one("SELECT confirmed FROM recipients WHERE email=?", email)
    if existing and existing["confirmed"]:
        return
    db.run("INSERT INTO recipients(email, confirmed, confirm_code_hash, created_at) VALUES (?,0,?,?) "
           "ON CONFLICT(email) DO UPDATE SET confirm_code_hash=excluded.confirm_code_hash",
           email, _hash_code(code), time.time())
    _queue_raw(email, "Confirm this address for your receipt scanner",
               f"Your confirmation code is {code}.\n\nEnter it in Settings on the receipt scanner to allow "
               "reports to be sent to this address. If you did not request this, ignore this email.", [])


def confirm_recipient(email: str, code: str) -> bool:
    email = email.strip().lower()
    r = db.one("SELECT confirm_code_hash FROM recipients WHERE email=?", email)
    if not r or not r["confirm_code_hash"] or not hmac.compare_digest(r["confirm_code_hash"], _hash_code(code.strip())):
        return False
    db.run("UPDATE recipients SET confirmed=1, confirmed_at=?, confirm_code_hash=NULL WHERE email=?", time.time(), email)
    return True


def remove_recipient(rid: int) -> None:
    db.run("DELETE FROM recipients WHERE id=?", rid)


def confirmed_email(recipient_id: int) -> str:
    r = db.one("SELECT email, confirmed FROM recipients WHERE id=?", recipient_id)
    if not r or not r["confirmed"]:
        raise EmailError("Recipient is not a confirmed address")
    return r["email"]


# ------------------------------------------------------------------ queueing


def zip_files(paths: list[tuple[str, str]], dest: Path) -> Path:
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for arcname, p in paths:
            z.write(p, arcname)
    return dest


def split_attachments(atts: list[tuple[str, str]], limit: int) -> list[list[tuple[str, str]]]:
    """First-fit-decreasing packing of (name, path) attachments into emails under
    `limit` bytes (after base64). An attachment bigger than the limit goes alone
    and is failed loudly at send time rather than silently dropped."""
    parts: list[tuple[int, list[tuple[str, str]]]] = []
    for name, p in sorted(atts, key=lambda a: -os.path.getsize(a[1])):
        sz = int(os.path.getsize(p) * B64_OVERHEAD)
        for i, (used, items) in enumerate(parts):
            if used + sz <= limit:
                items.append((name, p))
                parts[i] = (used + sz, items)
                break
        else:
            parts.append((sz, [(name, p)]))
    return [items for _, items in parts] or [[]]


def zip_images_in_chunks(images: list[tuple[str, str]], dest_dir: Path, limit: int, stem: str) -> list[tuple[str, str]]:
    """Zip images into one or more archives, each under the size limit."""
    out = []
    chunk: list[tuple[str, str]] = []
    size = 0
    n = 1
    budget = int(limit / B64_OVERHEAD * 0.95)
    for name, p in images:
        s = os.path.getsize(p)
        if chunk and size + s > budget:
            dest = dest_dir / f"{stem}-images-{n}.zip"
            out.append((dest.name, str(zip_files(chunk, dest))))
            chunk, size, n = [], 0, n + 1
        chunk.append((name, p))
        size += s
    if chunk:
        name = f"{stem}-images.zip" if n == 1 else f"{stem}-images-{n}.zip"
        dest = dest_dir / name
        out.append((dest.name, str(zip_files(chunk, dest))))
    return out


def _queue_raw(email: str, subject: str, body: str, atts: list[tuple[str, str]], group: str | None = None,
               part: int = 1, parts: int = 1) -> int:
    size = sum(int(os.path.getsize(p) * B64_OVERHEAD) for _, p in atts) + len(body)
    return db.run("INSERT INTO outbox(created_at, group_id, part, parts, recipient, subject, body, attachments_json, "
                  "size_bytes) VALUES (?,?,?,?,?,?,?,?,?)",
                  time.time(), group or uuid.uuid4().hex[:12], part, parts, email, subject, body,
                  db.jdump([{"name": n, "path": p} for n, p in atts]), size)


def queue(recipient_id: int, subject: str, body: str, atts: list[tuple[str, str]]) -> list[int]:
    """Queue one logical email to a confirmed recipient, split into parts if needed."""
    email = confirmed_email(recipient_id)
    limit = config.settings.email_max_bytes
    parts = split_attachments(atts, limit) if atts else [[]]
    group = uuid.uuid4().hex[:12]
    ids = []
    for i, p in enumerate(parts, start=1):
        sub = subject if len(parts) == 1 else f"{subject} (part {i} of {len(parts)})"
        b = body if len(parts) == 1 else body + f"\n\nThis is part {i} of {len(parts)}."
        ids.append(_queue_raw(email, sub, b, p, group, i, len(parts)))
    return ids


def outbox(limit: int = 100) -> list[dict]:
    rows = db.all_("SELECT id, created_at, group_id, part, parts, recipient, subject, size_bytes, status, attempts, "
                   "next_attempt, last_error, sent_at, attachments_json FROM outbox ORDER BY id DESC LIMIT ?", limit)
    out = []
    for r in rows:
        d = dict(r)
        d["attachments"] = [a["name"] for a in db.jload(d.pop("attachments_json"), [])]
        out.append(d)
    return out


def retry(outbox_id: int) -> None:
    db.run("UPDATE outbox SET status='queued', next_attempt=0 WHERE id=? AND status IN ('failed','queued')", outbox_id)


def cancel(outbox_id: int) -> None:
    db.run("DELETE FROM outbox WHERE id=? AND status IN ('failed','queued')", outbox_id)


# ------------------------------------------------------------------ sending


def online(timeout: float = 4.0) -> bool:
    s = config.settings
    try:
        with socket.create_connection((s.smtp_host, s.smtp_port), timeout=timeout):
            return True
    except OSError:
        return False


def _scrub(msg: str) -> str:
    pw = config.settings.smtp_password
    if pw:
        msg = msg.replace(pw, "***")
    return msg[:500]


def build_message(row: dict) -> EmailMessage:
    s = config.settings
    m = EmailMessage()
    m["From"] = s.smtp_from or s.smtp_user
    m["To"] = row["recipient"]
    m["Subject"] = row["subject"]
    m.set_content(row["body"])
    for a in db.jload(row["attachments_json"], []):
        p = Path(a["path"])
        ctype = mimetypes.guess_type(a["name"])[0] or "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        m.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype, filename=a["name"])
    return m


def _smtp_send(msg: EmailMessage) -> None:
    s = config.settings
    if not (s.smtp_user and s.smtp_password):
        raise EmailError("SMTP not configured (set RS_SMTP_USER / RS_SMTP_PASSWORD in .env)")
    ctx = ssl.create_default_context()
    if s.smtp_port == 465:
        with smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, context=ctx, timeout=60) as c:
            c.login(s.smtp_user, s.smtp_password)
            c.send_message(msg)
    else:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=60) as c:
            c.starttls(context=ctx)
            c.login(s.smtp_user, s.smtp_password)
            c.send_message(msg)


def backoff(attempts: int) -> float:
    return min(6 * 3600, 60 * (2 ** max(0, attempts - 1)))


def send_due(sender=None, is_online=None, now: float | None = None) -> dict:
    """Send queued emails whose time has come. Returns counts. `sender` and
    `is_online` are injectable for tests."""
    sender = sender or _smtp_send
    is_online = is_online or online
    now = now or time.time()
    rows = db.all_("SELECT * FROM outbox WHERE status='queued' AND next_attempt <= ? ORDER BY id", now)
    stats = {"sent": 0, "failed": 0, "deferred": 0, "offline": False}
    if not rows:
        return stats
    if not is_online():
        stats["offline"] = True
        return stats
    for r in rows:
        r = dict(r)
        if r["size_bytes"] > config.settings.email_max_bytes * 1.05:
            db.run("UPDATE outbox SET status='failed', last_error=? WHERE id=?",
                   "Attachment larger than the email size limit", r["id"])
            stats["failed"] += 1
            continue
        db.run("UPDATE outbox SET status='sending', attempts=attempts+1 WHERE id=?", r["id"])
        try:
            sender(build_message(r))
        except smtplib.SMTPAuthenticationError as e:
            db.run("UPDATE outbox SET status='failed', last_error=? WHERE id=?",
                   _scrub(f"SMTP login failed ({e.smtp_code}); check the app password"), r["id"])
            stats["failed"] += 1
            continue
        except (OSError, smtplib.SMTPException, EmailError) as e:
            attempts = r["attempts"] + 1
            if attempts >= MAX_ATTEMPTS or isinstance(e, EmailError):
                db.run("UPDATE outbox SET status='failed', last_error=? WHERE id=?", _scrub(str(e)), r["id"])
                stats["failed"] += 1
            else:
                db.run("UPDATE outbox SET status='queued', next_attempt=?, last_error=? WHERE id=?",
                       now + backoff(attempts), _scrub(str(e)), r["id"])
                stats["deferred"] += 1
            continue
        db.run("UPDATE outbox SET status='sent', sent_at=?, last_error=NULL WHERE id=?", time.time(), r["id"])
        stats["sent"] += 1
    return stats
