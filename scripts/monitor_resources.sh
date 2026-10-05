#!/usr/bin/env bash
# Append host resource samples for long TradeFed runs.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${LOG_DIR:-${ROOT_DIR}/results/logs}"
LOGFILE="${LOGFILE:-${LOG_DIR}/resource_usage.log}"
INTERVAL_SECS="${INTERVAL_SECS:-60}"

mkdir -p "${LOG_DIR}"
if [[ ! -f "${LOGFILE}" ]]; then
  echo "Timestamp,CPU_Load_1m,RAM_Used_GB,RAM_Free_GB" > "${LOGFILE}"
fi

echo "Writing resource samples to ${LOGFILE} every ${INTERVAL_SECS}s (Ctrl+C to stop)"
while true; do
  TIMESTAMP="$(date "+%Y-%m-%d %H:%M:%S")"
  LOAD="$(awk '{print $1}' /proc/loadavg)"
  RAM_USED="$(free -g | awk '/^Mem:/ {print $3}')"
  RAM_FREE="$(free -g | awk '/^Mem:/ {print $4}')"
  echo "${TIMESTAMP},${LOAD},${RAM_USED},${RAM_FREE}" >> "${LOGFILE}"
  sleep "${INTERVAL_SECS}"
done
