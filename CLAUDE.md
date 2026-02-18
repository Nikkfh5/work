# AI Orchestration System — CLAUDE.md

## Что это за проект

"AI OS" — система из супервайзора (Python бот) и воркеров (Claude Code CLI сессии),
которая принимает задания через Telegram и Email, маршрутизирует к нужному воркеру,
следит за прогрессом и уведомляет владельца о важных решениях.

Подписка: Claude Code Max ($300/мес, x20 лимиты). Без отдельного Anthropic API.
Воркеры = экземпляры `claude` CLI запускаемые как subprocess в их рабочих папках.

Референс архитектуры: https://github.com/vakovalskii/ValeDesk

---

## Как работает система (главное)

```
[Telegram / Email]
       │
       ▼
[supervisor/main.py]          ← asyncio Python бот, 24/7 на VPS
       │                         НЕ использует anthropic SDK
       ├── router.py             ← правило: contact → worker_id (из agents.yaml)
       ├── escalation.py         ← триггеры: написать владельцу в TG
       ├── summarizer.py         ← вызывает `claude --print` для дайджеста
       │
       │   subprocess(['claude', '--print', task, '--no-interactive'])
       │           запускается в директории воркера
       ▼
[workers/python_1/]           ← рабочая папка воркера
   ├── CLAUDE.md              ← роль + инструкции для Claude Code
   ├── workspace/             ← сюда пишется код
   └── .claude/               ← сессия Claude Code (--resume поддерживается)
```

### Воркер — это Claude Code сессия

Каждый воркер это НЕ Python код с anthropic SDK.
Каждый воркер — это `claude` CLI запущенный в своей папке:

```bash
cd workers/python_1
claude --print "ТЗ от клиента: {task_description}" --no-interactive
```

Claude Code сам:
- Читает CLAUDE.md как системный контекст
- Пишет код в workspace/
- Делает git commit + push
- Возвращает результат в stdout → супервайзор читает и пишет в БД

### Супервайзор — это Python asyncio бот

Супервайзор сам НЕ является Claude агентом для рутинных операций.
Только для сложных решений (суммаризация, эскалация) супервайзор вызывает:

```bash
claude --print "Суммаризируй статус проекта: {context}" --no-interactive
```

---

## Технологический стек

- **Python 3.11+** + asyncio — супервайзор и оркестрация
- **claude CLI** — все AI вызовы, через подписку Claude Code Max
- **SQLite** — единственный источник правды: `data/orchestrator.db`
- **python-telegram-bot** — Telegram интеграция
- **imaplib / smtplib** — Email интеграция
- **GitPython** — git операции (или Claude Code сам коммитит)
- **pytest** — тестирование
- **Docker Compose** — деплой на VPS

---

## Структура файлов

```
work/
├── CLAUDE.md                    ← этот файл
├── .env                         ← секреты (не в git)
├── .env.example                 ← шаблон
├── requirements.txt             ← БЕЗ anthropic SDK
├── docker-compose.yml
├── Makefile
├── data/
│   └── orchestrator.db          ← создаётся автоматически
├── supervisor/
│   ├── main.py                  ← asyncio event loop, точка входа
│   ├── router.py                ← contact → worker_id (rule-based)
│   ├── claude_runner.py         ← subprocess обёртка для claude CLI
│   ├── escalation.py            ← триггеры → уведомить владельца
│   └── summarizer.py            ← ежедневный дайджест через claude --print
├── workers/
│   ├── python_1/
│   │   ├── CLAUDE.md            ← роль: Python dev #1
│   │   └── workspace/           ← сюда пишется код
│   ├── python_2/                ← добавить месяц 2
│   ├── cpp_1/                   ← добавить месяц 3
│   ├── cpp_2/                   ← добавить месяц 4
│   └── go_1/                    ← добавить месяц 5
├── integrations/
│   ├── telegram_handler.py
│   ├── email_handler.py
│   └── meeting_handler.py
├── storage/
│   ├── schema.sql
│   ├── db.py
│   └── context_store.py
├── config/
│   ├── agents.yaml
│   └── routing_rules.yaml
└── tests/
    ├── test_db.py
    ├── test_router.py
    └── test_claude_runner.py
```

---

## Конвенции кода

- Все файлы: UTF-8, snake_case для переменных и функций
- Классы: PascalCase
- Константы: UPPER_SNAKE_CASE
- Каждый модуль начинается с docstring
- Async везде где есть I/O
- Логирование: `import logging; logger = logging.getLogger(__name__)`
- Никаких глобальных состояний — всё через DB

---

## Стратегия тестирования по этапам

### Этап 1 — БД (schema.sql + db.py)
```bash
pytest tests/test_db.py -v
```
Проверяем: все таблицы созданы, CRUD работает, контекст накапливается, директивы доставляются.

### Этап 2 — Router
```bash
pytest tests/test_router.py -v
```
Проверяем: каждый контакт из agents.yaml → правильный worker_id. Неизвестный → ValueError.

### Этап 3 — Claude Runner (мок claude CLI)
```bash
pytest tests/test_claude_runner.py -v
```
Проверяем: subprocess запускается в правильной директории, stdout читается, ошибки обрабатываются.

### Этап 4 — Telegram интеграция (mock бот)
```bash
pytest tests/test_integrations.py -v
```
Проверяем: входящее сообщение → task в БД, исходящее → API вызов.

### Этап 5 — End-to-end (реальный claude CLI)
```bash
python tests/e2e_test.py
```
Сценарий: симулировать TG сообщение → супервайзор создаёт директиву → claude CLI запускается в workers/python_1/ → появляется git коммит.

---

## Переменные окружения (.env)

```bash
# Telegram
TELEGRAM_BOT_TOKEN=...
TELEGRAM_OWNER_CHAT_ID=...

# Email
GMAIL_USER=...
GMAIL_APP_PASSWORD=...
IMAP_SERVER=imap.gmail.com
SMTP_SERVER=smtp.gmail.com

# Meetings
TLDV_API_KEY=...

# Git токены воркеров
GIT_TOKEN_PYTHON1=ghp_...
GIT_TOKEN_PYTHON2=ghp_...
GIT_TOKEN_CPP1=glpat-...
GIT_TOKEN_CPP2=glpat-...
GIT_TOKEN_GO1=ghp_...

# System
DB_PATH=data/orchestrator.db
LOG_LEVEL=INFO
POLL_INTERVAL_SECONDS=30
CLAUDE_CLI_PATH=claude          # или полный путь если не в PATH
DAILY_SUMMARY_HOUR_UTC=9
```

---

## Запуск локально

```bash
pip install -r requirements.txt
cp .env.example .env
# заполнить .env

# Проверить claude CLI доступен
claude --version

# Тесты
make test

# Запустить супервайзора
python -m supervisor.main
```

## Деплой на VPS

```bash
# Убедиться что claude CLI установлен на VPS и авторизован
claude auth login

docker-compose up -d
make logs
```

---

## Важные решения архитектуры (не менять)

1. **Нет anthropic SDK** — только `claude` CLI через подписку
2. **Воркеры не общаются напрямую** — только через БД
3. **Контекст append-only** — никогда не удалять из agent_context
4. **Жёсткая привязка** контакт → воркер, не динамическая
5. **Один SQLite файл** — монтируется как Docker volume
6. **Supervisor = единственный кто пишет клиенту**
7. **Каждый воркер** — отдельная папка со своим CLAUDE.md
