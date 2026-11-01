"""Measure peak RAM while OCR-ing a long receipt and then building a report
(with llama-server writing prose), as the worker would. Run on the Pi:

    .venv/bin/python scripts/measure_ram.py

Reports: this process's peak RSS, llama-server RSS, and the system-wide peak of
used memory (MemTotal - MemAvailable) sampled every 100 ms.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("RS_DATA_DIR", tempfile.mkdtemp(prefix="rs-ram-"))


def meminfo() -> dict[str, int]:
    out = {}
    try:
        for line in open("/proc/meminfo"):
            k, v = line.split(":", 1)
            out[k] = int(v.split()[0])  # kB
    except OSError:
        pass
    return out


def rss_kb(pid: int) -> int:
    try:
        for line in open(f"/proc/{pid}/status"):
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except OSError:
        pass
    return 0


def find_pid(name: str) -> int | None:
    if not Path("/proc").is_dir():
        return None
    for p in Path("/proc").iterdir():
        if p.name.isdigit():
            try:
                if name in (p / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore"):
                    return int(p.name)
            except OSError:
                continue
    return None


def main() -> None:
    import cv2
    import synth
    from app import config, db, llm, receipts, reports
    from app.worker import handle_ocr

    config.reload_settings().ensure_dirs()
    db.init_db()
    llama = find_pid("llama-server")
    peak = {"used": 0, "self": 0, "llama": 0}
    stop = threading.Event()

    def sampler():
        while not stop.is_set():
            mi = meminfo()
            if mi:
                peak["used"] = max(peak["used"], mi["MemTotal"] - mi["MemAvailable"])
            peak["self"] = max(peak["self"], rss_kb(os.getpid()))
            if llama:
                peak["llama"] = max(peak["llama"], rss_kb(llama))
            time.sleep(0.1)

    base = meminfo()
    t = threading.Thread(target=sampler, daemon=True)
    t.start()

    s = synth.long_multi(120)  # ~ a 2 m pharmacy roll
    img = synth.photograph(synth.render(s), angle=1.5)
    # phone-sized frames: upscale each section to ~3000 px tall like a 12 MP still
    rid = receipts.create("multi")
    for i, fr in enumerate(synth.split_sections(img, 8)):
        fr = cv2.resize(fr, (fr.shape[1] * 3, fr.shape[0] * 3), interpolation=cv2.INTER_CUBIC)
        receipts.add_frame(rid, cv2.imencode(".jpg", fr, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes(), i)
    del img
    t0 = time.time()
    handle_ocr({"receipt_id": rid})
    t1 = time.time()
    db.run("UPDATE receipts SET status='done' WHERE id=?", rid)
    use_llm = llm.available()
    rep = reports.build("narrative", {"ids": [rid]}, today=date.today(), use_llm=use_llm)
    from app import render
    render.to_pdf(rep)
    t2 = time.time()
    stop.set()
    t.join()
    r = receipts.get(rid)
    print(f"OCR of 8-section receipt: {t1 - t0:.1f}s · items found: {len(r['fields']['items'])}/120 · "
          f"stitch: {r['stitch'].get('method')}")
    print(f"Report (LLM {'on' if use_llm else 'OFF'}): {t2 - t1:.1f}s · prose: {rep.get('prose_statuses')}")
    print(f"Peak RSS this process (OCR+report): {peak['self'] / 1024:.0f} MB")
    if llama:
        print(f"llama-server RSS: {peak['llama'] / 1024:.0f} MB")
    if base:
        print(f"System used before: {(base['MemTotal'] - base['MemAvailable']) / 1024:.0f} MB · "
              f"peak during run: {peak['used'] / 1024:.0f} MB of {base['MemTotal'] / 1024:.0f} MB")


if __name__ == "__main__":
    main()
