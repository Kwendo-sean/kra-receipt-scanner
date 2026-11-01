#!/usr/bin/env bash
# One-shot install on the Pi (Debian 13 trixie, aarch64, Python 3.13).
#   cd ~/receipt-scanner && scripts/install_pi.sh
set -euo pipefail
cd "$(dirname "$0")/.."
DIR=$(pwd)
USER_NAME=$(id -un)

echo "== checks"
python3 --version
uname -m
free -m | head -2
if curl -s --max-time 3 http://127.0.0.1:8081/v1/models >/dev/null; then
  echo "llama-server: OK ($(curl -s http://127.0.0.1:8081/v1/models | head -c 200))"
else
  echo "llama-server: NOT reachable on 127.0.0.1:8081 (reports will use template prose)"
fi

echo "== system packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-dev openssl fonts-dejavu-core

echo "== python venv (downloads ~200 MB the first time; progress is shown below)"
[ -x .venv/bin/python ] || python3 -m venv .venv
# Prefer prebuilt aarch64 wheels; the heavy packages must never compile from
# source on the Pi (that takes an hour+), so fail fast instead.
PIP=(.venv/bin/pip install --progress-bar on --prefer-binary
     --only-binary=numpy,opencv-python-headless,onnxruntime,shapely,pyclipper,cryptography)
.venv/bin/pip install --upgrade pip wheel
"${PIP[@]}" -r requirements.txt
# RapidOCR without its declared deps (it would pull opencv-python, the GUI build needing libGL.so.1)
"${PIP[@]}" --no-deps rapidocr==3.9.2
"${PIP[@]}" -r requirements-ocr.txt
.venv/bin/pip uninstall -y -q opencv-python 2>/dev/null || true
.venv/bin/python -c "import cv2, onnxruntime, rapidocr; print('cv2', cv2.__version__, 'ort', onnxruntime.__version__)"
.venv/bin/python -c "from app.ocr import model_paths; print(model_paths())"

echo "== .env"
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created .env - edit it (SMTP app password, Wi-Fi, LAN host) with: nano .env"
fi
mkdir -p data && chmod 700 data

echo "== certificates"
[ -f certs/server.crt ] || scripts/make_certs.sh

echo "== systemd"
for unit in receipt-app receipt-worker; do
  sed -e "s#__DIR__#${DIR}#g" -e "s#__USER__#${USER_NAME}#g" "deploy/${unit}.service" | sudo tee "/etc/systemd/system/${unit}.service" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now receipt-app receipt-worker
sleep 2
systemctl --no-pager --lines=3 status receipt-app receipt-worker || true

echo
echo "Pair a phone now:   .venv/bin/python -m app.pair_cli"
