"""Phone pairing (single-use, short-lived QR tokens), sessions and CSRF."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from app import config, db

COOKIE = "rs_session"
PAIR_RATE_MAX = 10  # attempts per IP
PAIR_RATE_WINDOW = 300  # seconds


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ------------------------------------------------------------------ pairing


def new_pair_token() -> str:
    tok = secrets.token_urlsafe(24)
    now = time.time()
    db.run("DELETE FROM pair_tokens WHERE expires_at < ?", now - 3600)
    db.run("INSERT INTO pair_tokens(token_hash, created_at, expires_at) VALUES (?,?,?)",
           _h(tok), now, now + config.settings.pair_token_ttl)
    return tok


def pair_rate_limited(ip: str) -> bool:
    now = time.time()
    db.run("DELETE FROM pair_attempts WHERE at < ?", now - PAIR_RATE_WINDOW)
    n = db.one("SELECT COUNT(*) AS n FROM pair_attempts WHERE ip=?", ip)["n"]
    db.run("INSERT INTO pair_attempts(ip, at) VALUES (?,?)", ip, now)
    return n >= PAIR_RATE_MAX


def redeem_pair_token(token: str, user_agent: str = "") -> str | None:
    """Single use: returns a new session token, or None if invalid/expired/used."""
    now = time.time()
    with db.tx() as c:
        row = c.execute("SELECT * FROM pair_tokens WHERE token_hash=?", (_h(token),)).fetchone()
        if not row or row["used_at"] is not None or row["expires_at"] < now:
            return None
        c.execute("UPDATE pair_tokens SET used_at=? WHERE token_hash=?", (now, _h(token)))
        sess = secrets.token_urlsafe(32)
        label = _device_label(user_agent)
        c.execute("INSERT INTO sessions(id, created_at, last_seen, expires_at, user_agent, label) VALUES (?,?,?,?,?,?)",
                  (_h(sess), now, now, now + config.settings.session_days * 86400, user_agent[:300], label))
    db.audit(None, "paired", new={"device": label}, actor="phone")
    return sess


def _device_label(ua: str) -> str:
    ua = ua or ""
    for k, v in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android phone"),
                 ("Windows", "Windows PC"), ("Macintosh", "Mac"), ("Linux", "Linux")):
        if k in ua:
            return v
    return "Device"


# ------------------------------------------------------------------ sessions


def session_from_token(token: str | None) -> dict | None:
    if not token:
        return None
    row = db.one("SELECT * FROM sessions WHERE id=?", _h(token))
    now = time.time()
    if not row or row["revoked"] or row["expires_at"] < now:
        return None
    if now - row["last_seen"] > 60:
        db.run("UPDATE sessions SET last_seen=? WHERE id=?", now, row["id"])
    return dict(row)


def list_sessions() -> list[dict]:
    return [dict(r) for r in db.all_("SELECT id, created_at, last_seen, label, revoked FROM sessions "
                                     "WHERE revoked=0 AND expires_at > ? ORDER BY last_seen DESC", time.time())]


def revoke(session_id: str) -> None:
    db.run("UPDATE sessions SET revoked=1 WHERE id=?", session_id)
    db.audit(None, "session_revoked", new={"session": session_id[:8]})


# ------------------------------------------------------------------ CSRF


def csrf_token(session_id: str) -> str:
    return hmac.new(config.settings.get_secret_key(), f"csrf:{session_id}".encode(), hashlib.sha256).hexdigest()


def csrf_ok(session_id: str, token: str | None) -> bool:
    return bool(token) and hmac.compare_digest(csrf_token(session_id), token)
