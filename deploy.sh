#!/usr/bin/env bash
# Start the whole Safha backend on the Oracle A1 server (run as ubuntu after
# the first-boot bootstrap finished; see /opt/qari/SERVER_READY.txt).
#
#   CLOUDFLARE_TUNNEL_TOKEN=... /opt/safha-server/deploy.sh
#
# - Creates infra/.env on first run with fresh secrets (a new, empty database:
#   the old VPS data is gone, so users sign in again).
# - Builds and starts Postgres, Redis, core API, recitation API and worker.
# - Runs the Caddy edge (127.0.0.1:8088) and, with a token, the Cloudflare
#   Tunnel so aiquranic.com reaches this server without opening any port.
set -euo pipefail
QARI_DIR=${QARI_DIR:-/opt/qari}
cd "$QARI_DIR"
ROOT=$(pwd)
ENV=infra/.env
MODELS=${QARI_MODELS_DIR:-/opt/qari-models}
DOMAIN=${SAFHA_DOMAIN:-aiquranic.com}

for m in qari-fastconformer-pcd-rnnt-int8 qari-ct2-base; do
  [ -d "$MODELS/$m" ] || { echo "Missing $MODELS/$m (see /var/log/safha-bootstrap.log)"; exit 1; }
done

if [ ! -f "$ENV" ]; then
  PG=$(openssl rand -hex 24)
  cat > "$ENV" <<EOF
QARI_JWT_SECRET_KEY=$(openssl rand -hex 32)
QARI_JWT_ALGORITHM=HS256
QARI_JWT_ISSUER=qari-core-api
QARI_POSTGRES_PASSWORD=$PG
QARI_DATABASE_URL=postgresql+asyncpg://qari:$PG@postgres:5432/qari
QARI_CORS_ORIGINS='["https://$DOMAIN"]'
QARI_RECITATION_API_PUBLIC_URL=https://$DOMAIN
QARI_MODELS_DIR=$MODELS
QARI_LIVE_ASR_ENGINE=fastconformer_rnnt
QARI_LIVE_VOWEL_WITNESS=off
EOF
  chmod 600 "$ENV"
  echo "Created $ENV with new secrets (back it up)."
fi

(cd infra && docker compose up -d --build)

docker rm -f qari-edge >/dev/null 2>&1 || true
docker run -d --name qari-edge --restart unless-stopped --network host \
  -e QARI_WEBSITE_DIR=/srv/website \
  -v "$ROOT/infra/Caddyfile.edge:/etc/caddy/Caddyfile:ro" \
  -v "$ROOT/website/public:/srv/website:ro" \
  caddy:2.8.4

if [ -n "${CLOUDFLARE_TUNNEL_TOKEN:-}" ]; then
  docker rm -f qari-tunnel >/dev/null 2>&1 || true
  docker run -d --name qari-tunnel --restart unless-stopped --network host \
    cloudflare/cloudflared:latest tunnel --no-autoupdate run --token "$CLOUDFLARE_TUNNEL_TOKEN"
  echo "Tunnel started. In Cloudflare, point $DOMAIN to http://127.0.0.1:8088."
else
  echo "No CLOUDFLARE_TUNNEL_TOKEN: the site is only reachable on the server itself."
fi

echo "Waiting for health..."
for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8088/health >/dev/null; then
    echo "Core API healthy behind the edge."
    curl -fsS http://127.0.0.1:8001/health && echo " <- recitation API"
    exit 0
  fi
  sleep 5
done
echo "Not healthy yet: docker compose -f infra/docker-compose.yml logs --tail 50"
exit 1
