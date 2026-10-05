#!/usr/bin/env bash
# Production readiness gate for xTS Agent hosts.
# Exit 0 = ready for smoke/cert deploy (given required packages/devices).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

XTS_DIR="${XTS_PACKAGES_DIR:-/opt/xts}"
REQUIRED_SUITES="${REQUIRED_SUITES:-cts}"
MIN_DEVICES="${MIN_DEVICES:-1}"
STRICT_DEVICES="${STRICT_DEVICES:-1}"

PASS=0
FAIL=0
WARN=0

ok() { echo "[PASS] $*"; PASS=$((PASS + 1)); }
bad() { echo "[FAIL] $*"; FAIL=$((FAIL + 1)); }
warn() { echo "[WARN] $*"; WARN=$((WARN + 1)); }

echo "=== xTS Agent production readiness ==="
echo "repo=${ROOT_DIR}"

command -v python3 >/dev/null && ok "python3 present" || bad "python3 missing"
if command -v adb >/dev/null; then
  ok "adb present"
else
  if [[ "${STRICT_DEVICES}" == "1" ]]; then
    bad "adb missing"
  else
    warn "adb missing (STRICT_DEVICES=0)"
  fi
fi
command -v java >/dev/null && ok "java present" || bad "java missing"

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

if python3 -c "import xts_agent" 2>/dev/null; then
  ok "xts_agent importable"
else
  bad "xts_agent not importable (pip install -e .)"
fi

# Config load
if python3 - <<'PY'
from xts_agent.config_loader import ConfigLoader
p = ConfigLoader('config/test_plans/full_certification.yaml', 'config/default_config.yaml').load_plan()
assert p.name == 'full_aaos_certification'
assert sorted(p.suites, key=lambda s: s.priority)[0].name == 'cts'
print('plan_ok', p.name, len(p.suites))
PY
then
  ok "certification plan loads"
else
  bad "certification plan failed to load"
fi

# CLI surface
for cmd in run retry analyze report setup cleanup; do
  if python3 -m xts_agent.cli "${cmd}" --help >/dev/null 2>&1; then
    ok "CLI command: ${cmd}"
  else
    bad "CLI command missing/broken: ${cmd}"
  fi
done

# Packages
if [[ -x scripts/download_xts_packages.sh ]]; then
  pkg_log="/tmp/xts_pkg_check.log"
  if REQUIRED_SUITES="${REQUIRED_SUITES}" XTS_PACKAGES_DIR="${XTS_DIR}" \
      ALLOW_MISSING="${ALLOW_MISSING:-0}" \
      scripts/download_xts_packages.sh >"${pkg_log}" 2>&1; then
    if grep -q "ALLOW_MISSING=1" "${pkg_log}"; then
      warn "package check soft-passed via ALLOW_MISSING=1 (see ${pkg_log})"
    else
      ok "required packages present (${REQUIRED_SUITES})"
    fi
  else
    bad "required packages missing under ${XTS_DIR} (see ${pkg_log})"
  fi
fi

# Devices
if command -v adb >/dev/null; then
  count="$(adb devices | awk 'NR>1 && $2=="device" {c++} END{print c+0}')"
  if [[ "${count}" -ge "${MIN_DEVICES}" ]]; then
    ok "ADB devices online: ${count}"
  else
    if [[ "${STRICT_DEVICES}" == "1" ]]; then
      bad "Need >= ${MIN_DEVICES} devices; found ${count}"
    else
      warn "Need >= ${MIN_DEVICES} devices; found ${count} (STRICT_DEVICES=0)"
    fi
  fi
fi

# Dangerous legacy patches must not sit at repo root
shopt -s nullglob
root_patches=(patch_*.py)
if [[ ${#root_patches[@]} -gt 0 ]]; then
  bad "legacy patch_*.py still in repo root (move to scripts/legacy/)"
else
  ok "no root patch_*.py mutators"
fi
shopt -u nullglob

# Dry-run when packages may be absent still validates wiring
if python3 -m xts_agent.cli run \
  --plan config/test_plans/smoke_test.yaml \
  --config config/default_config.yaml \
  --dry-run >/tmp/xts_dry_run.log 2>&1; then
  ok "smoke dry-run succeeds"
else
  bad "smoke dry-run failed (see /tmp/xts_dry_run.log)"
fi

echo
echo "Summary: pass=${PASS} warn=${WARN} fail=${FAIL}"
if [[ "${FAIL}" -gt 0 ]]; then
  echo "RESULT: NOT READY"
  exit 1
fi
echo "RESULT: READY"
exit 0
