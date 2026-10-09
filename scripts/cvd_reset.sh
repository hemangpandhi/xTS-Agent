#!/usr/bin/env bash
# ============================================================
# xTS Agent - Cuttlefish reset hook for FULLY_ISOLATED retries
# Maps an adb serial (0.0.0.0:6520, 127.0.0.1:6521, ...) to its
# Cuttlefish instance number and powerwashes that instance only.
#
# Configure in default_config.yaml:
#   device:
#     virtual_reset_command: "scripts/cvd_reset.sh {serial}"
#
# Env: CVD_HOME   HOME used by start_cluster.sh (default /tmp/cvd_cluster_home)
#      CVD_BASE_PORT  adb port of instance 1 (default 6520)
#      DRY_RUN=1  print the command instead of running it
# ============================================================
set -euo pipefail

SERIAL="${1:-}"
if [[ -z "${SERIAL}" ]]; then
  echo "Usage: $0 <adb-serial>" >&2
  exit 2
fi

CVD_HOME="${CVD_HOME:-/tmp/cvd_cluster_home}"
CVD_BASE_PORT="${CVD_BASE_PORT:-6520}"

PORT="${SERIAL##*:}"
if [[ "${PORT}" == "${SERIAL}" || ! "${PORT}" =~ ^[0-9]+$ ]]; then
  echo "ERROR: ${SERIAL} is not a host:port Cuttlefish serial; refusing to reset" >&2
  exit 1
fi
INSTANCE=$((PORT - CVD_BASE_PORT + 1))
if [[ "${INSTANCE}" -lt 1 ]]; then
  echo "ERROR: port ${PORT} is below CVD_BASE_PORT ${CVD_BASE_PORT}" >&2
  exit 1
fi

if command -v powerwash_cvd >/dev/null 2>&1; then
  CMD=(powerwash_cvd "--instance_num=${INSTANCE}")
elif command -v cvd >/dev/null 2>&1; then
  CMD=(cvd powerwash "--instance_num=${INSTANCE}")
else
  echo "ERROR: neither powerwash_cvd nor cvd found on PATH" >&2
  exit 1
fi

echo "Resetting ${SERIAL} (instance ${INSTANCE}): ${CMD[*]}"
if [[ "${DRY_RUN:-0}" == "1" ]]; then
  exit 0
fi
HOME="${CVD_HOME}" "${CMD[@]}"
