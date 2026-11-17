#!/usr/bin/env bash
# ============================================================
# xTS Agent - Automated Cuttlefish Cluster Spawner
# Usage: ./scripts/start_cluster.sh [NUM_INSTANCES]
# Env:   AOSP_ROOT, LUNCH_TARGET, CVD_HOME, WAIT_FOR_ADB=1
# ============================================================
set -euo pipefail

NUM_INSTANCES="${1:-8}"
AOSP_ROOT="${AOSP_ROOT:-/mnt/aosp-17-master}"
LUNCH_TARGET="${LUNCH_TARGET:-aosp_cf_x86_64_auto-trunk_staging-user}"
CVD_HOME="${CVD_HOME:-/tmp/cvd_cluster_home}"
WAIT_FOR_ADB="${WAIT_FOR_ADB:-1}"
WAIT_TIMEOUT_SECS="${WAIT_TIMEOUT_SECS:-300}"

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

if command -v stop_cvd >/dev/null 2>&1; then
  stop_cvd >/dev/null 2>&1 || true
fi

if ! command -v launch_cvd >/dev/null 2>&1; then
  echo "ERROR: launch_cvd not found after lunch ${LUNCH_TARGET}" >&2
  exit 1
fi

launch_cvd --num_instances="${NUM_INSTANCES}" --daemon --resume=false --report_anonymous_usage_stats=y

if [[ "${WAIT_FOR_ADB}" == "1" ]]; then
  echo "Waiting for ADB devices to finish booting..."
  deadline=$((SECONDS + WAIT_TIMEOUT_SECS))
  while true; do
    booted=0
    for s in $(adb devices | awk 'NR>1 && $2=="device" {print $1}'); do
      bc="$(adb -s "$s" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')"
      if [[ "${bc}" == "1" ]]; then
        booted=$((booted + 1))
      fi
    done
    echo "Booted Cuttlefish user-build devices: ${booted}/${NUM_INSTANCES}"
    if [[ "${booted}" -ge "${NUM_INSTANCES}" ]]; then
      break
    fi
    if [[ "${SECONDS}" -ge "${deadline}" ]]; then
      echo "WARNING: timed out waiting for ${NUM_INSTANCES} devices (have ${booted})" >&2
      break
    fi
    sleep 5
  done
fi

echo "=== Verifying Cuttlefish user build fingerprint & ADB access ==="
for s in $(adb devices | awk 'NR>1 && $2=="device" {print $1}'); do
  fp="$(adb -s "$s" shell getprop ro.build.fingerprint 2>/dev/null | tr -d '\r')"
  bt="$(adb -s "$s" shell getprop ro.build.type 2>/dev/null | tr -d '\r')"
  dbg="$(adb -s "$s" shell getprop ro.debuggable 2>/dev/null | tr -d '\r')"
  ks="$(adb -s "$s" shell pm list packages com.google.android.car.kitchensink 2>/dev/null | tr -d '\r')"
  echo "  [${s}] type=${bt} debuggable=${dbg} fingerprint=${fp} kitchensink='${ks}'"
done

echo "Cluster launch complete."
