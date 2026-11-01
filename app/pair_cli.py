"""Print a fresh pairing QR in the terminal (works over SSH on a headless Pi).

    python -m app.pair_cli [--host 10.42.0.1]
"""
from __future__ import annotations

import argparse

from app import auth, config, db


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=config.settings.public_hosts[0])
    args = ap.parse_args()
    config.settings.ensure_dirs()
    db.init_db()
    tok = auth.new_pair_token()
    url = f"https://{args.host}:{config.settings.port}/pair/{tok}"
    import qrcode
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)
    print(url)
    print(f"Valid for {config.settings.pair_token_ttl // 60} minutes, single use.")


if __name__ == "__main__":
    main()
