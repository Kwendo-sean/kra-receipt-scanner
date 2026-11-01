"""Runtime settings, read from environment / .env. Secrets never leave this module
except where they are needed (SMTP login); they are never logged or rendered."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE, # comments). Does not override real env."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        os.environ.setdefault(key, val)


_load_dotenv(Path(os.environ.get("RS_ENV_FILE", ROOT / ".env")))


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("RS_DATA_DIR", str(ROOT / "data"))))
    host: str = field(default_factory=lambda: _env("RS_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _int("RS_PORT", 8090))
    public_hosts: list[str] = field(
        default_factory=lambda: [h.strip() for h in _env("RS_PUBLIC_HOSTS", "10.42.0.1").split(",") if h.strip()]
    )
    secret_key: str = field(default_factory=lambda: _env("RS_SECRET_KEY", ""))
    pair_token_ttl: int = field(default_factory=lambda: _int("RS_PAIR_TTL", 300))
    # Show the pairing QR to unpaired browsers on the network (not just the Pi's
    # own display / SSH CLI / already-paired phones). Off by default because the
    # Pi is also on the home Wi-Fi, where anyone could otherwise pair.
    pairing_from_network: bool = field(default_factory=lambda: _env("RS_PAIRING_FROM_NETWORK", "0") == "1")
    cert_file: str = field(default_factory=lambda: _env("RS_CERT_FILE", str(ROOT / "certs" / "server.crt")))
    key_file: str = field(default_factory=lambda: _env("RS_KEY_FILE", str(ROOT / "certs" / "server.key")))
    ca_file: str = field(default_factory=lambda: _env("RS_CA_FILE", str(ROOT / "certs" / "ca.crt")))
    session_days: int = field(default_factory=lambda: _int("RS_SESSION_DAYS", 90))

    # Wi-Fi QR (optional)
    wifi_ssid: str = field(default_factory=lambda: _env("RS_WIFI_SSID", ""))
    wifi_password: str = field(default_factory=lambda: _env("RS_WIFI_PASSWORD", ""))

    # Local LLM (llama-server, OpenAI-compatible)
    llm_url: str = field(default_factory=lambda: _env("RS_LLM_URL", "http://127.0.0.1:8081/v1"))
    llm_model: str = field(default_factory=lambda: _env("RS_LLM_MODEL", ""))
    llm_timeout: int = field(default_factory=lambda: _int("RS_LLM_TIMEOUT", 60))
    llm_max_tokens: int = field(default_factory=lambda: _int("RS_LLM_MAX_TOKENS", 256))

    # OCR
    ocr_threads: int = field(default_factory=lambda: _int("RS_OCR_THREADS", 2))
    ocr_max_side: int = field(default_factory=lambda: _int("RS_OCR_MAX_SIDE", 1600))
    ocr_model_dir: str = field(default_factory=lambda: _env("RS_OCR_MODEL_DIR", ""))

    # SMTP (password is only read here and in emailer.send)
    smtp_host: str = field(default_factory=lambda: _env("RS_SMTP_HOST", "smtp.gmail.com"))
    smtp_port: int = field(default_factory=lambda: _int("RS_SMTP_PORT", 587))
    smtp_user: str = field(default_factory=lambda: _env("RS_SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: _env("RS_SMTP_PASSWORD", ""), repr=False)
    smtp_from: str = field(default_factory=lambda: _env("RS_SMTP_FROM", ""))
    email_max_bytes: int = field(default_factory=lambda: _int("RS_EMAIL_MAX_BYTES", 20 * 1024 * 1024))

    # Optional: fetch KRA QR URLs when online (off by default)
    allow_kra_fetch: bool = field(default_factory=lambda: _env("RS_ALLOW_KRA_FETCH", "0") == "1")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "receipts.db"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def outbox_dir(self) -> Path:
        return self.data_dir / "outbox"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.media_dir, self.reports_dir, self.outbox_dir):
            d.mkdir(parents=True, exist_ok=True)

    def get_secret_key(self) -> bytes:
        """Secret for signing sessions/CSRF. Generated once and stored in data dir if not set."""
        if self.secret_key:
            return self.secret_key.encode()
        self.ensure_dirs()
        p = self.data_dir / ".secret_key"
        if not p.exists():
            p.write_bytes(os.urandom(32).hex().encode())
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass
        return p.read_bytes().strip()


settings = Settings()


def reload_settings() -> Settings:
    global settings
    settings = Settings()
    return settings
