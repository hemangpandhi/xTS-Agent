#!/bin/bash
echo "Waiting for devices..."
while true; do
  COUNT=$(adb devices | grep -w "device" | wc -l)
  if [ "$COUNT" -ge 4 ]; then
    echo "Found $COUNT devices! Starting xTS Agent with smoke_test.yaml..."
    python3 -m xts_agent.cli run --plan config/test_plans/smoke_test.yaml > execution.log 2>&1
    break
  fi
  sleep 10
done
