# KRA receipt scanner (Raspberry Pi 5)

Scan paper receipts with your phone camera. The Pi reads them with on-device OCR, extracts every KRA/VAT detail, checks the arithmetic, and writes Kenyan VAT reports. The prose in the reports comes from the **local** language model already running on the Pi. You can email yourself one receipt or any set of them.

- No cloud AI, OCR or vision services. Receipts leave the Pi only in emails you ask for.
- Every figure is computed in Python. The model writes only the prose around the figures, and any paragraph containing a number that is not in its fact sheet is replaced.
- Nothing is guessed. If a field is not printed, it is stored as missing and flagged. Your corrections are kept and audited.

> This app helps you prepare your VAT return. It is not tax advice.

---

## 1. Install on a fresh Pi

Requirements: Debian 13 (trixie), aarch64, Python 3.13. `llama-server` is optional; reports still render without it.

```bash
# from your laptop
scp -r "Reciept processing" pi@<pi-ip>:~/receipt-scanner      # or git clone
ssh pi@<pi-ip>
cd ~/receipt-scanner
chmod +x scripts/*.sh
scripts/install_pi.sh
```

`install_pi.sh` does the following:
1. Checks Python, the CPU architecture, RAM, and `llama-server` on `127.0.0.1:8081`.
2. Creates `.venv` and installs `requirements.txt`. It then installs **RapidOCR 3.9.2 with `--no-deps`** and its dependencies listed explicitly. This uses `opencv-python-headless`; the GUI build of OpenCV needs `libGL.so.1`.
3. Copies `.env.example` to `.env` with mode `600`.
4. Runs `scripts/make_certs.sh`, which creates a local CA and a server certificate for `10.42.0.1`, the Pi's LAN IP(s), `hostname.local` and `localhost`.
5. Installs and starts two systemd units: **receipt-app** (HTTPS on `0.0.0.0:8090`) and **receipt-worker** (OCR, reports, outbox).

The OCR models (`PP-OCRv6_det_small.onnx`, `PP-OCRv6_rec_small.onnx`, `ch_ppocr_mobile_v2.0_cls_mobile.onnx`) ship inside the RapidOCR wheel. Their paths are passed explicitly, so nothing is ever downloaded.

### Configure `.env`

```bash
nano .env
sudo systemctl restart receipt-app receipt-worker
```

| Setting | What to set |
|---|---|
| `RS_PUBLIC_HOSTS` | `10.42.0.1,<pi-lan-ip>,<hostname>.local`. The first entry goes into the pairing QR code. |
| `RS_SMTP_USER` / `RS_SMTP_PASSWORD` / `RS_SMTP_FROM` | Your Gmail address and a Gmail **App Password** (create one at myaccount.google.com/apppasswords). |
| `RS_WIFI_SSID` / `RS_WIFI_PASSWORD` | Optional. Shows a Wi-Fi QR code so the phone can join the hotspot. |

The password is kept only in `.env` on the Pi. It is never logged, never shown in the UI, and scrubbed from stored error messages.

If the Pi's LAN IP changes, re-run `scripts/make_certs.sh`. It keeps the CA, so your phone stays trusted.

## 2. Trust the certificate on your phone (once)

Phone browsers allow camera access only over HTTPS that the phone trusts. The Pi signs its certificate with a small local CA, and you install that CA once.

Join the Pi's hotspot (or the same Wi-Fi as the Pi), then open `https://10.42.0.1:8090/ca.crt` (or `https://<pi-lan-ip>:8090/ca.crt`). You will see a warning on this first visit; continue anyway.

**Android (Chrome)**
1. The file `receipt-scanner-ca.crt` downloads.
2. Go to **Settings → Security & privacy → More security settings → Encryption & credentials → Install a certificate → CA certificate**. The menu names vary by brand; search Settings for "CA certificate".
3. Tap **Install anyway**, then pick the downloaded file.
4. Close Chrome completely and reopen it.

**iPhone (Safari)**
1. Open the link in **Safari**. Tap **Allow** to download the configuration profile.
2. Go to **Settings → General → VPN & Device Management**, open the *Receipt Scanner Local CA* profile, and tap **Install**.
3. Go to **Settings → General → About → Certificate Trust Settings** and turn on **full trust** for *Receipt Scanner Local CA*. iOS skips this step silently if you miss it, and the camera will not work.

The CA private key (`certs/ca.key`) never leaves the Pi. Anyone holding that key could impersonate sites to your phone, so keep it private. To revoke trust, delete the profile or certificate on the phone.

## 3. Pair the phone

Pairing QR codes are single-use and expire after 5 minutes. To open the scanner, scan one with the phone camera.

You can show a pairing QR code in three ways:
- **Over SSH (headless Pi):** `cd ~/receipt-scanner && .venv/bin/python -m app.pair_cli` prints a QR code in the terminal.
- **On a screen attached to the Pi:** open `https://localhost:8090/pair-screen`. The QR code refreshes itself.
- **From a phone that is already paired:** **Settings → Pair another phone**.

By default, unpaired browsers on the network are **not** shown the pairing QR code. The Pi is also on your home Wi-Fi, so anyone there could otherwise pair. To allow it anyway, set `RS_PAIRING_FROM_NETWORK=1`. Pairing attempts are rate-limited.

You can see and revoke paired phones under **Settings → Paired phones**.

## 4. Scanning

- **Short receipts:** hold the phone over the receipt inside the frame. The meter turns green when the image is sharp, and the app captures automatically once you hold steady. You can also tap the shutter yourself. The receipt is queued straight away, so you can keep scanning.
- **Long receipts:** switch to **Long** and capture the top section. Tap **Next section**, then slide the phone down so that about 25% of the receipt overlaps the previous shot; a faint strip of the last shot helps you line it up. Repeat to the end, then tap **Done**.
  - Tap a thumbnail to **retake** or delete that section.
  - The Pi stitches the sections together (ORB features with a near-pure-translation homography, falling back to vertical template matching) and runs OCR on the tall image in tiles. If stitching fails, it runs OCR on each section and removes the lines repeated in the overlaps.
- **Queue:** status chips show each receipt while it is processed. Tap one when it says *done* to check the fields against the photo, fix anything, and save.

**Pre-processing:** the Pi finds the paper edges, crops the perspective, deskews, flattens uneven light with background division and CLAHE (which helps with faded thermal paper), and limits the image width. It also decodes the KRA eTIMS QR code with OpenCV and stores it as evidence. It never fetches the code's URL.

## 5. What is extracted and checked

**Extracted fields:**
- Seller name, seller KRA PIN (`[AP]\d{9}[A-Z]`), buyer PIN
- Receipt number, CU invoice number, CU serial or SCU ID, signature, KRA QR URL
- Date (**day first**) and time
- Line items with tax-band letters
- Tax bands (rate **as printed**, taxable value, VAT)
- Total, total VAT, payment method and M-PESA code

**Checks:**
- For each band, taxable × rate = VAT (within KES 1)
- Bands add up to the total
- Stated VAT equals the sum of band VAT
- Items add up to the total
- Flags: PIN missing, no eTIMS number, no VAT shown

All check results are stored. So are the raw OCR text, every frame and the stitched image.

## 6. Reports

| Report | Contents |
|---|---|
| Monthly input-VAT schedule | One row per receipt (supplier, PIN, CU invoice number, taxable value, VAT, claimable and why), totals by printed rate, and the VAT3 due date (the 20th of the following month) |
| Claimability & compliance | Receipts with no eTIMS number, no PIN, no VAT, or a claim window that is closed or closing, with what to do about each. Also covers the income-tax eTIMS rule, the supplier-declaration risk, and penalties ("check with KRA") |
| Spending summary | Spending and VAT paid by supplier, by category and by month |
| Single receipt | Every field, which fields you corrected, the checks, the line items, the image(s) and the raw OCR text |
| Explain my month | Plain-English narrative from the local model |

**Formats:** on-screen HTML, PDF (ReportLab), XLSX and CSV.

**How the model is used:** the model context is 1024 tokens, so each section gets a fact sheet of at most about 300 tokens and the model writes at most 256 tokens. Calls are made one at a time, with a timeout. A paragraph containing a number that is not in its fact sheet is retried once and then replaced by a template sentence. If `llama-server` is down, reports render with template prose and say so. Model output is stripped of markup and escaped before display.

The Kenyan rules live in [app/kra_rules.py](app/kra_rules.py), with dates and sources:
- 16% standard rate.
- Temporary 8% on petroleum, with no hard-coded expiry. The printed rate is always used.
- Input VAT is claimable only on an eTIMS invoice showing the supplier's PIN.
- The claim window ends 6 months after the end of the month of purchase.
- The VAT3 return is due on the 20th of the following month.
- Band letters are never mapped to rates.

The reference rate table can be edited in Settings.

## 7. Email

- Add your address under **Settings → Email recipients**. The SMTP account in `.env` is confirmed automatically. Any other address receives a 6-digit code to confirm before it can receive mail.
- On the **Email** tab, choose the receipts (a month, selected receipts, or everything, optionally filtered by supplier or "claimable only"). Then choose what to include: PDF, XLSX, CSV, original photos (zipped), the stitched image, and raw OCR text.
- Emails wait in the **outbox** until the Pi is online, and are retried with exponential backoff (1 minute up to 6 hours, at most 12 tries). Large sets are split into several emails under 20 MB each.
- **Monthly auto-email:** between the 1st and the 5th of each month, last month's VAT schedule and compliance report (PDF and XLSX) are queued for every confirmed address. You can turn this off in Settings.

## 8. Development (laptop, no Pi needed)

```bash
python -m venv .venv && .venv/Scripts/activate      # Windows; on Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
pip install --no-deps rapidocr==3.9.2 && pip install -r requirements-ocr.txt
python -m pytest -q                                   # OCR and the LLM are mocked; real-OCR tests run if RapidOCR is installed
python scripts/eval_ocr.py                            # end-to-end OCR accuracy on synthetic receipts and real photos
RS_DATA_DIR=/tmp/rs-demo python scripts/demo_seed.py  # demo data
RS_DATA_DIR=/tmp/rs-demo RS_PUBLIC_HOSTS=localhost python -m app.main   # http://localhost:8090 (localhost can use the camera without TLS)
RS_DATA_DIR=/tmp/rs-demo python -m app.worker
```

**Real-photo test set:** put `tests/fixtures/real/<name>.jpg` (for multi-section receipts, `<name>_1.jpg`, `<name>_2.jpg`, …) with a matching `<name>.json` of expected fields, then run `scripts/eval_ocr.py`. This folder is git-ignored, so real receipts never reach the repository.

**Peak RAM:** on the Pi, with `llama-server` running, run `.venv/bin/python scripts/measure_ram.py`. It processes an 8-section, 120-item receipt and then builds a report, and prints peak RSS and system memory use.

## Layout

```
app/        config, kra_rules, textparse, parser, checks, lines, preprocess, ocr, stitch, pipeline,
            receipts, db (SQLite + job queue), llm, reports, render, emailer, auth, worker, main, pair_cli
app/static  app.js (UI), scanner.js (camera, sharpness, auto-capture), pair.js, app.css
deploy/     systemd units          scripts/  install_pi.sh, make_certs.sh, eval_ocr.py, measure_ram.py, demo_seed.py
tests/      synth.py (synthetic receipts and ground truth), parser, rules, stitching, llm, app/auth/reports/outbox, OCR e2e
```

All data lives in `data/` (database, media, reports, outbox). `data/`, `certs/`, `.env` and image, PDF and XLSX files are git-ignored.
