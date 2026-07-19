#!/usr/bin/env python3
"""
Dex Heartbeat — проактивный тик агента
Запускается по cron каждые 10 минут.
Проверяет состояние, решает что делать, исполняет.
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
import yaml
from datetime import datetime, timezone
from pathlib import Path

# === CONFIG ===
BASE_DIR = Path.home() / ".hermes" / "proactive"
DB_PATH = BASE_DIR / "agent.db"
IDENTITY_PATH = BASE_DIR / "identity.yaml"
DISABLED_FLAG = BASE_DIR / "DISABLED"
TICK_LOG = BASE_DIR / "tick_history.jsonl"
ENV_PATH = BASE_DIR / ".env"

# === TELEGRAM (Dex Bot) ===
DEX_BOT_TOKEN = None
DEX_CHAT_ID = 386235337  # Rem — куда слать уведомления

def load_dex_token():
    """Загружает токен Dex бота из .env"""
    global DEX_BOT_TOKEN
    if not ENV_PATH.exists():
        log("WARN: .env не найден, Telegram-уведомления недоступны")
        return
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line.startswith("DEX_BOT_TOKEN="):
            DEX_BOT_TOKEN = line.split("=", 1)[1]
            break
    if not DEX_BOT_TOKEN:
        log("WARN: DEX_BOT_TOKEN не найден в .env")

def send_telegram(text):
    """Отправляет сообщение в Telegram через Dex бота (Bot API)"""
    if not DEX_BOT_TOKEN:
        log("Telegram: нет токена, пропускаю")
        return False
    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST",
             f"https://api.telegram.org/bot{DEX_BOT_TOKEN}/sendMessage",
             "-H", "Content-Type: application/json",
             "-d", json.dumps({
                 "chat_id": DEX_CHAT_ID,
                 "text": text,
                 "parse_mode": "HTML",
                 "disable_notification": False
             })],
            capture_output=True, text=True, timeout=15
        )
        resp = json.loads(result.stdout)
        if resp.get("ok"):
            log("Telegram: сообщение отправлено")
            return True
        else:
            log(f"Telegram: ошибка API — {resp.get('description', 'неизвестно')}")
            return False
    except Exception as e:
        log(f"Telegram: ошибка отправки — {e}")
        return False

# === HELPERS ===
def log(msg):
    ts = datetime.now(timezone.utc).isoformat()
    print(f"[{ts}] {msg}", flush=True)

def init_db():
    """Инициализация БД (потом переедет на TencentDB)"""
    db = sqlite3.connect(str(DB_PATH))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""
        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            what TEXT,
            status TEXT DEFAULT 'pending',
            source TEXT DEFAULT 'heartbeat',
            result TEXT,
            created_at TEXT,
            done_at TEXT
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS llm_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tick INTEGER,
            ts TEXT,
            system TEXT,
            prompt TEXT,
            response TEXT,
            latency_ms INTEGER,
            token_count INTEGER DEFAULT 0
        )
    """)
    db.commit()
    return db

def get_state(db, key, default=None):
    row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    if row:
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return row[0]
    return default

def set_state(db, key, value):
    db.execute(
        "INSERT OR REPLACE INTO state (key, value, updated_at) VALUES (?, ?, ?)",
        (key, json.dumps(value, ensure_ascii=False), datetime.now(timezone.utc).isoformat())
    )
    db.commit()

def load_identity():
    with open(IDENTITY_PATH) as f:
        return yaml.safe_load(f)

def read_tick_history(limit=5):
    """Читает последние N тиков из лога"""
    if not TICK_LOG.exists():
        return []
    with open(TICK_LOG) as f:
        lines = f.readlines()
    return [json.loads(l) for l in lines[-limit:]]

def write_tick(tick_data):
    with open(TICK_LOG, "a") as f:
        f.write(json.dumps(tick_data, ensure_ascii=False) + "\n")

GATEWAY_KEY = "123c867ed8cc504a5e602b4189cc201964a4e7331a20d7aeb883b88fdf86ed0a"

def call_llm(system, prompt, max_tokens=20, db=None, tick=None, temperature=0.3):
    """Вызов LLM через Hermes Gateway. Если передан db — логирует запрос."""
    t0 = time.time()
    result = subprocess.run(
        ["curl", "-s", "-X", "POST",
         "http://127.0.0.1:8642/v1/chat/completions",
         "-H", "Content-Type: application/json",
         "-H", f"Authorization: Bearer {GATEWAY_KEY}",
         "-d", json.dumps({
             "model": "deepseek-v4-flash",
             "messages": [
                 {"role": "system", "content": system},
                 {"role": "user", "content": prompt}
             ],
             "max_tokens": max_tokens,
             "temperature": temperature
         })],
        capture_output=True, text=True, timeout=30
    )
    latency = int((time.time() - t0) * 1000)
    response_text = None
    tokens = 0
    try:
        resp = json.loads(result.stdout)
        response_text = resp["choices"][0]["message"]["content"]
        tokens = resp.get("usage", {}).get("total_tokens", 0)
    except (KeyError, json.JSONDecodeError) as e:
        log(f"LLM call failed: {e}")
        log(f"Raw: {result.stdout[:200]}")
        response_text = None

    # Логируем в БД
    if db and tick:
        db.execute(
            "INSERT INTO llm_log (tick, ts, system, prompt, response, latency_ms, token_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tick, datetime.now(timezone.utc).isoformat(), system, prompt, response_text or "", latency, tokens)
        )
        db.commit()

    return response_text

def check_duty(db, identity):
    """Проверяет duties — что из обязанностей пора сделать"""
    duty_checks = get_state(db, "duty_checks", {})
    now = datetime.now(timezone.utc).timestamp()
    results = []
    for duty_item in identity.get("duties", []):
        for duty_key, duty_desc in duty_item.items():
            last_check = duty_checks.get(duty_key, 0)
            interval = {
                "check_backups": 86400,    # раз в день
                "check_updates": 86400,    # раз в день
                "check_disk": 3600,        # раз в час
                "check_tools": 86400,      # раз в день
                "check_services": 3600,    # раз в час
            }.get(duty_key, 86400)
            if now - last_check > interval:
                results.append(duty_key)
    return results

# === MAIN ===
def main():
    # 1. Красная кнопка
    if DISABLED_FLAG.exists():
        log("Dex спит (DISABLED флаг найден)")
        return

    log("=== Dex Heartbeat ===")

    # 2. Инициализация
    load_dex_token()
    identity = load_identity()
    db = init_db()
    tick_num = get_state(db, "tick_count", 0) + 1
    set_state(db, "tick_count", tick_num)

    # 3. Читаем историю и идентичность
    history = read_tick_history(3)
    focus = get_state(db, "current_focus", "nothing")
    drives = get_state(db, "drives", {"curiosity": 0.5, "diligence": 0.5})
    duty_due = check_duty(db, identity)

    # 4. Собираем промпт для решения
    prompt_parts = [
        f"Ты Dex — смотритель сервера. Тик #{tick_num}.",
        f"Твой текущий фокус: {focus}",
        f"Уровень любопытства: {drives.get('curiosity', 0.5)}",
        f"Уровень исполнительности: {drives.get('diligence', 0.5)}",
    ]
    if duty_due:
        prompt_parts.append(f"Пора проверить: {', '.join(duty_due)}")
    if history:
        prompt_parts.append("Последние тики:")
        for h in history:
            prompt_parts.append(f"  - {h.get('action', 'ничего')} → {h.get('result', '?')}")
    prompt_parts.append("""Что делаем в этом тике? Ответь ТОЛЬКО одним словом — одним из: check_updates, check_backups, check_disk, check_tools, check_services, explore_interest, none. Никаких других слов, никаких пояснений.""")
    prompt = "\n".join(prompt_parts)

    # 5. Зовём LLM
    decision = call_llm(
        "Ты — серверный помощник Dex. Отвечаешь ТОЛЬКО одним словом: check_updates, check_backups, check_disk, check_tools, check_services, explore_interest, none. Никаких других слов.",
        prompt,
        max_tokens=20,
        db=db,
        tick=tick_num
    )
    if not decision or decision.strip() == "none":
        log("Dex решил ничего не делать в этом тике")
        set_state(db, "current_focus", "nothing")
        write_tick({"tick": tick_num, "action": "none", "result": "ok", "ts": datetime.now(timezone.utc).isoformat()})
        return

    # Берём только первое слово ответа
    decision = decision.strip().lower().split()[0] if decision.strip() else "none"
    valid_choices = {"check_updates", "check_backups", "check_disk", "check_tools", "check_services", "explore_interest", "none"}
    if decision not in valid_choices:
        log(f"Dex ответил невалидным ключом: {decision}, пропускаю тик")
        set_state(db, "current_focus", "nothing")
        write_tick({"tick": tick_num, "action": "none", "result": f"bogus: {decision}", "ts": datetime.now(timezone.utc).isoformat()})
        return

    decision = decision.strip().lower()
    log(f"Dex решил: {decision}")
    set_state(db, "current_focus", f"doing: {decision}")

    # 6. Исполняем
    result = None
    if decision == "check_backups":
        result = execute_check_backups()
    elif decision == "check_updates":
        result = execute_check_updates()
    elif decision == "check_disk":
        result = execute_check_disk()
    elif decision == "check_tools":
        result = execute_check_tools()
    elif decision == "check_services":
        result = execute_check_services()
    elif decision == "explore_interest":
        result = execute_explore_interest(identity, db)
    else:
        result = f"неизвестная команда: {decision}"

    # 7. Логируем результат
    log(f"Результат: {result}")
    set_state(db, "current_focus", "nothing")
    # Обновляем drives
    drives["curiosity"] = min(1.0, drives.get("curiosity", 0.5) + 0.1)
    drives["diligence"] = min(1.0, drives.get("diligence", 0.5) + 0.05)
    set_state(db, "drives", drives)
    write_tick({"tick": tick_num, "action": decision, "result": result[:200], "ts": datetime.now(timezone.utc).isoformat()})

    # 8. Отправляем уведомление в Telegram (только если есть что сказать)
    notify = format_notification(decision, result)
    if notify:
        send_telegram(notify)

def format_notification(action, result):
    """Форматирует результат в уведомление для Telegram. Возвращает None если слать нечего."""
    if not result:
        return None
    # Слишком частые уведомления о диске не шлём — только при проблемах
    if action == "check_disk":
        if "свободно" in result:
            parts = result.split()
            for i, p in enumerate(parts):
                if p == "свободно" and i > 0:
                    free = parts[i-1].rstrip(',')
                    # Если свободно > 10% — молчим
                    if free.endswith('G') and float(free[:-1]) > 10:
                        return None
                    if free.endswith('%') and float(free[:-1]) > 10:
                        return None
        return f"💾 <b>Диск</b>: {result}"
    if action == "check_backups":
        return f"💿 <b>Бэкапы</b>: {result}"
    if action == "check_updates":
        if "актуальны" in result:
            return None  # не шлем если всё хорошо
        return f"📦 <b>Обновления</b>: {result}"
    if action == "check_tools":
        if "запланирован" in result:
            return None
        return f"🔧 <b>Инструменты</b>: {result}"
    if action == "check_services":
        # Всегда шлём — сервисы важны
        return f"🔍 <b>Сервисы</b>: {result}"
    return None

def execute_check_backups():
    log("Проверяю бэкапы...")
    backup_dir = Path("/root/backups")
    if not backup_dir.exists():
        return "директория бэкапов не найдена"
    files = list(backup_dir.glob("*.sql.gz")) + list(backup_dir.glob("*.tar.gz"))
    if not files:
        return "нет файлов бэкапов"
    newest = max(files, key=lambda f: f.stat().st_mtime)
    age_hours = (time.time() - newest.stat().st_mtime) / 3600
    return f"самый свежий: {newest.name}, возраст: {age_hours:.1f}ч, всего: {len(files)} файлов"

def execute_check_updates():
    log("Проверяю обновления...")
    try:
        result = subprocess.run(
            ["apt", "list", "--upgradable", "2>/dev/null"],
            capture_output=True, text=True, timeout=15, shell=True
        )
        lines = [l for l in result.stdout.split("\n") if l.strip() and "..." not in l]
        count = len(lines) - 1  # минус заголовок
        if count <= 0:
            return "все пакеты актуальны"
        return f"{count} пакетов доступно для обновления"
    except Exception as e:
        return f"ошибка проверки: {e}"

def execute_check_disk():
    log("Проверяю диск...")
    try:
        result = subprocess.run(
            ["df", "-h", "/"],
            capture_output=True, text=True, timeout=10
        )
        lines = result.stdout.strip().split("\n")
        if len(lines) >= 2:
            parts = lines[1].split()
            return f"диск: {parts[3]} свободно из {parts[1]} ({parts[4]} занято)"
        return "не удалось"
    except Exception as e:
        return f"ошибка: {e}"

def execute_check_tools():
    log("Ищу новые инструменты...")
    # Пока заглушка — в следующей версии будет реальный поиск
    return "поиск инструментов запланирован, пропускаю этот тик"

def execute_check_services():
    log("Проверяю сервисы...")
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "dex-poller.service"],
            capture_output=True, text=True, timeout=5
        )
        dex_poller = result.stdout.strip()
        # Проверяем критичные Docker-сервисы
        docker_result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10
        )
        containers = [l.strip() for l in docker_result.stdout.splitlines() if l.strip()]
        return f"Dex Poller: {dex_poller}, контейнеров: {len(containers)}"
    except Exception as e:
        return f"ошибка проверки сервисов: {e}"

def execute_explore_interest(identity, db):
    interests = identity.get("interests", [])
    if not interests:
        return "нет интересов для изучения"
    # Нормализуем: интересы могут быть строками или dict (из YAML)
    flat_interests = []
    for item in interests:
        if isinstance(item, dict):
            flat_interests.extend(item.values())
        else:
            flat_interests.append(str(item))
    flat_interests = [i for i in flat_interests if i]
    if not flat_interests:
        return "нет интересов для изучения"
    import random
    interest = random.choice(flat_interests)
    last_explored = get_state(db, "last_explored_interest", {})
    last_explored[interest] = datetime.now(timezone.utc).isoformat()
    set_state(db, "last_explored_interest", last_explored)
    log(f"Изучаю: {interest}")
    # Пока заглушка — будет дёргать LLM для анализа
    return f"посмотрю что нового в: {interest}"

if __name__ == "__main__":
    main()
