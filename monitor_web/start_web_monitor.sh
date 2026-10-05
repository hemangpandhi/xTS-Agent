#!/usr/bin/env bash
# Localhost-only resource monitor for lab hosts.
# Bind address defaults to 127.0.0.1 (not LAN-exposed).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MONITOR_DIR="${MONITOR_DIR:-${ROOT_DIR}/monitor_web}"
BIND_HOST="${BIND_HOST:-127.0.0.1}"
PORT="${PORT:-8585}"
REFRESH_SECS="${REFRESH_SECS:-5}"

mkdir -p "${MONITOR_DIR}"
cd "${MONITOR_DIR}"

cleanup() {
  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "${SERVER_PID}" 2>/dev/null; then
    kill "${SERVER_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

python3 -m http.server "${PORT}" --bind "${BIND_HOST}" >server.out 2>&1 &
SERVER_PID=$!
echo "${SERVER_PID}" > server.pid
echo "Monitor at http://${BIND_HOST}:${PORT}/ (pid=${SERVER_PID})"

while true; do
  {
    echo '<!DOCTYPE html><html><head><title>xTS Agent Resource Monitor</title>'
    echo "<meta http-equiv=\"refresh\" content=\"${REFRESH_SECS}\">"
    echo '<style>body{background:#111;color:#0f0;font-family:monospace;padding:20px}h2{color:#fff}</style>'
    echo '</head><body>'
    echo '<h2>xTS Agent Resource Monitor</h2>'
    echo "<h3>Updated: $(date '+%Y-%m-%d %H:%M:%S')</h3><hr><h3>Memory</h3><pre>"
    free -h
    echo '</pre><hr><h3>Disk</h3><pre>'
    df -h / /opt /tmp 2>/dev/null || df -h
    echo '</pre><hr><h3>Top</h3><pre>'
    top -b -n 1 | head -n 25
    echo '</pre></body></html>'
  } > index.html.tmp
  mv index.html.tmp index.html
  sleep "${REFRESH_SECS}"
done
