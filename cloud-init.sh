#!/bin/bash
# First boot of the Safha Oracle A1 server (Ubuntu 24.04, arm64).
# Installs Docker, clones the repo, downloads the models and leaves the server
# ready for /opt/safha-server/deploy.sh.
# Log: /var/log/safha-bootstrap.log
set -euxo pipefail
exec > >(tee -a /var/log/safha-bootstrap.log) 2>&1

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y docker.io docker-compose-v2 git curl python3-pip python3-venv jq
systemctl enable --now docker
usermod -aG docker ubuntu

# 4 GB swap as a safety net for model loading.
if [ ! -f /swapfile ]; then
  fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# Code: the app repo and this server repo (deploy.sh).
[ -d /opt/qari/.git ] || git clone https://github.com/khan23153/qari.git /opt/qari
[ -d /opt/safha-server/.git ] || git clone https://github.com/khan23153/safha-server.git /opt/safha-server
chown -R ubuntu:ubuntu /opt/qari /opt/safha-server

# Models.
M=/opt/qari-models
mkdir -p "$M/qari-fastconformer-pcd-rnnt-int8"

# Live engine: public, pinned revision, verified against production hashes.
REV=a57907844f7c72969b40bc526a57e9c9dbc8bf3f
BASE="https://huggingface.co/Synthesium/quran-recitation-v2-onnx/resolve/$REV/fastconformer-ar-transducer"
cd "$M/qari-fastconformer-pcd-rnnt-int8"
for f in encoder.int8.onnx decoder.int8.onnx joiner.int8.onnx tokens.txt; do
  [ -s "$f" ] || curl -fL --retry 5 -o "$f" "$BASE/$f"
done
cat > SHA256SUMS <<'EOF'
583d152c5bfd7dbfaa573319043fed4f1ef448add3f769d31b3dddf31e4e533f  encoder.int8.onnx
9ee2286747a5bd5cfeb1808af7eca42a763cd057e1ebb78cb3cb93a48e559a90  decoder.int8.onnx
5015e28f1ea3b8b56d25212a69629d7e49d750ce2a56d181dbd53a23b3b5dec2  joiner.int8.onnx
9b938381a19a69bb279cdcfc299419f25a049ea1de192e0d10317274a0f20074  tokens.txt
EOF
sha256sum -c SHA256SUMS

# Whisper base + V50 live only in the private Kaggle dataset telethonfool/safha-latest.
KAGGLE_API_TOKEN="__KAGGLE_API_TOKEN__"
if [ -n "$KAGGLE_API_TOKEN" ] && [ ! -s "$M/qari-ct2-base/model.bin" ]; then
  python3 -m venv /opt/kaggle-venv
  /opt/kaggle-venv/bin/pip install -q kaggle
  mkdir -p /tmp/safha-latest
  KAGGLE_API_TOKEN="$KAGGLE_API_TOKEN" KAGGLE_USERNAME=telethonfool \
    /opt/kaggle-venv/bin/kaggle datasets download telethonfool/safha-latest -p /tmp/safha-latest --unzip
  cp -r /tmp/safha-latest/models/qari-ct2-base /tmp/safha-latest/models/qari-v50-ct2 "$M/"
  (cd /tmp/safha-latest && sha256sum -c SHA256SUMS --ignore-missing)
  rm -rf /tmp/safha-latest
fi
chown -R ubuntu:ubuntu "$M"

cat > /opt/qari/SERVER_READY.txt <<EOF
Bootstrap finished $(date -u +%FT%TZ).
Models in $M: $(ls "$M" | tr '\n' ' ')
Next: CLOUDFLARE_TUNNEL_TOKEN=... /opt/safha-server/deploy.sh
EOF
