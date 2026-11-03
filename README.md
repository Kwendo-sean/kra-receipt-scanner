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

- **Built-in document scanner (phone):** while you aim, a box tracks the receipt's edges. Each side is a fitted straight line, so torn or curled corners don't throw it off. The box turns green when the shot is sharp and steady, and the photo is captured automatically. A review screen then shows the flattened scan, and you can drag the four corners to adjust it. In Auto mode it keeps the scan after 2.5 s unless you touch it. The Pi flattens the receipt using the corners you confirmed. No LiDAR is needed; it's camera-only, like Apple's scanner.
- **Short receipts:** hold the phone over the receipt inside the frame. The meter turns green when the image is sharp, and the app captures automatically once you hold steady. You can also tap the shutter yourself. The receipt is queued straight away, so you can keep scanning.
- **Long receipts:** switch to **Long** and capture the top section. Then slide the phone down the receipt:
  - The top quarter of the frame shows the end of your last shot, with a dashed amber **marker** below it.
  - A glowing **green line** shows where your last shot ended. The phone tracks it live by matching the camera view against that shot, so the line moves up as you slide down.
  - Stop when the green line reaches the marker (about 25% overlap). The frame turns green, the phone buzzes, and the section is captured. If you go too far, it tells you to move back up.
  - Repeat to the end of the receipt, then tap **Done**. **Undo last** removes the latest section. Tap a thumbnail to **retake** a section; the line then tracks from the section before it.
  - Blank paper in the overlap gives the phone nothing to match. In that case, capture manually with the shutter.
  - On the Pi, the sections are stitched together (ORB features with a near-pure-translation homography, falling back to vertical template matching). OCR then runs on the tall image in tiles. If stitching fails, OCR runs on each section and the lines repeated in the overlaps are removed.
- **Queue:** status chips show each receipt while it is processed. Tap one when it says *done* to check the fields against the photo, fix anything, and save.

**Pre-processing:** the Pi finds the paper edges, crops the perspective, deskews, flattens uneven light with background division and CLAHE (which helps with faded thermal paper), and limits the image width. It also decodes the KRA eTIMS QR code with OpenCV and stores it as evidence. It never fetches the code's URL.

**Faded receipts.** Thermal paper fades, so several steps help without any typing on your side:
- **White background:** the paper is found by its edge, not its brightness, so shadowed paper is never removed. Everything else becomes white, and that clean scan is what you see in the app.
- **Automatic second pass:** if a key field is missing or a check fails, the Pi re-reads the receipt with a more accurate model (PP-OCRv6 *medium*, fetched once by `scripts/fetch_models.py`) and keeps the better reading. Normal receipts are never re-read.
- **Arithmetic gap-filling:** when a faded figure follows exactly from other printed figures and an independent printed figure confirms it, it is filled in and marked *derived*, with the working shown. Otherwise it stays empty and flagged.
- **Faded cents:** an amount like `6,983` on a receipt that prints cents is treated as unreadable rather than stored as a wrong figure.

**Importing scans.** **Import** on the phone, or **Upload photos** on a computer, accepts photos and PDFs. On an iPhone you can scan with the built-in document scanner (Notes or Files → *Scan Documents*), which draws a box around the receipt and flattens it; save the scan as a PDF, then import it here.

### iPhone: use Apple's own document scanner (one-tap Shortcut)

A web page cannot open the iPhone's document scanner, but the **Shortcuts** app can. Its *Scan Documents* action uses the same scanner as Notes and Files: it draws a box around the receipt, captures automatically and flattens the image. The scan is then uploaded straight to the Pi.

1. On the iPhone, open the app → **Settings → iPhone document scanner → Create Shortcut key**. Copy the *Upload address* and the *Bearer key*. The key can only upload receipts and can be revoked in the same place.
2. In **Shortcuts**, create **Scan receipt** with these actions:
   1. **Scan Documents**
   2. **Get Contents of URL**: the URL is the upload address; Method **POST**; header `Authorization` set to `Bearer rsk_…`; Request Body **File**, set to the scanned document
   3. **Get Dictionary Value** `message`, then **Show Notification**. Optionally, **Get Dictionary Value** `url`, then **Open URLs** to see the parsed receipt.
3. Put it on the Home Screen or the Action Button. From then on: tap, scan, and it's on the Pi.

The Shortcut uses the phone's trusted certificates, so install the Pi's CA first (section 2). If *Scan Documents* isn't available, scan in Notes or Files instead and **Share → Scan receipt**. For that, turn on *Show in Share Sheet* and accept PDFs and images, and use *Shortcut Input* as the request body.

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
| Monthly input-VAT schedule | Key figures, claimable VAT by supplier (bar chart), one row per receipt (supplier, PIN, CU invoice number, taxable value, VAT, claimable and why), the receipts to fix before filing, totals by printed rate, the VAT3 due date with days left, and next steps |
| Claimability & compliance | Receipts with no eTIMS number, no PIN, no VAT, or a claim window that is closed or closing; the most common problems; which supplier to contact first and what to ask for; next steps. Also covers the income-tax eTIMS rule, the supplier-declaration risk, and penalties ("check with KRA") |
| Spending summary | Key figures, the change from the previous month, where the money went (bar charts), biggest purchases, the items you spent most on, and spending and VAT by supplier, category and month with shares |
| Single receipt | Every field, which fields you corrected, what to do, the checks, the line items, the image(s) and the raw OCR text |
| Explain my month | Plain-English brief from the local model: the month against the last one, where the money went, what you can claim, what needs fixing and who to contact, and numbered next steps |

**Formats:** on-screen HTML, PDF (ReportLab), XLSX and CSV. All of them use the app's look (DM Sans and IBM Plex Mono, off-white paper, red accent). A saved or emailed HTML report carries its stylesheet and fonts inside the file, so it looks the same offline. The PDF fonts are in `app/fonts/`.

**How the model is used:** `llama-server` runs with a 2048-token context. Each section gets a fact sheet of at most about 550 tokens (every figure, share and comparison already computed in Python) and the model writes at most 400 tokens. Calls are made one at a time, with a 150 s timeout, so a full "Explain my month" report takes a few minutes on the Pi. A paragraph containing a number that is not in its fact sheet is retried once and then replaced by a template paragraph. If `llama-server` is down, reports render with template prose and say so. Model output is stripped of markup and escaped before display.

The Kenyan rules live in [app/kra_rules.py](app/kra_rules.py), with dates and sources:
- 16% standard rate.
- Temporary 8% on petroleum, with no hard-coded expiry. The printed rate is always used.
- Input VAT is claimable only on an eTIMS invoice showing the supplier's PIN.
- The claim window ends 6 months after the end of the month of purchase.
- The VAT3 return is due on the 20th of the following month.
- Band letters are never mapped to rates.

The reference rate table can be edited in Settings.

## 7. Email

- **Layout:** phones get the camera scanner. Laptops and desktops get a dashboard (overview, receipts table, photo upload) without the camera. To switch, use **Settings → Layout**.
- Add your address under **Settings → Email recipients**. The SMTP account in `.env` is confirmed automatically. Any other address receives a 6-digit code to confirm before it can receive mail.
- On the **Email** tab, choose the receipts (a month, selected receipts, or everything, optionally filtered by supplier or "claimable only"). Then choose what to include: PDF, XLSX, CSV, original photos (zipped), the stitched image, and raw OCR text.
- Emails wait in the **outbox** until the Pi is online, and are retried with exponential backoff (1 minute up to 6 hours, at most 12 tries). Large sets are split into several emails under 20 MB each.
- **Monthly auto-email:** between the 1st and the 5th of each month, last month's VAT schedule and compliance report (PDF and XLSX) are queued for every confirmed address. You can turn this off in Settings.

**If email says "not configured":**
1. Run `.venv/bin/python scripts/check_smtp.py --send` on the Pi. It shows what the app can read from `.env` (the password is shown only as its length), tries a real login, and sends a test email to yourself.
2. Restart both services after editing `.env`: `sudo systemctl restart receipt-app receipt-worker`.
3. For Gmail, use a 16-letter **App Password**; your normal password won't work. Spaces in it are fine.

On a paired device, **Settings → Email** shows the same diagnosis, with a **Send test email** button.

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

**Start fresh:** to delete every receipt, photo, report and queued email while keeping paired phones, Shortcut keys, email recipients and settings:

```bash
sudo systemctl stop receipt-app receipt-worker
.venv/bin/python scripts/clear_receipts.py      # asks you to type DELETE
sudo systemctl start receipt-app receipt-worker
```

**Peak RAM:** on the Pi, with `llama-server` running, run `.venv/bin/python scripts/measure_ram.py`. It processes an 8-section, 120-item receipt and then builds a report, and prints peak RSS and system memory use.

## Layout

```
app/        config, kra_rules, textparse, parser, checks, lines, preprocess, ocr, stitch, pipeline,
            receipts, db (SQLite + job queue), llm, reports, render, emailer, auth, worker, main, pair_cli
app/static  app.js (UI), scanner.js (camera, sharpness, auto-capture), pair.js, app.css
deploy/     systemd units          scripts/  install_pi.sh, make_certs.sh, eval_ocr.py, measure_ram.py, demo_seed.py, clear_receipts.py
tests/      synth.py (synthetic receipts and ground truth), parser, rules, stitching, llm, app/auth/reports/outbox, OCR e2e
```

All data lives in `data/` (database, media, reports, outbox). `data/`, `certs/`, `.env` and image, PDF and XLSX files are git-ignored.
