# AI Orchestration System — Plan v5

## Контекст

Автоматизация работы разработчика на нескольких работах с разными стеками.
Система 24/7 принимает задания (Telegram/Email), изолированно выполняет через воркеров
(claude CLI по подписке, без API), проводит code review, доставляет код через git,
логирует в Notion, уведомляет в TG. Health-monitor агент следит за здоровьем системы
и берёт maintenance-задачи. Владелец участвует только в красных ситуациях.

**Архитектура:** развиваем существующий репо.
**Claude:** только CLI через подписку Claude Code Max. Без anthropic SDK. Без API ключей.
**VPS:** Ubuntu 24.04, 2 CPU / 4GB RAM (AI-compute на стороне Anthropic).

---

## Что уже готово

| Файл | Политика | Что делает |
|------|----------|-----------|
| `storage/db.py` | **НЕ ТРОГАТЬ** (исключение: PRAGMA WAL + busy_timeout — см. п.4) | CRUD база |
| `supervisor/router.py` | **НЕ ТРОГАТЬ** | contact → worker_id |
| `supervisor/claude_runner.py` | **НЕ ТРОГАТЬ** | subprocess обёртка |
| `storage/schema.sql` | **РАСШИРИТЬ** — только ADD (новые таблицы/колонки) | 8 таблиц |
| `config/agents.yaml` | **РАСШИРИТЬ** | конфиг агентов |
| `tests/test_db.py` | **НЕ ТРОГАТЬ** | 10 тестов |

**Принцип:** db.py — низкоуровневый слой. Новые CRUD-методы (lease, task_runs, dedup)
пишутся в новых модулях (`lease_manager.py` и др.), которые используют db.py как транспорт.

---

## Новые модули

```
supervisor/
├── main.py              ← asyncio event loop, точка входа
├── lease_manager.py     ← атомарный захват задачи (state machine + lease_token)
├── json_guard.py        ← парсинг/валидация stdout (не пишет в файлы сам)
├── run_logger.py        ← запись stdout/stderr в файлы + redact (отдельно от guard)
├── safe_exec.py         ← профильный allowlist-runner + realpath check
├── repo_manager.py      ← bare mirror + worktree + symlink per task
├── health_monitor.py    ← сбор snapshot + запуск health агента
├── escalation.py        ← 5 сценариев сбоя + pending_approval flow
├── summarizer.py        ← ежедневный дайджест
├── config_validator.py  ← валидация agents.yaml на старте (fail-fast)
└── log_utils.py         ← redact() для секретов в логах
storage/
└── migrate.py           ← schema versioning, идемпотентные миграции
integrations/
├── telegram_handler.py  ← polling + transactional dedup (processed_tg_updates)
├── email_handler.py     ← IMAP + idempotency (Message-ID dedup)
└── notion_handler.py    ← task page + event log + digest (fallback → файл)
workers/
├── {job}_worker/        ← CLAUDE.md + .mcp.json + .claude/settings.json (hooks)
├── {job}_reviewer/      ← CLAUDE.md + .mcp.json + .claude/settings.json (hooks)
└── health_monitor/      ← CLAUDE.md (audit + maintenance режимы)
```

---

## Критические архитектурные решения

### 1. MCP + worktree: как совместить

**Проблема:** Claude CLI ищет `.mcp.json` по scope-правилам от `cwd`.
Если запускаем из `worktrees/{task_id}/api/` — Context7 не подключится.

**Решение:** Claude **всегда** запускается из `workers/{agent}/`.
Supervisor создаёт симлинк на время задачи:

```
workers/{job}_worker/workspace/{task_id}/api  →  /app/worktrees/{task_id}/api
```

В CLAUDE.md и в prompt путь фиксирован как `workspace/{task_id}/{alias}/`.
После завершения задачи — симлинк удаляется. Worktree остаётся изолированным.

### 2. State machine задач (разрешённые переходы)

```
pending          → running         (lease acquired)
pending_approval → pending         (/approve)
pending_approval → rejected        (/reject)
running          → done            (worker+reviewer+push ok)
running          → blocked         (confidence < threshold)
running          → error           (crash/timeout/invalid JSON)
running          → requires_manual (max_attempts или max_review_iterations)
blocked          → running         (после escalation resolved)
requires_manual  → running         (/approve + restart)
requires_manual  → cancelled       (/reject)
done / cancelled / rejected        [terminal — не меняются]
```

Любой другой переход запрещён. `lease_manager.py` проверяет текущий статус
перед каждым `UPDATE`. Если переход недопустим — ошибка + лог.

### 3. Хранение попыток и логов: таблица `task_runs`

`tasks` хранит только **текущее состояние**. Все попытки — в `task_runs`:

```sql
-- Источник истины: все новые таблицы (через migrate.py, пронумерованно)

CREATE TABLE schema_migrations (
  version     INTEGER PRIMARY KEY,
  applied_at  DATETIME
);

CREATE TABLE task_runs (
  id          INTEGER PRIMARY KEY,
  task_id     INTEGER NOT NULL REFERENCES tasks(id),
  phase       TEXT NOT NULL,     -- worker|reviewer|health|supervisor_reasoning
  attempt     INTEGER NOT NULL DEFAULT 1,  -- попытка внутри фазы
  started_at  DATETIME NOT NULL,
  finished_at DATETIME,
  returncode  INTEGER,
  stdout_path TEXT,              -- путь к файлу лога (не строка в БД)
  stderr_path TEXT,
  parsed_json TEXT,
  json_valid  INTEGER            -- 0|1
);

CREATE TABLE processed_tg_updates (
  update_id    INTEGER PRIMARY KEY,
  task_id      INTEGER,
  processed_at DATETIME
);

CREATE TABLE processed_emails (
  message_id   TEXT PRIMARY KEY,
  task_id      INTEGER,
  processed_at DATETIME
);

CREATE TABLE kv_store (
  key   TEXT PRIMARY KEY,        -- напр. 'tg_last_update_id', 'schema_version'
  value TEXT
);

-- Новые поля в tasks (через migrate.py):
-- locked_by TEXT, locked_until DATETIME, lease_token TEXT
-- worker_attempt INTEGER DEFAULT 0   ← счётчик попыток воркера
-- review_iteration INTEGER DEFAULT 0 ← счётчик итераций ревью
-- last_error_code INTEGER, last_error_reason TEXT
-- notion_page_url TEXT, partial_result TEXT

-- Индексы (добавить в migrate.py):
CREATE INDEX IF NOT EXISTS idx_tasks_status         ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_locked_until   ON tasks(locked_until);
CREATE INDEX IF NOT EXISTS idx_task_runs_task_phase ON task_runs(task_id, phase, started_at);
```

`raw_stdout` и `raw_stderr` — только в файлах `logs/worker_{id}_{task}_{ts}.log`.
В БД — только путь. За запись отвечает `run_logger.py`, не json_guard.

### 4. SQLite надёжность

```python
# ИСКЛЮЧЕНИЕ из "db.py НЕ ТРОГАТЬ":
# Разрешена минимальная правка db.py — только добавить PRAGMA при открытии соединения.
# Всё остальное в db.py не трогать.
#
# В db.py (единственная правка):
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA busy_timeout=10000")  # 10 секунд ждать блокировку

# Lease acquire — один атомарный UPDATE (не read-then-write):
UPDATE tasks
SET status='running', locked_by=?, locked_until=datetime('now','+300 seconds'),
    lease_token=?, worker_attempt=worker_attempt+1
WHERE id=? AND (locked_until < datetime('now') OR locked_until IS NULL)
  AND status IN ('pending', 'blocked')
-- Проверить rowcount: если 0 — другой воркер уже взял

# renew_lease / release_lease — проверять (task_id, locked_by, lease_token) — все три
```

### 5. safe_exec: hardening (не наивный allowlist)

```python
# Профили команд — не просто cmd[0]:
COMMAND_PROFILES = {
    "git": {
        "allowed_subcommands": ["clone","fetch","worktree","add","commit",
                                 "push","diff","log","status","gc","prune"],
        "blocked_flags": ["-c", "--upload-pack", "--exec"],
    },
    "pytest": {"allowed_forms": ["pytest", "python -m pytest"]},
    "npm":    {"allowed_subcommands": ["test", "run"],
               "allowed_run_scripts": ["lint", "build", "test"]},
    "go":     {"allowed_subcommands": ["test", "vet", "build"]},
    "ruff":   {"allowed_subcommands": ["check", "format"]},
}

# Дополнительные ограничения:
# cwd whitelist: только /app/worktrees/{task_id}/ или /app/workers/{agent}/workspace/
# env whitelist: передавать минимальный env (PATH, HOME, GOPATH если нужно)
#   НЕ передавать: GIT_TOKEN_*, TELEGRAM_*, NOTION_*, GMAIL_*
#   git токен прокидывать только для git-команд через HTTPS URL с токеном в URL
# stdout/stderr cap: maxsize=10MB → если больше → truncate + WARNING в лог
# timeout: per-command из ci_policy, default 300s
```

### 6. Идемпотентность входящих задач

```python
# Telegram: хранить max processed update_id в DB (таблица kv_store или tasks meta)
#   При старте: загружать offset, передавать в bot.get_updates(offset=last+1)
#   Дубликаты при рестарте: impossible если offset корректно обновляется

# Email: хранить Message-ID каждого обработанного письма в DB
#   При обработке: SELECT COUNT(*) WHERE message_id=? → если > 0 → skip
#   Таблица: processed_emails(message_id TEXT PRIMARY KEY, task_id INTEGER, processed_at DATETIME)
```

### 7. Редактирование секретов в логах

```python
# supervisor/log_utils.py:
REDACT_PATTERNS = [
    (r'ghp_[A-Za-z0-9]+',         '[GITHUB_TOKEN]'),
    (r'glpat-[A-Za-z0-9_-]+',     '[GITLAB_TOKEN]'),
    (r'secret_[A-Za-z0-9]+',      '[NOTION_TOKEN]'),
    (r'https://[^:@\s]+:[^@\s]+@', 'https://[REDACTED]@'),
    (r'AAF[A-Za-z0-9_-]{30,}',    '[TG_TOKEN]'),
]
def redact(text: str) -> str: ...  # применить все паттерны

# redact() вызывается ДО любой записи в файл или БД
# команды логируются ПОСЛЕ redact (особенно git push с URL-токеном)
```

### 8. Валидация конфига на старте

```python
# supervisor/config_validator.py:
# validate_agents_yaml(config) → list[str] (список ошибок)
#   - каждый active worker имеет reviewer_id если is_reviewer не задан
#   - все git_token_env существуют в os.environ
#   - delivery_policy.mode ∈ {push, pr, patch_to_owner}
#   - clone_strategy ∈ {mirror, shallow, partial, sparse}
#
# Если ошибки → logger.critical + TG алерт (если TG доступен) + sys.exit(1)
# Запускается первым в main.py до старта любых asyncio tasks
```

### 9. Claude Hooks — read-only guard (не полный запрет)

**Проблема:** "воркер не запускает команды" — только policy в CLAUDE.md, не hard guarantee.

**Решение:** Claude Code Hooks с read-only allowlist.
Полный запрет Bash снижает качество (агент не может посмотреть структуру проекта, diff, grep).
Вместо этого: разрешить диагностические read-only команды, заблокировать всё опасное.

В каждой директории воркера: `workers/{agent}/.claude/settings.json`:
```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python /app/supervisor/hooks/guard_bash.py"
          }
        ]
      }
    ]
  }
}
```

`supervisor/hooks/guard_bash.py`:
```python
# Читает tool input из stdin (JSON {"tool":"Bash","input":{"command":"..."}})
#
# РАЗРЕШИТЬ (exit 0) — только read-only команды:
ALLOWED_CMDS = ["pwd", "ls", "find", "cat", "head", "tail",
                "grep", "rg", "wc", "echo",
                "git status", "git diff", "git log", "git show"]
#
# БЛОКИРОВАТЬ (exit 2) — всегда:
BLOCKED_CMDS = ["git add", "git commit", "git push", "git reset",
                "pytest", "npm", "go test", "make", "pip install",
                "rm", "mv", "cp", "chmod", "chown", "sudo", "curl", "wget"]
BLOCKED_OPERATORS = ["&&", "||", ";", "|", ">", "<", "`", "$("]
#
# Логика:
# 1. Если command содержит BLOCKED_OPERATORS → exit(2)
# 2. Если cmd[0] в BLOCKED_CMDS → exit(2)
# 3. Если cmd[0] в ALLOWED_CMDS → realpath-проверка пути (только workspace/)
#    → если путь ok → exit(0)
# 4. Иначе → exit(2) (неизвестная команда = запрет по умолчанию)
#
# Путь: только внутри /app/workers/{agent}/workspace/{task_id}/
# Логировать каждую попытку (разрешённую и заблокированную) в supervisor.log WARNING
```

**Принцип:** агент может читать файлы и смотреть diff, но не запускает тесты/git/сеть.
Тесты/линтер/git — только supervisor через safe_exec.
Результат: даже если Claude решит запустить `git push` — hook заблокирует.

### 10. Schema migrations

**Проблема:** "schema.sql расширяется ADD-only" без версионирования → prod/local расхождение.

`storage/migrate.py`:
```python
# Таблица schema_migrations(version INTEGER PRIMARY KEY, applied_at DATETIME)
# или kv_store ключ 'schema_version'
#
# MIGRATIONS = [
#   (1, "ALTER TABLE tasks ADD COLUMN locked_by TEXT"),
#   (2, "ALTER TABLE tasks ADD COLUMN lease_token TEXT"),
#   (3, "CREATE TABLE task_runs (...)"),
#   ...
# ]
#
# apply_migrations(conn):
#   current = get_version(conn)
#   for version, sql in MIGRATIONS:
#     if version > current:
#       conn.execute(sql)
#       set_version(conn, version)
#       logger.info(f"Migration {version} applied")
#
# Вызывать в main.py до config_validator, один раз при старте
# Идемпотентно: повторный запуск ничего не ломает
```

### 11. Lease token — защита от stale owner

**Проблема:** воркер A взял lease → истёк → воркер B взял → A проснулся и сделал release_lease().
Без lease_token — A убьёт lease B.

```sql
ALTER TABLE tasks ADD COLUMN lease_token TEXT;
```

```python
# acquire_lease: генерировать UUID, сохранить в lease_token
# renew_lease(task_id, locked_by, lease_token) → UPDATE только если все три совпадают
# release_lease(task_id, locked_by, lease_token) → аналогично
# Если rowcount=0 → наш lease уже не актуален → не паниковать, логировать WARNING
```

### 12. Разделение полей attempt

**Проблема:** `attempt` перегружен — воркер, ревьюер, краш, health — это разные счётчики.

```sql
-- Убрать generic attempt из tasks, добавить:
ALTER TABLE tasks ADD COLUMN worker_attempt    INTEGER DEFAULT 0;
ALTER TABLE tasks ADD COLUMN review_iteration  INTEGER DEFAULT 0;
ALTER TABLE tasks ADD COLUMN last_error_code   INTEGER;
ALTER TABLE tasks ADD COLUMN last_error_reason TEXT;
-- В task_runs: attempt = номер попытки внутри конкретной phase
```

### 13. json_guard: разделение ответственности

**Проблема:** json_guard не должен писать файлы — это обязанность `run_logger.py`.

```python
# json_guard.py:
#   extract_json(raw: str) → dict | None     (только парсинг)
#   validate_*_schema(obj) → (bool, str)     (только валидация)
#   НЕ пишет ничего в файлы/DB
#
# run_logger.py (новый модуль):
#   log_run(task_id, phase, stdout, stderr, parsed, json_valid) → (stdout_path, stderr_path)
#   - redact(stdout), redact(stderr) → записать в logs/worker_{id}_{task}_{ts}.log
#   - вернуть пути
#   - создать запись в task_runs с путями
```

### 14. TG idempotency: транзакционная обработка

**Проблема:** "дубликаты невозможны" слишком сильная гарантия. Краш между
"task создан" и "update_id сохранён" даст дубликат.

```sql
CREATE TABLE processed_tg_updates (
  update_id  INTEGER PRIMARY KEY,
  task_id    INTEGER,
  processed_at DATETIME
);
```

```python
# В одной DB-транзакции:
# 1. SELECT COUNT(*) FROM processed_tg_updates WHERE update_id=?
# 2. Если 0 → создать task + INSERT INTO processed_tg_updates
# 3. COMMIT
# Атомарность → дубликат невозможен даже при краше
```

### 15. safe_exec: realpath проверка (symlink escape)

```python
# Текущая проверка cwd.startswith('/app/worktrees/') обходится симлинками.
# Правильно:
cwd_real = os.path.realpath(cwd)
allowed_roots = ['/app/worktrees/', '/app/workers/']
assert any(cwd_real.startswith(r) for r in allowed_roots)
# Для файловых аргументов (paths) — аналогично нормализовать через realpath
```

### 16. Docker: user 1000 + claude auth path

**Проблема:** `user: "1000:1000"` + `~/.claude:/root/.claude` — конфликт.
User 1000 не имеет доступа к /root/.

```yaml
# docker-compose.yml исправить:
services:
  supervisor:
    user: "1000:1000"
    environment:
      - HOME=/home/app
    volumes:
      - ~/.claude:/home/app/.claude   # ← правильный путь
```

### 17. Пиннинг версии Claude CLI

**Проблема:** native install автообновляется в фоне → ночью может всё поменяться.

```dockerfile
# Dockerfile: установить конкретную версию, отключить автообновления
# или использовать package manager без auto-update

# В main.py: логировать при старте
import subprocess
version = subprocess.run(['claude', '--version'], capture_output=True, text=True).stdout.strip()
logger.info(f"Claude CLI version: {version}")

# В health snapshot: включать версию Claude CLI
```

### 18. Health agent: политика для supervisor/ файлов

**Проблема:** health agent в maintenance режиме может поменять `supervisor/*.py` или `storage/*.py` без review.

```python
# В run_health_task(): перед выполнением проверять changed_files из worker JSON
# Если любой файл из: supervisor/, storage/, integrations/, config/agents.yaml
#   → delivery_policy: patch_to_owner (не применять автоматически)
#   → TG: "Health agent хочет изменить системный файл. Diff прикреплён."
#   → pending_approval снова
# Для безопасных файлов (workers/*/CLAUDE.md, logs/, данные) → автоприменение ок
```

---

## Архитектура потока данных

### Основной цикл (задания от клиентов)

```
[Telegram / Email]
       │ входящее задание
       ▼
supervisor/main.py (asyncio, 24/7)
       │
       ├─ router.py → worker_id
       ├─ db.py → task(status=pending)
       │
       ▼
lease_manager: pending → running (атомарно)
       │
       ▼
repo_manager: prepare worktrees/{task_id}/{alias}/
       │
       ▼
claude_runner → workers/{job}_worker/ → json_guard → validate_worker_schema
       │
  status?
  ├─ done + confidence≥threshold → reviewer cycle
  ├─ blocked/low confidence      → escalation (Сценарий 1/2)
  └─ error / invalid JSON        → json_agent fallback → Сценарий 3
       │
       ▼ reviewer cycle
claude_runner → workers/{job}_reviewer/ → json_guard → validate_reviewer_schema
       │
  verdict?
  ├─ APPROVED → safe_exec: ci_policy tests → commit → push → Notion + TG
  └─ NEEDS_CHANGES → (iter≤max) worker+feedback / (iter>max) Сценарий 4
```

### Health-monitor цикл (живучесть системы)

```
main.py: schedule_health_check() → каждые N дней в 03:00 UTC
       │
       ▼
health_monitor.py: build_snapshot() → текстовый срез системы
  (db агрегаты, диск, хвост supervisor.log, snippets worker логов)
       │
       ▼
claude_runner → workers/health_monitor/ → json_guard → validate_health_schema
       │
  overall?
  ├─ GREEN  → Notion event (без TG)
  ├─ YELLOW → TG короткий алерт + Notion
  └─ RED    → TG красный алерт + Notion
       │
  auto_actions? → safe_exec (только из allowlist: gc, prune, cleanup)
       │
  create_tasks? → db: task(status=pending_approval)
                  TG: "health предлагает задачу: {title}. /approve {id} или /reject {id}"
```

### pending_approval flow

```
health_monitor создаёт task(status=pending_approval)
       │
       ▼ TG → владелец
"task#42: Fix repeated timeouts in job1_worker.
 /approve 42 [+ пояснения] или /reject 42 [+ причина]"
       │
  /approve 42 [текст]  → task.status=pending + заметка в task.context
                        → health_monitor берёт как executor (Maintenance-режим)
  /reject 42 [текст]   → task.status=rejected + причина → Notion event
  ответный текст       → диалог с health_monitor через эскалацию
                        (несколько итераций пока не придут к решению)
```

---

## Фаза 0 — Safety & Determinism primitives

**Без этого система 24/7 сломается. Реализуется первым.**

### `supervisor/lease_manager.py`

```python
# Атомарный захват задачи через DB transaction:
# pending → running только если locked_until < now ИЛИ locked_by is None
#
# schema.sql добавить в tasks: locked_by TEXT, locked_until DATETIME, attempt INTEGER
#
# acquire_lease(task_id, worker_id, ttl=300) → bool
# renew_lease(task_id, worker_id, ttl=300)   ← asyncio task каждые ttl/2 сек
# release_lease(task_id)                      ← при завершении/ошибке
#
# Heartbeat: supervisor пишет data/heartbeat.txt каждые 60 сек
# Docker HEALTHCHECK читает этот файл (см. docker-compose)
```

### `supervisor/json_guard.py`

```python
# Двухступенчатый парсинг для всех агентов:
#
# extract_json(raw: str) -> dict | None
#   1. между маркерами <<<JSON>>> ... <<<END>>>
#   2. fallback: regex первого {} блока + json.loads
#
# validate_worker_schema(obj) → (bool, error_msg)
#   status ∈ {done, blocked, error}, confidence 0..100, result{repos,notes}, question
#
# validate_reviewer_schema(obj) → (bool, error_msg)
#   verdict ∈ {APPROVED, NEEDS_CHANGES}, feedback str, issues list
#
# validate_health_schema(obj) → (bool, error_msg)
#   health.overall ∈ {GREEN, YELLOW, RED}, findings list, auto_actions list, create_tasks list
#
# JSON-agent fallback (только если extract провалился):
#   claude --print "Извлеки JSON по схеме X. Только JSON." --no-interactive
#   1 попытка, timeout 30s → если всё равно невалидно → вернуть None
#
# В DB сохраняет ТОЛЬКО: parsed_json TEXT, json_valid INTEGER (0|1)
# raw_stdout/raw_stderr — НЕ в DB, только в файлах через run_logger.py
```

### `supervisor/safe_exec.py`

```python
# Все внешние команды через safe_exec — никогда shell=True
# safe_exec(cmd: list[str], cwd: str, timeout: int, env_extra: dict) → (stdout, stderr, returncode)
#
# Проверки (в порядке):
# 1. cmd[0] в COMMAND_PROFILES
# 2. subcommand/flags проверены по профилю (см. Архитектурные решения п.5)
# 3. cwd начинается с /app/worktrees/ или /app/workers/*/workspace/
# 4. env = MINIMAL_ENV + env_extra (без секретов — GIT_TOKEN_* только через URL)
# 5. stdout/stderr читаются в память с cap 10MB → truncate если больше
# 6. returncode != 0 → WARNING в лог (с redact)
#
# Health auto_actions (Python-функции, не shell):
#   cleanup_worktrees() → repo_manager.cleanup_old_worktrees()
#   rotate_logs()       → удалить logs/ старше LOG_RETENTION_DAYS
#   gc_mirrors()        → git gc в repos_cache/
#   release_stale_leases() → lease_manager.release_stale()
#
# DENYLIST паттернов (проверять в cmd аргументах):
#   rm, sudo, mkfs, dd, shutdown, reboot, chmod 777, chown -R /
#   shell операторы невозможны без shell=True
#
# Воркер НИКОГДА не запускает команды — только меняет файлы + JSON на stdout
# Все git/test/build — supervisor через safe_exec
```

### `supervisor/repo_manager.py`

```python
# Структура:
#   repos_cache/{job}/{alias}.git/              ← bare mirror
#   worktrees/{task_id}/{alias}/                ← изолированная копия на задачу
#   workers/{job}_worker/workspace/{task_id}/   ← СИМЛИНК → /app/worktrees/{task_id}/
#
# ensure_mirror(job, alias, git_url, token) → создать/обновить bare mirror
# prepare_worktree(task_id, job, alias)     → создать из mirror + создать симлинк
# cleanup_worktree(task_id, job)            → удалить симлинк, потом worktree
#
# Симлинк для MCP совместимости:
#   os.symlink(f'/app/worktrees/{task_id}',
#              f'workers/{job}_worker/workspace/{task_id}')
#   Claude запускается с cwd=workers/{job}_worker/ → видит .mcp.json
#   В CLAUDE.md путь фиксирован: workspace/{task_id}/{alias}/
#
# clone_strategy из agents.yaml:
#   mirror / shallow (--depth 1) / partial (--filter=blob:none) / sparse
#
# cleanup_old_worktrees() → удалить worktrees старше WORKTREE_RETENTION_DAYS
#   (вызывается health auto_actions и nightly housekeeping)
#
# Nightly housekeeping (00:00 UTC):
#   git gc + prune в mirrors
#   cleanup_old_worktrees()
#   ротация logs/ старше LOG_RETENTION_DAYS
#   lease_manager.release_stale() (tasks в running без renewal > WORKER_TIMEOUT)
```

---

## Протоколы агентов (stdout с маркерами)

Все агенты печатают JSON **только между маркерами** (правило в каждом CLAUDE.md):

### Worker

```
<<<JSON>>>
{
  "status": "done|blocked|error",
  "confidence": 85,
  "result": {
    "repos": [{"alias": "api", "changed_files": ["a.py"], "entrypoint": null}],
    "notes": "что сделал"
  },
  "question": null
}
<<<END>>>
```

### Reviewer

```
<<<JSON>>>
{
  "verdict": "APPROVED|NEEDS_CHANGES",
  "feedback": "общее мнение",
  "issues": [{"repo":"api","file":"a.py","line":42,"type":"bug","message":"..."}]
}
<<<END>>>
```

### Health Monitor

```
<<<JSON>>>
{
  "status": "done|blocked|error",
  "confidence": 80,
  "health": {
    "overall": "GREEN|YELLOW|RED",
    "findings": [
      {"severity": "low|med|high", "title": "...", "evidence": "...", "suggestion": "..."}
    ],
    "auto_actions": ["cleanup_worktrees", "gc_mirrors"],
    "create_tasks": [
      {"title": "Fix repeated timeouts in job1_worker", "summary": "...", "priority": "P2"}
    ],
    "tg_alert": "RED: 3 задачи в requires_manual, диск 89%. [Notion →](url)",
    "notion_detail": "подробный markdown блок для Notion"
  },
  "question": null
}
<<<END>>>
```

**Контекст для worker при перезапуске с feedback:**
```
Задача: {original_task}
Попытка: {attempt}/3

Предыдущий результат: {prev_result.notes}
Замечания ревьюера:
- api/a.py:42 [bug] refresh token не инвалидируется

Исправь только указанные замечания.
```

---

## Полная схема эскалации

```
Сценарий 1 — blocked/low confidence
  supervisor_reasoning() → claude --print "воркер заблокирован: {q}. Контекст: {ctx}"
  ├─ supervisor уверен → директива воркеру → перезапуск
  └─ нет → TG + Notion → await_owner_response(timeout=24h)

Сценарий 2 — blocked с partial result (застрял посередине)
  → partial сохранить в DB → та же логика + контекст включает partial

Сценарий 3 — claude CLI упал / timeout / invalid JSON после json_agent
  → log stderr в logs/worker_{id}_{task}_{ts}.log
  → attempt += 1; если attempt < max_attempts → retry 60s
  → если attempt >= max → TG красный + Notion + task=requires_manual

Сценарий 4 — reviewer исчерпал max_review_iterations
  → TG: "task#{id}: {N} итераций, не принято. [Notion →](url)" + diff файлом
  → task=requires_manual → ждать /approve или /reject

Сценарий 5 — git push упал
  → retry 3×5min → TG алерт + Notion → task=requires_manual

pending_approval flow (health_monitor tasks):
  /approve {id} [текст] → task=pending + заметка → health_monitor как executor
  /reject {id} [текст]  → task=rejected + причина → Notion event
  ответный текст        → route_owner_reply() → диалог через эскалацию
```

---

## `supervisor/health_monitor.py`

```python
# build_snapshot(db, config) → str
#   Супервайзор собирает срез (агент не читает DB/диск сам):
#   - now, git sha supervisor
#   - tasks агрегаты: по статусам, errors за 24ч, avg cycle time, топ причин падений
#   - tasks в requires_manual (список)
#   - диск: du worktrees/, repos_cache/, logs/ + df свободно
#   - последние 100 строк logs/supervisor.log
#   - 3 последних worker_...log, snippets ошибок (первые 500 символов каждого)
#   - stale leases: tasks в running без renewal > WORKER_TIMEOUT / 2
#
# run_health_check(db, config) → health_result
#   1. build_snapshot()
#   2. claude_runner → workers/health_monitor/ → json_guard.validate_health_schema
#   3. execute auto_actions (через safe_exec / python функции, строго allowlist)
#   4. create pending_approval tasks для create_tasks
#   5. если RED/YELLOW → TG + Notion; если GREEN → только Notion
#
# run_health_task(task, db, config) → (Maintenance-режим, после /approve)
#   Та же логика что run_worker_cycle, но:
#   - воркер = workers/health_monitor/
#   - нет reviewer cycle (health агент сам проверяет свою работу)
#   - диалог с владельцем через обычную эскалацию
```

---

## MCP политика

### Обязательный Context7 (все воркеры + health_monitor)

**Подключение** (один раз в каждой директории агента, часть `make setup-mcp`):
```bash
cd workers/{job}_worker
claude mcp add --transport http --scope project context7 https://mcp.context7.com/mcp
# Создаёт .mcp.json в директории агента
```

**Правило в CLAUDE.md каждого воркера:**
```markdown
## Обязательное правило: Context7
Если задача касается сторонней библиотеки, фреймворка, API или конфига инструментов:
1. Сначала запроси актуальную документацию через Context7
2. Только после этого пиши/правь код
3. Если дока противоречит памяти модели — верить доке
```

### Распределение MCP по агентам

| Агент | MCP | Причина |
|-------|-----|---------|
| `{job}_worker` | context7 | актуальные API и библиотеки |
| `{job}_reviewer` | context7 | проверка правильности использования API |
| `health_monitor` | context7 | при maintenance задачах с кодом |
| supervisor (main.py) | — | Python код, не Claude CLI |

**Правило:** воркеры не получают MCP которые мутируют внешний мир
(github write, notion write, slack). Мутации только через supervisor.

### Опциональные MCP (подключать на конкретной работе)

- `github` MCP — если reviewer нужен контекст PR/issues (read-only)
- `linear`/`jira` MCP — если задачи приходят из трекера
- `sentry` MCP — если работа про прод-ошибки

---

## Модули — детали

### `supervisor/main.py`

```python
# asyncio tasks:
# - telegram_polling()
# - email_polling() (каждые POLL_INTERVAL_SECONDS)
# - dispatch_pending_tasks() (каждые POLL_INTERVAL_SECONDS, max_concurrent из config)
# - heartbeat_writer() (каждые 60s → data/heartbeat.txt)
# - schedule_at(hour=9, coro=run_daily_summary)
# - schedule_periodic(hour=3, every_days=N, coro=run_health_check)
# - schedule_at(hour=0, coro=nightly_housekeeping)
# - graceful shutdown на SIGTERM → release все leases

# run_worker_cycle(task):
#   lease → worktrees → worker → json_guard → escalation | review → git → notion+tg

# run_health_task(task):
#   lease → health_monitor (maintenance режим) → notion+tg
#   нет worktrees, нет reviewer, диалог через эскалацию
```

### `supervisor/escalation.py`

```python
# handle_worker_blocked(task, output)          → Сценарии 1/2
# handle_worker_crash(task, stderr, code)      → Сценарий 3
# handle_review_exhausted(task, feedback)      → Сценарий 4
# handle_git_failure(task, error)              → Сценарий 5
# handle_pending_approval(task)                → pending_approval flow
# await_owner_response(escalation_id, t=86400) → asyncio.Event (timeout → повтор TG)
# route_owner_reply(text)                      → найти открытый escalation → resolve

# supervisor_reasoning(question, task_ctx):
#   claude --print "воркер спрашивает: {question}. Контекст: {ctx}" --no-interactive
#   → json_guard (supervisor тоже выдаёт JSON: {answer, confidence})
```

### `integrations/notion_handler.py`

```python
# pip: notion-client; NOTION_TOKEN + NOTION_DATABASE_ID в .env
#
# create_task_page(task) → page_url (сохранить в tasks.notion_page_url)
# append_event(task_id, event_type, data)
# append_health_report(date, health_result)
# append_digest(date, content)
#
# Fallback при недоступности Notion: логировать в logs/notion_fallback_{date}.md
# Не падать — предупреждать в supervisor.log уровень WARNING
```

### `integrations/telegram_handler.py`

```python
# Идемпотентность: хранить last_update_id в DB (kv_store таблица)
#   get_updates(offset=last_update_id+1) → обработать → сохранить новый offset
#   При рестарте: загрузить offset из DB → дубликаты невозможны
#
# Политика уведомлений — максимально коротко:
# "job1/api/task#42: NEEDS_CHANGES (2/3). [Notion →](url)"
# "task#42: APPROVED ✓. [Notion →](url)"
# "task#42: requires_manual. [Notion →](url)"
# "[RED] диск 89%, 3 requires_manual. [Notion →](url)"
# "Health предлагает: Fix timeouts. /approve 42 или /reject 42"
#
# Fallback если TG недоступен: логировать в logs/tg_fallback_{date}.log + retry 3×
#
# Commands:
# /status          → активные задачи из DB
# /summary         → дайджест сейчас
# /approve {id} [текст] → pending_approval → pending (+ заметка)
# /reject {id} [текст]  → rejected (+ причина)
# свободный текст  → route_owner_reply() → диалог через эскалацию
```

---

## `config/agents.yaml` — полная схема

```yaml
supervisor:
  model: claude-opus-4-6
  max_concurrent: 2
  confidence_threshold: 70
  escalation_threshold_hours: 24
  daily_summary_hour_utc: 9
  health_check_hour_utc: 3
  health_check_every_days: 1

workers:
  health_monitor:
    model: claude-opus-4-6
    active: true
    is_health: true
    max_attempts: 2
    confidence_threshold: 60
    mcp:
      required: ["context7"]
      optional: []

  job1_worker:
    model: claude-opus-4-6
    lang: python
    active: true
    confidence_threshold: 75
    max_review_iterations: 3
    max_attempts: 3
    reviewer_id: job1_reviewer
    mcp:
      required: ["context7"]
      optional: []

    repos:
      - alias: api
        url: "https://github.com/org/api"
        token_env: GIT_TOKEN_JOB1
        clone_strategy: shallow      # mirror|shallow|partial|sparse
        sparse_paths: []
        max_disk_mb: 500

    branching_policy:
      pattern: "ai/task-{task_id}"
      base: "main"
      protected: ["main", "master", "production"]

    delivery_policy:
      mode: "push"                   # push|patch_to_owner (pr — деферировано, не реализовано в Фазах 0-3)

    ci_policy:
      run_before_push:           # argv-списки для safe_exec (не строки!)
        - ["pytest"]
        - ["ruff", "check", "."]
      required_pass: true

    style_policy:
      formatters:                # argv-списки для safe_exec
        - ["ruff", "format", "."]
      run_before_commit: true

  job1_reviewer:
    model: claude-opus-4-6
    is_reviewer: true
    reviews_for: job1_worker
    active: true
    mcp:
      required: ["context7"]
      optional: []
```

---

## `workers/health_monitor/CLAUDE.md` — шаблон

```markdown
# Роль: SRE/оператор оркестратора

Ты работаешь в двух режимах. Определяй режим по содержимому входящего контекста.

## Режим 1: AUDIT (плановая проверка здоровья)
Получаешь: snapshot системы (статистика, диск, логи).
Задача:
1. Классифицируй здоровье: GREEN / YELLOW / RED
2. Найди findings с evidence из данных (не придумывай)
3. Предложи auto_actions только из allowlist:
   cleanup_worktrees, rotate_logs, gc_mirrors, release_stale_leases
4. Если нужна ручная работа — сформулируй create_tasks (title + summary + priority)
5. Если YELLOW/RED — подготовь tg_alert (1 строка) и notion_detail (markdown)

## Режим 2: MAINTENANCE (исполнение одобренной задачи)
Получаешь: конкретную задачу + контекст (что нашли, почему создали).
Задача: выполни задачу, измени нужные файлы (конфиги, CLAUDE.md воркеров, скрипты).
Используй Context7 если задача касается инструментов/библиотек.
Если нужно уточнение у владельца — заполни question.

## Обязательное правило: Context7
Если задача касается библиотек, фреймворков или конфигов инструментов —
сначала запроси актуальную документацию через Context7.

## НЕ делай
- Не запускай команды
- Не читай бинарные файлы (sqlite)
- Не выдумывай данные которых нет в snapshot

## Вывод — только между маркерами:
<<<JSON>>>
{
  "status": "done|blocked|error",
  "confidence": 0-100,
  "health": {
    "overall": "GREEN|YELLOW|RED",
    "findings": [{"severity":"low|med|high","title":"...","evidence":"...","suggestion":"..."}],
    "auto_actions": [],
    "create_tasks": [{"title":"...","summary":"...","priority":"P1|P2|P3"}],
    "tg_alert": "...",
    "notion_detail": "..."
  },
  "question": null
}
<<<END>>>
```

---

## `workers/{job}_worker/CLAUDE.md` — обновлённый шаблон

```markdown
# Роль: разработчик для {job_name}

## Стек
{tech_stack}

## Рабочая директория
workspace/{task_id}/{repo_alias}/
(симлинк на /app/worktrees/{task_id}/{repo_alias}/)
Не меняй файлы вне своего worktree.

## Обязательное правило: Context7
Если задача касается сторонней библиотеки, фреймворка, API или конфига:
1. Сначала запроси актуальную документацию через Context7
2. Только после этого пиши/правь код
3. Если дока противоречит твоей памяти — верить доке

## НЕ делай
- НЕ запускай git add/commit/push
- НЕ запускай тесты, сборку, линтеры
- НЕ используй rm, sudo и системные команды
- Только читай и меняй файлы в своём worktree

## Вывод — только между маркерами:
<<<JSON>>>
{
  "status": "done|blocked|error",
  "confidence": 0-100,
  "result": {
    "repos": [{"alias":"...","changed_files":[...],"entrypoint":null}],
    "notes": "что сделал"
  },
  "question": null
}
<<<END>>>
```

---

## Структура директорий на VPS

```
work/
├── supervisor/            ← Python код
├── workers/
│   ├── job1_worker/       ← CLAUDE.md + .mcp.json + workspace/
│   ├── job1_reviewer/     ← CLAUDE.md + .mcp.json
│   └── health_monitor/    ← CLAUDE.md + .mcp.json + workspace/
├── repos_cache/
│   └── job1/
│       └── api.git/       ← bare mirror
├── worktrees/
│   └── {task_id}/
│       └── api/           ← рабочая копия для задачи
├── logs/
│   ├── supervisor.log
│   ├── notion_fallback_YYYY-MM-DD.md
│   └── worker_{id}_{task}_{ts}.log
├── data/
│   ├── orchestrator.db
│   └── heartbeat.txt      ← Docker HEALTHCHECK
└── config/
    └── agents.yaml
```

---

## `docker-compose.yml`

```yaml
services:
  supervisor:
    build: .
    restart: always
    user: "1000:1000"
    healthcheck:
      test: ["CMD", "python", "-c",
             "import os,time,sys; h=os.stat('data/heartbeat.txt').st_mtime;
              sys.exit(0 if time.time()-h<120 else 1)"]
      interval: 60s
      timeout: 10s
      retries: 3
      start_period: 30s
    volumes:
      - ./data:/app/data
      - ./workers:/app/workers
      - ./repos_cache:/app/repos_cache
      - ./worktrees:/app/worktrees
      - ./logs:/app/logs
      - ~/.claude:/home/app/.claude    # ← /home/app, не /root (user 1000!)
    env_file: .env
    environment:
      - PYTHONUNBUFFERED=1
      - HOME=/home/app               # ← обязательно для claude auth
```

---

## `.env` (все переменные)

```bash
# Telegram
TELEGRAM_BOT_TOKEN=...
TELEGRAM_OWNER_CHAT_ID=...

# Email
GMAIL_USER=...
GMAIL_APP_PASSWORD=...
IMAP_SERVER=imap.gmail.com
SMTP_SERVER=smtp.gmail.com

# Notion
NOTION_TOKEN=secret_...
NOTION_DATABASE_ID=...

# Git tokens (по одному на работу)
GIT_TOKEN_JOB1=ghp_...

# System
DB_PATH=data/orchestrator.db
LOG_LEVEL=INFO
POLL_INTERVAL_SECONDS=30
CLAUDE_CLI_PATH=claude

# Timing
DAILY_SUMMARY_HOUR_UTC=9
HEALTH_CHECK_HOUR_UTC=3
HEALTH_CHECK_EVERY_DAYS=1

# Limits
MAX_CONCURRENT_WORKERS=2
WORKER_LEASE_TTL_SECONDS=300
WORKER_TIMEOUT_SECONDS=1800
LOG_RETENTION_DAYS=30
WORKTREE_RETENTION_DAYS=7
```

---

## Фазы реализации

### Фаза 0 — Safety primitives
1. `log_utils.py` — redact() (первым: логи не должны течь с самого старта)
2. `storage/migrate.py` — schema versioning + идемпотентные миграции (второй)
3. `storage/schema.sql` — базовые таблицы + все миграции пронумерованы
4. `config_validator.py` — fail-fast валидация agents.yaml + claude --version логирование
5. `lease_manager.py` — state machine + атомарный UPDATE + lease_token + WAL/busy_timeout
6. `json_guard.py` — три схемы (worker, reviewer, health) + json_agent fallback (только парсинг)
7. `run_logger.py` — запись stdout/stderr в файлы с redact, запись в task_runs
8. `safe_exec.py` — профильный allowlist + realpath check + env whitelist + stdout cap
9. `repo_manager.py` — mirror + worktree + symlink + housekeeping
10. `supervisor/hooks/guard_bash.py` — Claude hook: read-only allowlist (pwd/ls/grep/git diff) + блокировка опасных
11. В каждом `workers/{agent}/.claude/settings.json` — PreToolUse hook на Bash
12. `data/heartbeat.txt` механизм
13. Тест: два acquire_lease параллельно → только один успешен (lease_token правильный)
14. Тест: safe_exec(['rm','-rf','/'], ...) → ошибка; symlink escape → ошибка
15. Тест guard_bash.py: `pwd` → exit(0) разрешено; `ls workspace/{task_id}/` → exit(0);
    `git diff` → exit(0); `git push` → exit(2) блокировка; `rm -rf /` → exit(2);
    `ls workspace/ && git push` → exit(2) (shell-оператор); путь вне workspace → exit(2)

### Фаза 1 — Telegram + main.py
1. `telegram_handler.py` (polling + update_id idempotency, все команды)
2. `email_handler.py` (IMAP + Message-ID dedup)
3. `main.py` (event loop, config_validator первым, все scheduled tasks, heartbeat_writer)
4. Тест: TG сообщение → task в DB; рестарт → дубликата нет

### Фаза 2 — Воркер-цикл
1. CLAUDE.md шаблоны (worker + reviewer) с Context7 правилом
2. `run_worker_cycle()`: lease → worktrees → claude → json_guard → review
3. Тест: задача → JSON с маркерами → parse OK → review cycle

### Фаза 3 — Code review
1. Reviewer CLAUDE.md + .mcp.json
2. Итерации с structured feedback
3. safe_exec: ci_policy → commit → push
4. Тест: bug → NEEDS_CHANGES → fix → APPROVED → git push

### Фаза 4 — Эскалация + pending_approval
1. `escalation.py` (все 5 сценариев)
2. pending_approval flow (/approve, /reject, диалог)
3. Тест каждого сценария

### Фаза 5 — Notion + email + summarizer
1. `notion_handler.py` с fallback
2. Обновить все уведомления: TG коротко + Notion подробно
3. `summarizer.py` + scheduler
4. `email_handler.py`

### Фаза 6 — Health Monitor
1. `health_monitor.py` (build_snapshot + run_health_check + run_health_task)
2. `workers/health_monitor/CLAUDE.md`
3. Scheduler в main.py
4. validate_health_schema в json_guard
5. auto_actions через safe_exec
6. Тест: искусственно заполнить requires_manual → RED алерт в TG

### Фаза 7 — Docker + VPS deploy
1. `docker-compose.yml` с HEALTHCHECK
2. `Dockerfile` (Python + claude CLI)
3. `Makefile`
4. Деплой → `make setup-mcp` → `claude auth login` → `docker-compose up -d`

*(Future / Фаза 8: GraphRAG Memory — граф связей задач/файлов/решений в SQLite.
Воркер получает релевантный контекст из прошлых задач через graph traversal.
Таблицы: task_files, task_relations, task_summaries. Без Neo4j, без embeddings на старте.
Источник идеи: MiroFish (github.com/666ghj/MiroFish) — адаптировано под наш стек.)*

*(Future / Фаза 9: одноразовые воркер-контейнеры. Дизайн через safe_exec и
worktrees это уже позволяет — реализовывать после стабильной Фазы 7.)*

*(Future / Фаза 10: Meeting Agent — отдельный контур, не смешивать с кодовым
оркестратором. Поэтапно: (1) "тихий агент" — слушает, транскрибирует, конспект
в Notion + подсказки в TG; (2) "push-to-talk" — твой голос через ElevenLabs,
только после твоего подтверждения; (3) автономный режим для низкорисковых митингов
(standups, статусные синки) с жёсткими guardrails. Требует отдельный API-стек
STT+LLM+TTS, platform_bridge для Zoom/Teams/Meet, policy_guard и consent policy.
Не использует claude CLI — нужен real-time API для low-latency диалога.)*

---

## Makefile (ключевые команды)

```makefile
setup-mcp:
    @for dir in workers/*/; do \
        echo "Настройка MCP в $$dir"; \
        cd $$dir && claude mcp add --transport http --scope project \
            context7 https://mcp.context7.com/mcp; \
        cd ../..; \
    done

test:       pytest tests/ -v
run:        python -m supervisor.main
logs:       docker-compose logs -f supervisor
deploy:     docker-compose up -d --build
shell:      docker-compose exec supervisor bash
health:     python -c "from supervisor.health_monitor import build_snapshot; print(build_snapshot(None,None))"
```

---

## Верификация (end-to-end)

```bash
# Фаза 0: Safety
pytest tests/ -v
python -c "from supervisor.json_guard import extract_json; assert extract_json('<<<JSON>>>{}<<<END>>>')"
python -c "from supervisor.safe_exec import safe_exec; safe_exec(['rm','-rf','/'], '.')"  # → ошибка

# Фаза 6: Health Monitor
python -c "from supervisor.health_monitor import build_snapshot; print(build_snapshot(db, cfg))"
# искусственно: INSERT INTO tasks (status) VALUES ('requires_manual') 5 раз
# → run_health_check() → TG красный алерт

# Docker HEALTHCHECK
docker-compose up -d
docker inspect --format='{{.State.Health.Status}}' supervisor  # → healthy

# Full e2e
# TG сообщение → DB → воркер → review → git push → TG алерт → Notion страница
```

---

## Критические файлы

| Файл | Действие |
|------|----------|
| `supervisor/log_utils.py` | Создать (Фаза 0) |
| `storage/migrate.py` | Создать (Фаза 0) |
| `supervisor/config_validator.py` | Создать (Фаза 0) |
| `supervisor/lease_manager.py` | Создать (Фаза 0) — с lease_token |
| `supervisor/json_guard.py` | Создать (Фаза 0) — только парсинг/валидация |
| `supervisor/run_logger.py` | Создать (Фаза 0) — запись файлов + task_runs |
| `supervisor/safe_exec.py` | Создать (Фаза 0) — с realpath check |
| `supervisor/repo_manager.py` | Создать (Фаза 0) |
| `supervisor/hooks/guard_bash.py` | Создать (Фаза 0) — Claude hook: read-only allowlist |
| `workers/{agent}/.claude/settings.json` | Создать (Фаза 0) — PreToolUse hook |
| `supervisor/main.py` | Создать (Фаза 1) |
| `supervisor/escalation.py` | Создать (Фаза 4) |
| `supervisor/summarizer.py` | Создать (Фаза 5) |
| `supervisor/health_monitor.py` | Создать (Фаза 6) |
| `integrations/telegram_handler.py` | Создать (Фаза 1) — с processed_tg_updates |
| `integrations/email_handler.py` | Создать (Фаза 1) |
| `integrations/notion_handler.py` | Создать (Фаза 5) |
| `workers/health_monitor/CLAUDE.md` | Создать (Фаза 6) |
| `workers/{job}_worker/CLAUDE.md` | Создать + .mcp.json (Фаза 2) |
| `workers/{job}_reviewer/CLAUDE.md` | Создать + .mcp.json (Фаза 3) |
| `config/agents.yaml` | Расширить (Фазы 0-6) |
| `storage/schema.sql` | Расширить через migrate.py (Фаза 0) |
| `docker-compose.yml` | Создать (Фаза 7) — исправлен HOME + claude path |
| `Makefile` | Создать (Фаза 7) |
| `storage/db.py` | **НЕ ТРОГАТЬ** |
| `supervisor/router.py` | **НЕ ТРОГАТЬ** |
| `supervisor/claude_runner.py` | **НЕ ТРОГАТЬ** |
| `tests/test_db.py` | **НЕ ТРОГАТЬ** |
