#!/usr/bin/env bash
# ============================================================
# xTS Agent — Multi-device nightly launcher
# Optionally spawns a Cuttlefish cluster, waits for ADB devices,
# then runs a hardware-oriented test plan.
#
# Usage:
#   ./scripts/run_nightly.sh
#   NUM_DEVICES=4 SPAWN_CLUSTER=0 TEST_PLAN=config/test_plans/cts_only.yaml \
#     ./scripts/run_nightly.sh
#
# Env: AOSP_ROOT, LUNCH_TARGET, XTS_AGENT_HOME, NUM_DEVICES, TEST_PLAN
# ============================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
XTS_AGENT_HOME="${XTS_AGENT_HOME:-${ROOT_DIR}}"
NUM_DEVICES="${NUM_DEVICES:-10}"
WAIT_TIMEOUT_SECS="${WAIT_TIMEOUT_SECS:-1800}"
# Default to hardware CTS; override with full_cts.yaml for Cuttlefish/virtual.
TEST_PLAN="${TEST_PLAN:-config/test_plans/full_cts_hardware.yaml}"
CONFIG_PATH="${CONFIG_PATH:-config/default_config.yaml}"
STOP_EXISTING="${STOP_EXISTING:-0}"
SPAWN_CLUSTER="${SPAWN_CLUSTER:-1}"

cd "${XTS_AGENT_HOME}"

if [[ -d .venv ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

log() { echo "[nightly] $*"; }
die() { echo "[nightly] FATAL: $*" >&2; exit 1; }

command -v adb >/dev/null || die "adb not found"
command -v python3 >/dev/null || die "python3 not found"

if [[ "${STOP_EXISTING}" == "1" ]]; then
  log "STOP_EXISTING=1 — stopping local Cuttlefish cluster if present"
  if command -v stop_cvd >/dev/null 2>&1; then
    stop_cvd || true
  else
    die "stop_cvd not found; refuse host-wide process kills. Set STOP_EXISTING=0 or install CVD tools."
  fi
fi

if [[ "${SPAWN_CLUSTER}" == "1" ]]; then
  log "Spawning ${NUM_DEVICES} CVD instances via scripts/start_cluster.sh"
  "${XTS_AGENT_HOME}/scripts/start_cluster.sh" "${NUM_DEVICES}"
fi

log "Waiting up to ${WAIT_TIMEOUT_SECS}s for >= ${NUM_DEVICES} ADB devices"
deadline=$((SECONDS + WAIT_TIMEOUT_SECS))
while true; do
  count="$(adb devices | awk 'NR>1 && $2=="device" {c++} END{print c+0}')"
  log "Online devices: ${count}"
  if [[ "${count}" -ge "${NUM_DEVICES}" ]]; then
    break
  fi
  if [[ "${SECONDS}" -ge "${deadline}" ]]; then
    die "Timed out waiting for ${NUM_DEVICES} devices (have ${count})"
  fi
  sleep 15
done

mkdir -p "${XTS_AGENT_HOME}/results/logs"
log "Starting plan ${TEST_PLAN}"
python3 -m xts_agent.cli run \
  --plan "${TEST_PLAN}" \
  --config "${CONFIG_PATH}" \
  --auto-retry \
  | tee "${XTS_AGENT_HOME}/results/logs/nightly_execution_$(date +%Y%m%d_%H%M%S).log"
