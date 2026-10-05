#!/usr/bin/env bash
# Wait for a minimum number of ADB devices, then run a plan.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT_DIR}"

MIN_DEVICES="${MIN_DEVICES:-4}"
WAIT_TIMEOUT_SECS="${WAIT_TIMEOUT_SECS:-900}"
TEST_PLAN="${TEST_PLAN:-config/test_plans/smoke_test.yaml}"
CONFIG_PATH="${CONFIG_PATH:-config/default_config.yaml}"
DRY_RUN="${DRY_RUN:-0}"

if [[ -d .venv ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

command -v adb >/dev/null || { echo "adb not found" >&2; exit 1; }

echo "Waiting up to ${WAIT_TIMEOUT_SECS}s for >= ${MIN_DEVICES} devices..."
deadline=$((SECONDS + WAIT_TIMEOUT_SECS))
while true; do
  count="$(adb devices | awk 'NR>1 && $2=="device" {c++} END{print c+0}')"
  echo "Current online devices: ${count}"
  if [[ "${count}" -ge "${MIN_DEVICES}" ]]; then
    break
  fi
  if [[ "${SECONDS}" -ge "${deadline}" ]]; then
    echo "Timed out waiting for devices" >&2
    exit 1
  fi
  sleep 10
done

args=(run --plan "${TEST_PLAN}" --config "${CONFIG_PATH}")
if [[ "${DRY_RUN}" == "1" ]]; then
  args+=(--dry-run)
fi

mkdir -p results/logs
python3 -m xts_agent.cli "${args[@]}" | tee "results/logs/run_when_ready_$(date +%Y%m%d_%H%M%S).log"
