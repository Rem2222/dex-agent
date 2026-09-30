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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dex_tools import (TOOLS, execute_tool, skills_index_text,
                        get_access_level, set_access_level,
                        active_tools, LEVEL_NAMES, SANDBOX_DIR)

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

# === Белый список: личный бот, команды и чат только для Рома ===
# Раньше chat_id не проверялся вовсе — бота мог завести кто угодно.
ALLOWED_CHAT_IDS = {386235337}
# Команды, которым нужно подтверждение /yes
CONFIRM_CMDS = {"pause", "restart", "tick", "access"}
CONFIRM_TTL = 120  # секунд живёт ожидающая команда


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
        "allowed_updates": ["message", "callback_query"]
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

def _state_get(key, default=None):
    """Читает один ключ из state в agent.db (не тащит весь файл)."""
    try:
        db = sqlite3.connect(AGENT_DB)
        row = db.execute("SELECT value FROM state WHERE key=?",
                         (key,)).fetchone()
        db.close()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except Exception:
            return row[0]
    except Exception:
        return default


def _access_text():
    """Что сейчас разрешено на текущем уровне."""
    lv = get_access_level()
    rows = {
        3: "3 — только чтение (как было до 30.09): 9 инструментов, "
           "записи и запуска нет.",
        2: "2 — песочница: + запись в sandbox/ и skills/, "
           "+ запуск своих .py от пользователя nobody, БЕЗ сети, "
           "с лимитами (CPU 20с, память 1 ГБ, файл 10 МБ).",
        1: "1 — root: + запись по всему /root/.hermes, "
           "+ запуск от root со счётом. Самый опасный уровень.",
    }
    lines = ["🔒 <b>Уровень доступа</b>", rows.get(lv, str(lv)),
             "", "Переключение: /access 1 | /access 2 | /access 3 "
                 "(нужен /yes, кроме перехода на 3)."]
    return "\n".join(lines)


def _state_set(key, value):
    try:
        db = sqlite3.connect(AGENT_DB)
        db.execute(
            "INSERT OR REPLACE INTO state (key, value, updated_at) "
            "VALUES (?, ?, ?)",
            (key, json.dumps(value, ensure_ascii=False),
             datetime.now(timezone.utc).isoformat()))
        db.commit()
        db.close()
    except Exception as e:
        log(f"state set {key}: {e}")


def _help_text():
    return (
        "🤖 <b>Dex — команды</b>\n\n"
        "<b>Состояние</b>\n"
        "/status — тик, драйвы, фокус\n"
        "/last — последние 5 тиков\n"
        "/drives — любопытство и исполнительность\n"
        "/memory &lt;запрос&gt; — поиск по 1398 тикам\n\n"
        "<b>Проверки</b>\n"
        "/check &lt;name&gt; — disk|backups|updates|services|tools|interest\n"
        "/skills — список процедур\n"
        "/skill &lt;имя&gt; — прочитать процедуру\n\n"
        "<b>Задачи</b>\n"
        "/tasks — что висит\n"
        "/task &lt;текст&gt; — завести\n"
        "/done &lt;id&gt; — закрыть\n\n"
        "<b>Управление</b>\n"
        "/tick — форс heartbeat (нужен /yes)\n"
        "/pause [мин] — выключить Декса (нужен /yes)\n"
        "/resume — включить обратно\n"
        "/restart — очистить историю диалога (нужен /yes)\n"
        "/access [1|2|3] — уровень доступа (запрос/переключение)\n"
        "/help — эта справка"
    )


def _run_tick():
    """Принудительный heartbeat — то же, что /api/action/tick в панели."""
    try:
        r = subprocess.run(
            ["python3", str(BASE_DIR / "heartbeat.py")],
            capture_output=True, text=True, timeout=90, cwd=str(BASE_DIR))
        tail = [ln for ln in (r.stdout or "").splitlines() if ln.strip()][-6:]
        err = (r.stderr or "").strip().splitlines()[-1] if r.stderr and r.stderr.strip() else ""
        out = "\n".join(tail) or "вывода нет"
        if err and "Traceback" not in err:
            out += f"\nstderr: {err[:200]}"
        return out
    except subprocess.TimeoutExpired:
        return "heartbeat не завершился за 90с (возможно, долгий вызов LLM)"
    except Exception as e:
        return f"ошибка запуска: {e}"


def _do_command(chat_id, cmd, rest):
    """Исполняет подтверждённую или неопасную команду. Возвращает текст."""
    import dex_tools as dt

    # --- состояние ---
    if cmd in ("status", "state"):
        return "📊 <b>Dex — состояние</b>\n" + read_state_summary(5)

    if cmd == "last":
        try:
            with open(TICK_LOG) as f:
                rows = [json.loads(ln) for ln in f if ln.strip()]
        except Exception:
            return "история тиков недоступна"
        if not rows:
            return "тиков нет"
        out = ["<b>Последние тики:</b>"]
        for t in rows[-5:]:
            out.append(f"  #{t.get('tick','?')} {str(t.get('ts',''))[5:16]} "
                       f"{t.get('action','')} → {str(t.get('result',''))[:60]}")
        return "\n".join(out)

    if cmd == "drives":
        d = _state_get("drives", {"curiosity": 0.5, "diligence": 0.5})
        v2 = _state_get("drives_v2", False)
        try:
            import heartbeat as hb
            lo, hi = hb.DRIVE_FLOOR, hb.DRIVE_CEIL
        except Exception:
            lo, hi = 0.10, 1.00
        return (
            f"🧠 <b>Драйвы</b> (v2={v2})\n"
            f"  любопытство:     {d.get('curiosity')}\n"
            f"  исполнительность:{d.get('diligence')}\n"
            f"  пол/потолок: {lo} / {hi}")

    if cmd == "memory":
        if not rest:
            return "формат: /memory &lt;запрос&gt;"
        res = search_memory(rest, k=5)
        if not res:
            return f"По запросу «{rest}» ничего не нашлось."
        out = [f"🔍 <b>Память</b> — {len(res)} тиков:"]
        for m in res:
            out.append(f"  #{m['tick']} {m['ts'][5:16]} {m['action']} → "
                       f"{m['result'][:70]}")
        out.append("Это история — данные могли устареть.")
        return "\n".join(out)

    # --- проверки ---
    if cmd == "check":
        name = (rest or "").strip().split()[0] if rest.strip() else ""
        if not name:
            return "формат: /check disk|backups|updates|services|tools|interest"
        return dt.execute_tool("run_check", json.dumps({"name": name}))

    if cmd == "skills":
        return "<b>Скилы:</b>\n" + dt.skills_index_text()

    if cmd == "skill":
        if not rest.strip():
            return "формат: /skill &lt;имя&gt;. Сначала /skills"
        return dt.execute_tool("read_skill", json.dumps({"name": rest.strip()}))

    # --- задачи ---
    if cmd == "access":
        raw = (rest or "").strip().split()[0] if rest.strip() else ""
        if raw not in ("1", "2", "3"):
            return ("формат: /access 1|2|3. Сейчас уровень "
                    f"{get_access_level()} ({LEVEL_NAMES[get_access_level()]}).")
        lv = int(raw)
        old_lv = get_access_level()
        set_access_level(lv)
        log(f"Уровень доступа: {old_lv} -> {lv}")
        return (_access_text() + f"\n\nБыло: {old_lv} "
                f"({LEVEL_NAMES[old_lv]}) → стало: {lv}.")

    if cmd == "tasks":
        return "📋 <b>Задачи</b>\n" + dt.execute_tool(
            "tasks", json.dumps({"action": "list"}))
    if cmd == "task":
        if not rest.strip():
            return "формат: /task &lt;текст&gt;"
        return dt.execute_tool("tasks", json.dumps(
            {"action": "add", "what": rest.strip()}))
    if cmd == "done":
        if not rest.strip().isdigit():
            return "формат: /done &lt;id&gt; — номер из /tasks"
        return dt.execute_tool("tasks", json.dumps(
            {"action": "done", "id": int(rest.strip())}))

    # --- управление (сюда попадают только уже подтверждённые) ---
    if cmd == "tick":
        return "▶️ <b>Принудительный тик</b>\n" + _run_tick()

    if cmd == "pause":
        minutes = 0
        if rest.strip().isdigit():
            minutes = int(rest.strip())
        import heartbeat as hb
        if minutes > 0:
            until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
            hb.DISABLED_FLAG.write_text(
                f"paused at {datetime.now(timezone.utc).isoformat()} "
                f"until={until.isoformat()}")
            return f"⏸ Dex выключен на {minutes} мин (до {until:%H:%M} UTC). /resume — раньше."
        hb.DISABLED_FLAG.write_text(
            f"paused at {datetime.now(timezone.utc).isoformat()}")
        return "⏸ Dex выключен до /resume."

    if cmd == "resume":
        import heartbeat as hb
        if hb.DISABLED_FLAG.exists():
            hb.DISABLED_FLAG.unlink()
            return "▶️ Dex снова работает."
        return "Dex и так работал — флага не было."

    if cmd == "restart":
        try:
            db = sqlite3.connect(SESSIONS_DB)
            n = db.execute("SELECT count(*) FROM sessions WHERE chat_id=?",
                           (chat_id,)).fetchone()[0]
            db.execute("DELETE FROM sessions WHERE chat_id=?", (chat_id,))
            db.commit()
            db.close()
        except Exception as e:
            return f"не смог очистить историю: {e}"
        return (f"🔄 История очищена (сообщений было: {n}). "
                "Начинаю с чистого листа — контекст прошлого разговора потерян, "
                "но состояние сервера и драйвы на месте.")

    return f"неизвестная команда: /{cmd}"


def handle_command(chat_id, text):
    """Диспетчер команд: белый список + подтверждение опасных."""
    # Повторная проверка здесь же — process_message тоже проверяет, но
    # защита должна жить в точке входа команд, а не только в вызывающем.
    if chat_id not in ALLOWED_CHAT_IDS:
        log(f"Команда отклонена: chat_id {chat_id} не в белом списке")
        send_message(chat_id, "Это личный бот. Обращение не принято.")
        return

    parts = text.split(None, 1)
    token = parts[0]
    rest = parts[1].strip() if len(parts) > 1 else ""
    cmd = token.lstrip("/").lower()
    # Telegram в группах присылает /access@Jawl_Moishe_bot — убираем суффикс,
    # иначе команда не найдётся и молча уйдёт в обычный текст
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]
    # Без этого команды не видны в журнале — их невозможно диагностировать
    log(f"Команда: /{cmd} {rest[:50]} (chat_id={chat_id})")

    # «посмотреть уровень» — без подтверждения; «переключить» — с ним
    if cmd == "access" and not rest.strip():
        send_message(chat_id, _access_text() + "\n\nВыбери уровнем кнопкой "
                    "или введи /access 1|2|3:", buttons=LEVEL_BUTTONS)
        return

    # переход НА 3 — безопасное направление, спрашивать незачем
    if cmd == "access" and rest.strip() == "3":
        send_message(chat_id, _do_command(chat_id, "access", "3"))
        return

    if cmd in ("start", "привет", "help", "хелп", "помощь"):
        _state_set("pending_cmd", None)
        send_message(chat_id,
                     "Привет! Я Dex — смотритель сервера. 🤖\n\n" + _help_text())
        return

    # подтверждение / отмена
def _do_yes(chat_id):
    """Общий обработчик подтверждения — и для /yes, и для кнопки «Да»."""
    p = _state_get("pending_cmd")
    if not p or p.get("chat_id") != chat_id:
        return "Нет ожидающей команды."
    if time.time() - float(p.get("ts", 0)) > CONFIRM_TTL:
        _state_set("pending_cmd", None)
        return "Подтверждение устарело (прошло 2 мин). Повтори команду."
    _state_set("pending_cmd", None)
    log(f"подтверждено: {p.get('cmd')} {str(p.get('args'))[:40]}")
    return (f"✅ Подтверждено: /{p.get('cmd')} — выполняю…\n\n"
            + _do_command(chat_id, p.get("cmd", ""), p.get("args", "")))


def _do_no(chat_id):
    _state_set("pending_cmd", None)
    return "Отменил."


def _level_text(lv=None):
    lv = get_access_level() if lv is None else lv
    rows = {
        3: "3 — только чтение: 9+1 инструментов, записи и запуска нет",
        2: "2 — песочница: запись в sandbox/ и skills/, запуск от "
           "nobody без сети, с лимитами",
        1: "1 — root: запись по /root/.hermes и запуск от root",
    }
    return f"🔒 Сейчас уровень {lv}. {rows.get(lv, '')}"


LEVEL_BUTTONS = [[("🔒 Только чтение (3)", "lv:3")],
                 [("📦 Песочница (2)", "lv:2")],
                 [("🔓 Root (1)", "lv:1")]]
CONFIRM_BUTTONS = [[("✅ Да", "yes"), ("❌ Нет", "no")]]


def handle_callback(cb):
    """Нажатие inline-кнопки: callback_data = yes/no/lv:N/setlv:N."""
    cb_id = cb.get("id") or ""
    data = (cb.get("data") or "").strip()
    chat_id = cb.get("message", {}).get("chat", {}).get("id")
    try:
        subprocess.run(
            ["curl", "-s", "-X", "POST",
             f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery",
             "-H", "Content-Type: application/json",
             "-d", json.dumps({"callback_query_id": cb_id})],
            capture_output=True, text=True, timeout=10)
    except Exception:
        pass
    if chat_id is None:
        return
    if chat_id not in ALLOWED_CHAT_IDS:
        log(f"Кнопка отклонена: chat_id {chat_id} не в белом списке")
        return

    log(f"кнопка: {data} (chat_id={chat_id})")

    if data == "yes":
        send_message(chat_id, _do_yes(chat_id))
        return
    if data == "no":
        send_message(chat_id, _do_no(chat_id))
        return
    if data.startswith("lv:"):
        lv = data.split(":", 1)[1]
        if lv not in ("1", "2", "3"):
            send_message(chat_id, "Неизвестный уровень.")
            return
        cur = get_access_level()
        if str(cur) == lv:
            send_message(chat_id, _level_text() + "\nЭто уже текущий уровень.")
            return
        _state_set("pending_cmd", {"cmd": "access", "args": lv,
                                   "chat_id": chat_id, "ts": time.time()})
        send_message(
            chat_id,
            f"⚠️ Переключить уровень <b>{cur} → {lv}</b>?\n"
            + _level_text(int(lv)) + "\n\nНажми «Да» или «Нет» "
            "(действует 2 минуты, можно и /yes / /no).",
            buttons=CONFIRM_BUTTONS)
        return
    send_message(chat_id, f"Неизвестная кнопка: {data}")


    if cmd in ("yes", "y", "да", "ок"):
        send_message(chat_id, _do_yes(chat_id))
        return

    if cmd in ("no", "n", "нет", "отмена"):
        send_message(chat_id, _do_no(chat_id))
        return

    # опасные команды — спрашиваем
    if cmd in CONFIRM_CMDS:
        _state_set("pending_cmd",
                   {"cmd": cmd, "args": rest, "chat_id": chat_id,
                    "ts": time.time()})
        hint = {"pause": "/pause [мин]",
                "restart": "/restart",
                "tick": "/tick"}.get(cmd, f"/{cmd}")
        extra = _level_text(int(rest)) + "\n\n" if cmd == "access" and rest.strip() in ("1", "2", "3") else ""
        send_message(chat_id,
                     f"⚠️ <b>Подтверди</b>: <code>{hint}</code> "
                     f"{'с аргументом <code>' + rest + '</code> ' if rest else ''}"
                     "— нажми «Да» или «Нет» (или отправь /yes, /no). "
                     f"Действует {CONFIRM_TTL // 60} мин.\n\n" + extra,
                     buttons=CONFIRM_BUTTONS)
        return

    # обычные команды
    try:
        out = _do_command(chat_id, cmd, rest)
    except Exception as e:
        out = f"ошибка выполнения /{cmd}: {type(e).__name__}: {e}"
    if out.startswith("неизвестная команда"):
        out = f"Команда <code>/{cmd}</code> не найдена.\n\n" + _help_text()
    send_message(chat_id, out)


def process_message(msg_data):
    """Обрабатывает одно сообщение: отправляет в Hermes, возвращает ответ"""
    message = msg_data.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "").strip()
    msg_id = message.get("message_id")

    if not chat_id or not text:
        log(f"Пропущено: пустое сообщение (chat_id={chat_id}, text='{text}')")
        return

    # Белый список: личный бот, чужие игнорируем (см. ALLOWED_CHAT_IDS)
    if chat_id not in ALLOWED_CHAT_IDS:
        log(f"Отклонено: chat_id {chat_id} не в белом списке")
        send_message(chat_id, "Это личный бот. Обращение не принято.")
        return

    # Команды: известные обслуживаем, про неизвестные подсказываем /help
    if text.startswith("/"):
        handle_command(chat_id, text)
        return

    log(f"Сообщение от {chat_id}: {text[:100]}")

    # Готовим запрос: личность + состояние + история
    identity = load_identity()
    system_prompt = build_system_prompt(identity)
    system_prompt += (
        "\n\n---\nТвоё текущее состояние (читай, когда спрашивают о тебе, о тиках или о сервере):\n"
        + read_state_summary(5)
        + "\n\nУ тебя есть инструменты — вызывай их, когда нужно посмотреть что-то реально:\n"
          "- read_state() — состояние сервера и твои драйвы\n"
          "- run_check(name) — чеки: backups, updates, disk, services, tools\n"
          "- read_file(path, limit) — прочитать файл\n"
          "- list_dir(path) — содержимое каталога\n"
          "- tail_log(path, lines) — последние строки журнала\n"
          "Песочница разрешает ТОЛЬКО чтение и только эти каталоги: /root/.hermes/proactive, "
          "/root/.hermes/scripts, /root/backups, /var/log, /etc, /root/Documents/wiki/ops/services.\n"
          "Доступа в интернет нет, записи в файлы нет, запуска произвольных программ нет, "
          "секреты (.env, auth.json, ключи) закрыты.\n"
          "- Если нужного пути нет в списке — скажи прямо, что не можешь, не выдумывай.\n"
          "- Если инструмент вернул ошибку или отказ песочницы — передай это как есть.\n"
          "- Ответы про «параметры Gmail отключены» / «не могу использовать Workspace» — ошибка провайдера, а не твои слова: переспроси иначе.\n"
    )

    # Скилы: короткий индекс, чтобы Dex знал про процедуры,
    # а сам текст читал через инструмент read_skill.
    lv = get_access_level()
    if lv < 3:
        system_prompt += (
            f"\n\nУровень доступа: {lv} ({LEVEL_NAMES[lv]}). "
            "Доступны write_file и run_script — свои скрипты кладёшь "
            f"в {SANDBOX_DIR} и запускаешь их. На уровне 2 сеть отрезана "
            "и запуск идёт от пользователя, поэтому скрипт самодостаточен: "
            "входные файлы передавай через параметр inputs.\n")

    system_prompt += ("\n\nТвои скилы (название — назначение). "
                      "Когда тема подходит — сначала read_skill(name):\n"
                      + skills_index_text())


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
    response = chat_with_tools(messages)
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


def call_hermes(messages, tools=None):
    """Вызывает LLM-провайдер Dex (DEX_API_URL, см. .env).

    Раньше был жёстко захардкожен Gateway (127.0.0.1:8642) + модель
    deepseek-v4-flash. Ключ GATEWAY_KEY перестал действовать: секции
    api_server в config.yaml нет, API_SERVER_KEY не задан → 401.
    Теперь дефолт — gemini-web2api на 8083 (бесплатный, без Gateway).

    Возвращает пару (text, tool_calls):
      - text — текст ответа либо None при ошибке провайдера;
      - tool_calls — список вызовов инструментов OpenAI-формата, пустой
        список, если модель ответила текстом.
    """
    payload = {
        "model": DEX_MODEL,
        "messages": messages,
        "max_tokens": 1000,
        "temperature": 0.7,
    }
    if tools:
        payload["tools"] = tools

    for attempt in (1, 2):
        result = None
        try:
            result = subprocess.run(
                ["curl", "-s", "-X", "POST",
                 DEX_API_URL,
                 "-H", "Content-Type: application/json",
                 "-H", "Authorization: " + "Bearer " + DEX_API_KEY,
                 "-d", json.dumps(payload)],
                capture_output=True, text=True, timeout=60
            )
            resp = json.loads(result.stdout)
            msg = resp["choices"][0]["message"]
            content = msg.get("content")
            tool_calls = msg.get("tool_calls") or []
        except Exception as e:
            log(f"LLM API error: {e}")
            if result is not None and result.stdout:
                log(f"Raw: {result.stdout[:200]}")
            return (None, [])

        text = (content or "").strip()

        # Инструменты отдаём сразу: на tool_calls ошибка Workspace
        # не накладывается, а повтор сломал бы вызов.
        if tool_calls:
            return (text, tool_calls)

        if _is_google_tool_error(text) and attempt == 1:
            log("LLM: ошибка Workspace от провайдера, повторяю запрос")
            time.sleep(1)
            continue

        if _is_google_tool_error(text):
            log("LLM: ошибка Workspace повторилась — отдаю честный ответ")
            return ("Не смог выполнить: провайдер на такие запросы отвечает "
                    "ошибкой Workspace. Сформулируй иначе — или сделай это сам.", [])
        return (text, [])
    return (None, [])


def chat_with_tools(messages, max_rounds=4):
    """Диалог с инструментами: пока модель просит tool_call — выполняем.

    Правила песочницы — см. dex_tools.py. Всегда возвращает строку
    либо None (если провайдер недоступен).
    """
    text = None
    for step in range(1, max_rounds + 1):
        text, tool_calls = call_hermes(messages, tools=active_tools())
        if text is None and not tool_calls:
            return None
        if not tool_calls:
            return text

        log(f"тулзы: шаг {step}/{max_rounds}, запросов {len(tool_calls)}")
        calls = []
        for i, tc in enumerate(tool_calls):
            fn = tc.get("function") or {}
            calls.append({
                "id": tc.get("id") or f"call_{i}",
                "type": "function",
                "function": {
                    "name": fn.get("name", "?"),
                    "arguments": fn.get("arguments", "{}"),
                },
            })
        messages.append({"role": "assistant",
                         "content": text or "",
                         "tool_calls": calls})

        for tc in calls:
            name = tc["function"]["name"]
            args = tc["function"]["arguments"]
            out = execute_tool(name, args)
            log(f"  -> {name}({args[:70]}) => {str(out)[:110]}")
            messages.append({"role": "tool",
                             "tool_call_id": tc["id"],
                             "content": out})

    # Лимит шагов: просим итоговый ответ уже без инструментов
    log("тулзы: превышен лимит шагов, запрашиваю итог без инструментов")
    text, _ = call_hermes(messages, tools=None)
    return text or "(не успел закончить: слишком много шагов подряд)"


def send_message(chat_id, text, buttons=None):
    """Отправляет сообщение в Telegram через Dex бота.

    buttons — список строк вида [("Да", "yes"), ("Нет", "no")];
    одна строка = один ряд inline-кнопок.
    """
    try:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": t, "callback_data": d} for t, d in row]
                    for row in buttons
                ]
            }
        result = subprocess.run(
            ["curl", "-s", "-X", "POST",
             f"https://api.telegram.org/bot{bot_token}/sendMessage",
             "-H", "Content-Type: application/json",
             "-d", json.dumps(payload)],
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
                if update.get("callback_query"):
                    try:
                        handle_callback(update["callback_query"])
                    except Exception as e:
                        log(f"callback error: {e}")
                else:
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
