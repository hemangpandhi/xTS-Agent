#!/bin/bash
LATEST_LOG=$(ls -t /mnt/xTS_Agent/results/logs/tradefed_run_*.log | head -n 1)
COMPLETED=$(grep -c "Sharded test completed:" "$LATEST_LOG")
FAILED=$(grep -c "FAILED:" "$LATEST_LOG")
TOTAL_MODULES="VCTS_Optimized"

# Extract start timestamp from filename (e.g. tradefed_run_1791037911.log)
START_EPOCH=$(basename "$LATEST_LOG" | grep -o -E '[0-9]+')
CURRENT_EPOCH=$(date +%s)
ELAPSED_SEC=$((CURRENT_EPOCH - START_EPOCH))

# Format elapsed time
ELAPSED_H=$((ELAPSED_SEC / 3600))
ELAPSED_M=$(((ELAPSED_SEC % 3600) / 60))

# Estimate remaining time
if [ "$COMPLETED" -gt 0 ]; then
    SEC_PER_MODULE=$((ELAPSED_SEC / COMPLETED))
    REMAINING_MODULES=$((TOTAL_MODULES - COMPLETED))
    REMAINING_SEC=$((REMAINING_MODULES * SEC_PER_MODULE))
    REMAINING_H=$((REMAINING_SEC / 3600))
    REMAINING_M=$(((REMAINING_SEC % 3600) / 60))
else
    REMAINING_H="?"
    REMAINING_M="?"
fi

echo "==========================================="
echo " xTS Nightly Run - Live Performance Matrix"
echo "==========================================="
echo "Log File: $LATEST_LOG"
echo "Completed Modules: $COMPLETED / ~$TOTAL_MODULES"
echo "Module Failures/Crashes: $FAILED"
echo "-------------------------------------------"
echo "Elapsed Time: ${ELAPSED_H}h ${ELAPSED_M}m"
echo "Estimated Remaining: ~${REMAINING_H}h ${REMAINING_M}m"
echo "Average Speed: ~$SEC_PER_MODULE seconds / module"
echo "==========================================="
