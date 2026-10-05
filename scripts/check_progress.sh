#!/usr/bin/env bash
# Live progress summary from the newest TradeFed agent log.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${LOG_DIR:-${ROOT_DIR}/results/logs}"
TOTAL_MODULES="${TOTAL_MODULES:-0}"

shopt -s nullglob
logs=("${LOG_DIR}"/tradefed_run_*.log)
if [[ ${#logs[@]} -eq 0 ]]; then
  echo "No TradeFed logs found under ${LOG_DIR}" >&2
  exit 1
fi

# Newest by mtime
LATEST_LOG="$(ls -t "${logs[@]}" | head -n 1)"
COMPLETED="$(grep -c "Sharded test completed:" "${LATEST_LOG}" || true)"
FAILED="$(grep -c "FAILED:" "${LATEST_LOG}" || true)"

START_EPOCH="$(basename "${LATEST_LOG}" | grep -oE '[0-9]+' | head -n 1 || true)"
CURRENT_EPOCH="$(date +%s)"
ELAPSED_SEC=0
if [[ -n "${START_EPOCH}" ]]; then
  ELAPSED_SEC=$((CURRENT_EPOCH - START_EPOCH))
fi
ELAPSED_H=$((ELAPSED_SEC / 3600))
ELAPSED_M=$(((ELAPSED_SEC % 3600) / 60))

SEC_PER_MODULE="n/a"
REMAINING_H="?"
REMAINING_M="?"
if [[ "${COMPLETED}" -gt 0 ]]; then
  SEC_PER_MODULE=$((ELAPSED_SEC / COMPLETED))
  if [[ "${TOTAL_MODULES}" =~ ^[0-9]+$ && "${TOTAL_MODULES}" -gt 0 ]]; then
    REMAINING_MODULES=$((TOTAL_MODULES - COMPLETED))
    if [[ "${REMAINING_MODULES}" -lt 0 ]]; then
      REMAINING_MODULES=0
    fi
    REMAINING_SEC=$((REMAINING_MODULES * SEC_PER_MODULE))
    REMAINING_H=$((REMAINING_SEC / 3600))
    REMAINING_M=$(((REMAINING_SEC % 3600) / 60))
  fi
fi

echo "==========================================="
echo " xTS Run - Live Performance Matrix"
echo "==========================================="
echo "Log File: ${LATEST_LOG}"
echo "Completed Modules: ${COMPLETED} / ${TOTAL_MODULES:-unknown}"
echo "Module Failures/Crashes: ${FAILED}"
echo "-------------------------------------------"
echo "Elapsed Time: ${ELAPSED_H}h ${ELAPSED_M}m"
echo "Estimated Remaining: ~${REMAINING_H}h ${REMAINING_M}m"
echo "Average Speed: ~${SEC_PER_MODULE} seconds / module"
echo "==========================================="
