#!/bin/bash
AOSP_ROOT="/mnt/users/hemang.pandhi_1/out_merged_auto"

for i in {2..10}; do
  echo "Spawning instance $i..."
  
  export HOME=/tmp/cvd_$i
  mkdir -p $HOME
  
  export ANDROID_PRODUCT_OUT=$AOSP_ROOT/target/product/vsoc_x86_64_auto
  export ANDROID_HOST_OUT=$AOSP_ROOT/host/linux-x86
  export PATH=$ANDROID_HOST_OUT/bin:$PATH
  
  # Launch the instance in background
  launch_cvd --base_instance_num=$i --num_instances=1 --daemon -report_anonymous_usage_stats=y
done
