#!/usr/bin/env bash
# ============================================================
# xTS Agent — Validate / guide xTS package acquisition
# Public CTS/VTS/STS cannot always be auto-fetched (license /
# partner portals). This script:
#   1) Documents required layout under $XTS_PACKAGES_DIR
#   2) Validates installed packages
#   3) Exits non-zero if REQUIRED_SUITES are missing
#
# Usage:
#   REQUIRED_SUITES="cts,vts" ./scripts/download_xts_packages.sh
# ============================================================
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_step()  { echo -e "\n${BLUE}===== $1 =====${NC}"; }

XTS_DIR="${XTS_PACKAGES_DIR:-/opt/xts}"
ANDROID_VERSION="${ANDROID_VERSION:-15}"
ARCH="${ARCH:-arm64-v8a}"
REQUIRED_SUITES="${REQUIRED_SUITES:-cts}"
ALLOW_MISSING="${ALLOW_MISSING:-0}"

mkdir -p "${XTS_DIR}"

print_manual_hints() {
  local name="$1"
  case "${name}" in
    cts)
      log_info "Download CTS from https://source.android.com/docs/compatibility/cts/downloads"
      log_warn "Extract to ${XTS_DIR}/android-cts (expect tools/cts-tradefed)"
      ;;
    vts)
      log_warn "Build/partner-fetch VTS then extract to ${XTS_DIR}/android-vts"
      ;;
    sts)
      log_warn "Build/partner-fetch STS then extract to ${XTS_DIR}/android-sts"
      ;;
    gts)
      log_warn "GTS is partner-portal only → ${XTS_DIR}/android-gts"
      ;;
    ats)
      log_warn "ATS is partner-portal only → ${XTS_DIR}/android-ats"
      ;;
    catbox)
      log_warn "Build CATBox from AOSP automotive target → ${XTS_DIR}/android-catbox"
      ;;
  esac
}

validate_suite() {
  local short="$1"
  local dir="${XTS_DIR}/android-${short}"
  local tf="${dir}/tools/${short}-tradefed"
  # CATBox / STS command names can vary slightly
  if [[ "${short}" == "sts" && ! -f "${tf}" ]]; then
    tf="${dir}/tools/sts-tradefed"
  fi
  if [[ "${short}" == "catbox" && ! -f "${tf}" ]]; then
    tf="${dir}/tools/catbox-tradefed"
  fi
  if [[ -d "${dir}" && -f "${tf}" ]]; then
    echo "ok"
  elif [[ -d "${dir}" ]]; then
    echo "partial"
  else
    echo "missing"
  fi
}

log_step "xTS package validation (${XTS_DIR})"
log_info "Android=${ANDROID_VERSION} arch=${ARCH} required=${REQUIRED_SUITES}"

ALL_SUITES=(cts vts sts gts ats catbox)
MISSING_REQUIRED=0

for short in "${ALL_SUITES[@]}"; do
  status="$(validate_suite "${short}")"
  case "${status}" in
    ok) log_info "OK      android-${short}" ;;
    partial)
      log_warn "PARTIAL android-${short} (dir exists, tradefed script missing)"
      print_manual_hints "${short}"
      ;;
    missing)
      log_warn "MISSING android-${short}"
      print_manual_hints "${short}"
      ;;
  esac
done

IFS=',' read -r -a REQ <<< "${REQUIRED_SUITES}"
for short in "${REQ[@]}"; do
  short="$(echo "${short}" | tr '[:upper:]' '[:lower:]' | xargs)"
  [[ -z "${short}" ]] && continue
  status="$(validate_suite "${short}")"
  if [[ "${status}" != "ok" ]]; then
    log_error "Required suite not ready: ${short} (${status})"
    MISSING_REQUIRED=1
  fi
done

log_step "Expected layout"
cat <<EOF
  ${XTS_DIR}/
  ├── android-cts/tools/cts-tradefed
  ├── android-vts/tools/vts-tradefed
  ├── android-sts/tools/sts-tradefed
  ├── android-gts/tools/gts-tradefed
  ├── android-ats/tools/ats-tradefed
  └── android-catbox/tools/catbox-tradefed
EOF

if [[ "${MISSING_REQUIRED}" -ne 0 ]]; then
  if [[ "${ALLOW_MISSING}" == "1" ]]; then
    log_warn "ALLOW_MISSING=1 — continuing despite missing required suites"
    exit 0
  fi
  log_error "Package validation failed"
  exit 1
fi

log_info "Required suites present"
exit 0
