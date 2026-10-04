#!/bin/bash
echo "Cleaning up old instances..."
pkill -f run_cvd
pkill -f crosvm
pkill -f launch_cvd

echo "Starting fleet..."
cd /home/hemang/android_internals/orchestrator
node spawn_fleet.js > /mnt/xTS_Agent/spawn.log 2>&1

echo "Waiting 60 seconds for hypervisors to stabilize..."
sleep 60

echo "Waiting for devices to come online in ADB..."
cd /mnt/xTS_Agent
while true; do
  COUNT=$(adb devices | grep -w "device" | wc -l)
  echo "Current online devices: $COUNT"
  if [ "$COUNT" -ge 10 ]; then
    echo "Fleet has hit critical mass ($COUNT devices online)! Triggering Full CTS Run..."
    python3 -m xts_agent.cli run --plan config/test_plans/full_cts.yaml > execution.log 2>&1
    break
  fi
  sleep 15
done
