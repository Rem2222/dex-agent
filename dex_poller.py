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
import struct
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# === CONFIG ===
BASE_DIR = Path.home() / ".hermes" / "proactive"
ENV_PATH = BASE_DIR / ".env"
IDENTITY_PATH = BASE_DIR / "identity.yaml"
SESSIONS_DB = BASE_DIR / "sessions.db"
AGENT_DB = BASE_DIR / "agent.db"
TICK_LOG = BASE_DIR / "tick_history.jsonl"
VEC_SO = BASE_DIR / "lib" / "vec0.so"
OLLAMA_EMBED = "http://127.0.0.1:11434/api/embed"
EMBED_MODEL = "bge-m3"
EMBED_DIM = 1024
POLL_INTERVAL = 3  # секунд между опросами
DISABLED_FLAG = BASE_DIR / "DISABLED"


def load_env_file():
    """Подставляет KEY=VALUE из proactive/.env в os.environ (не перезаписывая).

    Раньше LLM-вызов был захардкожен на Gateway (порт 8642) с литералом
    'Bearer ***' — из-за этого приходил 401 и Dex отвечал «не смог
    связаться с мозгом». Теперь провайдер/ключ/модель берутся из .env.
    """
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


load_env_file()

# === LLM-провайдер Dex ===
# Дефолт — gemini-web2api (docker, 127.0.0.1:8083): бесплатный, живой,
# не зависит от Gateway и от наличия API_SERVER_KEY.
DEX_API_URL = os.environ.get("DEX_API_URL", "http://127.0.0.1:8083/v1/chat/completions")
DEX_API_KEY = os.environ.get("DEX_API_KEY", "sk-gemini")
DEX_MODEL = os.environ.get("DEX_MODEL", "gemini-3.5-flash")

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
        "Если не знаешь — скажи честно. "
        "Пиши СТРОГО по-русски (или по-английски) — без иероглифов, без индийских, "
        "корейских и любых других иностранных письменностей, без символов вроде ${...}. "
        "Числа пиши обычными цифрами."
    )
    return "\n".join(parts)

def read_state_summary(last_ticks=5):
    """Компактное состояние Dex: тик, фокус, драйвы, последние тики.

    Раньше этого блока не было вовсе — в чат уходил только текст identity.yaml
    и последние 10 реплик, поэтому Dex не мог ответить «что ты проверял».
    """
    lines = []
    try:
        db = sqlite3.connect(f"file:{AGENT_DB}?mode=ro", uri=True)
        state = dict(db.execute("SELECT key, value FROM state").fetchall())
        db.close()

        tick = state.get("tick_count", "?")
        focus = state.get("current_focus", "nothing")
        try:
            drives = json.loads(state.get("drives", "{}"))
        except Exception:
            drives = {}
        cur = drives.get("curiosity", "?")
        dil = drives.get("diligence", "?")
        lines.append(f"тик #{tick} | фокус: {focus}")
        lines.append(f"драйвы: любопытство {cur}, исполнительность {dil}")
    except Exception as e:
        lines.append(f"состояние недоступно: {e}")

    try:
        rows = []
        if TICK_LOG.exists():
            with open(TICK_LOG) as f:
                for raw in f:
                    raw = raw.strip()
                    if raw:
                        try:
                            rows.append(json.loads(raw))
                        except Exception:
                            pass
        for r in rows[-last_ticks:]:
            act = r.get("action", "?")
            res = str(r.get("result", ""))[:110]
            ts = str(r.get("ts", ""))[11:16]
            lines.append(f"  {ts} {act} -> {res}")
        if not rows:
            lines.append("  (история тиков пуста)")
    except Exception as e:
        lines.append(f"  история тиков недоступна: {e}")

    return "\n".join(lines)


def embed_query(text):
    """Эмбеддинг запроса через Ollama bge-m3 (1024 измерения). None при ошибке."""
    try:
        body = json.dumps({"model": EMBED_MODEL, "input": [text[:500]],
                           "keep_alive": "10m"}).encode()
        req = urllib.request.Request(OLLAMA_EMBED, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())["embeddings"][0]
    except Exception as e:
        log(f"память: эмбеддинг запроса не удался: {e}")
        return None


def search_memory(query, k=4, pool=30):
    """Семантический поиск по собственным тикам (sqlite-vec + bge-m3).

    Возвращает [{"tick", "ts", "action", "result", "distance"}].

    vec0 не разрешает ORDER BY distance, rowid в одном KNN-запросе, поэтому
    тянем пул и сортируем сами: дубликаты текста дают одинаковое расстояние,
    и без второго ключа побеждал бы САМЫЙ СТАРЫЙ тик. Сортируем по
    (расстояние, -тик) — при равенстве берём свежий.
    """
    vec = embed_query(query)
    if not vec:
        return []
    try:
        c = sqlite3.connect(f"file:{AGENT_DB}?mode=ro", uri=True, timeout=15)
        c.enable_load_extension(True)
        c.load_extension(str(VEC_SO))
        c.execute("PRAGMA busy_timeout=5000")
        blob = sqlite3.Binary(struct.pack("%df" % EMBED_DIM, *vec))
        rows = c.execute("SELECT rowid, distance FROM vec_ticks "
                         "WHERE embedding MATCH ? AND k=?",
                         (blob, pool)).fetchall()
        picked = sorted(rows, key=lambda r: (r[1], -r[0]))[:k]

        out = []
        for tick, dist in picked:
            meta = c.execute("SELECT ts, action, result FROM ticks_meta WHERE tick=?",
                             (tick,)).fetchone()
            if not meta:
                continue
            out.append({"tick": tick, "ts": meta[0], "action": meta[1],
                        "result": meta[2], "distance": round(dist, 3)})
        c.close()
        return out
    except Exception as e:
        log(f"память: поиск не удался: {e}")
        return []


MEMORY_HINTS = (
    "был", "были", "было", "раньше", "вчера", "недавн", "давно", "прошл",
    "предыдущ", "помни", "помню", "вспомни", "что было", "как было",
    "что ты делал", "что делал", "истори", "проблем", "случал", "а когда",
    "когда", "откуда", "первый раз", "в первый", "никогда", "постоянн",
    "опять", "снова", "свеж",
)


def wants_memory(text):
    """Нужен ли семантический поиск по прошлым тикам.

    Эмбеддинг стоит 6-7 секунд, поэтому на обычные вопросы (и на вопросы
    о текущем состоянии, где ответ уже есть в блоке состояния) поиск
    пропускается.
    """
    low = (text or "").lower()
    return any(h in low for h in MEMORY_HINTS)


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

    # Команды: известные обслуживаем, про неизвестные честно сообщаем
    if text.startswith("/"):
        cmd = text.split()[0].lower()
        if cmd == "/start":
            send_message(chat_id, "Привет! Я Dex — смотритель сервера. Можешь спросить меня о состоянии сервера или просто поболтать 🤖")
        elif cmd in ("/status", "/state"):
            send_message(chat_id, "📊 <b>Dex — состояние</b>\n" + read_state_summary(5))
        else:
            send_message(chat_id, f"Команда <code>{cmd}</code> пока не реализована. Есть /start и /status.")
        return

    log(f"Сообщение от {chat_id}: {text[:100]}")

    # Готовим запрос: личность + состояние + история
    identity = load_identity()
    system_prompt = build_system_prompt(identity)
    system_prompt += (
        "\n\n---\nТвоё текущее состояние (читай, когда спрашивают о тебе, о тиках или о сервере):\n"
        + read_state_summary(5)
        + "\n\nЧестность о возможностях:\n"
          "- Сейчас у тебя НЕТ файловых инструментов: ты не открываешь файлы, не выполняешь команды, не ходишь в интернет.\n"
          "- Если просят что-то найти, открыть или выполнить — скажи прямо, что пока не умеешь, и предложи сделать это Рому.\n"
          "- НЕ выдумывай, что искал по индексам, имеешь песочницу или доступ к чужим репозиториям.\n"
          "- Ответы про «параметры Gmail отключены» / «не могу использовать Workspace» — ошибка провайдера, а не твои слова: переспроси иначе.\n"
    )

    # Векторная память нужна только на вопросы о прошлом: на вопросы о
    # текущем состоянии ответ уже есть в блоке состояния, а эмбеддинг
    # стоит 6-7 секунд и тормозит обычный чат.
    memory = []
    if wants_memory(text):
        memory = search_memory(text, k=4)
        if memory:
            block = ["\n---\nПохожие тики из твоей памяти (semantic search):"]
            for m in memory:
                block.append(f"  #{m['tick']} {m['ts'][5:16]} {m['action']} -> "
                             f"{m['result'][:120]}")
            block.append("Это ИСТОРИЯ — данные могли устареть. Для текущего "
                         "положения смотри блок «Твоё текущее состояние» выше.")
            system_prompt += "\n".join(block)
            log(f"память: подобрано {len(memory)} тиков для запроса")
        else:
            log("память: пусто (эмбеддинг недоступен или база пуста)")
    else:
        log("память: запрос не про прошлое, поиск пропущен")

    history = load_session(chat_id)

    messages = [{"role": "system", "content": system_prompt}]
    # История: 30 сообщений (15 обменов) — раньше было 10, контекст терялся
    for h in history[-30:]:
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

GOOGLE_TOOL_ERROR_MARKERS = (
    "параметры Gmail отключены",
    "не могу использовать Workspace",
    "не могу использовать Google Drive",
)


def _is_google_tool_error(text):
    """Сервер Google возвращает это, когда его расширение Workspace недоступно.

    Воспроизведено отдельным чистым запросом: просьба «открой файлы, найди
    правила» даёт ровно этот текст — провайдерная ошибка, а не ответ модели.
    """
    low = (text or "").lower()
    return any(m.lower() in low for m in GOOGLE_TOOL_ERROR_MARKERS)


def call_hermes(messages):
    """Вызывает LLM-провайдера Dex (DEX_API_URL, см. .env).

    Раньше был жёстко захардкожен Gateway (127.0.0.1:8642) + модель
    deepseek-v4-flash. Ключ GATEWAY_KEY перестал действовать: секции
    api_server в config.yaml нет, API_SERVER_KEY не задан → 401.
    Теперь дефолт — gemini-web2api на 8083 (бесплатный, без Gateway).

    На запросы про файлы/документы провайдер иногда возвращает текст ошибки
    Workspace. Ловим, повторяем один раз, при повторе — честный ответ.
    """
    for attempt in (1, 2):
        result = None
        try:
            result = subprocess.run(
                ["curl", "-s", "-X", "POST",
                 DEX_API_URL,
                 "-H", "Content-Type: application/json",
                 "-H", "Authorization: Bearer " + DEX_API_KEY,
                 "-d", json.dumps({
                     "model": DEX_MODEL,
                     "messages": messages,
                     "max_tokens": 1000,
                     "temperature": 0.7
                 })],
                capture_output=True, text=True, timeout=60
            )
            resp = json.loads(result.stdout)
            content = resp["choices"][0]["message"]["content"]
        except Exception as e:
            log(f"LLM API error: {e}")
            if result is not None and result.stdout:
                log(f"Raw: {result.stdout[:200]}")
            return None

        text = (content or "").strip()

        if _is_google_tool_error(text) and attempt == 1:
            log("LLM: ошибка Workspace от провайдера, повторяю запрос")
            time.sleep(1)
            continue

        if _is_google_tool_error(text):
            log("LLM: ошибка Workspace повторилась — отдаю честный ответ")
            return ("Не смог выполнить: у меня нет доступа к файлам и документам, "
                    "а провайдер на такие запросы отвечает ошибкой Workspace. "
                    "Сформулируй иначе — или сделай это сам.")
        return text
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
