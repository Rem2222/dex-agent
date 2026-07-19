---
title: "Dex — Проактивный серверный агент"
description: "Смотритель сервера: heartbeat, идентичность, интересы, subconscious, self-improvement"
---

# Dex — Проактивный серверный агент

## Концепция
Dex — не просто cron-задача. Это «второй админ» на сервере:
- **Следит** за бэкапами, обновлениями, диском, сервисами
- **Интересуется** AI-агентами, LLM провайдерами, 1С, open source
- **Делится** новым через Telegram (отдельный канал)
- **Уважает границы** — /sleep, /mute, /nap

## Архитектура

```
~/.hermes/proactive/
├── identity.yaml         # Душа: характер, интересы, обязанности
├── agent.db              # TencentDB Agent Memory (L0-L3)
├── heartbeat.py          # Основной цикл (Python, ~270 строк)
├── dex-tick.sh           # Запуск из cron
├── DISABLED              # Красная кнопка (флаг)
├── tick_history.jsonl    # Лог последних тиков
└── heartbeat.log         # Лог heartbeat

Telegram:
├── Home                  # Чистый канал, только ответы на запросы
└── Dex (новый канал)     # Инициативы агента, новости, проверки

Hermes skills:
├── dex-identity          # Загружает контекст Dex в Telegram-канал
```

## Heartbeat (каждые 10 мин)

1. Проверяет `DISABLED` флаг
2. Читает `identity.yaml`
3. Смотрит какие обязанности (duties) пора исполнить
4. Вызывает LLM (через Gateway, opencode-go/deepseek-v4-flash)
5. LLM решает: `check_*` или `explore_interest` или `none`
6. Исполняет, логирует в `agent.db` + `tick_history.jsonl`

## Технические детали

### LLM провайдер для heartbeat
- **Gateway API key:** `123c867ed8cc504a5e602b4189cc201964a4e7331a20d7aeb883b88fdf86ed0a`
- **Модель:** `opencode-go/deepseek-v4-flash`
- **Стоимость:** ~200k токенов/день (около $0.02)

### База данных
- **Текущая:** SQLite (WAL mode, 2 таблицы: state + tasks)
- **Целевая:** TencentDB Agent Memory (sqlite-vec + BM25 + L0-L3)
- **Установка:** `openclaw plugins install @tencentdb-agent-memory/memory-tencentdb`

### Подсистемы (этапы)

| Этап | Компонент | Статус |
|------|-----------|--------|
| **1** | identity.yaml + heartbeat | ✅ Готово |
| **2** | Телеграм канал Dex | 🔧 Настроить chat_id |
| **3** | dex-identity skill | 🔧 Доделать загрузку в контекст |
| **4** | Subconscious (консолидация) | 🔜 |
| **5** | TencentDB Agent Memory | 🔜 |
| **6** | Self-improvement loop | 🔜 |
| **7** | Event-driven daemon (вместо cron) | 🔜 |

### Команды
- `touch ~/.hermes/proactive/DISABLED` — красная кнопка
- `rm ~/.hermes/proactive/DISABLED` — пробуждение
- `/status` в канале Dex — текущее состояние
- `/mute <тема>` — исключить интерес
- `/nap <часов>` — временно уснуть

### Связанные вики
- [[concepts/self-improving-agent-theory]]
- [[tech/proactive-agent-decision]]
- [[tech/jawl]]
