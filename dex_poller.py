#!/usr/bin/env python3
"""
Dex Telegram Poller — ретранслятор сообщений из Dex бота в Hermes.
Запускается как фоновый процесс (или cron).

Логика:
1. Каждые 3 секунды спрашивает Telegram API: есть ли новые сообщения?
2. Если есть — отправляет в Hermes Gateway API (с dex-identity)
3. Ответ шлёт обратно в Telegram
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# === CONFIG ===
BASE_DIR = Path.home() / ".hermes" / "proactive"
ENV_PATH = BASE_DIR / ".env"
IDENTITY_PATH = BASE_DIR / "identity.yaml"
SESSIONS_DB = BASE_DIR / "sessions.db"
POLL_INTERVAL = 3  # секунд между опросами
DISABLED_FLAG = BASE_DIR / "DISABLED"

GATEWAY_KEY = "123c867ed8cc504a5e602b4189cc201964a4e7331a20d7aeb883b88fdf86ed0a"

# === STATE ===
bot_token = None
last_update_id = 0

def log(msg):
    ts = datetime.now(timezone.utc).isoformat()
    print(f"[DEXP][{ts}] {msg}", flush=True)

def load_token():
    global bot_token
    if not ENV_PATH.exists():
        log("FATAL: .env не найден")
        return False
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line.startswith("DEX_BOT_TOKEN="):
            bot_token = line.split("=", 1)[1]
            break
    if not bot_token:
        log("FATAL: DEX_BOT_TOKEN не найден в .env")
        return False
    return True

def load_identity():
    try:
        import yaml
        with open(IDENTITY_PATH) as f:
            return yaml.safe_load(f)
    except Exception as e:
        log(f"WARN: не удалось загрузить identity: {e}")
        return None

def build_system_prompt(identity):
    """Собирает system prompt для Hermes из identity Dex"""
    name = identity.get("name", "Dex") if identity else "Dex"
    role = identity.get("role", "смотритель сервера") if identity else "смотритель сервера"
    desc = identity.get("description", "") if identity else ""
    char = identity.get("character", []) if identity else []
    interests = identity.get("interests", []) if identity else []

    parts = [
        f"Ты {name} — {role}. Отвечай на сообщения как {name}.",
        "",
    ]
    if desc:
        parts.append(desc.strip())
        parts.append("")

    if char:
        parts.append("Твой характер:")
        for c in char:
            parts.append(f"- {c}")
        parts.append("")

    if interests:
        parts.append("Твои интересы:")
        for i in interests:
            parts.append(f"- {i}")
        parts.append("")

    parts.append(
        "Ты общаешься в Telegram. Пиши кратко, по делу, без лести. "
        "Если тебя спрашивают о состоянии сервера — можешь ответить что знаешь. "
        "Если не знаешь — скажи честно."
    )
    return "\n".join(parts)

def get_updates():
    """Получает новые сообщения из Telegram Bot API"""
    global last_update_id
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    params = {
        "offset": last_update_id + 1 if last_update_id else 0,
        "timeout": 10,
        "allowed_updates": ["message"]
    }
    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST", url,
             "-H", "Content-Type: application/json",
             "-d", json.dumps(params)],
            capture_output=True, text=True, timeout=15
        )
        resp = json.loads(result.stdout)
        if not resp.get("ok"):
            log(f"getUpdates error: {resp.get('description', 'unknown')}")
            return []

        updates = resp.get("result", [])
        if updates:
            # обновляем offset
            last_update_id = updates[-1]["update_id"]
        return updates
    except Exception as e:
        log(f"getUpdates exception: {e}")
        return []

def process_message(msg_data):
    """Обрабатывает одно сообщение: отправляет в Hermes, возвращает ответ"""
    message = msg_data.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "").strip()
    msg_id = message.get("message_id")

    if not chat_id or not text:
        log(f"Пропущено: пустое сообщение (chat_id={chat_id}, text='{text}')")
        return

    # Игнорируем команды /start и т.п.
    if text.startswith("/"):
        # На /start отвечаем приветствием
        if text == "/start":
            send_message(chat_id, "Привет! Я Dex — смотритель сервера. Можешь спросить меня о состоянии сервера или просто поболтать 🤖")
        return

    log(f"Сообщение от {chat_id}: {text[:100]}")

    # Готовим запрос к Hermes
    identity = load_identity()
    system_prompt = build_system_prompt(identity)
    history = load_session(chat_id)

    messages = [{"role": "system", "content": system_prompt}]
    # Добавляем историю (последние 10 сообщений)
    for h in history[-10:]:
        messages.append(h)
    messages.append({"role": "user", "content": text})

    # Вызываем Hermes Gateway
    response = call_hermes(messages)
    if not response:
        send_message(chat_id, "🙈 Сорян, не смог связаться с мозгом. Попробуй позже.")
        return

    # Сохраняем в историю
    save_message(chat_id, {"role": "user", "content": text})
    save_message(chat_id, {"role": "assistant", "content": response})

    # Отправляем ответ
    send_message(chat_id, response)

def call_hermes(messages):
    """Вызывает Hermes Gateway API"""
    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST",
             "http://127.0.0.1:8642/v1/chat/completions",
             "-H", "Content-Type: application/json",
             "-H", f"Authorization: Bearer {GATEWAY_KEY}",
             "-d", json.dumps({
                 "model": "deepseek-v4-flash",
                 "messages": messages,
                 "max_tokens": 1000,
                 "temperature": 0.7
             })],
            capture_output=True, text=True, timeout=60
        )
        resp = json.loads(result.stdout)
        content = resp["choices"][0]["message"]["content"]
        return content.strip()
    except Exception as e:
        log(f"Hermes API error: {e}")
        if 'result' in dir() and result.stdout:
            log(f"Raw: {result.stdout[:200]}")
        return None

def send_message(chat_id, text):
    """Отправляет сообщение в Telegram через Dex бота"""
    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST",
             f"https://api.telegram.org/bot{bot_token}/sendMessage",
             "-H", "Content-Type: application/json",
             "-d", json.dumps({
                 "chat_id": chat_id,
                 "text": text,
                 "parse_mode": "HTML"
             })],
            capture_output=True, text=True, timeout=15
        )
        resp = json.loads(result.stdout)
        if resp.get("ok"):
            log(f"Ответ отправлен в {chat_id}")
        else:
            log(f"sendMessage error: {resp.get('description', 'unknown')}")
    except Exception as e:
        log(f"sendMessage exception: {e}")

# === SESSION MANAGEMENT (SQLite) ===
def init_sessions():
    db = sqlite3.connect(str(SESSIONS_DB))
    db.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            chat_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            ts TEXT NOT NULL
        )
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_sessions_chat ON sessions(chat_id, ts)
    """)
    db.commit()
    return db

def load_session(chat_id, limit=20):
    db = init_sessions()
    rows = db.execute(
        "SELECT role, content FROM sessions WHERE chat_id=? ORDER BY ts DESC LIMIT ?",
        (chat_id, limit)
    ).fetchall()
    db.close()
    # Возвращаем в хронологическом порядке
    result = [{"role": r[0], "content": r[1]} for r in rows]
    result.reverse()
    return result

def save_message(chat_id, msg):
    db = init_sessions()
    db.execute(
        "INSERT INTO sessions (chat_id, role, content, ts) VALUES (?, ?, ?, ?)",
        (chat_id, msg["role"], msg["content"], datetime.now(timezone.utc).isoformat())
    )
    db.commit()
    db.close()

# === MAIN LOOP ===
def main():
    if DISABLED_FLAG.exists():
        log("Dex спит (DISABLED)")
        time.sleep(60)
        return

    if not load_token():
        time.sleep(30)
        return

    log("Dex Poller запущен")
    global last_update_id

    # Восстанавливаем offset при перезапуске
    offset_file = BASE_DIR / ".poller_offset"
    if offset_file.exists():
        try:
            last_update_id = int(offset_file.read_text().strip())
            log(f"Восстановлен offset: {last_update_id}")
        except:
            pass

    # Сначала проверяем, отвечает ли бот
    try:
        me = subprocess.run(
            ["curl", "-s", f"https://api.telegram.org/bot{bot_token}/getMe"],
            capture_output=True, text=True, timeout=10
        )
        me_data = json.loads(me.stdout)
        if me_data.get("ok"):
            bot_user = me_data["result"]
            log(f"Бот: @{bot_user.get('username', '?')} ({bot_user.get('first_name', '?')})")
        else:
            log(f"Бот НЕ ОТВЕЧАЕТ: {me_data.get('description')}")
    except Exception as e:
        log(f"getMe error: {e}")

    while True:
        if DISABLED_FLAG.exists():
            log("Dex выключен (DISABLED), жду...")
            time.sleep(60)
            continue

        try:
            updates = get_updates()
            for update in updates:
                process_message(update)

            # Сохраняем offset
            offset_file.write_text(str(last_update_id))
        except Exception as e:
            log(f"Poll error: {e}")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Dex Poller остановлен")
        sys.exit(0)
