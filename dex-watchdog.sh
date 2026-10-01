#!/bin/bash
# Сторож Dex: если успешного тика нет дольше 30 мин (и нет намеренной DISABLED-паузы) — алерт в ntfy.
set -u
BASE=/root/.hermes/proactive
LOG="$BASE/tick_history.jsonl"
NTFY="http://localhost:2586/rem2222-hermes"

# Намеренная пауза через DISABLED-флаг — молчим (Dex спит по команде)
[ -f "$BASE/DISABLED" ] && exit 0
[ -f "$LOG" ] || exit 0

MT=$(stat -c %Y "$LOG")
AGE=$(($(date +%s) - MT))
if [ "$AGE" -gt 1800 ]; then
  MINS=$((AGE / 60))
  LAST=$(date -d @"$MT" '+%H:%M' 2>/dev/null || echo "?")
  curl -s \
    -d "Нет тиков Dex уже $MINS мин (последний — $LAST). Heartbeat упал или таймер встал, DISABLED-флага нет." \
    -H "Title: ⚠️ Dex не тикает" \
    -H "Priority: high" \
    -H "Tags: warning" \
    "$NTFY" >/dev/null 2>&1
fi
exit 0