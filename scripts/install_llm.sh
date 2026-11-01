#!/usr/bin/env bash
# Build llama.cpp's llama-server and install a small local model as a systemd
# service on 127.0.0.1:8081 (OpenAI-compatible). Only used for report prose.
#
#   scripts/install_llm.sh            # default: Qwen2.5-1.5B-Instruct Q4_K_M (~1.1 GB)
#   MODEL_URL=<gguf url> scripts/install_llm.sh   # any other instruct GGUF
set -euo pipefail
cd "$(dirname "$0")/.."
DIR=$(pwd)
USER_NAME=$(id -un)
LLM_DIR="${LLM_DIR:-$HOME/llm}"
MODEL_URL="${MODEL_URL:-https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf}"
MODEL_FILE="$LLM_DIR/models/$(basename "$MODEL_URL")"

echo "== build tools"
sudo apt-get update -qq
sudo apt-get install -y -qq build-essential cmake git curl

echo "== llama.cpp (llama-server only; ~10-15 min on a Pi 5)"
mkdir -p "$LLM_DIR/models"
if [ ! -d "$LLM_DIR/llama.cpp" ]; then
  git clone --depth 1 https://github.com/ggml-org/llama.cpp "$LLM_DIR/llama.cpp"
fi
cd "$LLM_DIR/llama.cpp"
cmake -B build -DGGML_NATIVE=ON -DLLAMA_CURL=OFF -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release -j4 --target llama-server
cd "$DIR"

echo "== model"
if [ ! -s "$MODEL_FILE" ]; then
  curl -L --fail --progress-bar -o "$MODEL_FILE.part" "$MODEL_URL"
  mv "$MODEL_FILE.part" "$MODEL_FILE"
fi
ls -lh "$MODEL_FILE"

echo "== systemd"
sed -e "s#__USER__#${USER_NAME}#g" -e "s#__LLM_DIR__#${LLM_DIR}#g" -e "s#__MODEL__#${MODEL_FILE}#g" \
  deploy/receipt-llm.service | sudo tee /etc/systemd/system/receipt-llm.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now receipt-llm
echo "Waiting for the model to load..."
# /health returns 503 while the model loads and 200 once it is ready
for _ in $(seq 1 90); do
  if curl -sf --max-time 2 http://127.0.0.1:8081/health >/dev/null; then break; fi
  sleep 2
done
curl -s http://127.0.0.1:8081/v1/models; echo
echo "Quick test:"
time curl -sf http://127.0.0.1:8081/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"In one sentence: total spent KES 1,600.00, VAT KES 193.10."}],"max_tokens":60}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["choices"][0]["message"]["content"])'
