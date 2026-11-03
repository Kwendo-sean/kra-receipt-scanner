"""Delete every receipt, photo, report and queued email so you can start fresh.

Keeps: paired devices (sessions), the iPhone Shortcut keys, saved email
recipients, settings and the app's secret key. Receipt numbers restart at 1.

Stop the app first, so nothing is written while it runs:

    sudo systemctl stop receipt-app receipt-worker
    .venv/bin/python scripts/clear_receipts.py
    sudo systemctl start receipt-app receipt-worker
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db  # noqa: E402

TABLES = ("frames", "audit", "outbox", "reports", "jobs", "receipts")


def clear() -> dict:
    s = config.settings
    counts = {}
    with db.tx() as conn:
        for t in TABLES:
            counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            conn.execute(f"DELETE FROM {t}")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
            conn.execute("DELETE FROM sqlite_sequence WHERE name IN (%s)" % ",".join("?" * len(TABLES)), TABLES)
    for d in (s.media_dir, s.reports_dir, s.outbox_dir):
        if d.is_dir():
            for p in d.iterdir():
                shutil.rmtree(p) if p.is_dir() else p.unlink()
    with db.conn() as conn:
        conn.execute("VACUUM")
    return counts


def main() -> None:
    s = config.settings
    print(f"Data folder: {s.data_dir}")
    n = db.one("SELECT COUNT(*) AS n FROM receipts")["n"]
    print(f"This deletes {n} receipt(s) with their photos, every report and every queued email.")
    print("Paired phones, Shortcut keys, email recipients and settings are kept.")
    if "--yes" not in sys.argv and input("Type DELETE to continue: ").strip() != "DELETE":
        print("Nothing deleted.")
        return
    counts = clear()
    print("Deleted: " + ", ".join(f"{v} {k}" for k, v in counts.items()))
    print("Done. Start the app again: sudo systemctl start receipt-app receipt-worker")


if __name__ == "__main__":
    main()
