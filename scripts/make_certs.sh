#!/usr/bin/env bash
# Create a small local CA (installed once on the phone) and a server certificate
# for the Pi, covering the hotspot IP, the home-LAN IP(s) and the .local name.
#
#   scripts/make_certs.sh [extra-ip-or-name ...]
#
# Re-running keeps the existing CA (so the phone stays trusted) and only
# re-issues the server certificate - e.g. after the Pi's LAN IP changes.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p certs
chmod 700 certs
cd certs

HOST=$(hostname)
SANS="DNS:localhost,DNS:${HOST},DNS:${HOST}.local,IP:127.0.0.1,IP:10.42.0.1"
# every IPv4 address the Pi currently has (hotspot + Wi-Fi client / Ethernet)
for ip in $(hostname -I 2>/dev/null || true); do
  case "$ip" in *:*) continue ;; esac
  case ",$SANS," in *",IP:$ip,"*) ;; *) SANS="$SANS,IP:$ip" ;; esac
done
for extra in "$@"; do
  if [[ "$extra" =~ ^[0-9.]+$ ]]; then SANS="$SANS,IP:$extra"; else SANS="$SANS,DNS:$extra"; fi
done

if [ ! -f ca.key ]; then
  echo "Creating local CA (ca.key stays on the Pi; ca.crt goes on the phone)"
  openssl genrsa -out ca.key 3072
  chmod 600 ca.key
  # Android/iOS need basicConstraints CA:TRUE + keyUsage keyCertSign
  openssl req -x509 -new -key ca.key -sha256 -days 3650 -out ca.crt \
    -subj "/CN=Receipt Scanner Local CA (${HOST})" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign"
fi

echo "Issuing server certificate for: $SANS"
openssl genrsa -out server.key 2048
chmod 600 server.key
openssl req -new -key server.key -out server.csr -subj "/CN=${HOST}"
cat > server.ext <<EOF
basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=${SANS}
EOF
# iOS rejects server certificates valid for more than 825 days
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out server.crt -days 800 -sha256 -extfile server.ext
rm -f server.csr server.ext
echo
echo "Done. Phone: open https://10.42.0.1:8090/ca.crt (or https://<lan-ip>:8090/ca.crt) and install it - see README."
openssl x509 -in server.crt -noout -ext subjectAltName
