#!/usr/bin/env python3
"""
Dex Control Center — веб-дашборд и API управления агентом Dex.
"""
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, jsonify, render_template, request, send_file

# === CONFIG ===
BASE_DIR = Path.home() / ".hermes" / "proactive"
DB_PATH = BASE_DIR / "agent.db"
SESSIONS_DB = BASE_DIR / "sessions.db"
TICK_LOG = BASE_DIR / "tick_history.jsonl"
DISABLED_FLAG = BASE_DIR / "DISABLED"
ENV_PATH = BASE_DIR / ".env"
IDENTITY_PATH = BASE_DIR / "identity.yaml"
HEARTBEAT_SCRIPT = BASE_DIR / "heartbeat.py"
POLLER_SERVICE = "dex-poller.service"

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True

# ─── HELPERS ───────────────────────────────────────────────

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def read_db():
    """Читает все ключи из state-таблицы agent.db"""
    if not DB_PATH.exists():
        return {}
    try:
        db = sqlite3.connect(str(DB_PATH))
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT key, value, updated_at FROM state").fetchall()
        db.close()
        result = {}
        for r in rows:
            try:
                result[r["key"]] = {"value": json.loads(r["value"]), "updated_at": r["updated_at"]}
            except:
                result[r["key"]] = {"value": r["value"], "updated_at": r["updated_at"]}
        return result
    except Exception as e:
        return {"error": str(e)}

def read_llm_log(limit=50):
    """Читает последние N записей из llm_log"""
    if not DB_PATH.exists():
        return []
    try:
        db = sqlite3.connect(str(DB_PATH))
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT * FROM llm_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        db.close()
        return [dict(r) for r in rows]
    except Exception as e:
        return []

def read_config():
    """Читает кастомные настройки из agent.db"""
    if not DB_PATH.exists():
        return {}
    try:
        db = sqlite3.connect(str(DB_PATH))
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT key, value, updated_at FROM config").fetchall()
        db.close()
        result = {}
        for r in rows:
            try:
                result[r["key"]] = json.loads(r["value"])
            except:
                result[r["key"]] = r["value"]
        return result
    except:
        return {}

def write_config(key, value):
    """Пишет настройку в agent.db"""
    try:
        db = sqlite3.connect(str(DB_PATH))
        db.execute(
            "INSERT OR REPLACE INTO config (key, value, updated_at) VALUES (?, ?, ?)",
            (key, json.dumps(value, ensure_ascii=False), now_iso())
        )
        db.commit()
        db.close()
        return True
    except Exception as e:
        return str(e)

def read_ticks(limit=100):
    """Читает последние N тиков из tick_history.jsonl"""
    if not TICK_LOG.exists():
        return []
    try:
        with open(TICK_LOG) as f:
            lines = f.readlines()
        ticks = []
        for line in lines[-limit:]:
            try:
                ticks.append(json.loads(line.strip()))
            except:
                pass
        return ticks
    except:
        return []

def read_sessions():
    """Статистика по сессиям из sessions.db"""
    if not SESSIONS_DB.exists():
        return {"count": 0, "sessions": []}
    try:
        db = sqlite3.connect(str(SESSIONS_DB))
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT chat_id, role, content, ts FROM sessions ORDER BY ts DESC LIMIT 100"
        ).fetchall()
        db.close()
        # Группируем по чатам
        chats = {}
        for r in rows:
            cid = r["chat_id"]
            if cid not in chats:
                chats[cid] = {"chat_id": cid, "messages": [], "count": 0}
            chats[cid]["messages"].append({
                "role": r["role"], "content": r["content"], "ts": r["ts"]
            })
            chats[cid]["count"] += 1
        # Берём последние 5 сообщений из каждого чата
        for c in chats.values():
            c["messages"] = c["messages"][:10]
            c["first_ts"] = c["messages"][-1]["ts"] if c["messages"] else None
            c["last_ts"] = c["messages"][0]["ts"] if c["messages"] else None
        return {"total": sum(c["count"] for c in chats.values()),
                "sessions": list(chats.values())}
    except:
        return {"count": 0, "sessions": []}

def read_identity():
    try:
        import yaml
        with open(IDENTITY_PATH) as f:
            return yaml.safe_load(f)
    except:
        return {}

def poller_status():
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", POLLER_SERVICE],
            capture_output=True, text=True, timeout=5
        )
        active = r.stdout.strip() == "active"
        if active:
            r2 = subprocess.run(
                ["systemctl", "--user", "show", POLLER_SERVICE, "-p", "MainPID", "-p", "ActiveEnterTimestamp"],
                capture_output=True, text=True, timeout=5
            )
            info = {}
            for line in r2.stdout.strip().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    info[k] = v
            return {"active": True, "pid": info.get("MainPID"), "since": info.get("ActiveEnterTimestamp")}
        return {"active": False}
    except:
        return {"active": False, "error": "check failed"}

def db_stats():
    """Размеры БД и количество записей"""
    stats = {}
    for name, path in [("agent.db", DB_PATH), ("sessions.db", SESSIONS_DB), ("tick_history.jsonl", TICK_LOG)]:
        p = Path(path)
        if p.exists():
            stats[name] = {"size": p.stat().st_size, "path": str(p)}
        else:
            stats[name] = {"size": 0, "path": str(p)}
    return stats

# ─── API ───────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    state = read_db()
    config = read_config()
    ticks = read_ticks(20)
    llm = read_llm_log(10)
    sessions_data = read_sessions()
    poller = poller_status()
    dbs = db_stats()
    identity = read_identity()

    # Собираем drives
    drives_raw = state.get("drives", {}).get("value", {"curiosity": 0.5, "diligence": 0.5})
    tick_count = state.get("tick_count", {}).get("value", 0)
    focus = state.get("current_focus", {}).get("value", "unknown")

    disabled = DISABLED_FLAG.exists()

    return jsonify({
        "alive": not disabled,
        "disabled": disabled,
        "tick_count": tick_count,
        "focus": focus,
        "drives": drives_raw if isinstance(drives_raw, dict) else {"curiosity": 0.5, "diligence": 0.5},
        "last_tick": ticks[-1] if ticks else None,
        "ticks": ticks,
        "llm_log": llm,
        "config": config,
        "poller": poller,
        "sessions": sessions_data,
        "db_stats": dbs,
        "identity": {
            "name": identity.get("name", "Dex"),
            "role": identity.get("role", ""),
            "interests": identity.get("interests", [])
        },
        "updated_at": now_iso()
    })

@app.route("/api/config")
def api_get_config():
    return jsonify(read_config())

@app.route("/api/config", methods=["POST"])
def api_set_config():
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "no data"}), 400
    results = {}
    for key, value in data.items():
        r = write_config(key, value)
        results[key] = "ok" if r is True else r
    return jsonify({"saved": results})

@app.route("/api/action/tick", methods=["POST"])
def api_action_tick():
    """Принудительный heartbeat"""
    try:
        r = subprocess.run(
            ["python3", str(HEARTBEAT_SCRIPT)],
            capture_output=True, text=True, timeout=60,
            cwd=str(BASE_DIR)
        )
        return jsonify({"ok": True, "output": r.stdout[-500:], "errors": r.stderr[-500:]})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": True, "output": "heartbeat запущен, но не завершился за 60с (возможно долгий LLM вызов)"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/action/disable", methods=["POST"])
def api_action_disable():
    data = request.get_json(force=True, silent=True) or {}
    minutes = data.get("minutes", 0)
    DISABLED_FLAG.write_text(f"disabled at {now_iso()}" + (f" for {minutes}min" if minutes else ""))
    return jsonify({"ok": True, "disabled": True, "minutes": minutes})

@app.route("/api/action/enable", methods=["POST"])
def api_action_enable():
    if DISABLED_FLAG.exists():
        DISABLED_FLAG.unlink()
    return jsonify({"ok": True, "disabled": False})

@app.route("/api/action/poller-restart", methods=["POST"])
def api_action_poller_restart():
    try:
        r = subprocess.run(
            ["systemctl", "--user", "restart", POLLER_SERVICE],
            capture_output=True, text=True, timeout=30
        )
        ok = r.returncode == 0
        return jsonify({"ok": ok, "output": r.stdout, "errors": r.stderr})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/action/set-focus", methods=["POST"])
def api_action_set_focus():
    data = request.get_json(force=True, silent=True)
    if not data or "focus" not in data:
        return jsonify({"error": "focus required"}), 400
    try:
        db = sqlite3.connect(str(DB_PATH))
        db.execute(
            "INSERT OR REPLACE INTO state (key, value, updated_at) VALUES (?, ?, ?)",
            ("current_focus", json.dumps(data["focus"], ensure_ascii=False), now_iso())
        )
        db.commit()
        db.close()
        return jsonify({"ok": True, "focus": data["focus"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/action/set-drives", methods=["POST"])
def api_action_set_drives():
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "data required"}), 400
    try:
        db = sqlite3.connect(str(DB_PATH))
        existing = db.execute("SELECT value FROM state WHERE key='drives'").fetchone()
        drives = json.loads(existing[0]) if existing else {"curiosity": 0.5, "diligence": 0.5}
        if "curiosity" in data:
            drives["curiosity"] = max(0, min(1, float(data["curiosity"])))
        if "diligence" in data:
            drives["diligence"] = max(0, min(1, float(data["diligence"])))
        db.execute(
            "INSERT OR REPLACE INTO state (key, value, updated_at) VALUES (?, ?, ?)",
            ("drives", json.dumps(drives, ensure_ascii=False), now_iso())
        )
        db.commit()
        db.close()
        return jsonify({"ok": True, "drives": drives})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/data/clear-sessions", methods=["POST"])
def api_data_clear_sessions():
    try:
        if SESSIONS_DB.exists():
            db = sqlite3.connect(str(SESSIONS_DB))
            db.execute("DELETE FROM sessions")
            db.commit()
            db.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/data/clear-ticks", methods=["POST"])
def api_data_clear_ticks():
    try:
        if TICK_LOG.exists():
            TICK_LOG.write_text("")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/data/reset-state", methods=["POST"])
def api_data_reset_state():
    try:
        db = sqlite3.connect(str(DB_PATH))
        db.execute("DELETE FROM state")
        db.execute("DELETE FROM llm_log")
        db.commit()
        db.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/data/export")
def api_data_export():
    """Скачать дамп agent.db"""
    if DB_PATH.exists():
        return send_file(str(DB_PATH), as_attachment=True, download_name="agent.db")
    return jsonify({"error": "not found"}), 404

# ─── DASHBOARD ─────────────────────────────────────────────

@app.route("/")
def dashboard():
    return render_template("dex_dashboard.html")

# ─── MAIN ──────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=3333)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    print(f"Dex Control Center → http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)
