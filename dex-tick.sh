#!/bin/bash
set -euo pipefail
cd /root/.hermes/proactive
DISABLED_FILE="/root/.hermes/proactive/DISABLED"
if [ -f "$DISABLED_FILE" ]; then
  echo "Dex спит"
  exit 0
fi
exec python3 heartbeat.py 2>&1
