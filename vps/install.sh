#!/usr/bin/env bash
# Run the Oracle A1 grabber on your own VPS (instead of GitHub Actions).
# Usage, as root on the VPS:
#   git clone https://github.com/khan23153/safha-server /opt/safha-server
#   bash /opt/safha-server/vps/install.sh            # first time: writes the settings file, then stops
#   nano /etc/safha-grabber/grabber.env              # fill in your values, copy your two key files
#   bash /opt/safha-server/vps/install.sh            # second time: starts the grabber
# Logs: journalctl -u safha-grabber -f      Stop: systemctl disable --now safha-grabber
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
CONF=/etc/safha-grabber
[ "$(id -u)" = 0 ] || { echo "Run as root (sudo bash $0)"; exit 1; }

if ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
  apt-get update -qq && apt-get install -y -qq python3-venv
fi
[ -x "$REPO/.venv/bin/python" ] || python3 -m venv "$REPO/.venv"
"$REPO/.venv/bin/pip" install -q oci==2.189.0

install -d -m 700 "$CONF"
if [ ! -f "$CONF/grabber.env" ]; then
  install -m 600 "$REPO/vps/grabber.env.example" "$CONF/grabber.env"
  echo "Wrote $CONF/grabber.env. Fill it in, put oci_api_key.pem and ssh.pub in $CONF, then run this script again."
  exit 0
fi
if grep -q 'ocid1.user.oc1..xxxx' "$CONF/grabber.env"; then
  echo "$CONF/grabber.env still has the example values. Fill it in first."; exit 1
fi
for f in oci_api_key.pem ssh.pub; do
  [ -s "$CONF/$f" ] || { echo "Missing $CONF/$f"; exit 1; }
done
chmod 600 "$CONF"/*

sed "s#__REPO__#$REPO#g" "$REPO/vps/safha-grabber.service" > /etc/systemd/system/safha-grabber.service
systemctl daemon-reload
systemctl enable --now safha-grabber
echo "Started. Follow it with: journalctl -u safha-grabber -f"
