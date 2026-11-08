#!/usr/bin/env bash
# Build the PWA on this Mac, copy everything to the server, build + start the
# container there with docker compose, then check /api/health.
#
#   ./deploy.sh            # full deploy
#   ./deploy.sh --logs     # only show the container's last log lines
#
# Server access goes through the Home Assistant SSH add-on (no SFTP -> scp -O,
# /share only writable with sudo, docker only with sudo, no compose plugin in the
# add-on -> compose runs from the official docker:cli image).
set -euo pipefail

HOST="${DEPLOY_HOST:-hassio@192.168.1.120}"
SERVER_IP="${HOST#*@}"
PORT=8090
ADDON_DIR=/share/printerdash                     # path inside the SSH add-on
HOST_DIR=/mnt/data/supervisor/share/printerdash  # the same folder on the host (for the docker daemon)
CLI_IMAGE=docker:28.3.3-cli

cd "$(dirname "$0")"

if [[ "${1:-}" == "--logs" ]]; then
  ssh "$HOST" "sudo docker logs --tail 80 printerdash"
  exit 0
fi

echo "==> Building the frontend"
(
  cd frontend
  [[ -d node_modules ]] || npm ci
  npm run build
)

echo "==> Packing"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
tar --exclude='__pycache__' --exclude='*.pyc' -czf "$TMP/printerdash.tgz" \
  Dockerfile docker-compose.yml .dockerignore backend/requirements.txt backend/app frontend/dist

echo "==> Copying to $HOST"
scp -O -q "$TMP/printerdash.tgz" "$HOST:/tmp/printerdash.tgz"

echo "==> Building and starting on the server"
ssh "$HOST" "set -e
  sudo mkdir -p $ADDON_DIR
  sudo rm -rf $ADDON_DIR/backend $ADDON_DIR/frontend
  sudo tar -xzf /tmp/printerdash.tgz -C $ADDON_DIR
  rm -f /tmp/printerdash.tgz
  sudo docker run --rm \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v $HOST_DIR:$HOST_DIR -w $HOST_DIR \
    $CLI_IMAGE docker compose -p printerdash up -d --build
  sudo docker image prune -f --filter label=app=printerdash >/dev/null"

echo "==> Health check (http://$SERVER_IP:$PORT)"
for _ in $(seq 1 30); do
  if curl -fsS -m 3 "http://$SERVER_IP:$PORT/api/health"; then
    echo
    echo "OK - open http://$SERVER_IP:$PORT"
    exit 0
  fi
  sleep 2
done
echo "Health check FAILED - last log lines:"
ssh "$HOST" "sudo docker logs --tail 40 printerdash" || true
exit 1
