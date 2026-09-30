#!/usr/bin/env python3
"""
Инструменты Dex: песочница (allowlist) + схемы tool calling.

Dex работает от root, поэтому доступ строго ограничен:
  - только чтение, только каталоги из ALLOWED_ROOTS
  - запрет на «..» и на пути, содержащие секреты (DENY_SUBSTRINGS)
  - лимит размера результата MAX_RESULT символов

Схемы TOOLS отправляются в gemini-web2api блоком "tools" в запросе
/v1/chat/completions. Провайдер возвращает tool_calls в формате
OpenAI — см. chat_with_tools() в dex_poller.py.
"""
import json
import os
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path

# === ПЕСОНИЦА ===
ALLOWED_ROOTS = (
    "/root/.hermes/proactive",   # сам Dex: код, логи, база
    "/root/.hermes/scripts",     # его же чеки
    "/root/backups",             # проверка бэкапов
    "/var/log",                  # журналы сервисов
    "/etc",                      # конфиги системы (без секретов)
    "/root/Documents/wiki/ops/services",  # реестр сервисов
)

DENY_SUBSTRINGS = (
    "shadow", "gshadow", "sudoers", "ssh", "id_rsa", "id_ed25519",
    ".env", "auth.json", "credentials", "secret", "token", "api-key",
    "apikey", "private", "password", "passwd",
)

MAX_RESULT = 4000
MAX_LINES = 200

SKILLS_DIR = Path(__file__).resolve().parent / "skills"


def _skill_meta(raw, fallback_name):
    """Минимальный разбор YAML-фронтматтера скила (без PyYAML)."""
    meta = {"name": fallback_name, "description": "", "when": ""}
    if not raw.startswith("---"):
        return meta
    end = raw.find("\n---", 3)
    if end == -1:
        return meta
    for line in raw[3:end].strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            k, v = k.strip(), v.strip()
            if k in meta and v:
                meta[k] = v
    return meta


def _iter_skills():
    if not SKILLS_DIR.is_dir():
        return
    for d in sorted(SKILLS_DIR.iterdir()):
        f = d / "SKILL.md"
        if d.is_dir() and f.is_file():
            yield d.name, f


def skills_index_text():
    """Короткий индекс скилов для system prompt: «- имя — описание»."""
    rows = []
    for name, f in _iter_skills():
        try:
            meta = _skill_meta(f.read_text(encoding="utf-8"), name)
        except OSError:
            continue
        desc = meta["description"] or "без описания"
        rows.append(f"- {name} — {desc}")
    if not rows:
        return "  (скилов пока нет)"
    return "\n".join(rows)


def tool_list_skills():
    """Список доступных скилов с описанием и условием применения."""
    rows = []
    for name, f in _iter_skills():
        try:
            meta = _skill_meta(f.read_text(encoding="utf-8"), name)
        except OSError:
            continue
        line = f"{name}: {meta['description'] or 'без описания'}"
        if meta["when"]:
            line += f" | применять: {meta['when']}"
        rows.append(line)
    if not rows:
        return "скилов нет. Создай каталог skills/<имя>/SKILL.md"
    return f"доступно скилов: {len(rows)}\n" + "\n".join(rows)


def tool_read_skill(name):
    """Отдаёт содержимое скила. Имя — только буквы/дефис, без путей."""
    name = str(name or "").strip()
    if not name or not all(c.isalnum() or c in "-_" for c in name):
        return "недопустимое имя скила: разрешены только буквы, цифры, дефис"
    f = SKILLS_DIR / name / "SKILL.md"
    try:
        resolved = f.resolve()
    except OSError as e:
        return f"не удалось разрешить путь: {e}"
    if not str(resolved).startswith(str(SKILLS_DIR.resolve())):
        return "отказано песочницей: выход за пределы каталога скилов"
    if not resolved.is_file():
        return f"скил '{name}' не найден. Сначала list_skills."
    return _clip(resolved.read_text(encoding="utf-8", errors="replace"))



# === СХЕМЫ ДЛЯ ПРОВАЙДЕРА ===
TOOLS = [
    {
        "name": "read_state",
        "description": "Состояние сервера и Dex: номер тика, драйвы, свободное место, последние тики. Используй, когда спрашивают про текущее положение дел.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "run_check",
        "description": "Выполнить один из встроенных чеков сервера. Возвращает текстовый результат.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "enum": ["backups", "updates", "disk", "services", "tools", "interest"],
                    "description": "Какой чек выполнить: backups=свежесть бэкапов, updates=доступные обновления apt, disk=свободное место, services=состояние сервисов из реестра, tools=новые скрипты, interest=исследовать одну из тем своих интересов",
                }
            },
            "required": ["name"],
        },
    },
    {
        "name": "read_file",
        "description": "Прочитать первые строки файла (только чтение, только разрешённые каталоги).",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Абсолютный путь к файлу"},
                "limit": {"type": "integer", "description": "Сколько строк прочитать, по умолчанию 80, максимум 200"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_dir",
        "description": "Показать содержимое каталога: имена, тип, размер.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Абсолютный путь к каталогу"},
                "limit": {"type": "integer", "description": "Максимум записей, по умолчанию 60"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_skills",
        "description": "Список процедур (скилов), которые ты умеешь применять: диагностика сервисов, нехватка места, разбор изменений.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "read_skill",
        "description": "Прочитать выбранную процедуру целиком — по ней дальше и действовать.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Имя скила из list_skills"}
            },
            "required": ["name"],
        },
    },
    {
        "name": "tasks",
        "description": "Твои задачи. add — когда заметил проблему, которую сам не можешь устранить (песочница только читает); list — что висит; done — закрыть по номеру.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "add", "done"]},
                "what": {"type": "string", "description": "Текст задачи, только для action=add"},
                "id": {"type": "integer", "description": "Номер задачи, только для action=done"}
            },
            "required": ["action"],
        },
    },
    {
        "name": "run_cmd",
        "description": "Выполнить проверенную заранее команду из списка шаблонов. НЕ произвольная строка: ты выбираешь шаблон и подставляешь параметры.",
        "parameters": {
            "type": "object",
            "properties": {
                "template": {
                    "type": "string",
                    "enum": ["journalctl-unit", "journalctl-since",
                             "systemctl-status", "systemctl-user-status",
                             "docker-logs", "docker-inspect",
                             "git-log", "git-status",
                             "net-ports", "memory", "load",
                             "top-by-mem", "disk-inodes", "docker-stats"],
                    "description": "Какой шаблон выполнить"
                },
                "args": {
                    "type": "object",
                    "description": "Параметры шаблона: например unit=dex-poller, lines=50, name=gemini-web2api",
                }
            },
            "required": ["template"],
        },
    },
    {
        "name": "write_file",
        "description": "Записать файл. Уровень 2: только sandbox/ и skills/. Уровень 1: весь /root/.hermes. Уровень 3: отключено.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Абсолютный путь"},
                "content": {"type": "string", "description": "Содержимое файла целиком"}
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "run_script",
        "description": "Запустить свой Python-скрипт из sandbox. Уровень 2: от пользователя, без сети, с лимитами. Уровень 1: от root. Уровень 3: отключено.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Имя .py в sandbox, например parse_log.py"},
                "inputs": {"type": "array", "items": {"type": "string"},
                           "description": "Файлы-входы: будут скопированы в sandbox/_in и доступны скрипту"}
            },
            "required": ["name"],
        },
    },
    {
        "name": "fetch_url",
        "description": "Прочитать веб-страницу и получить её текст. Только публичные адреса: localhost, приватные сети и метаданные облака заблокированы.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Полный URL, http:// или https://"},
                "lines": {"type": "integer", "description": "Сколько строк вернуть, до 400 (по умолчанию 200)"},
                "as_text": {"type": "boolean", "description": "true — вернуть чистый текст без разметки (по умолчанию)"}
            },
            "required": ["url"],
        },
    },
    {
        "name": "tail_log",
        "description": "Прочитать последние строки файла (журнал, лог, JSONL).",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Абсолютный путь к файлу"},
                "lines": {"type": "integer", "description": "Сколько последних строк, по умолчанию 40, максимум 200"},
            },
            "required": ["path"],
        },
    },
]


class SandboxError(Exception):
    """Путь вне песочницы или содержит секрет."""


def safe_path(raw):
    """Возвращает Path, если путь разрешён песочницей. Иначе SandboxError."""
    if not raw or not isinstance(raw, str):
        raise SandboxError("путь не указан")
    if "\x00" in raw:
        raise SandboxError("недопустимый символ в пути")

    raw = raw.strip()
    if not raw.startswith("/"):
        raise SandboxError("путь должен быть абсолютным")

    resolved = Path(raw).resolve()
    text = str(resolved)

    if ".." in raw:
        raise SandboxError("'..' запрещён")

    low = text.lower()
    for deny in DENY_SUBSTRINGS:
        if deny in low:
            raise SandboxError(f"доступ к '{deny}' закрыт песочницей")

    if not any(text == root or text.startswith(root + "/") for root in ALLOWED_ROOTS):
        raise SandboxError(
            "каталог вне списка разрешённых: " + ", ".join(ALLOWED_ROOTS)
        )
    return resolved


def _clip(text):
    if len(text) <= MAX_RESULT:
        return text
    return text[:MAX_RESULT] + f"\n... [обрезано: {len(text)} символов всего]"


def tool_read_file(path, limit=80):
    p = safe_path(path)
    if not p.is_file():
        return f"это не файл: {p}"
    limit = max(1, min(int(limit), MAX_LINES))
    lines = p.read_text(errors="replace").splitlines()[:limit]
    total = len(p.read_text(errors="replace").splitlines())
    head = f"# {p} — строк: {total}, показано: {len(lines)}\n"
    return _clip(head + "\n".join(lines))


def tool_list_dir(path, limit=60):
    p = safe_path(path)
    if not p.is_dir():
        return f"это не каталог: {p}"
    limit = max(1, min(int(limit), MAX_LINES))
    entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name))
    out = []
    for e in entries[:limit]:
        if e.is_dir():
            out.append(f"[дир]  {e.name}")
        else:
            try:
                size = e.stat().st_size
            except OSError:
                size = 0
            out.append(f"[файл] {e.name}  {size} байт")
    note = ""
    if len(entries) > limit:
        note = f"\n... и ещё {len(entries) - limit}"
    return _clip(f"# {p} — записей: {len(entries)}\n" + "\n".join(out) + note)


def tool_tail_log(path, lines=40):
    p = safe_path(path)
    if not p.is_file():
        return f"это не файл: {p}"
    lines = max(1, min(int(lines), MAX_LINES))
    content = p.read_text(errors="replace").splitlines()
    tail = content[-lines:]
    return _clip(
        f"# {p} — строк всего: {len(content)}, последние {len(tail)}:\n"
        + "\n".join(tail)
    )


def tool_read_state():
    """Состояние сервера: переиспользует read_state_summary() из dex_poller."""
    from dex_poller import read_state_summary
    return _clip(read_state_summary(5))


def tool_run_check(name):
    """Запускает встроенные чеки heartbeat.py (они уже написаны и работают)."""
    import heartbeat as hb

    dispatch = {
        "backups": lambda: hb.execute_check_backups(),
        "updates": lambda: hb.execute_check_updates(),
        "disk": lambda: hb.execute_check_disk(),
        "services": lambda: hb.execute_check_services(),
        "tools": lambda: hb.execute_check_tools(),
        "interest": lambda: hb.execute_explore_interest(
            hb.load_identity(), sqlite3.connect(str(hb.DB_PATH))),
    }
    if name not in dispatch:
        return "неизвестный чек: " + name + ". Доступны: " + ", ".join(dispatch)
    return _clip(dispatch[name]())


# === ШАБЛОНЫ КОМАНД (Вариант А) ===
# Не строка, которую модель придумала, а заранее описанный argv.
# Каждый слот имеет свой regex; значение с ведущим '-' не проходит —
# иначе юнит вида '--version' превратился бы в флаг systemctl.
RE_UNIT = "^[A-Za-z0-9_.@-]{1,64}$"
RE_INT = "^[1-9][0-9]{0,4}$"
RE_NAME = "^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"
RE_PATH = r"^/root/[\w./-]{1,150}$"  # абсолютные пути под /root
RE_WHEN = r"^(?:[0-9]+ (?:minutes?|hours?|days?) ago|today|yesterday)$"

COMMAND_TEMPLATES = {
    "journalctl-unit": {
        "desc": "Журнал systemd-юнита: journalctl -u <unit> -n <lines>",
        "argv": ["journalctl", "-u", "{unit}", "-n", "{lines}", "--no-pager"],
        "slots": {"unit": (True, RE_UNIT, None), "lines": (False, RE_INT, "50")},
        "timeout": 25,
    },
    "journalctl-since": {
        "desc": "Журнал за период: journalctl --since <when> -n <lines>",
        "argv": ["journalctl", "--since", "{when}", "-n", "{lines}", "--no-pager"],
        "slots": {"when": (True, RE_WHEN, "1 hour ago"), "lines": (False, RE_INT, "100")},
        "timeout": 25,
    },
    "systemctl-status": {
        "desc": "Статус юнита: systemctl status <unit>",
        "argv": ["systemctl", "status", "{unit}", "--no-pager", "-l"],
        "slots": {"unit": (True, RE_UNIT, None)},
        "timeout": 15,
    },
    "systemctl-user-status": {
        "desc": "Статус ПОЛЬЗОВАТЕЛЬСКОГО юнита (dex-* живут здесь): --user status",
        "argv": ["systemctl", "--user", "status", "{unit}", "--no-pager", "-l"],
        "slots": {"unit": (True, RE_UNIT, None)},
        "timeout": 15,
    },
    "docker-logs": {
        "desc": "Последние строки журнала контейнера: docker logs --tail",
        "argv": ["docker", "logs", "--tail", "{lines}", "{name}"],
        "slots": {"name": (True, RE_NAME, None), "lines": (False, RE_INT, "80")},
        "timeout": 25,
    },
    "docker-inspect": {
        "desc": "Конфигурация контейнера + статус (без вывода логов)",
        "argv": ["docker", "inspect", "--format",
                 "{{.Name}} {{.State.Status}} restarts={{.RestartCount}} "
                 "oom={{.State.OOMKilled}} started={{.State.StartedAt}}",
                 "{name}"],
        "slots": {"name": (True, RE_NAME, None)},
        "timeout": 15,
    },
    "git-log": {
        "desc": "Последние коммиты репозитория (путь должен лежать под /root)",
        "argv": ["git", "-C", "{repo}", "log", "--oneline", "--date=short",
                 "--pretty=%h %ad %s", "-n", "{lines}"],
        "slots": {"repo": (True, RE_PATH, None), "lines": (False, RE_INT, "15")},
        "timeout": 15,
    },
    "git-status": {
        "desc": "Состояние репозитория: git status --short",
        "argv": ["git", "-C", "{repo}", "status", "--short"],
        "slots": {"repo": (True, RE_PATH, None)},
        "timeout": 15,
    },
    "net-ports": {
        "desc": "Открытые TCP-порты и кто их держит: ss -tlnp",
        "argv": ["ss", "-tlnp"],
        "slots": {},
        "timeout": 15,
    },
    "memory": {
        "desc": "Память и подкачка: free -m",
        "argv": ["free", "-m"],
        "slots": {},
        "timeout": 10,
    },
    "load": {
        "desc": "Аптайм и средняя нагрузка: uptime",
        "argv": ["uptime"],
        "slots": {},
        "timeout": 10,
    },
    "top-by-mem": {
        "desc": "Топ процессов по памяти: ps aux --sort=-%mem",
        "argv": ["ps", "aux", "--sort=-%mem"],
        "slots": {},
        "timeout": 15,
    },
    "disk-inodes": {
        "desc": "Свободные inode и монтирования: df -ih",
        "argv": ["df", "-ih"],
        "slots": {},
        "timeout": 10,
    },
    "docker-stats": {
        "desc": "Расход ресурсов контейнеров одним снимком",
        "argv": ["docker", "stats", "--no-stream",
                 "--format", "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"],
        "slots": {},
        "timeout": 30,
    },
}

CMD_DESC = "\n".join(f"  {k} — {v['desc']}" for k, v in COMMAND_TEMPLATES.items())


def _xdg_env(argv):
    """Окружение для запуска: для --user команд нужен XDG_RUNTIME_DIR."""
    env = os.environ.copy()
    if "--user" in argv and not env.get("XDG_RUNTIME_DIR"):
        import os as _os
        cand = f"/run/user/{_os.getuid()}"
        if Path(cand).is_dir():
            env["XDG_RUNTIME_DIR"] = cand
    return env


def tool_run_cmd(template, args=None):
    """Исполняет заранее описанный шаблон. Список argv, shell=False."""
    import subprocess
    t = COMMAND_TEMPLATES.get(template)
    if t is None:
        return (f"нет шаблона '{template}'. Доступные:\n{CMD_DESC}")

    args = args if isinstance(args, dict) else {}

    # 1. значения слотов с валидацией
    values = {}
    for slot, (required, pattern, default) in t["slots"].items():
        raw = args.get(slot, default)
        if raw is None or str(raw).strip() == "":
            if required:
                return f"шаблон {template} требует параметр '{slot}'"
            raw = default
        val = str(raw).strip()
        if val.startswith("-"):
            return f"отказано: параметр '{slot}' не может начинаться с '-'"
        if not re.match(pattern, val):
            return f"параметр '{slot}' не проходит проверку: {val!r}"
        # слот-путь: та же защита секретов, что и в safe_path
        if slot in ("repo", "path", "file"):
            low = val.lower()
            for deny in DENY_SUBSTRINGS:
                if deny in low:
                    return f"отказано песочницей: доступ к '{deny}' закрыт"
        values[slot] = val

    # 2. подстановка в argv — построчно, shell=False, без format()
    #    (format сломал бы {{.Name}} в шаблонах docker)
    argv = list(t["argv"])
    for slot, val in values.items():
        argv = [tok.replace("{" + slot + "}", val) for tok in argv]

    # 3. неподставленный плейсхолдер = ошибка сборки, не рискнем
    left = [tok for tok in argv
            if "{" in tok.replace("{{", "").replace("}}", "")]
    if left:
        return "не подставлены параметры: " + " ".join(left)

    try:
        r = subprocess.run(argv, capture_output=True, text=True,
                           timeout=t.get("timeout", 20),
                           env=_xdg_env(argv))
    except FileNotFoundError:
        return f"команда не найдена: {argv[0]}"
    except subprocess.TimeoutExpired:
        return f"таймаут {t.get('timeout', 20)}с: {' '.join(argv[:4])}"
    except Exception as e:
        return f"ошибка запуска: {type(e).__name__}: {e}"

    body = (r.stdout or "") + (("STDERR:\n" + r.stderr) if r.stderr and r.stderr.strip() else "")
    head = f"$ {' '.join(argv)}  →  код {r.returncode}"
    return _clip(head + "\n" + body.strip())


# === УРОВНИ ДОСТУПА ===
# 3 — только чтение (по умолчанию, «как сейчас»)
# 2 — песочница: запись в sandbox/skills, запуск от nobody без сети
# 1 — root: запись по /root/.hermes, запуск от root
SANDBOX_DIR = SKILLS_DIR.parent / "sandbox"
NOBODY_UID = NOBODY_GID = 65534
SCRIPT_TIMEOUT = 20
SCRIPT_MAX_BYTES = 100 * 1024          # что можно записать одним махом
TOOL_LEVELS = {"write_file": (1, 2), "run_script": (1, 2)}
WRITE_ROOTS = {
    2: (SANDBOX_DIR, SKILLS_DIR),
    1: (Path("/root/.hermes"),),
}
LEVEL_NAMES = {1: "root", 2: "песочница", 3: "только чтение"}
_AGENT_DB = Path(__file__).resolve().parent / "agent.db"


def get_access_level():
    """Текущий уровень. 3 = самый закрытый, 1 = root. Ошибка -> 3."""
    try:
        db = sqlite3.connect(str(_AGENT_DB))
        row = db.execute("SELECT value FROM state WHERE key='access_level'").fetchone()
        db.close()
        if row:
            lv = int(json.loads(row[0]))
            if lv in (1, 2, 3):
                return lv
    except Exception:
        pass
    return 3


def set_access_level(lv):
    db = sqlite3.connect(str(_AGENT_DB))
    db.execute("INSERT OR REPLACE INTO state (key, value, updated_at) "
               "VALUES (?, ?, ?)",
               ("access_level", json.dumps(int(lv)),
                __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc).isoformat()))
    db.commit()
    db.close()


def active_tools(level=None):
    """Схемы, доступные на текущем уровне. L3 не видит write/run."""
    level = get_access_level() if level is None else level
    out = []
    for t in TOOLS:
        allowed = TOOL_LEVELS.get(t["name"], (1, 2, 3))
        if level in allowed:
            out.append(t)
    return out


def _write_roots(level):
    return WRITE_ROOTS.get(level, ())


def tool_write_file(path, content):
    """Запись файла. Разрешена только внутри каталогов своего уровня."""
    level = get_access_level()
    if level == 3:
        return ("уровень доступа 3 — запись запрещена. "
                "Переключение: /access 2 (песочница) или /access 1 (root).")
    if content is None:
        return "нет содержимого (параметр content)"
    content = str(content)
    if len(content.encode()) > SCRIPT_MAX_BYTES:
        return f"слишком большой файл: {len(content.encode())} > {SCRIPT_MAX_BYTES} байт"

    try:
        p = Path(str(path).strip()).resolve()
    except Exception as e:
        return f"не разобрал путь: {e}"
    low = str(p).lower()
    for deny in DENY_SUBSTRINGS:
        if deny in low:
            return f"отказано: доступ к '{deny}' закрыт"
    if p.suffix in (".db", ".sqlite", ".sqlite3"):
        return "отказано: базы данных писать нельзя (для них есть свои инструменты)"

    roots = _write_roots(level)
    if not any(str(p) == str(r) or str(p).startswith(str(r) + "/") for r in roots):
        return ("отказано: путь вне зоны записи уровня "
                f"{level} ({LEVEL_NAMES[level]}). Разрешено: "
                + ", ".join(str(r) for r in roots))

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except Exception as e:
        return f"не смог записать: {type(e).__name__}: {e}"
    lines = content.count("\n") + 1
    return f"записано: {p} ({len(content.encode())} байт, {lines} строк)"


def _sandbox_limits():
    """Лимиты процесса: CPU, память, размер файла, дескрипторы, core."""
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (SCRIPT_TIMEOUT, SCRIPT_TIMEOUT))
    resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
    resource.setrlimit(resource.RLIMIT_FSIZE, (10 << 20, 10 << 20))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def tool_run_script(name, inputs=None):
    """Запуск своего скрипта из sandbox.

    Уровень 2: от nobody (65534), без сети (unshare --net), с лимитами.
      ВАЖНО: nobody не пройдёт через /root (права 0700), поэтому скрипт
      и входные файлы копируются во временный каталог 0755 в /tmp,
      а после запуска удаляются — артефакты остаются только в stdout.
    Уровень 1: от root, в своём sandbox, сеть есть, артефакты сохраняются.
    Уровень 3: запрещено.
    """
    import subprocess
    level = get_access_level()
    if level == 3:
        return ("уровень доступа 3 — запуск запрещён. "
                "Переключение: /access 2 (песочница) или /access 1 (root).")

    name = str(name or "").strip()
    if not name.endswith(".py"):
        name += ".py"
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,60}\.py", name):
        return f"недопустимое имя скрипта: {name!r}"
    try:
        script = (SANDBOX_DIR / name).resolve()
    except Exception as e:
        return f"не разобрал путь: {e}"
    if not str(script).startswith(str(SANDBOX_DIR.resolve()) + "/"):
        return "отказано песочницей: скрипт вне sandbox"
    if not script.is_file():
        return f"скрипт '{name}' не найден. Сначала write_file в {SANDBOX_DIR}"

    # входные файлы — только из read-allowlist
    in_files = []
    if inputs:
        if isinstance(inputs, str):
            inputs = [inputs]
        for raw in list(inputs)[:5]:
            try:
                src = safe_path(raw)
            except SandboxError as e:
                return f"входной файл отклонён: {e}"
            if not src.is_file():
                return f"входной файл не найден: {src}"
            in_files.append(src)

    workdir = None
    try:
        if level == 1:
            # root: запускаем там же, рядом со скриптом — артефакты живут
            workdir = SANDBOX_DIR
            run_path = script
            in_dir = SANDBOX_DIR / "_in"
            argv = ["/usr/bin/python3", "-I", str(run_path)]
            who, net = "root", "сеть разрешена"
        else:
            # nobody: отдельный каталог, куда ему разрешён проход
            workdir = Path(tempfile.mkdtemp(prefix="dexrun_", dir="/tmp"))
            workdir.chmod(0o777)  # nobody может писать результат; каталог одноразовый
            run_path = workdir / script.name
            shutil.copy2(script, run_path)
            run_path.chmod(0o644)
            in_dir = workdir / "_in"
            argv = ["/usr/bin/unshare", "--net",
                    f"--setuid={NOBODY_UID}", f"--setgid={NOBODY_GID}",
                    "--", "/usr/bin/python3", "-I", str(run_path)]
            who, net = "nobody (65534)", "сеть отрезана (unshare --net)"

        copied = []
        if in_files:
            in_dir.mkdir(parents=True, exist_ok=True)
            in_dir.chmod(0o755)
            for src in in_files:
                dst = in_dir / re.sub(r"[^A-Za-z0-9._-]", "_", src.name)
                shutil.copy2(src, dst)
                dst.chmod(0o644)
                copied.append(dst.name)

        env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
               "HOME": str(workdir),
               "TMPDIR": "/tmp",
               "DEX_INPUT_DIR": str(in_dir),
               "PYTHONDONTWRITEBYTECODE": "1"}

        def _pre():
            _sandbox_limits()
            os.setsid()

        try:
            r = subprocess.run(argv, capture_output=True, text=True,
                               timeout=SCRIPT_TIMEOUT, cwd=str(workdir),
                               env=env, preexec_fn=_pre)
        except subprocess.TimeoutExpired:
            return f"таймаут {SCRIPT_TIMEOUT}с — скрипт убит (лимит CPU)"
        except PermissionError:
            return "не смог запустить: прав не хватило (unshare/setuid)"
        except Exception as e:
            return f"ошибка запуска: {type(e).__name__}: {e}"

        # забираем то, что скрипт создал рядом с собой
        saved = []
        if level == 2 and workdir is not None and workdir.is_dir():
            for f in sorted(workdir.iterdir()):
                if f.name in ("_in", script.name) or f.name == "__pycache__":
                    continue
                if not f.is_file():
                    continue
                try:
                    if f.stat().st_size > SCRIPT_MAX_BYTES:
                        saved.append(f"{f.name}: слишком большой, пропущен")
                        continue
                    dst = SANDBOX_DIR / f.name
                    shutil.copy2(f, dst)
                    dst.chmod(0o644)
                    saved.append(f"{f.name} ({f.stat().st_size} байт)")
                except Exception as e:
                    saved.append(f"{f.name}: не забрал ({e})")

        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        head = (f"$ {name}  →  код {r.returncode}  |  {who}, {net}"
                + (f"  |  входные файлы: {', '.join(copied)}" if copied else ""))
        if level == 2:
            if saved:
                head += "\nсохранено в sandbox: " + "; ".join(saved)
            else:
                head += "  |  новых файлов нет"
        body = out
        if err:
            body = (body + "\n\nSTDERR:\n" + err) if body else ("STDERR:\n" + err)
        return _clip(head + ("\n" + body if body else "\n(скрипт ничего не вывел)"))
    finally:
        if level == 2 and workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)

# === ЧТЕНИЕ САЙТОВ (доступно на всех уровнях — это тоже чтение) ===
FETCH_TIMEOUT = 15
FETCH_MAX_BYTES = 400 * 1024
FETCH_MAX_REDIRECTS = 5
USER_AGENT = "DexBot/1.0 (+VPS caretaker; read-only fetch)"

from urllib.request import HTTPRedirectHandler as _HRH  # нужно классу ниже


class _NoRedirect(_HRH):
    """Редиректы обрабатываем сами — иначе ушли бы на приватный адрес."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _public_ip(ip):
    """True, если адрес публичный. Всё приватное/специальное — False."""
    import ipaddress
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped is not None:
        return _public_ip(str(a.ipv4_mapped))
    return not (a.is_private or a.is_loopback or a.is_link_local
                or a.is_multicast or a.is_reserved or a.is_unspecified)


def _check_url(url):
    """Валидация URL ДО запроса: схема, литерал IP, все адреса из DNS."""
    import ipaddress, socket
    from urllib.parse import urlparse
    try:
        u = urlparse(str(url).strip())
    except Exception as e:
        return False, f"не разобрал URL: {e}"
    if u.scheme not in ("http", "https"):
        return False, f"схема {u.scheme or '(пусто)'} запрещена — только http/https"
    if not u.hostname:
        return False, "в URL нет хоста"
    host = u.hostname.lower()
    if host in ("localhost", "metadata", "metadata.google.internal"):
        return False, f"хост '{host}' заблокирован"
    bare = host.strip("[]")
    try:
        ipaddress.ip_address(bare)
        is_ip = True
    except ValueError:
        is_ip = False
    if is_ip:
        # литерал IP: проверяем напрямую, резолвить нечего
        if not _public_ip(bare):
            return False, f"адрес {bare} не публичный"
        return True, None
    try:
        infos = socket.getaddrinfo(
            host, u.port or (443 if u.scheme == "https" else 80),
            proto=socket.IPPROTO_TCP)
    except Exception as e:
        return False, f"не разрешил {host}: {e}"
    for info in infos:
        ip = info[4][0].split("%")[0]
        if not _public_ip(ip):
            return False, f"{host} -> {ip} не публичный адрес"
    return True, None


def _html_to_text(html):
    """Чистый текст из HTML: выкидываем скрипты, стили и разметку."""
    from html.parser import HTMLParser

    class P(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.skip = 0
            self.out = []

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style", "noscript", "svg", "head"):
                self.skip += 1
            if tag in ("p", "br", "div", "li", "tr", "h1", "h2", "h3",
                       "h4", "section", "article"):
                self.out.append("\n")

        def handle_endtag(self, tag):
            if tag in ("script", "style", "noscript", "svg", "head") and self.skip:
                self.skip -= 1
            if tag in ("p", "div", "li", "tr", "h1", "h2", "h3", "h4"):
                self.out.append("\n")

        def handle_data(self, data):
            if not self.skip and data.strip():
                self.out.append(data)

    p = P()
    try:
        p.feed(html)
    except Exception:
        return html[:FETCH_MAX_BYTES]
    lines = [" ".join(l.split()) for l in "".join(p.out).split("\n")]
    return "\n".join(l for l in lines if l)


def tool_fetch_url(url, lines=200, as_text=True):
    """Читает страницу по URL и возвращает ТЕКСТ, а не HTML."""
    import socket
    from urllib.parse import urljoin
    from urllib.request import Request, build_opener, HTTPRedirectHandler
    from urllib.error import HTTPError, URLError

    lines = max(1, min(int(lines or 200), 400))
    current = str(url or "").strip()
    if not current:
        return "URL не указан"

    seen, resp, ctype, raw, truncated = set(), None, "", b"", False
    for _hop in range(FETCH_MAX_REDIRECTS + 1):
        ok, why = _check_url(current)
        if not ok:
            return f"заблокировано: {why}"
        if current in seen:
            return "зацикленный редирект"
        seen.add(current)

        req = Request(current, headers={"User-Agent": USER_AGENT,
                                        "Accept": "*/*"})
        try:
            resp = build_opener(_NoRedirect()).open(req, timeout=FETCH_TIMEOUT)
        except HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = e.headers.get("Location")
                if not loc:
                    return f"редирект {e.code} без Location"
                current = urljoin(current, loc)
                continue
            return f"HTTP {e.code} {e.reason}"
        except (URLError, socket.timeout, OSError) as e:
            return f"не смог запросить: {type(e).__name__}: {e}"

        if resp.status in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location")
            if not loc:
                return f"редирект {resp.status} без Location"
            current = urljoin(current, loc)
            continue
        ctype = (resp.headers.get("Content-Type") or "").lower()
        while len(raw) < FETCH_MAX_BYTES:
            chunk = resp.read(min(65536, FETCH_MAX_BYTES - len(raw)))
            if not chunk:
                break
            raw += chunk
        truncated = len(raw) >= FETCH_MAX_BYTES
        resp.close()
        break
    else:
        return f"слишком много редиректов (>{FETCH_MAX_REDIRECTS})"

    charset = "utf-8"
    if "charset=" in ctype:
        charset = ctype.split("charset=")[-1].split(";")[0].strip() or "utf-8"
    body = raw.decode(charset, errors="replace")
    if as_text and "html" in ctype:
        body = _html_to_text(body)

    out = body.strip()
    if not out:
        return f"страница пуста (тип: {ctype or 'нет'})"
    if len(out) > MAX_RESULT:
        out = out[:MAX_RESULT] + f"\n... [обрезано, всего {len(out)} символов]"
    out = "\n".join(out.splitlines()[:lines]) + (
        f"\n[показано {lines} строк из {len(out.splitlines())}]"
        if len(out.splitlines()) > lines else "")
    head = (f"URL: {current}  |  {ctype or 'без типа'}"
            + (f"  |  байт: {FETCH_MAX_BYTES} (лимит)" if truncated else ""))
    return _clip(head + "\n" + out)


def tool_tasks(action, what=None, id=None):
    """Задачи Dex: list — открыть, add — завести, done — закрыть."""
    import heartbeat as hb
    db = sqlite3.connect(str(hb.DB_PATH))
    try:
        action = str(action or "").strip().lower()
        if action == "list":
            rows = hb.task_list(db, "pending", 15)
            if not rows:
                out = "открытых задач нет"
                done = hb.task_list(db, "done", 5)
                if done:
                    out += "\nнедавно закрытые:"
                    out += "\n".join(f"  #{r['id']} {r['what'][:80]}"
                                      for r in done)
                return out
            body = "\n".join(
                f"  #{r['id']} [{r['source']}] {r['what'][:90]}" for r in rows)
            return f"открытых задач: {len(rows)}\n{body}"
        if action == "add":
            text = str(what or "").strip()
            if not text:
                return "нужен текст задачи — параметр what"
            tid = hb.task_add(db, text, source="dex", result="создано из чата")
            return f"задача #{tid} создана" if tid else "такая задача уже открыта"
        if action == "done":
            if id is None or str(id).strip() == "":
                return "нужен номер задачи — параметр id"
            try:
                tid = int(id)
            except (TypeError, ValueError):
                return f"id должен быть числом, получено: {id}"
            return (f"задача #{tid} закрыта" if hb.task_done(db, tid)
                    else f"задача #{tid} не найдена или уже закрыта")
        return "action: list | add | done"
    finally:
        db.close()


DISPATCH = {
    "read_file": tool_read_file,
    "list_dir": tool_list_dir,
    "tail_log": tool_tail_log,
    "read_state": lambda **_: tool_read_state(),
    "run_check": tool_run_check,
    "list_skills": lambda **_: tool_list_skills(),
    "read_skill": tool_read_skill,
    "tasks": tool_tasks,
    "run_cmd": tool_run_cmd,
    "write_file": tool_write_file,
    "run_script": tool_run_script,
    "fetch_url": tool_fetch_url,
}


def execute_tool(name, args_json):
    """Исполняет tool_call. Всегда возвращает строку — и при успехе, и при ошибке."""
    fn = DISPATCH.get(name)
    if fn is None:
        return f"неизвестный инструмент: {name}"
    try:
        args = json.loads(args_json) if args_json else {}
        if not isinstance(args, dict):
            return "аргументы должны быть объектом"
    except Exception as e:
        return f"не разобрал аргументы: {e}"

    try:
        if name == "run_check":
            return fn(args.get("name", "disk"))
        return fn(**args)
    except SandboxError as e:
        return f"отказано песочницей: {e}"
    except Exception as e:
        return f"ошибка выполнения {name}: {type(e).__name__}: {e}"


if __name__ == "__main__":
    for t in TOOLS:
        print(t["name"], "->", t["description"][:60])
    print("\nтест песочницы:")
    for probe in ("/etc/hostname", "/etc/passwd", "/etc/shadow",
                  "/root/.hermes/.env", "/root/.proactive", "/tmp/x",
                  "/root/Documents/wiki/index.md", "../etc/passwd"):
        try:
            safe_path(probe)
            print(f"  РАЗРЕШЕНО  {probe}")
        except SandboxError as e:
            print(f"  закрыто    {probe}  ({e})")
