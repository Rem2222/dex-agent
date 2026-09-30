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
                    "enum": ["backups", "updates", "disk", "services", "tools"],
                    "description": "Какой чек выполнить: backups=свежесть бэкапов, updates=доступные обновления apt, disk=свободное место, services=состояние сервисов, tools=поиск новых инструментов",
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
    }
    if name not in dispatch:
        return "неизвестный чек: " + name + ". Доступны: " + ", ".join(dispatch)
    return _clip(dispatch[name]())


DISPATCH = {
    "read_file": tool_read_file,
    "list_dir": tool_list_dir,
    "tail_log": tool_tail_log,
    "read_state": lambda **_: tool_read_state(),
    "run_check": tool_run_check,
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
