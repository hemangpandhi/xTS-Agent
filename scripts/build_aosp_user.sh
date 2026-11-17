#!/usr/bin/env bash
set -e

cd /mnt/aosp-17-master

echo "=== [1/3] Removing logpersist.start from Cuttlefish device.mk on user variant ==="
python3 - <<'PY'
from pathlib import Path
p = Path("/mnt/aosp-17-master/device/google/cuttlefish/shared/device.mk")
txt = p.read_text()
old_block = """PRODUCT_PACKAGES += \\
    logpersist.start
"""
if old_block in txt:
    p.write_text(txt.replace(old_block, ""))
    print("Removed logpersist.start from device.mk user block")
else:
    print("device.mk already clean")
PY

echo "=== [2/3] Configuring lunch aosp_cf_x86_64_auto-trunk_staging-user ==="
export USE_CCACHE=1
export CCACHE_DIR=/mnt/ccache
export CCACHE_EXEC=/usr/bin/ccache
export SOONG_INCREMENTAL_ANALYSIS=false
source build/envsetup.sh
lunch aosp_cf_x86_64_auto-trunk_staging-user

echo "=== [3/3] Starting AOSP 17 user build (m -j60) ==="
m -j60
echo "=== BUILD COMPLETE ==="
