#!/bin/bash
# Сторож Dex: если успешного тика нет дольше 30 мин (и нет намеренной DISABLED-паузы) — алерт в ntfy.
#
# Тест-кейсы: dex-watchdog.sh --selftest  (журнал: «сценарий N: ok»)
# Состояния: DISABLED-флаг (молчим) · нет файла тиков (молчим) · свежий тик (молчим) ·
#            тик старше порога (алерт) · тик старше 30 мин, но младше своего порога (молчим).
set -u
BASE=${DEX_BASE:-/root/.hermes/proactive}
LOG="$BASE/tick_history.jsonl"
NTFY=${DEX_NTFY:-http://localhost:2586/rem2222-hermes}
STALE=${DEX_STALE_SECONDS:-1800}
CURL=${DEX_CURL:-curl}

send_alert() {  # $1 — текст
  "$CURL" -s \
    -d "$1" \
    -H "Title: ⚠️ Dex не тикает" \
    -H "Priority: high" \
    -H "Tags: warning" \
    "$NTFY" >/dev/null 2>&1
  return 0
}

check() {
  # Намеренная пауза через DISABLED-флаг — молчим (Dex спит по команде)
  [ -f "$BASE/DISABLED" ] && return 0
  [ -f "$LOG" ] || return 0

  local mt age mins last
  mt=$(stat -c %Y "$LOG")
  age=$(($(date +%s) - mt))
  if [ "$age" -gt "$STALE" ]; then
    mins=$((age / 60))
    last=$(date -d @"$mt" '+%H:%M' 2>/dev/null || echo "?")
    send_alert "Нет тиков Dex уже $mins мин (последний — $last). Heartbeat упал или таймер встал, DISABLED-флага нет."
  fi
  return 0
}

selftest() {
  local tmp stub out rc n=0 failed=0 got name expect age mode
  tmp=$(mktemp -d)
  stub="$tmp/curl-stub.sh"
  out="$tmp/alerts.log"
  cat > "$stub" <<'STUB'
#!/bin/bash
# заглушка curl: складывает аргументы (включая текст -d) в файл
printf '%s\n' "$*" >> "$STUB_OUT"
STUB
  chmod +x "$stub"
  export STUB_OUT="$out"

  # сценарий: имя | возраст тика (мин, "-" — файла нет) | DISABLED | порог (с) | ожидание
  while IFS='|' read -r name age mode stale expect; do
    [ -z "$name" ] && continue
    age="${age// /}"
    mode="${mode// /}"
    n=$((n + 1))
    rm -f "$out" "$tmp/tick_history.jsonl" "$tmp/DISABLED"
    case "$age" in
      -) : ;;
      *) : > "$tmp/tick_history.jsonl"
         touch -d "$age minutes ago" "$tmp/tick_history.jsonl" ;;
    esac
    [ "$mode" = "disabled" ] && : > "$tmp/DISABLED"
    got=$(DEX_BASE="$tmp" DEX_NTFY="http://stub.local/x" DEX_CURL="$stub" \
          DEX_STALE_SECONDS="$stale" bash "$0" 2>&1; echo "rc=$?")
    if [ -f "$out" ]; then got="alert: $(tr '\n' ' ' < "$out") $got"; else got="silent $got"; fi
    if [[ "$got" == *"$expect"* ]]; then
      echo "сценарий $n: ok — $name"
    else
      echo "сценарий $n: FAIL — $name (получено «$got», ожидалась подстрока «$expect»)"
      failed=$((failed + 1))
    fi
  done <<'CASES'
DISABLED-флаг — Dex спит по команде|40|disabled|1800|silent rc=0
файла тиков ещё нет| -|-|1800|silent rc=0
свежий тик (2 мин)|2|-|1800|silent rc=0
тик 29 мин — в пределах порога|29|-|1800|silent rc=0
тик 40 мин — старше порога → алерт|40|-|1800|alert: -s -d Нет тиков Dex уже 40 мин
тик 40 мин, но порог поднят до 2 ч → молчим|40|-|7200|silent rc=0
тик 3 ч → алерт с числом минут|180|-|1800|alert: -s -d Нет тиков Dex уже 180 мин
CASES
  rm -rf "$tmp"
  echo
  if [ "$failed" -eq 0 ]; then
    echo "selftest dex-watchdog: $n/$n сценариев ok"
    return 0
  fi
  echo "selftest dex-watchdog: $((n - failed))/$n сценариев ok"
  return 1
}

if [ "${1:-}" = "--selftest" ]; then
  selftest
  exit $?
fi

check
exit 0
