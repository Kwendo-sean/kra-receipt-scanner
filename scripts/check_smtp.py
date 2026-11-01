"""Check the SMTP settings in .env from the Pi's shell and try a real login.

    .venv/bin/python scripts/check_smtp.py [--send]
"""
from __future__ import annotations

import smtplib
import ssl
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, emailer  # noqa: E402


def main() -> int:
    s = config.settings
    env = Path(__file__).resolve().parent.parent / ".env"
    print(f".env file: {env} ({'found' if env.is_file() else 'MISSING'})")
    info = emailer.smtp_info()
    print(f"host={info['host']}:{info['port']}  user={info['user'] or '(empty)'}  from={info['from'] or '(empty)'}  "
          f"password={'set, ' + str(info['password_len']) + ' chars' if info['password_set'] else '(empty)'}")
    if info["missing"]:
        print("MISSING in .env:", ", ".join(info["missing"]))
        return 1
    if s.smtp_host.endswith("gmail.com") and info["password_len"] != 16:
        print("WARNING: Gmail App Passwords are 16 letters; this one isn't. Your normal Gmail password will not work.")
    print("network:", "reachable" if emailer.online() else "UNREACHABLE (Pi offline or port blocked)")
    try:
        ctx = ssl.create_default_context()
        if s.smtp_port == 465:
            c = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, context=ctx, timeout=20)
        else:
            c = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20)
            c.starttls(context=ctx)
        c.login(s.smtp_user, s.smtp_password)
        print("login: OK")
        if "--send" in sys.argv:
            c.sendmail(info["from"], [info["from"]], f"Subject: Receipt scanner test\r\n\r\nSMTP works.\r\n")
            print("test email sent to", info["from"])
        c.quit()
    except smtplib.SMTPAuthenticationError as e:
        print(f"login: REFUSED ({e.smtp_code}). Use a Gmail App Password (myaccount.google.com/apppasswords).")
        return 1
    except (OSError, smtplib.SMTPException) as e:
        print("login: FAILED", emailer._scrub(f"{type(e).__name__}: {e}"))
        return 1
    print("If the web app still says 'not configured': sudo systemctl restart receipt-app receipt-worker")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
