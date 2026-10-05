#!/usr/bin/env bash
# Compatibility wrapper — prefer start_web_monitor.sh
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${DIR}/start_web_monitor.sh"
