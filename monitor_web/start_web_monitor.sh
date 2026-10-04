#!/bin/bash
cd /mnt/xTS_Agent/monitor_web

# Start a python web server in the background
python3 -m http.server 8585 > /dev/null 2>&1 &
SERVER_PID=$!
echo $SERVER_PID > server.pid

# Loop to continually update index.html
while true; do
    echo '<!DOCTYPE html><html><head><title>xTS Agent Resource Monitor</title>' > index.html
    echo '<meta http-equiv="refresh" content="3">' >> index.html
    echo '<style>body{background-color:#1e1e1e;color:#00ff00;font-family:monospace;padding:20px;} h2{color:#ffffff;}</style>' >> index.html
    echo '</head><body>' >> index.html
    echo '<h2>Live System Resources (Auto-refreshes every 3s)</h2>' >> index.html
    echo "<h3>Last Updated: $(date "+%Y-%m-%d %H:%M:%S")</h3>" >> index.html
    
    echo '<hr><h3>Memory Usage</h3><pre>' >> index.html
    free -h >> index.html
    echo '</pre>' >> index.html
    
    echo '<hr><h3>Disk Usage (/mnt)</h3><pre>' >> index.html
    df -h /mnt >> index.html
    echo '</pre>' >> index.html
    
    echo '<hr><h3>Top Processes (CPU/RAM)</h3><pre>' >> index.html
    top -b -n 1 | head -n 30 >> index.html
    echo '</pre>' >> index.html
    
    echo '</body></html>' >> index.html
    
    sleep 3
done
