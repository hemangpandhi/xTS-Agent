#!/usr/bin/env bash
# ============================================================
# xTS Agent — Office / CI host pre-flight checks
# Safe production replacement for the old "assassin" deployer.
#
# Usage:
#   ./office_deploy.sh
#   ANDROID_HOME=/opt/android-sdk XTS_PACKAGES_DIR=/opt/xts ./office_deploy.sh
# ============================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
XTS_DIR="${XTS_PACKAGES_DIR:-/opt/xts}"
ANDROID_HOME="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}"
MIN_DEVICES="${MIN_DEVICES:-1}"
REPAIR_AAPT="${REPAIR_AAPT:-1}"

log() { echo "[+] $*"; }
warn() { echo "[!] $*" >&2; }
die() { echo "[-] FATAL: $*" >&2; exit 1; }

log "xTS Agent production pre-flight (repo=${ROOT_DIR})"

# 1) Python / package
command -v python3 >/dev/null || die "python3 not found"
if [[ -d "${ROOT_DIR}/.venv" ]]; then
  # shellcheck disable=SC1091
  source "${ROOT_DIR}/.venv/bin/activate"
fi
python3 -c "import xts_agent" 2>/dev/null || {
  warn "xts_agent not importable; installing editable package"
  pip install -e "${ROOT_DIR}"
}

# 2) Java / ADB
command -v java >/dev/null || die "java not found (need JDK 17+)"
command -v adb >/dev/null || die "adb not found (install platform-tools)"

# 3) Android SDK / aapt2
resolve_aapt2() {
  local sdk="${1:-}"
  if [[ -n "${sdk}" && -d "${sdk}/build-tools" ]]; then
    find "${sdk}/build-tools" -type f -name aapt2 2>/dev/null | sort -V | tail -n 1
  fi
}

if [[ -z "${ANDROID_HOME}" ]]; then
  for candidate in "${HOME}/Android/Sdk" /opt/android-sdk /usr/lib/android-sdk; do
    if [[ -d "${candidate}" ]]; then
      ANDROID_HOME="${candidate}"
      break
    fi
  done
fi
export ANDROID_HOME="${ANDROID_HOME:-}"
export ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-${ANDROID_HOME}}"

AAPT2="$(resolve_aapt2 "${ANDROID_HOME}")"
if [[ -z "${AAPT2}" ]]; then
  die "No aapt2 found. Set ANDROID_HOME to an SDK with build-tools installed."
fi
log "AAPT2: ${AAPT2}"

# 4) Optional safe TradeFed aapt repair (backup first)
if [[ "${REPAIR_AAPT}" == "1" ]]; then
  TF_SCRIPT="${XTS_DIR}/android-cts/tools/cts-tradefed"
  if [[ -f "${TF_SCRIPT}" ]]; then
    python3 - <<PY
from pathlib import Path
from xts_agent.utils.env_validator import EnvironmentValidator
ok = EnvironmentValidator.validate_aapt2(Path("${TF_SCRIPT}"), repair=True)
raise SystemExit(0 if ok else 1)
PY
    log "Validated/repaired CTS TradeFed aapt2 mapping"
  else
    warn "CTS TradeFed not found at ${TF_SCRIPT} (skip aapt repair)"
  fi
fi

# 5) xTS packages presence
MISSING=0
for suite in android-cts; do
  if [[ ! -d "${XTS_DIR}/${suite}" ]]; then
    warn "Missing required package: ${XTS_DIR}/${suite}"
    MISSING=1
  else
    log "Found ${suite}"
  fi
done
[[ "${MISSING}" -eq 0 ]] || die "Required xTS packages missing under ${XTS_DIR}. Run scripts/download_xts_packages.sh"

# 6) Devices
DEVICE_COUNT="$(adb devices | awk 'NR>1 && $2=="device" {c++} END{print c+0}')"
log "Online ADB devices: ${DEVICE_COUNT}"
if [[ "${DEVICE_COUNT}" -lt "${MIN_DEVICES}" ]]; then
  die "Need at least ${MIN_DEVICES} device(s); found ${DEVICE_COUNT}"
fi

# 7) Agent setup command
python3 -m xts_agent.cli setup --config "${ROOT_DIR}/config/default_config.yaml"

log "Pre-flight PASSED. Example run:"
echo "  python3 -m xts_agent.cli run --plan config/test_plans/smoke_test.yaml --config config/default_config.yaml --dry-run"
