#!/usr/bin/env bash
# ============================================================
# xTS Agent - Automated Cuttlefish Cluster Spawner
# Usage: ./scripts/start_cluster.sh [NUM_INSTANCES]
# Env:   AOSP_ROOT, LUNCH_TARGET, CVD_HOME, WAIT_FOR_ADB=1
# ============================================================
set -euo pipefail

NUM_INSTANCES="${1:-10}"
AOSP_ROOT="${AOSP_ROOT:-/mnt/aosp}"
LUNCH_TARGET="${LUNCH_TARGET:-aosp_cf_x86_64_phone-userdebug}"
CVD_HOME="${CVD_HOME:-/tmp/cvd_cluster_home}"
WAIT_FOR_ADB="${WAIT_FOR_ADB:-1}"
WAIT_TIMEOUT_SECS="${WAIT_TIMEOUT_SECS:-600}"

if [[ ! -d "${AOSP_ROOT}" ]]; then
  echo "ERROR: AOSP_ROOT does not exist: ${AOSP_ROOT}" >&2
  echo "Export AOSP_ROOT to your Android source tree before launching." >&2
  exit 1
fi

echo "======================================================"
echo "Spawning ${NUM_INSTANCES} Cuttlefish instance(s)"
echo "AOSP_ROOT=${AOSP_ROOT}"
echo "LUNCH_TARGET=${LUNCH_TARGET}"
echo "======================================================"

cd "${AOSP_ROOT}"
# envsetup/lunch are not always set -e friendly
set +e
# shellcheck disable=SC1091
source build/envsetup.sh
lunch "${LUNCH_TARGET}"
set -e

mkdir -p "${CVD_HOME}"
export HOME="${CVD_HOME}"

if ! command -v launch_cvd >/dev/null 2>&1; then
  echo "ERROR: launch_cvd not found after lunch ${LUNCH_TARGET}" >&2
  exit 1
fi

launch_cvd --num_instances="${NUM_INSTANCES}" --daemon -report_anonymous_usage_stats=y

if [[ "${WAIT_FOR_ADB}" == "1" ]]; then
  echo "Waiting for ADB devices..."
  deadline=$((SECONDS + WAIT_TIMEOUT_SECS))
  while true; do
    count="$(adb devices | awk 'NR>1 && $2=="device" {c++} END{print c+0}')"
    echo "Online devices: ${count}"
    if [[ "${count}" -ge "${NUM_INSTANCES}" ]]; then
      break
    fi
    if [[ "${SECONDS}" -ge "${deadline}" ]]; then
      echo "WARNING: timed out waiting for ${NUM_INSTANCES} devices (have ${count})" >&2
      break
    fi
    sleep 5
  done
fi

echo "Cluster launch complete. Verify with: adb devices"
echo "Stop with: stop_cvd"
