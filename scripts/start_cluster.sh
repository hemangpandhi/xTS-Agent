#!/bin/bash
# ==============================================================================
# xTS Agent - Automated Cuttlefish Cluster Spawner
# Purpose: Spawns multiple Cuttlefish (CVD) emulators natively for TradeFed Sharding
# Usage: ./start_cluster.sh [NUM_INSTANCES]
# ==============================================================================

set -e

NUM_INSTANCES=${1:-10}
AOSP_ROOT=${AOSP_ROOT:-"/mnt/aosp-17-master"}
LUNCH_TARGET=${LUNCH_TARGET:-"aosp_cf_x86_64_phone-userdebug"}
CVD_HOME=${CVD_HOME:-"/tmp/cvd_cluster_home"}

echo "======================================================"
echo "🚀 Spawning $NUM_INSTANCES Cuttlefish Instances"
echo "======================================================"

# 1. Setup Environment
echo "[1/3] Sourcing AOSP environment..."
cd $AOSP_ROOT
source build/envsetup.sh
lunch $LUNCH_TARGET

# 2. Prepare Isolated Home Directory
# Cuttlefish requires a dedicated HOME directory to store instance logs/configs
echo "[2/3] Preparing workspace at $CVD_HOME..."
mkdir -p $CVD_HOME
export HOME=$CVD_HOME

# 3. Launch Cluster
echo "[3/3] Launching instances in the background..."
# Using native AOSP multi-instance flag
launch_cvd --num_instances=$NUM_INSTANCES --daemon -report_anonymous_usage_stats=y

echo "======================================================"
echo "✅ Cluster successfully triggered!"
echo "Use 'adb devices' to verify all instances are online."
echo "Ports will be automatically assigned (6520, 6524, 6528...)"
echo "To stop the cluster, run: stop_cvd"
echo "======================================================"
