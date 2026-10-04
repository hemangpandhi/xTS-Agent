#!/usr/bin/env bash
# ============================================================
# xTS Agent — Download xTS Packages
# ============================================================
# Downloads and extracts Android xTS test suite packages.
#
# Note: GTS and some packages require partner portal access.
# Public packages (CTS, VTS, STS) are available from
# source.android.com. CATBox must be built from AOSP source.
#
# Usage:
#   export ANDROID_VERSION="15"    # Android version
#   export ARCH="arm64-v8a"        # arm64-v8a | x86_64
#   chmod +x scripts/download_xts_packages.sh
#   ./scripts/download_xts_packages.sh
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

mkdir -p "${XTS_DIR}"
cd "${XTS_DIR}"

# ─────────────────────────────────────────────────────────────
# CTS Download
# ─────────────────────────────────────────────────────────────
log_step "CTS (Compatibility Test Suite)"

CTS_DIR="${XTS_DIR}/android-cts"
if [ -d "${CTS_DIR}" ]; then
    log_info "CTS already downloaded at ${CTS_DIR}"
else
    log_info "Download CTS from: https://source.android.com/docs/compatibility/cts/downloads"
    log_info "Select Android ${ANDROID_VERSION} for ${ARCH}"
    log_warn "CTS must be downloaded manually from the official page."
    log_warn "After downloading, extract to: ${CTS_DIR}"
    log_warn ""
    log_warn "Example:"
    log_warn "  wget <CTS_DOWNLOAD_URL> -O android-cts.zip"
    log_warn "  unzip android-cts.zip -d ${XTS_DIR}/"
    echo ""

    # Also download CTS media files
    log_info "CTS Media files are also required:"
    log_warn "  Download android-cts-media-*.zip from the same page"
    log_warn "  Then run: ./android-cts/tools/cts-tradefed run cts-media-copy"
fi

# ─────────────────────────────────────────────────────────────
# VTS Download
# ─────────────────────────────────────────────────────────────
log_step "VTS (Vendor Test Suite)"

VTS_DIR="${XTS_DIR}/android-vts"
if [ -d "${VTS_DIR}" ]; then
    log_info "VTS already downloaded at ${VTS_DIR}"
else
    log_info "Download VTS from Android partner portal or build from AOSP:"
    log_warn "  source build/envsetup.sh"
    log_warn "  lunch <target>-userdebug"
    log_warn "  make vts -j\$(nproc)"
    log_warn "  # Output: out/host/linux-x86/vts/android-vts.zip"
    log_warn "  unzip android-vts.zip -d ${XTS_DIR}/"
fi

# ─────────────────────────────────────────────────────────────
# STS Download
# ─────────────────────────────────────────────────────────────
log_step "STS (Security Test Suite)"

STS_DIR="${XTS_DIR}/android-sts"
if [ -d "${STS_DIR}" ]; then
    log_info "STS already downloaded at ${STS_DIR}"
else
    log_info "Download STS from partner portal or build from AOSP:"
    log_warn "  make sts -j\$(nproc)"
    log_warn "  unzip android-sts.zip -d ${XTS_DIR}/"
fi

# ─────────────────────────────────────────────────────────────
# GTS Download
# ─────────────────────────────────────────────────────────────
log_step "GTS (Google Test Suite)"

GTS_DIR="${XTS_DIR}/android-gts"
if [ -d "${GTS_DIR}" ]; then
    log_info "GTS already downloaded at ${GTS_DIR}"
else
    log_warn "GTS is PROPRIETARY and available only through Google Partner Portal."
    log_warn "Contact your Google TAM (Technical Account Manager) for access."
    log_warn "Once downloaded, extract to: ${GTS_DIR}"
fi

# ─────────────────────────────────────────────────────────────
# ATS Download
# ─────────────────────────────────────────────────────────────
log_step "ATS (Automotive Test Suite)"

ATS_DIR="${XTS_DIR}/android-ats"
if [ -d "${ATS_DIR}" ]; then
    log_info "ATS already downloaded at ${ATS_DIR}"
else
    log_warn "ATS is distributed through Google Partner Portal for automotive OEMs."
    log_warn "Contact your Google TAM for access."
    log_warn "Once downloaded, extract to: ${ATS_DIR}"
fi

# ─────────────────────────────────────────────────────────────
# CATBox Build
# ─────────────────────────────────────────────────────────────
log_step "CATBox (Complete Automotive Tests in a Box)"

CATBOX_DIR="${XTS_DIR}/android-catbox"
if [ -d "${CATBOX_DIR}" ]; then
    log_info "CATBox already available at ${CATBOX_DIR}"
else
    log_info "CATBox must be built from AOSP source tree:"
    log_warn "  source build/envsetup.sh"
    log_warn "  lunch <automotive_target>-userdebug"
    log_warn "  m catbox -j\$(nproc)"
    log_warn "  # Copy from: out/host/linux-x86/catbox/android-catbox/"
    log_warn "  cp -r out/host/linux-x86/catbox/android-catbox/ ${CATBOX_DIR}"
fi

# ─────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────
log_step "Summary"

echo ""
echo "Expected directory structure:"
echo "  ${XTS_DIR}/"
echo "  ├── platform-tools/     (adb, fastboot)"
echo "  ├── android-cts/        (CTS package)"
echo "  ├── android-vts/        (VTS package)"
echo "  ├── android-sts/        (STS package)"
echo "  ├── android-gts/        (GTS package - partner access)"
echo "  ├── android-ats/        (ATS package - partner access)"
echo "  └── android-catbox/     (CATBox - built from AOSP)"
echo ""

log_info "Available packages:"
for dir in android-cts android-vts android-sts android-gts android-ats android-catbox; do
    if [ -d "${XTS_DIR}/${dir}" ]; then
        echo -e "  ${GREEN}✓${NC} ${dir}"
    else
        echo -e "  ${RED}✗${NC} ${dir} (not found)"
    fi
done
echo ""
