#!/usr/bin/env python3
"""Векторная память Dex: sqlite-vec + bge-m3.

В agent.db:
  - ticks_meta  — текст тиков (action/result/ts) для показа результата
  - vec_ticks   — vec0 виртуальная таблица, rowid = tick, embedding float[1024]

Ключевой приём — ДЕДУПЛИКАЦИЯ. 1362 тика дают всего ~69 уникальных
строк «action: result» (none повторяется 857 раз в 4 вариантах).
Эмбеддим каждую уникальную строку один раз и раздаём вектор всем
тикам с этим текстом. 69 эмбеддингов вместо 1362 — минуты вместо часов.

Запуск: python3 vec_build.py [--reset] [--limit N]
"""
import json, os, sqlite3, struct, sys, time, urllib.request
from collections import Counter

BASE = os.path.expanduser("~/.hermes/proactive")
AGENT_DB = os.path.join(BASE, "agent.db")
TICK_LOG = os.path.join(BASE, "tick_history.jsonl")
VEC_SO = os.path.join(BASE, "lib", "vec0.so")
OLLAMA = "http://127.0.0.1:11434/api/embed"
MODEL = "bge-m3"
DIM = 1024
BATCH = 16


def conn():
    c = sqlite3.connect(AGENT_DB, timeout=30)
    c.enable_load_extension(True)
    c.load_extension(VEC_SO)
    c.execute("PRAGMA busy_timeout=10000")
    return c


def ensure_schema(c, reset=False):
    if reset:
        for t in ("vec_ticks", "ticks_meta"):
            try:
                c.execute(f"DROP TABLE IF EXISTS {t}")
            except Exception:
                pass
    c.execute("""CREATE TABLE IF NOT EXISTS ticks_meta(
        tick INTEGER PRIMARY KEY, ts TEXT, action TEXT, result TEXT)""")
    try:
        c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS vec_ticks "
                  "USING vec0(embedding float[%d])" % DIM)
    except sqlite3.OperationalError as e:
        if "already exists" not in str(e):
            raise
    c.commit()


def load_ticks(limit=None):
    """tick -> (ts, action, result). Последняя запись по tick побеждает."""
    rows = {}
    with open(TICK_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            tick = r.get("tick")
            if tick is None:
                continue
            rows[tick] = (r.get("ts", ""), r.get("action", "?"),
                          str(r.get("result", ""))[:300])
    items = sorted(rows.items())
    return items[:limit] if limit else items


def embed(texts):
    body = json.dumps({"model": MODEL, "input": texts, "keep_alive": "10m"}).encode()
    req = urllib.request.Request(OLLAMA, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.loads(resp.read().decode())["embeddings"]


def main():
    reset = "--reset" in sys.argv
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    c = conn()
    ensure_schema(c, reset)

    items = load_ticks(limit)
    existing = {r[0] for r in c.execute("SELECT tick FROM ticks_meta")}
    todo = [(t, v) for t, v in items if t not in existing]
    print(f"тиков всего: {len(items)} | уже в памяти: {len(existing)} "
          f"| к добавлению: {len(todo)}")

    if not todo:
        n = c.execute("SELECT count(*) FROM ticks_meta").fetchone()[0]
        v = c.execute("SELECT count(*) FROM vec_ticks").fetchone()[0]
        print(f"  обновлять нечего: ticks_meta={n}, vec_ticks={v}")
        return

    # ДЕДУПЛИКАЦИЯ: уникальные строки «action: result»
    texts = [f"{v[1]}: {v[2]}" for _, v in todo]
    uniq = sorted(set(texts))
    print(f"  уникальных текстов среди них: {len(uniq)} "
          f"(экономия x{len(texts)/max(1,len(uniq)):.0f})")

    t0 = time.time()
    vec_by_text = {}
    for i in range(0, len(uniq), BATCH):
        chunk = uniq[i:i + BATCH]
        try:
            vecs = embed(chunk)
        except Exception as e:
            print(f"  ошибка эмбеддинга на пачке {i}: {e}")
            return
        for t, v in zip(chunk, vecs):
            vec_by_text[t] = struct.pack("%df" % DIM, *v)
        done = min(i + BATCH, len(uniq))
        el = time.time() - t0
        rate = done / el if el else 0
        eta = (len(uniq) - done) / rate if rate else 0
        print(f"  эмбеддинг {done}/{len(uniq)} | {rate:.2f} текст/с | "
              f"осталось {eta:.0f}с", flush=True)

    t1 = time.time()
    with c:
        for (tick, (ts, action, result)), text in zip(todo, texts):
            buf = vec_by_text.get(text)
            if not buf:
                continue
            c.execute("INSERT OR REPLACE INTO ticks_meta(tick, ts, action, result) "
                      "VALUES (?,?,?,?)", (tick, ts, action, result))
            c.execute("DELETE FROM vec_ticks WHERE rowid=?", (tick,))
            c.execute("INSERT INTO vec_ticks(rowid, embedding) VALUES (?, ?)",
                      (tick, sqlite3.Binary(buf)))
    print(f"  вставка {len(todo)} строк: {time.time()-t1:.1f}с")

    n = c.execute("SELECT count(*) FROM ticks_meta").fetchone()[0]
    v = c.execute("SELECT count(*) FROM vec_ticks").fetchone()[0]
    print(f"Готово: ticks_meta={n}, vec_ticks={v}, всего {time.time()-t0:.1f}с")
    c.close()


if __name__ == "__main__":
    main()
