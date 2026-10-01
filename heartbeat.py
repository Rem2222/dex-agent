#!/usr/bin/env python3
"""
Dex Heartbeat — проактивный тик агента
Запускается по cron каждые 10 минут.
Проверяет состояние, решает что делать, исполняет.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import yaml
from datetime import datetime, timezone, timedelta
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

# === NOTIFICATION POLICY ===
# В Telegram — только аномалии. Рутина копится в ежедневный дайджест.
# Кулдаун на повтор того же сообщения (сек).
NOTIFY_COOLDOWN_SECONDS = {
    "check_services": 6 * 3600,
    "check_backups": 6 * 3600,
    "check_updates": 24 * 3600,
    "check_disk": 6 * 3600,
    "check_tools": 24 * 3600,
    # Находки должны доезжать: раньше был дефолт 24ч — за сутки он
    # успевал исследовать 4 раза, а до Рома дошло бы одно.
    "explore_interest": 4 * 3600,
    # Выход на контакт — чаще отчётов, но всё равно не чаще раза в 6 ч
    "outreach": 6 * 3600,
}

# Инициативные сообщения ночью не шлём: разбудят. Пишутся, но не
# отправляются до утра (mark_notified не ставится — уйдёт после 08:00).
QUIET_ACTIONS = {"outreach", "explore_interest"}
QUIET_FROM, QUIET_TO = 0, 8   # часы по Ростову (UTC+3)


def _quiet_hours(action):
    """True, если для этого действия сейчас ночное окно."""
    if action not in QUIET_ACTIONS:
        return False
    hour = datetime.now(timezone(timedelta(hours=3))).hour
    return QUIET_FROM <= hour < QUIET_TO
DIGEST_INTERVAL_SECONDS = 24 * 3600
DIGEST_MAX_ENTRIES = 12

# === Драйвы: дефицит копится, насыщение сбрасывает ===
# Модель утверждена Романом 30.09: пока потребность не выполнена, уровень
# РАСТЁТ, а выполнение действия её УДОВЛЕТВОРЯЕТ и уровень падает.
# Раньше было ровно наоборот — драйв увеличивался ПОСЛЕ действия и никогда
# не убывал, поэтому оба упёрлись в потолок 1.0 и стали константой.
DRIVE_FLOOR = 0.10               # пол, чтобы не исчезал совсем
DRIVE_CEIL = 1.00                # потолок
DRIVE_STEP_IDLE = 0.15           # простой тик -> растёт любопытство
DRIVE_STEP_OVERDUE = 0.05        # просроченная обязанность -> растёт исполнительность
DRIVE_SAT_CURIOSITY = 0.40       # explore_interest -> сильное насыщение
DRIVE_SAT_DILIGENCE = 0.30       # выполненная обязанность -> насыщение
DRIVE_SAT_PARTIAL = 0.10         # любое другое действие -> частичное насыщение

# Обязанности (совпадает с интервалами в check_duty)
DUTY_KEYS = {"check_backups", "check_updates", "check_disk",
             "check_tools", "check_services"}

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

def load_env_file():
    """Подставляет KEY=VALUE из proactive/.env в os.environ (не перезаписывая).

    Раньше вызов был жёстко захардкожен на Gateway (порт 8642) с моделью
    deepseek-v4-flash. Ключ GATEWAY_KEY перестал действовать: секции
    api_server в config.yaml нет, API_SERVER_KEY не задан → 401.
    Провайдер теперь берётся из .env (дефолт — gemini-web2api :8083).
    """
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


load_env_file()

DEX_API_URL = os.environ.get("DEX_API_URL", "http://127.0.0.1:8083/v1/chat/completions")
DEX_API_KEY = os.environ.get("DEX_API_KEY", "sk-gemini")
DEX_MODEL = os.environ.get("DEX_MODEL", "gemini-3.5-flash")

def call_llm(system, prompt, max_tokens=20, db=None, tick=None, temperature=0.3):
    """Вызов LLM провайдера Dex (DEX_API_URL, см. .env). Если передан db — логирует запрос."""
    t0 = time.time()
    result = subprocess.run(
        ["curl", "-s", "-X", "POST",
         DEX_API_URL,
         "-H", "Content-Type: application/json",
         "-H", "Authorization: Bearer " + DEX_API_KEY,
         "-d", json.dumps({
             "model": DEX_MODEL,
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

def migrate_drives(db, drives):
    """Разовый сброс драйвов под новую модель (v2).

    Старая логика не имела убывания, поэтому curiosity и diligence давно
    упёрлись в 1.0 — градиента не было с чего начать. Ставим середину
    и помечаем миграцию, чтобы выполнить её ровно один раз.
    """
    if get_state(db, "drives_v2", False):
        return drives
    fresh = {"curiosity": 0.4, "diligence": 0.4}
    set_state(db, "drives", fresh)
    set_state(db, "drives_v2", True)
    log(f"Драйвы мигрированы на модель v2: {drives} -> {fresh}")
    return fresh

# === MAIN ===
# === ЗАДАЧИ ===
# Таблица была создана, но никто ей не пользовался (0 строк). Теперь это
# список дел, которые Dex заметил, но сам не может сделать: песочница
# разрешает только чтение, лечить может только Ром.


def _db_rows(db, sql, args=()):
    db.row_factory = sqlite3.Row
    return [dict(r) for r in db.execute(sql, args).fetchall()]


def task_add(db, what, source="heartbeat", result=None):
    """Открывает задачу. Дедупликация: такая же pending не плодится —
    иначе check_services завёл бы одну и ту же 20 раз подряд."""
    what = str(what or "").strip()
    if not what:
        return None
    row = db.execute(
        "SELECT id FROM tasks WHERE what=? AND status='pending' LIMIT 1",
        (what,)).fetchone()
    if row:
        return int(row[0])
    cur = db.execute(
        "INSERT INTO tasks(what, status, source, result, created_at) "
        "VALUES (?,?,?,?,?)",
        (what, "pending", source, result,
         datetime.now(timezone.utc).isoformat()))
    db.commit()
    tid = int(cur.lastrowid)
    # Ром об этом не узнает иначе: раньше задачи жили в БД в тишине
    try:
        send_telegram(f"📋 Новая задача <b>#{tid}</b>: {what}")
    except Exception as e:
        log(f"уведомление о задаче не ушло: {e}")
    return tid


def task_list(db, status="pending", limit=20):
    """Задачи как список dict: id, what, source, created_at, done_at."""
    return _db_rows(
        db,
        "SELECT id, what, status, source, result, created_at, done_at "
        "FROM tasks WHERE status=? ORDER BY id DESC LIMIT ?",
        (status, limit))


def task_done(db, task_id):
    """Закрывает задачу. Возвращает True, если нашли."""
    row = db.execute("SELECT id FROM tasks WHERE id=? AND status='pending'",
                     (task_id,)).fetchone()
    if not row:
        return False
    db.execute("UPDATE tasks SET status='done', done_at=?, result=? "
               "WHERE id=?",
               (datetime.now(timezone.utc).isoformat(),
                "закрыто вручную", task_id))
    db.commit()
    return True


def task_stats(db):
    """{"pending": N, "done": M, "oldest_hours": H|None}."""
    try:
        p = db.execute(
            "SELECT count(*) FROM tasks WHERE status='pending'").fetchone()[0]
        d = db.execute(
            "SELECT count(*) FROM tasks WHERE status='done'").fetchone()[0]
        row = db.execute(
            "SELECT min(created_at) FROM tasks WHERE status='pending'"
        ).fetchone()[0]
    except sqlite3.Error:
        return {"pending": 0, "done": 0, "oldest_hours": None}
    oldest = None
    if row:
        try:
            ts = datetime.fromisoformat(row)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            oldest = round((datetime.now(timezone.utc) - ts).total_seconds() / 3600, 1)
        except Exception:
            pass
    return {"pending": p, "done": d, "oldest_hours": oldest}


def maybe_file_task(db, decision, result):
    """Что-то заметили, а чинить нельзя — кладём задачу Рома.

    ВАЖНО: в `what` — только СТАБИЛЬНАЯ фраза, детали в `result`.
    Иначе дедупликация не работает: список неактивных юнитов меняется
    от тика к тику, и одна и та же проблема плодила бы задачи (#2 и #7).
    """
    text = str(result or "")
    try:
        if decision == "check_services" and "НЕ активны" in text:
            m = re.search(r"НЕ активны \d+ — (.+?)(?:;|$)", text)
            detail = m.group(1).strip() if m else text
            return task_add(db, "разобраться с неактивными сервисами",
                            "heartbeat", detail)
        if decision == "check_disk":
            m = re.search(r"\((\d+)% занято\)", text)
            if m and int(m.group(1)) >= 90:
                return task_add(db, "нехватка места на диске",
                                "heartbeat", f"{m.group(1)}% занято; {text}")
        if decision == "check_backups" and "нет файлов бэкапов" in text:
            return task_add(db, "бэкапы не найдены — проверить pg-backup",
                            "heartbeat", text)
    except sqlite3.Error:
        return None
    return None


def _disable_expired():
    """Снимает DISABLED, если истекла отложенная пауза (/pause 30).

    В файле лежит 'until=<ISO>'. Флажок без until ведёт себя как раньше —
    вечно, пока не пришлют /resume.
    """
    try:
        txt = DISABLED_FLAG.read_text()
    except OSError:
        return False
    if "until=" not in txt:
        return False
    try:
        until = datetime.fromisoformat(txt.split("until=", 1)[1].strip())
    except Exception:
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) >= until:
        try:
            DISABLED_FLAG.unlink()
        except OSError:
            return False
        return True
    return False


def main():
    # 1. Красная кнопка
    if DISABLED_FLAG.exists():
        if _disable_expired():
            log("Пауза истекла — снял DISABLED, продолжаю")
        else:
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
    drives = migrate_drives(db, get_state(db, "drives", {"curiosity": 0.5, "diligence": 0.5}))
    duty_due = check_duty(db, identity)

    # Дефицит: пока обязанность не сделана, исполнительность копится
    if duty_due:
        drives["diligence"] = round(min(DRIVE_CEIL,
                                        drives.get("diligence", 0.5) + DRIVE_STEP_OVERDUE), 2)
        set_state(db, "drives", drives)

    # 4. Собираем промпт для решения
    prompt_parts = [
        f"Ты Dex — смотритель сервера. Тик #{tick_num}.",
        f"Твой текущий фокус: {focus}",
        f"Уровень любопытства: {drives.get('curiosity', 0.5)}",
        f"Уровень исполнительности: {drives.get('diligence', 0.5)}",
    ]
    # Драйвы должны ВЛИЯТЬ на выбор, а не быть декорацией: голые числа LLM
    # не учитывает, поэтому при высоком уровне даём прямую подсказку.
    if drives.get("curiosity", 0) >= 0.7:
        prompt_parts.append(
            f"Драйв любопытства высокий ({drives['curiosity']:.2f}) — "
            "если ничего не срочно, выбери explore_interest.")
    if drives.get("diligence", 0) >= 0.7:
        prompt_parts.append(
            f"Драйв исполнительности высокий ({drives['diligence']:.2f}) — "
            "выбери одну из обязанностей в списке «Пора проверить».")
    if duty_due:
        prompt_parts.append(f"Пора проверить: {', '.join(duty_due)}")
    # Открытые задачи: влияют и на выбор, и на исполнительность.
    # Просрочка старше суток копит драйв — потребность не насыщена.
    tstats = task_stats(db)
    if tstats["pending"]:
        prompt_parts.append(
            f"Открытых задач: {tstats['pending']} (старшая — "
            f"{tstats['oldest_hours']}ч). Если ничего не срочно, можно "
            "взяться за одну — выбери check_services или check_disk.")
        if (tstats["oldest_hours"] or 0) >= 24:
            drives["diligence"] = round(min(
                DRIVE_CEIL,
                drives.get("diligence", 0.5) + DRIVE_STEP_OVERDUE), 2)
            set_state(db, "drives", drives)
    if history:
        prompt_parts.append("Последние тики:")
        for h in history:
            prompt_parts.append(f"  - {h.get('action', 'ничего')} → {h.get('result', '?')}")
    # Что реально можно выбрать в ЭТОМ тике: привычка/исследование всегда,
    # обязанности — только те, что подошли по интервалу.
    available = ["none", "explore_interest"] + list(duty_due)
    # Выход на контакт — только когда накоплен интерес И есть материал
    if drives.get("curiosity", 0) >= 0.6 and _outreach_material(db):
        available.append("outreach")
    if "outreach" in available:
        prompt_parts.append(
            "Есть материал для Рома (открытая задача или свежая находка) — "
            "можно выйти на контакт, выбери outreach. Если полезного нечего "
            "сказать — выбери none.")
    prompt_parts.append(
        "Что делаем в этом тике? Ответь ТОЛЬКО одним словом — одним из: "
        + ", ".join(available)
        + ". Никаких других слов, никаких пояснений. Обязанности, которых "
        "нет в этом списке, уже сделаны — не повторяй их.")
    prompt = "\n".join(prompt_parts)

    # 5. Зовём LLM
    decision = call_llm(
        "Ты — серверный помощник Dex. Отвечаешь ТОЛЬКО одним словом из списка, "
        "который дан в задании. Никаких других слов.",
        prompt,
        max_tokens=20,
        db=db,
        tick=tick_num
    )
    if not decision or decision.strip() == "none":
        log("Dex решил ничего не делать в этом тике")
        set_state(db, "current_focus", "nothing")
        # Простой: потребность не насыщена, уровень растёт
        drives["curiosity"] = round(min(DRIVE_CEIL,
                                        drives.get("curiosity", 0.5) + DRIVE_STEP_IDLE), 2)
        set_state(db, "drives", drives)
        write_tick({"tick": tick_num, "action": "none", "result": "ok", "ts": datetime.now(timezone.utc).isoformat()})
        return

    # Берём только первое слово ответа
    decision = decision.strip().lower().split()[0] if decision.strip() else "none"
    valid_choices = {"check_updates", "check_backups", "check_disk", "check_tools", "check_services", "explore_interest", "outreach", "none"}
    if decision not in valid_choices:
        log(f"Dex ответил невалидным ключом: {decision}, пропускаю тик")
        set_state(db, "current_focus", "nothing")
        write_tick({"tick": tick_num, "action": "none", "result": f"bogus: {decision}", "ts": datetime.now(timezone.utc).isoformat()})
        return

    # Страховка: если LLM выбрал обязанность вне расписания — не выполняем,
    # тик уходит в простой (тогда curiosity растёт).
    if decision in DUTY_KEYS and decision not in duty_due:
        log(f"повтор {decision} вне интервала — пропускаю")
        set_state(db, "current_focus", "nothing")
        drives["curiosity"] = round(min(DRIVE_CEIL,
                                        drives.get("curiosity", 0.5) + DRIVE_STEP_IDLE), 2)
        set_state(db, "drives", drives)
        write_tick({"tick": tick_num, "action": "none",
                    "result": f"повтор {decision} вне интервала",
                    "ts": datetime.now(timezone.utc).isoformat()})
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
    elif decision == "outreach":
        result = execute_outreach(db)
    else:
        result = f"неизвестная команда: {decision}"

    # Замеченное, но неисправимое — в задачи (дедуп по тексту)
    maybe_file_task(db, decision, result)

    if decision == "outreach" and not str(result or "").strip():
        # Материал иссяк или LLM решил нечего говорить — молчим,
        # тик идёт в простой и interest растёт дальше.
        log("outreach: полезного нечего сказать — пропускаю")
        set_state(db, "current_focus", "nothing")
        drives["curiosity"] = round(min(DRIVE_CEIL,
                                        drives.get("curiosity", 0.5) + DRIVE_STEP_IDLE), 2)
        set_state(db, "drives", drives)
        write_tick({"tick": tick_num, "action": "none",
                    "result": "outreach: нечего сказать",
                    "ts": datetime.now(timezone.utc).isoformat()})
        return

    # Отметили выполнение обязанности — БЕЗ ЭТОГО check_duty считает
    # last_check = 0 и каждый тик возвращает все пять, то есть
    # «Пора проверить» висит всегда и LLM повторяет одно и то же.
    if decision in DUTY_KEYS:
        dc = get_state(db, "duty_checks", {})
        dc[decision] = datetime.now(timezone.utc).timestamp()
        set_state(db, "duty_checks", dc)

    # 7. Логируем результат
    log(f"Результат: {result}")
    set_state(db, "current_focus", "nothing")
    # Насыщение: выполненное действие гасит потребность — уровень ПАДАЕТ
    if decision in DUTY_KEYS:
        drives["diligence"] = round(max(DRIVE_FLOOR,
                                        drives.get("diligence", 0.5) - DRIVE_SAT_DILIGENCE), 2)
    if decision in ("explore_interest", "outreach"):
        drives["curiosity"] = round(max(DRIVE_FLOOR,
                                        drives.get("curiosity", 0.5) - DRIVE_SAT_CURIOSITY), 2)
    elif decision not in DUTY_KEYS:
        # Обязанность любопытство НЕ гасит: проверка диска никак не связана
        # с интересом. Раньше это была ветка else — и она ловила все 5
        # обязанностей, то есть 84% тиков. Из-за этого curiosity держался
        # на поле (среднее 0.14), а подсказка ≥0.7 не срабатывала ни разу.
        drives["curiosity"] = round(max(DRIVE_FLOOR,
                                        drives.get("curiosity", 0.5) - DRIVE_SAT_PARTIAL), 2)
    set_state(db, "drives", drives)
    write_tick({"tick": tick_num, "action": decision, "result": result[:200], "ts": datetime.now(timezone.utc).isoformat()})

    # 8. Отчёт в Telegram — на каждое действие, кроме none.
    #    Обещание дано 30.09 («на каждый тик отписываться, если ты что-то делаешь»).
    #    Кулдаун на идентичный текст не даёт заспамить: 15 одинаковых проверок = 1 сообщение.
    anomaly = format_notification(decision, result)
    icon = {"check_disk": "💾", "check_backups": "💿", "check_updates": "🔄",
            "check_services": "🔍", "check_tools": "🔧",
            "explore_interest": "🧠", "outreach": "💬"}.get(decision, "▫️")
    if decision == "outreach":
        # outreach — уже готовое обращение, шапка «check_x:» его убьёт
        notify = result
    else:
        notify = anomaly or f"{icon} <b>{decision}</b>: {result}"

    if _quiet_hours(decision):
        # Не шлём и НЕ отмечаем — уйдёт после 08:00, кулдаун считается
        # от последней реально отправленной копии.
        log(f"тихие часы (МСК {datetime.now(timezone(timedelta(hours=3))):%H:%M}) — {decision} не отправляю")
    elif should_notify(db, decision, notify):
        if send_telegram(notify):
            mark_notified(db, decision, notify)
            log(f"Telegram: отчёт по {decision} отправлен")
        else:
            log(f"Telegram: не удалось отправить отчёт по {decision}")
    else:
        log(f"Telegram: {decision} — кулдаун, пропускаю (уже слали то же)")

    # Дайджест — отдельный сводный отчёт раз в сутки (оставлен намеренно)
    add_digest_entry(db, decision, result)
    maybe_send_digest(db)

def should_notify(db, action, text):
    """Кулдаун: не слать то же сообщение чаще раза в N часов."""
    last = get_state(db, "last_notify", {})
    entry = last.get(action)
    cooldown = NOTIFY_COOLDOWN_SECONDS.get(action, 24 * 3600)
    now = time.time()
    if entry:
        # То же самое сообщение в пределах кулдауна — не дублируем
        if entry.get("text") == text and now - entry.get("ts", 0) < cooldown:
            return False
    return True

def mark_notified(db, action, text):
    last = get_state(db, "last_notify", {})
    last[action] = {"ts": time.time(), "text": text}
    set_state(db, "last_notify", last)

def add_digest_entry(db, action, result):
    if not result:
        return
    entries = get_state(db, "digest_entries", [])
    # Дедуп: то же действие с тем же результатом не дублируем
    for e in entries:
        if e.get("action") == action and e.get("result") == result:
            return
    entries.append({"action": action, "result": result[:150], "ts": datetime.now(timezone.utc).isoformat()})
    set_state(db, "digest_entries", entries[-DIGEST_MAX_ENTRIES:])

def maybe_send_digest(db):
    """Раз в сутки шлёт сводку рутинных результатов."""
    last = get_state(db, "last_digest_sent", 0)
    entries = get_state(db, "digest_entries", [])
    if not entries:
        return
    if time.time() - last < DIGEST_INTERVAL_SECONDS:
        return
    lines = []
    for e in entries[-DIGEST_MAX_ENTRIES:]:
        ts = e.get("ts", "").replace("T", " ")[5:16]  # MM-DD HH:MM
        lines.append(f"• {ts} {e.get('action','?')}: {e.get('result','')}")
    text = "📊 <b>Dex — дайджест за сутки</b>\n" + "\n".join(lines)
    ok = send_telegram(text)
    if ok:
        set_state(db, "last_digest_sent", time.time())
        set_state(db, "digest_entries", [])

def format_notification(action, result):
    """Форматирует результат для Telegram. Возвращает None, если слать нечего.
    Правило: в Telegram — только аномалии. Рутина уходит в дайджест."""
    if not result:
        return None
    if action == "check_disk":
        # Шлём только если места мало (<=10G или <=10%)
        if "свободно" in result:
            parts = result.split()
            for i, p in enumerate(parts):
                if p == "свободно" and i > 0:
                    free = parts[i-1].rstrip(',')
                    if free.endswith('G') and float(free[:-1]) > 10:
                        return None
                    if free.endswith('%') and float(free[:-1]) > 10:
                        return None
        return f"💾 <b>Диск</b>: {result}"
    if action == "check_backups":
        # Аномалия: бэкапов нет или ошибка
        if "нет файлов" in result or "не найдена" in result or "ошибка" in result:
            return f"💿 <b>Бэкапы</b>: {result}"
        return None
    if action == "check_updates":
        # Рутина — доступные обновления не аномалия, уходит в дайджест
        return None
    if action == "check_tools":
        if "запланирован" in result:
            return None
        return f"🔧 <b>Инструменты</b>: {result}"
    if action == "check_services":
        # Аномалия: сервис не active, контейнеров 0 или ошибка
        if "inactive" in result or "failed" in result or "контейнеров: 0" in result or "ошибка" in result:
            return f"🔍 <b>Сервисы</b>: {result}"
        return None
    return None

def execute_check_backups():
    log("Проверяю бэкапы...")
    backup_dir = Path("/root/backups")
    if not backup_dir.exists():
        return "директория бэкапов не найдена"
    # Глобы должны видеть и .enc (шифрованные), и .tgz — иначе после
    # шифрования ночного прогона проверка врала «нет файлов бэкапов»
    # и заводила ложную задачу (случай 30.09, 21:30 UTC).
    files = []
    for f in backup_dir.iterdir():
        if not f.is_file():
            continue
        name = f.name[:-4] if f.name.endswith(".enc") else f.name
        if name.endswith((".sql.gz", ".tar.gz", ".tgz")):
            files.append(f)
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

def _script_snapshot():
    """Имена исполняемых скриптов в каталогах, где живут инструменты."""
    now = {}
    for d in (Path("/usr/local/bin"), Path("/root/.hermes/scripts")):
        if not d.exists():
            continue
        for f in d.iterdir():
            if f.is_file() and os.access(f, os.X_OK):
                now[str(f)] = int(f.stat().st_mtime)
    return now


def execute_check_tools(db=None):
    """Реальный поиск новых инструментов: diff снимка скриптов с прошлым тиком.

    Раньше была заглушка («поиск инструментов запланирован, пропускаю этот
    тик») — тик расходовался впустую 6 раз за день.
    """
    log("Ищу новые инструменты...")
    now = _script_snapshot()
    if not now:
        return "каталоги скриптов не найдены"

    own = db is not None
    if not own:
        db = sqlite3.connect(DB_PATH)
    try:
        prev = get_state(db, "tools_snapshot", {})
        if not prev:
            set_state(db, "tools_snapshot", now)
            return f"базовый снимок: {len(now)} скриптов, начинаю отслеживать"

        added = sorted(k for k in now if k not in prev)
        removed = sorted(k for k in prev if k not in now)
        updated = sorted(k for k in now if k in prev and now[k] != prev[k])
        set_state(db, "tools_snapshot", now)
    finally:
        if not own:
            db.close()

    if not (added or removed or updated):
        return f"скриптов: {len(now)}, изменений с прошлого раза нет"

    parts = []
    if added:
        parts.append("новые: " + ", ".join(Path(k).name for k in added[:8]))
    if removed:
        parts.append("убраны: " + ", ".join(Path(k).name for k in removed[:8]))
    if updated:
        parts.append("изменены: " + ", ".join(Path(k).name for k in updated[:8]))
    return f"всего {len(now)} скриптов. " + "; ".join(parts)


def _registry_units():
    """Считывает systemd_units со всех страниц реестра сервисов в вики."""
    import re
    reg = Path.home() / "Documents" / "wiki" / "ops" / "services"
    units, pages = set(), 0
    if not reg.exists():
        return pages, sorted(units)
    for md in reg.glob("*.md"):
        pages += 1
        try:
            txt = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # инлайн-вид:  systemd_units: [freeqwenapi, dex-control]
        for m in re.finditer(r"systemd_units:\s*\[([^\]]*)\]", txt):
            for u in m.group(1).split(","):
                u = u.strip().strip("'\"")
                if u:
                    units.add(u)
        # блочный вид:  systemd_units:\n    - dex-poller.service
        for m in re.finditer(r"systemd_units:\s*\n((?:\s*-\s*\S+\s*\n)+)", txt):
            for line in m.group(1).splitlines():
                u = line.strip().lstrip("-").strip()
                if u:
                    units.add(u)
    # Жёсткая валидация: имя юнита уходит в argv systemctl. Без неё
    # запись из вики вида '--version' или 'a b' стала бы флагом либо
    # лишним аргументом. Разрешены буквы, цифры и _.@-
    units = {u for u in units
             if re.fullmatch(r"[A-Za-z0-9_.@\-]{1,64}", u)}
    return pages, sorted(units)


def _xdg_env():
    """XDG_RUNTIME_DIR для systemctl --user: без него «No medium found»."""
    env = os.environ.copy()
    if not env.get("XDG_RUNTIME_DIR"):
        cand = f"/run/user/{os.getuid()}"
        if os.path.isdir(cand):
            env["XDG_RUNTIME_DIR"] = cand
    return env


def _user_unit_names():
    """Имена unit-файлов, известных пользовательскому менеджеру systemd."""
    try:
        out = subprocess.run(
            ["systemctl", "--user", "list-unit-files", "--no-legend",
             "--plain", "--no-pager"],
            capture_output=True, text=True, timeout=15,
            env=_xdg_env()
        ).stdout
    except Exception:
        return set()
    return {ln.split()[0] for ln in out.splitlines() if ln.strip()}


def _unit_states(units):
    """-> {юнит: состояние}. Пользовательские юниты проверяем через --user.

    Без этого dex-poller и dex-control числились «inactive», хотя работают:
    они установлены в менеджере сеанса, а не в системном.
    """
    # вторая линия обороны: чистим и здесь
    units = [u for u in units
             if re.fullmatch(r"[A-Za-z0-9_.@\-]{1,64}", str(u))]
    user_files = _user_unit_names()

    def is_user(u):
        return u in user_files or u + ".service" in user_files

    states = {}
    for flag, group in ((["--user"], [u for u in units if is_user(u)]),
                        ([], [u for u in units if not is_user(u)])):
        if not group:
            continue
        r = subprocess.run(["systemctl", *flag, "is-active", *group],
                           capture_output=True, text=True, timeout=30,
                           env=_xdg_env())
        vals = [ln.strip() for ln in r.stdout.splitlines()]
        if len(vals) != len(group):
            # ответ неполный — по одному, чтобы не приписать чужое состояние
            vals = []
            for u in group:
                q = subprocess.run(["systemctl", *flag, "is-active", u],
                                   capture_output=True, text=True, timeout=5,
                                   env=_xdg_env())
                vals.append(q.stdout.strip())
        for u, st in zip(group, vals):
            states[u] = (st or "unknown").strip()
    return states


def execute_check_services():
    """Проверяет ВСЕ сервисы из реестра вики, а не один юнит.

    Раньше проверял только `is-active dex-poller` и число контейнеров,
    то есть 1 сервис из 41.
    """
    log("Проверяю сервисы по реестру вики...")
    try:
        pages, units = _registry_units()
        if not units:
            return f"реестр: {pages} страниц, systemd_units не найдены"

        states = _unit_states(units)
        ok, bad = [], []
        for u in units:
            st = states.get(u, "unknown")
            if st in ("active", "activating", "reloading"):
                ok.append(u)
            else:
                bad.append(f"{u}={st}")

        try:
            d = subprocess.run(["docker", "ps", "-a", "--format",
                                "{{.Names}}\t{{.Status}}"],
                               capture_output=True, text=True, timeout=15)
            rows = [ln.split("\t") for ln in d.stdout.splitlines() if ln.strip()]
            containers = len(rows)
            down = [r0[0] for r0 in rows
                    if len(r0) > 1 and not r0[1].lower().startswith("up")]
        except Exception:
            containers, down = -1, []

        base = f"реестр: {pages} стр., юнитов {len(units)}: активны {len(ok)}"
        if bad:
            base += f", НЕ активны {len(bad)} — " + ", ".join(bad[:6])
        if containers >= 0:
            base += (f"; docker: {containers} "
                     f"(не запущены: {', '.join(down[:6]) or 'нет'})")
        return base
    except Exception as e:
        return f"ошибка проверки сервисов: {type(e).__name__}: {e}"


def execute_explore_interest(identity, db):
    """Реальное исследование: LLM даёт короткое наблюдение по теме.

    Раньше была заглушка — ставила timestamp и возвращала «посмотрю что
    нового в: ...», ничего не изучая.
    """
    interests = (identity or {}).get("interests", []) or []
    flat = []
    for item in interests:
        if isinstance(item, dict):
            flat.extend(str(v) for v in item.values() if v)
        else:
            flat.append(str(item))
    flat = [i for i in flat if i]
    if not flat:
        return "нет интересов для изучения"

    # круговой выбор, а не случайный: иначе часть тем никогда не выпадет
    cursor = int(get_state(db, "explore_cursor", 0)) % len(flat)
    interest = flat[cursor]
    set_state(db, "explore_cursor", (cursor + 1) % len(flat))

    log(f"Изучаю: {interest}")
    prompt = (
        f"Тема для исследования: {interest}\n"
        "Контекст: я — Dex, смотритель VPS. На сервере работают Hermes, "
        "Dex, Multica, OpenViking с векторной памятью, есть вики на markdown.\n"
        "Дай ОДНО конкретное наблюдение по этой теме: 1-2 предложения, "
        "по-русски, конкретно, без общих слов.\n"
        "И добавь отдельной последней строкой ОДИН полезный URL по теме "
        "(документация, релиз, статья) — формат: URL: https://...\n"
        "URL обязан быть публичным: localhost, приватные сети и 127.0.0.1 "
        "не подойдут, запрос всё равно отклонят. Если полезной страницы "
        "нет — строку URL не пиши."
    )
    note = call_llm(
        "Ты — любопытный серверный помощник Dex. Отвечаешь по-русски, "
        "коротко и конкретно, без иероглифов и без воды.",
        prompt,
        max_tokens=250,
        db=db,
    )
    if not note or not str(note).strip():
        note = "LLM недоступна — наблюдение не получено"
    raw_note = str(note).strip()
    # URL ищем ДО обрезки — иначе адрес, стоящий последним, отрезается
    import re as _re
    m = _re.search(r"https?://[^\s)>\]]+", raw_note)
    note = " ".join(raw_note.split())[:300]

    # Данные вместо одного мнения: если модель назвала страницу — читаем.
    # Ошибки сети не должны ронять тик, поэтому только прибавляем.
    if m:
        url = m.group(0).rstrip(".,;")
        try:
            from dex_tools import tool_fetch_url
            page = tool_fetch_url(url, lines=25)
            if page and not str(page).startswith(("заблокировано", "не смог")):
                snippet = " ".join(str(page).split())[:400]
                note = f"{note}\nДанные с {url}: {snippet}"
            else:
                note = f"{note}\nСтраница {url} не прочитана: {str(page)[:120]}"
        except Exception as e:
            note = f"{note}\n(страницу прочитать не удалось: {e})"
        note = note[:700]

    history = get_state(db, "explorations", [])
    if not isinstance(history, list):
        history = []
    history.append({
        "interest": interest,
        "note": note[:600],
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    set_state(db, "explorations", history[-20:])

    last = get_state(db, "last_explored_interest", {})
    if not isinstance(last, dict):
        last = {}
    last[interest] = datetime.now(timezone.utc).isoformat()
    set_state(db, "last_explored_interest", last)

    return f"{interest}: {note[:700]}"


def _outreach_material(db):
    """Есть ли что сказать: открытая задача или свежая находка (<24 ч)."""
    try:
        if task_stats(db)["pending"]:
            return True
    except Exception:
        pass
    exp = get_state(db, "explorations", [])
    if not isinstance(exp, list) or not exp:
        return False
    try:
        ts = exp[-1].get("ts", "")
        age = datetime.now(timezone.utc) - datetime.fromisoformat(ts)
        return age.total_seconds() < 24 * 3600
    except Exception:
        return True


def execute_outreach(db):
    """Выход на контакт: ОДНО сообщение от первого лица, только по фактам.

    Если полезного нечего сказать — возвращает пустую строку, и тик
    уходит в простой (никакой генерации пустоты).
    """
    facts = []
    for r in task_list(db, "pending", 5):
        facts.append(f"открытая задача #{r['id']} ({str(r.get('created_at'))[:16]}): "
                     f"{r.get('what')}")
    exp = get_state(db, "explorations", [])
    if isinstance(exp, list) and exp:
        e = exp[-1]
        facts.append(f"последнее исследование — {e.get('interest')}: {e.get('note')}")
    hist = read_tick_history(2)
    for t in hist:
        facts.append(f"тик: {t.get('action')} → {t.get('result')}")
    if not facts:
        return ""

    prompt = (
        "Напиши ОДНО короткое сообщение Рому — владельцу сервера — от первого лица.\n"
        "Требования: без шапки и без HTML-разметки, без списка эмодзи, "
        "1–2 предложения, строго по фактам ниже, живо, но без воды и без лести. "
        "Это не отчёт, а обращение: скажи то, что стоит знать именно сейчас.\n"
        "Если по этим фактам полезного сказать нечего — ответь одним словом: NONE\n\n"
        "Факты:\n" + "\n".join("- " + f for f in facts))
    txt = call_llm(
        "Ты — Dex, смотритель сервера. Пишешь Рому короткие сообщения "
        "по-русски, по делу, без разметки и без лести.",
        prompt, max_tokens=200, db=db)
    if not txt:
        return ""
    txt = str(txt).strip()
    if txt.upper().startswith("NONE") or len(txt) < 15:
        return ""
    return " ".join(txt.split())[:400]


if __name__ == "__main__":
    main()
