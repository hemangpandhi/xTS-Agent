#!/usr/bin/env bash
# Incremental AOSP/OEM build used by `xts-agent fix-loop` code fixes.
#   aosp_build.sh [module ...]
# Builds the touched modules first (fast compile errors), then the images the
# deploy step flashes. Env: AOSP_ROOT, LUNCH_TARGET.
set -eo pipefail
AOSP_ROOT="${AOSP_ROOT:-/mnt/aosp-17-master}"
LUNCH_TARGET="${LUNCH_TARGET:-aosp_cf_x86_64_auto-trunk_staging-user}"
cd "${AOSP_ROOT}"
export USE_CCACHE=1 CCACHE_DIR="${CCACHE_DIR:-/mnt/ccache}" CCACHE_EXEC=/usr/bin/ccache
set +u
source build/envsetup.sh >/dev/null
lunch "${LUNCH_TARGET}" >/dev/null
if [[ $# -gt 0 ]]; then
  echo "=== m $*"
  m "$@"
fi
echo "=== m (images)"
m
echo "=== build OK: $(date -Is)"
