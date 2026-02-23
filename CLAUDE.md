# AI Orchestration System — CLAUDE.md

## Что это

Python supervisor (asyncio, 24/7) + воркеры (claude CLI subprocess).
Задания через Telegram/Email → маршрутизация → воркер выполняет код → review → git → Notion.
Подписка Claude Code Max. **Без anthropic SDK. Без API ключей.**

Детальная архитектура: `plans/wild-humming-seal.md` (Plan v5).

---

## Инварианты проекта (не нарушать никогда)

| # | Инвариант |
|---|-----------|
| 1 | `db.py` — низкоуровневый транспорт, логику не добавлять |
| 2 | `json_guard` — только парсинг/валидация, не пишет файлы и не пишет в DB |
| 3 | `run_logger` — только запись файлов + task_runs, не валидирует JSON |
| 4 | Воркеры не запускают команды (только читают/пишут файлы + JSON stdout) |
| 5 | Все внешние команды — только через `safe_exec`, никогда `shell=True` |
| 6 | Все логи — только через `redact()`, raw stdout/stderr только в файлах |
| 7 | В DB только пути к логам, не сами строки |
| 8 | Все изменения схемы — только через `storage/migrate.py` |
| 9 | Bash hook воркеров: read-only allowlist (ls/cat/grep/git diff), блокировка записи/сети |
| 10 | `supervisor` — единственный кто пишет клиенту (TG/Email) |

---

## Файлы — НЕ ТРОГАТЬ

```
storage/db.py               ← низкоуровневый execute/query (исключение: PRAGMA busy_timeout)
supervisor/router.py        ← contact → worker_id
supervisor/claude_runner.py ← subprocess обёртка
tests/test_db.py            ← 10 тестов, должны всегда быть зелёными
```

Если нужно изменить запрещённый файл — **не меняй**, объясни почему.

---

## Ответственность модулей

```
supervisor/log_utils.py      redact() для секретов в логах
storage/migrate.py           schema versioning, идемпотентные миграции
supervisor/config_validator  fail-fast валидация agents.yaml на старте
supervisor/lease_manager.py  атомарный захват задачи + state machine
supervisor/json_guard.py     парсинг/валидация JSON из stdout агентов
supervisor/run_logger.py     запись stdout/stderr в файлы + task_runs
supervisor/safe_exec.py      профильный allowlist-runner + realpath check
supervisor/repo_manager.py   bare mirror + worktree + symlink per task
supervisor/hooks/guard_bash  read-only Bash guard для воркеров
integrations/*_handler.py   только внешний транспорт (TG, Email, Notion)
supervisor/main.py           asyncio orchestration, не бизнес-логика
```

---

## Порядок реализации (Фаза 0 — первая)

```
1. log_utils.py       ← ПЕРВЫМ: логи не должны течь с самого старта
2. migrate.py
3. config_validator.py
4. lease_manager.py
5. json_guard.py
6. run_logger.py
7. safe_exec.py
8. repo_manager.py
9. guard_bash.py + settings.json
```

Не перескакивать. Не начинать main.py раньше Фазы 0.

---

## Как просить модель (шаблон задачи)

Для каждого модуля давать **жёсткий контракт**, не "сделай модуль":

```
Реализуй `supervisor/json_guard.py` и `tests/test_json_guard.py`.

Можно менять:
- supervisor/json_guard.py
- tests/test_json_guard.py

Нельзя менять:
- storage/db.py, supervisor/router.py, supervisor/claude_runner.py, tests/test_db.py

Контракт:
- extract_json(raw: str) -> dict | None
  1. ищет между <<<JSON>>>...<<<END>>>
  2. fallback: первый JSON-блок regex + json.loads
- validate_worker_schema(obj) -> tuple[bool, str]
- validate_reviewer_schema(obj) -> tuple[bool, str]
- validate_health_schema(obj) -> tuple[bool, str]
- НЕ пишет файлы, НЕ пишет в DB, НЕ запускает subprocess

Тесты: happy path с маркерами, fallback path, невалидный JSON,
       невалидная схема worker, валидная схема health

Сначала дай план (функции + тесты), потом код.
```

---

## Definition of Done (каждого модуля)

- [ ] Модуль реализован полностью по контракту
- [ ] Unit-тесты: happy path + ошибка + 1–2 edge cases
- [ ] Нет изменений запрещённых файлов
- [ ] Нет `shell=True`
- [ ] Все ошибки логируются через `logger.warning/error` с context (task_id, phase)
- [ ] `pytest tests/ -v` — всё зелёное включая старые тесты

---

## Формат ответа модели (требовать в конце)

```
## Что сделано
## Изменённые файлы
## Добавленные тесты
## Что не сделано / риски
## Следующий шаг
```

---

## Соглашения кода

- Python 3.11+, asyncio везде где есть I/O
- snake_case / PascalCase / UPPER_SNAKE_CASE
- Каждый модуль начинается с docstring
- `import logging; logger = logging.getLogger(__name__)`
- DI вместо глобалов: передавать `db`, `now_fn`, `uuid_fn`, `runner` параметрами
- Никаких глобальных состояний

### Коды ошибок (last_error_reason)

```python
E_JSON_INVALID     = "json_invalid"
E_JSON_SCHEMA      = "json_schema_invalid"
E_LEASE_STALE      = "lease_stale"
E_LEASE_CONFLICT   = "lease_conflict"
E_SAFEEXEC_DENY    = "safeexec_denied"
E_SAFEEXEC_TIMEOUT = "safeexec_timeout"
E_GIT_PUSH_FAIL    = "git_push_failed"
E_WORKER_CRASH     = "worker_crash"
```

### Формат лог-записей

```python
logger.warning("safeexec_denied task_id=%s cmd=%s reason=%s", task_id, cmd[0], reason)
logger.error("lease_conflict task_id=%s worker=%s", task_id, worker_id)
```

---

## Тестирование

- Для файловых операций: `tmp_path` (pytest) или `tempfile.TemporaryDirectory`
- Для safe_exec: передавать `allowed_roots` параметром (не хардкодить `/app/`)
- Для lease_manager: мокать `now_fn` и `uuid_fn` через параметры
- Для claude_runner: мокать subprocess через `monkeypatch`
- Интеграционные тесты с реальным git — только для `repo_manager` (изолированно в tmp)

Запускать после каждого модуля:
```bash
pytest tests/ -v
```

---

## Стек

```
Python 3.11+ / asyncio / SQLite (WAL) / pytest
python-telegram-bot / notion-client / imaplib
claude CLI (subprocess) / GitPython
Docker Compose (VPS)
```

---

## Запуск

```bash
pip install -r requirements.txt
cp .env.example .env  # заполнить

claude --version      # проверить CLI
pytest tests/ -v      # все зелёные?
python -m supervisor.main
```

## Деплой

```bash
claude auth login
docker-compose up -d
make logs
```
