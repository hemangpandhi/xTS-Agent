#!/bin/bash
AOSP_ROOT="/mnt/aosp-17-master"
CVD_HOME="/tmp/cvd_cluster"

mkdir -p $CVD_HOME
export HOME=$CVD_HOME

echo "Sourcing AOSP..."
cd $AOSP_ROOT
source build/envsetup.sh
lunch aosp_cf_x86_64_auto-trunk_staging-userdebug

echo "Launching 9 more instances starting from port 6524..."
launch_cvd --base_instance_num=2 --num_instances=9 --daemon -report_anonymous_usage_stats=y
