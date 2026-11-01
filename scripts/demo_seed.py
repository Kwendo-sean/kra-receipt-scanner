"""Fill a (throwaway) data dir with the synthetic receipts, OCR'd for real.

    RS_DATA_DIR=/tmp/rs-demo python scripts/demo_seed.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import cv2  # noqa: E402

import synth  # noqa: E402
from app import config, db, receipts  # noqa: E402
from app.worker import handle_ocr  # noqa: E402


def main() -> None:
    config.settings.ensure_dirs()
    db.init_db()
    for fn in synth.ALL:
        s = fn()
        img = synth.photograph(synth.render(s), angle=2.0)
        frames = synth.split_sections(img, 4) if s.name == "long_multi" else [img]
        rid = receipts.create("multi" if len(frames) > 1 else "single")
        for i, f in enumerate(frames):
            receipts.add_frame(rid, cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes(), i)
        receipts.submit(rid)
        job = db.claim_job(["ocr"])
        handle_ocr(db.jload(job["payload_json"]))
        db.finish_job(job["id"])
        print(f"#{rid} {s.name}: {receipts.get(rid)['fields'].get('seller_name')}")


if __name__ == "__main__":
    main()
