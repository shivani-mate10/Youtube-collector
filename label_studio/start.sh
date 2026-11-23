#!/usr/bin/env bash
# Start the Cloudflare tunnel (if it isn't running) and the shared Label Studio.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
set -a
source "$DIR/label-studio.env"
set +a

# The tunnel also serves other sites (e.g. the backend), so it is left running when Label Studio stops.
if pgrep -f "cloudflared tunnel run ${TUNNEL_NAME}" >/dev/null; then
  echo "Cloudflare tunnel '${TUNNEL_NAME}' is already running."
elif command -v cloudflared >/dev/null; then
  nohup cloudflared tunnel run "${TUNNEL_NAME}" >"$DIR/cloudflared.log" 2>&1 &
  echo "Started Cloudflare tunnel '${TUNNEL_NAME}' (log: label_studio/cloudflared.log)."
else
  echo "WARNING: cloudflared is not installed; only the local address will work." >&2
fi

# Open the public address (not localhost) once the server has had time to start.
if command -v xdg-open >/dev/null; then
  (sleep 8; xdg-open "${HOST}" >/dev/null 2>&1) &
fi

# run_label_studio.py = normal Label Studio plus a Referrer-Policy fix (YouTube player Error 153).
exec "$HOME/label-studio-env/bin/python3" "$DIR/run_label_studio.py" start --no-browser --port "${LS_PORT:-8080}"


#####To export data from label studio run folllowing command####
# in screendetox-youtube-collector
# .venv/bin/collector label merge
# .venv/bin/collector thumbnails

