#!/usr/bin/env bash
# ============================================================
# xTS Agent — GitLab Runner Setup Script
# ============================================================
# Installs and configures a GitLab Runner with Shell executor
# for Android xTS test execution with direct hardware access.
#
# Usage:
#   export GITLAB_URL="https://gitlab.example.com/"
#   export REGISTRATION_TOKEN="your-registration-token"
#   chmod +x scripts/setup_gitlab_runner.sh
#   sudo ./scripts/setup_gitlab_runner.sh
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

GITLAB_URL="${GITLAB_URL:-}"
REGISTRATION_TOKEN="${REGISTRATION_TOKEN:-}"
RUNNER_DESCRIPTION="${RUNNER_DESCRIPTION:-AAOS xTS Test Host}"
RUNNER_TAGS="${RUNNER_TAGS:-android-test-host,xts,aaos}"

# ─────────────────────────────────────────────────────────────
# Step 1: Install GitLab Runner
# ─────────────────────────────────────────────────────────────
log_step "Step 1: Installing GitLab Runner"

if command -v gitlab-runner &>/dev/null; then
    log_info "GitLab Runner already installed: $(gitlab-runner --version | head -n1)"
else
    log_info "Installing GitLab Runner..."
    curl -L "https://packages.gitlab.com/install/repositories/runner/gitlab-runner/script.deb.sh" | bash
    apt-get install -y gitlab-runner
    log_info "GitLab Runner installed: $(gitlab-runner --version | head -n1)"
fi

# ─────────────────────────────────────────────────────────────
# Step 2: Register Runner
# ─────────────────────────────────────────────────────────────
log_step "Step 2: Registering GitLab Runner"

if [ -z "${GITLAB_URL}" ] || [ -z "${REGISTRATION_TOKEN}" ]; then
    log_warn "GITLAB_URL and REGISTRATION_TOKEN not set."
    log_warn "To register manually, run:"
    log_warn "  sudo gitlab-runner register \\"
    log_warn "    --url 'https://gitlab.example.com/' \\"
    log_warn "    --registration-token 'YOUR_TOKEN' \\"
    log_warn "    --executor 'shell' \\"
    log_warn "    --tag-list '${RUNNER_TAGS}' \\"
    log_warn "    --description '${RUNNER_DESCRIPTION}'"
else
    gitlab-runner register \
        --non-interactive \
        --url "${GITLAB_URL}" \
        --registration-token "${REGISTRATION_TOKEN}" \
        --executor "shell" \
        --tag-list "${RUNNER_TAGS}" \
        --description "${RUNNER_DESCRIPTION}" \
        --run-untagged="false" \
        --locked="true"

    log_info "Runner registered successfully with tags: ${RUNNER_TAGS}"
fi

# ─────────────────────────────────────────────────────────────
# Step 3: Configure Runner for xTS
# ─────────────────────────────────────────────────────────────
log_step "Step 3: Configuring Runner for xTS workloads"

RUNNER_CONFIG="/etc/gitlab-runner/config.toml"

if [ -f "${RUNNER_CONFIG}" ]; then
    # Set concurrent to 1 to prevent overlapping xTS jobs
    # xTS jobs require exclusive device access
    sed -i 's/^concurrent = .*/concurrent = 1/' "${RUNNER_CONFIG}" 2>/dev/null || true

    # Set check_interval for reasonable polling
    if ! grep -q "check_interval" "${RUNNER_CONFIG}"; then
        sed -i '/^concurrent/a check_interval = 30' "${RUNNER_CONFIG}" 2>/dev/null || true
    fi

    log_info "Runner configured with concurrent=1 (exclusive device access)"
fi

# ─────────────────────────────────────────────────────────────
# Step 4: Grant gitlab-runner user permissions
# ─────────────────────────────────────────────────────────────
log_step "Step 4: Setting permissions"

# Add gitlab-runner to plugdev for USB device access
usermod -aG plugdev gitlab-runner 2>/dev/null || true

# Ensure gitlab-runner can access ADB
PLATFORM_TOOLS_DIR="/opt/xts/platform-tools"
if [ -d "${PLATFORM_TOOLS_DIR}" ]; then
    chmod -R 755 "${PLATFORM_TOOLS_DIR}"
fi

# Set up adb for gitlab-runner user
su - gitlab-runner -c "mkdir -p ~/.android" 2>/dev/null || true

# Add environment to gitlab-runner profile
cat >> /home/gitlab-runner/.bashrc 2>/dev/null << 'EOF' || true
export JAVA_HOME="/usr/lib/jvm/java-17-openjdk-amd64"
export PATH="/opt/xts/platform-tools:${PATH}"
export ANDROID_HOME="/opt/xts"
EOF

log_info "gitlab-runner user configured with USB and ADB access"

# ─────────────────────────────────────────────────────────────
# Step 5: Start/Restart Runner
# ─────────────────────────────────────────────────────────────
log_step "Step 5: Starting GitLab Runner service"

gitlab-runner restart
gitlab-runner status

# ─────────────────────────────────────────────────────────────
# Step 6: Verify
# ─────────────────────────────────────────────────────────────
log_step "Step 6: Verification"

echo "─────────────────────────────────────────────"
echo "  Runner: $(gitlab-runner --version | head -n1)"
echo "  Status: $(gitlab-runner status 2>&1)"
echo "  Tags:   ${RUNNER_TAGS}"
echo "  Config: ${RUNNER_CONFIG}"
echo "─────────────────────────────────────────────"
echo ""
log_info "GitLab Runner setup complete!"
log_info ""
log_info "The runner is configured with:"
log_info "  - Shell executor (direct hardware access)"
log_info "  - concurrent=1 (exclusive device access per job)"
log_info "  - Tags: ${RUNNER_TAGS}"
log_info ""
log_info "Ensure your .gitlab-ci.yml jobs use tag: android-test-host"
