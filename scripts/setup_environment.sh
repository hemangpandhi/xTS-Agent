#!/usr/bin/env bash
# ============================================================
# xTS Agent — Environment Setup Script
# ============================================================
# One-time setup script for the xTS test execution host.
# Installs all prerequisites for running xTS suites via
# TradeFed with GitLab CI/CD integration.
#
# Usage:
#   chmod +x scripts/setup_environment.sh
#   sudo ./scripts/setup_environment.sh
#
# Requirements:
#   - Ubuntu 20.04+ or Debian 11+
#   - Root/sudo access
#   - Internet connectivity
# ============================================================

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_step()  { echo -e "\n${BLUE}===== $1 =====${NC}"; }

XTS_PACKAGES_DIR="${XTS_PACKAGES_DIR:-/opt/xts}"
XTS_AGENT_DIR="${XTS_AGENT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"

# ─────────────────────────────────────────────────────────────
# Step 1: System packages
# ─────────────────────────────────────────────────────────────
log_step "Step 1: Installing system packages"

apt-get update -qq
apt-get install -y --no-install-recommends \
    openjdk-17-jdk \
    python3 \
    python3-pip \
    python3-venv \
    unzip \
    wget \
    curl \
    git \
    usbutils \
    libusb-1.0-0 \
    udev \
    sqlite3 \
    jq

# Verify Java
JAVA_VERSION=$(java -version 2>&1 | head -n1)
log_info "Java installed: ${JAVA_VERSION}"

# Set JAVA_HOME
export JAVA_HOME="/usr/lib/jvm/java-17-openjdk-amd64"
echo "export JAVA_HOME=${JAVA_HOME}" >> /etc/profile.d/xts_agent.sh

# ─────────────────────────────────────────────────────────────
# Step 2: Android SDK Platform Tools (adb, fastboot)
# ─────────────────────────────────────────────────────────────
log_step "Step 2: Installing Android SDK Platform Tools"

PLATFORM_TOOLS_DIR="${XTS_PACKAGES_DIR}/platform-tools"
if [ ! -f "${PLATFORM_TOOLS_DIR}/adb" ]; then
    log_info "Downloading Android Platform Tools..."
    mkdir -p "${XTS_PACKAGES_DIR}"
    cd "${XTS_PACKAGES_DIR}"
    wget -q "https://dl.google.com/android/repository/platform-tools-latest-linux.zip" \
        -O platform-tools.zip
    unzip -o -q platform-tools.zip
    rm -f platform-tools.zip
    log_info "Platform Tools installed to ${PLATFORM_TOOLS_DIR}"
else
    log_info "Platform Tools already installed at ${PLATFORM_TOOLS_DIR}"
fi

# Add to PATH
export PATH="${PLATFORM_TOOLS_DIR}:${PATH}"
echo "export PATH=${PLATFORM_TOOLS_DIR}:\${PATH}" >> /etc/profile.d/xts_agent.sh

# Verify adb
ADB_VERSION=$(adb version 2>&1 | head -n1)
log_info "ADB installed: ${ADB_VERSION}"

# ─────────────────────────────────────────────────────────────
# Step 3: USB permissions for Android devices
# ─────────────────────────────────────────────────────────────
log_step "Step 3: Configuring USB permissions"

# Create udev rules for Android devices
cat > /etc/udev/rules.d/51-android.rules << 'EOF'
# Google
SUBSYSTEM=="usb", ATTR{idVendor}=="18d1", MODE="0666", GROUP="plugdev"
# Samsung
SUBSYSTEM=="usb", ATTR{idVendor}=="04e8", MODE="0666", GROUP="plugdev"
# Qualcomm
SUBSYSTEM=="usb", ATTR{idVendor}=="05c6", MODE="0666", GROUP="plugdev"
# MediaTek
SUBSYSTEM=="usb", ATTR{idVendor}=="0e8d", MODE="0666", GROUP="plugdev"
# NXP
SUBSYSTEM=="usb", ATTR{idVendor}=="1fc9", MODE="0666", GROUP="plugdev"
# Renesas
SUBSYSTEM=="usb", ATTR{idVendor}=="045b", MODE="0666", GROUP="plugdev"
# Generic catch-all for Android Debug Bridge
SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", MODE="0666", GROUP="plugdev"
EOF

udevadm control --reload-rules
udevadm trigger

# Ensure current user and gitlab-runner are in plugdev group
for user in "$(logname 2>/dev/null || echo $SUDO_USER)" "gitlab-runner"; do
    if id "$user" &>/dev/null; then
        usermod -aG plugdev "$user" 2>/dev/null || true
        log_info "Added user '${user}' to plugdev group"
    fi
done

# ─────────────────────────────────────────────────────────────
# Step 4: Python environment for xTS Agent
# ─────────────────────────────────────────────────────────────
log_step "Step 4: Setting up Python environment"

cd "${XTS_AGENT_DIR}"

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install xTS Agent
pip install --upgrade pip
if [ -f "pyproject.toml" ]; then
    pip install -e "."
elif [ -f "requirements.txt" ]; then
    pip install -r requirements.txt
fi

log_info "Python environment ready at ${XTS_AGENT_DIR}/.venv"

# ─────────────────────────────────────────────────────────────
# Step 5: Create directory structure
# ─────────────────────────────────────────────────────────────
log_step "Step 5: Creating directory structure"

mkdir -p "${XTS_PACKAGES_DIR}"
mkdir -p "${XTS_AGENT_DIR}/results"
mkdir -p "${XTS_AGENT_DIR}/results/junit"
mkdir -p "${XTS_AGENT_DIR}/results/reports"
mkdir -p "${XTS_AGENT_DIR}/results/rca"
mkdir -p "${XTS_AGENT_DIR}/results/retry"
mkdir -p "${XTS_AGENT_DIR}/logs"
mkdir -p "${XTS_AGENT_DIR}/logs/setup"
mkdir -p "${XTS_AGENT_DIR}/logs/health"

log_info "Directory structure created"

# ─────────────────────────────────────────────────────────────
# Step 6: Verify setup
# ─────────────────────────────────────────────────────────────
log_step "Step 6: Verification"

echo "─────────────────────────────────────────────"
echo "  Java:     $(java -version 2>&1 | head -n1)"
echo "  Python:   $(python3 --version)"
echo "  ADB:      $(adb version | head -n1)"
echo "  xTS Dir:  ${XTS_PACKAGES_DIR}"
echo "  Agent:    ${XTS_AGENT_DIR}"
echo "─────────────────────────────────────────────"
echo ""

# Check for connected devices
DEVICE_COUNT=$(adb devices 2>/dev/null | grep -c 'device$' || echo "0")
if [ "${DEVICE_COUNT}" -gt "0" ]; then
    log_info "Found ${DEVICE_COUNT} connected Android device(s)"
    adb devices -l
else
    log_warn "No Android devices detected. Connect devices before running tests."
fi

echo ""
log_info "Environment setup complete!"
log_info ""
log_info "Next steps:"
log_info "  1. Download xTS packages:  ./scripts/download_xts_packages.sh"
log_info "  2. Connect Android devices (USB or adb connect)"
log_info "  3. Run smoke test:         python3 -m xts_agent.cli run --plan config/test_plans/smoke_test.yaml"
log_info "  4. Run full certification: python3 -m xts_agent.cli run --plan config/test_plans/full_certification.yaml"
