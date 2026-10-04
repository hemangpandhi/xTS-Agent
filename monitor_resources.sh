#!/bin/bash
LOGFILE="/mnt/xTS_Agent/results/logs/resource_usage.log"
echo "Timestamp, CPU_Load_1m, RAM_Used_GB, RAM_Free_GB" > "$LOGFILE"

while true; do
    TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")
    
    # Get CPU Load Average (1 minute)
    LOAD=$(cat /proc/loadavg | awk '{print $1}')
    
    # Get RAM Usage in GB
    RAM_USED=$(free -g | awk '/^Mem:/ {print $3}')
    RAM_FREE=$(free -g | awk '/^Mem:/ {print $4}')
    
    echo "$TIMESTAMP, $LOAD, ${RAM_USED}GB, ${RAM_FREE}GB" >> "$LOGFILE"
    
    sleep 60
done
