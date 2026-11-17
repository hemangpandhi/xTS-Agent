#!/usr/bin/env bash
# "Flash" a Cuttlefish DUT with the images just built: stop its cvd group and
# relaunch it from the build output with a wiped userdata, then wait for boot.
#   cf_flash.sh <serial>
# Env: AOSP_ROOT, CVD_HOME (the DUT's own cvd group), BASE_INSTANCE, IMAGE_DIR.
set -eo pipefail
SERIAL="${1:?serial}"
AOSP_ROOT="${AOSP_ROOT:-/mnt/aosp-17-master}"
CVD_HOME="${CVD_HOME:-/tmp/cvd_oem_home}"
BASE_INSTANCE="${BASE_INSTANCE:-9}"
IMAGE_DIR="${IMAGE_DIR:-${AOSP_ROOT}/out/target/product/vsoc_x86_64_only}"
HOST_OUT="${AOSP_ROOT}/out/host/linux-x86"
export PATH="${HOST_OUT}/bin:${PATH}" ANDROID_HOST_OUT="${HOST_OUT}" HOME="${CVD_HOME}"
mkdir -p "${CVD_HOME}"
echo "=== stop ${SERIAL} (cvd group in ${CVD_HOME})"
stop_cvd >/dev/null 2>&1 || true
adb disconnect "${SERIAL}" >/dev/null 2>&1 || true
echo "=== flash images from ${IMAGE_DIR} ($(cat "${IMAGE_DIR}/build_fingerprint-"*.txt 2>/dev/null | head -1))"
launch_cvd --base_instance_num="${BASE_INSTANCE}" --num_instances=1 --daemon --resume=false \
  --report_anonymous_usage_stats=n -system_image_dir="${IMAGE_DIR}"
adb connect "${SERIAL}" >/dev/null
deadline=$((SECONDS + ${BOOT_TIMEOUT_SECS:-600}))
until [[ "$(adb -s "${SERIAL}" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" == "1" ]]; do
  [[ ${SECONDS} -ge ${deadline} ]] && { echo "boot timeout" >&2; exit 1; }
  sleep 5; adb connect "${SERIAL}" >/dev/null 2>&1 || true
done
echo "=== ${SERIAL} booted: $(adb -s "${SERIAL}" shell getprop ro.build.fingerprint | tr -d '\r')"
