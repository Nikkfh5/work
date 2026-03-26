# Progress Tracker

## Текущий статус

| Параметр | Значение |
|----------|----------|
| Активная фаза | 2 |
| Тесты (всего) | 247 |
| Тесты (статус) | ✅ все зелёные |
| Последнее обновление | 2026-03-21 |

---

## Фаза 0 — Safety & Determinism ✅ ЗАВЕРШЕНА

- [x] `supervisor/log_utils.py` — redact() для секретов (8 тестов)
- [x] `storage/migrate.py` — schema versioning, 15 миграций (8 тестов)
- [x] `supervisor/config_validator.py` — fail-fast agents.yaml (15 тестов)
- [x] `supervisor/lease_manager.py` — атомарный захват + lease_token (12 тестов)
- [x] `supervisor/json_guard.py` — парсинг/валидация JSON (38 тестов)
- [x] `supervisor/run_logger.py` — запись файлов + task_runs (8 тестов)
- [x] `supervisor/safe_exec.py` — allowlist runner + realpath (16 тестов)
- [x] `supervisor/repo_manager.py` — bare mirror + worktree + symlink (14 тестов)
- [x] `supervisor/hooks/guard_bash.py` — Claude hook read-only (44 теста)

## Фаза 1 — Telegram/Email + main.py ✅ ЗАВЕРШЕНА

- [x] `integrations/telegram_handler.py` — polling + dedup + commands (24 теста)
- [x] `integrations/email_handler.py` — IMAP + Message-ID dedup (17 тестов)
- [x] `supervisor/main.py` — asyncio event loop + scheduling (28 тестов)

## Фаза 2 — Воркер-цикл ⏳ В ОЧЕРЕДИ

- [x] `workers/job1_worker/CLAUDE.md` — CLAUDE.md шаблон для воркера с Context7 правилом (8 тестов)
- [x] `workers/job1_worker/.mcp.json` — Context7 MCP подключение
- [x] `workers/job1_worker/.claude/settings.json` — PreToolUse hook → guard_bash.py
- [x] `run_worker_cycle()` в `supervisor/main.py` — lease → worktree → claude CLI → json_guard → cleanup
- [x] `tests/test_worker_cycle.py` — worktree setup/cleanup/error/multi-repo (8 тестов)

## Фаза 3 — Code review ✅ ЗАВЕРШЕНА

- [x] `workers/job1_reviewer/CLAUDE.md` — шаблон ревьюера (verdict/feedback/issues)
- [x] `workers/job1_reviewer/.mcp.json` + `.claude/settings.json` — Context7 + guard_bash
- [x] Review iteration flow — APPROVED → CI → push | NEEDS_CHANGES → retry (10 тестов)
- [x] `_run_ci_and_push()` — style → commit → CI → push через safe_exec
- [x] `tests/test_review_cycle.py` — 10 тестов (approved, retry, exhausted, CI fail, invalid JSON)

## Фаза 4 — Эскалация + pending_approval ✅ ЗАВЕРШЕНА

- [x] `supervisor/escalation.py` — supervisor_reasoning, handle_worker_blocked, pending_approval (8 тестов)
- [x] CRUD escalations — create/resolve/get_open_escalation + route_owner_reply
- [x] `tests/test_escalation.py` — 8 тестов (reasoning, auto-resolve, escalate, CRUD, routing)

## Фаза 5 — Summarizer (daily digest) ⏳ В ОЧЕРЕДИ

~~Notion убран~~ → заменяется GraphRAG (Фаза 8) для контекста supervisor'а.

- [ ] `supervisor/summarizer.py` — daily digest: сбор статистики за день + отправка в TG
- [ ] Условие: отправлять ТОЛЬКО если за день были задачи (не спамить пустым дайджестом)
- [ ] Подключить в main.py → schedule_at(daily_summary_hour_utc)
- [ ] `tests/test_summarizer.py`

## Фаза 6 — Health Monitor ⏳ В ОЧЕРЕДИ

- [ ] `supervisor/health_monitor.py` — build_snapshot + run_health_check + run_health_task
- [ ] `workers/health_monitor/CLAUDE.md` — шаблон для health агента
- [ ] Scheduler в main.py (health_check_hour_utc)
- [ ] auto_actions через safe_exec
- [ ] `tests/test_health_monitor.py`

## Фаза 7 — Docker + VPS deploy ⏳ В ОЧЕРЕДИ

- [ ] `docker-compose.yml` с HEALTHCHECK
- [ ] `Dockerfile` (Python + claude CLI + user 1000)
- [ ] `Makefile` (setup-mcp, test, run, logs, deploy)
- [ ] Деплой + верификация

---

## Журнал решений

| Дата | Фаза | Решение | Причина |
|------|------|---------|---------|
| 2026-02 | 0 | 15 миграций в migrate.py | Идемпотентность + версионирование |
| 2026-02 | 0 | lease_token UUID | Защита от stale owner |
| 2026-02 | 1 | kv_store для TG offset | Простота vs отдельная таблица |
| 2026-02 | 1 | asyncio_mode=auto в pytest.ini | pytest-asyncio требует |
| 2026-02 | 1 | os.environ["DB_PATH"] в фикстуре | Паттерн изоляции тестов |

## Блокеры

_Нет активных блокеров._

## Backlog рефакторинга (собрано после Фазы 2)

### Высокий приоритет (перед Фазой 3)
- [x] Создать `tests/conftest.py` — shared fixtures: `db_path`, `mock_tg_handler`, `WORKER_DONE_JSON` ✅
- [x] Использовать константы ошибок (`E_WORKER_CRASH` и т.д.) вместо строковых литералов ✅
- [x] Декомпозировать `run_worker_cycle()` → 5 private-функций ✅
- [x] `_notify_failure()` — единый helper вместо 5 copy-paste TG блоков ✅
- [x] `_fail_final`, `_set_error_reason` — из closures в обычные функции ✅

### Средний приоритет (после Фазы 3)
- [ ] `_set_error_reason` — вынести в db.py как `update_task_error_reason()` (нужно одобрение, protected file)
- [ ] `SELECT *` в dispatch → `SELECT id, assigned_worker, description`

### Низкий приоритет (после стабилизации)
- [ ] `repo_manager._run_git` → async (asyncio.create_subprocess_exec) чтобы не блокировать event loop
- [ ] Параллельный setup worktrees для N repos (asyncio.gather + run_in_executor)
- [ ] `os.getenv` в run_worker_cycle → передавать через config (DI)
- [ ] Добавить тест на cleanup_worktree failure (finally block в main.py)
- [ ] `_make_task()` → возвращать полный dict из DB, не хардкодить поля

### Идеи (обсудить на brainstorm)
- [x] Workspace path в промпте воркера ✅ (реализовано)
- [x] Git push в worker cycle ✅ (реализовано в Фазе 3: _run_ci_and_push)
- [ ] sequential thinking MCP, filesystem MCP, wcgw MCP
- [ ] Декомпозиция main.py → отдельный `supervisor/worker_cycle.py`

### Внешние референсы (посмотреть перед реализацией)

**[claude-server-kit](https://github.com/doffskiii/claude-server-kit)** — VPS setup для Claude Code CLI (тоже Max, без API). Полезность 3/10, но есть точечные модули:

- **Фаза 6 (Health):** `brain/scripts/monitor.py` — psutil мониторинг + TG алерты с cooldown. Посмотреть подход к health checks перед реализацией health_monitor.py
- **Фаза 8 (GraphRAG):** `brain/src/brain/vault/embeddings.py` — ONNX sentence-transformers на CPU, инкрементальная индексация. Референс для semantic search без GPU
- **repo_manager:** `brain/src/brain/vault/sync.py` — debounced git sync (батчинг записей в один коммит)

**[Gas Town](https://github.com/steveyegge/gastown)** — multi-agent оркестрация от Steve Yegge (Go + Claude CLI). Анализ: `Gas_Town_example.txt`. Полезные идеи:

- **Фаза 6 (Health):** Witness/Patrol паттерн — фоновый детектор зависших задач ("30 мин без обновления → эскалация"). Добавить `release_stale()` в lease_manager
- **Checkpoint/Handoff** (новая фича): при переполнении контекста Claude Code — сохранять в БД что сделано + что осталось, новая сессия продолжает. Реализация: поле `partial_result` в tasks (migrate.py) + инжект в промпт воркера при retry
- **DAG workflows (Molecules)** — многошаговые задачи с зависимостями (шаг 2 после шага 1). Пока не нужно, обсудить если появятся сложные многоэтапные ТЗ

Доп. ссылки: [maggieappleton.com/gastown](https://maggieappleton.com/gastown), [paddo.dev/blog](https://paddo.dev/blog/gastown-two-kinds-of-multi-agent/)

### Future: Фаза 8 — GraphRAG Memory (единый Knowledge Graph)

**Проблема:** воркеры stateless + нет базы знаний. Воркер не помнит прошлые задачи, а пользователь не может закинуть документы/статьи для контекста.

**Решение:** Единый Knowledge Graph в SQLite — объединяет task memory И knowledge base.

**Источник идеи:** MiroFish (github.com/666ghj/MiroFish) — seed-информация → extraction → граф.

#### Схема (SQLite, через migrate.py)

```sql
-- ═══ Сущности (люди, библиотеки, концепции, файлы, паттерны) ═══
CREATE TABLE entities (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,           -- person|library|concept|pattern|file|api|module
    description TEXT,
    source_type TEXT NOT NULL,    -- task|document|manual
    source_id TEXT,               -- task_id или document_id
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ═══ Связи между сущностями ═══
CREATE TABLE relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_a TEXT NOT NULL REFERENCES entities(id),
    entity_b TEXT NOT NULL REFERENCES entities(id),
    relation_type TEXT NOT NULL,  -- uses|implements|depends_on|related_to|same_file|followup|regression
    context TEXT,                 -- краткое описание связи
    confidence REAL DEFAULT 1.0,
    source_type TEXT NOT NULL,    -- auto|extracted|manual
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ═══ Чанки (привязаны к сущностям) ═══
CREATE TABLE chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    source_file TEXT,             -- путь к файлу-источнику
    source_type TEXT NOT NULL,    -- task_log|document|article|code
    entity_ids TEXT,              -- JSON list привязанных entity ids
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ═══ Task-specific memory (расширение) ═══
CREATE TABLE task_files (
    task_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    repo_alias TEXT NOT NULL,
    change_type TEXT DEFAULT 'modified',
    UNIQUE(task_id, file_path)
);

CREATE TABLE task_summaries (
    task_id TEXT NOT NULL PRIMARY KEY,
    summary TEXT NOT NULL,
    error_summary TEXT,
    files_changed TEXT,           -- JSON list
    tags TEXT,                    -- extracted tags
    embedding BLOB               -- optional: для semantic search
);
```

#### Два потока данных

**Поток 1: Task Memory (автоматический)**
1. Worker done → supervisor записывает в task_files + task_summaries
2. Claude извлекает entities из result (файлы, концепции) → entities + relations
3. Автоматические relations: "task#55 и task#38 трогали один файл" → same_file

**Поток 2: Knowledge Ingest (по запросу пользователя)**
1. Пользователь кидает файл в TG → task type="knowledge_ingest"
2. knowledge_worker (Claude CLI) извлекает entities + relations + key_facts
3. Промпт:
   ```
   Прочитай документ. Извлеки:
   1. ENTITIES — ключевые сущности (name, type, description)
   2. RELATIONS — связи (entity_a, entity_b, type, context)
   3. KEY_FACTS — главные факты (3-5 штук)
   ```
4. Supervisor сохраняет в граф + дедупликация entities по name+type

#### Query при новой задаче
```sql
-- 1. Ключевые слова задачи → entities
SELECT * FROM entities WHERE name LIKE '%auth%' OR description LIKE '%middleware%'

-- 2. Связанные entities через relations (1 hop)
SELECT e2.* FROM relations r JOIN entities e2 ON r.entity_b = e2.id
WHERE r.entity_a IN (found_ids)

-- 3. Chunks с контекстом
SELECT content FROM chunks WHERE entity_ids LIKE '%found_id%'

-- 4. Task summaries (если есть task-related entities)
SELECT summary FROM task_summaries ts
JOIN task_files tf ON ts.task_id = tf.task_id
WHERE tf.file_path IN (related_files)
```

Результат → секция "Контекст из базы знаний" в промпте воркера.

#### Новый воркер: knowledge_worker
```yaml
# config/agents.yaml
knowledge_worker:
    model: claude-opus-4-6
    active: false  # включить в Фазе 8
    is_knowledge: true
    max_attempts: 2
    mcp:
      required: ["context7"]
```

#### Что НЕ нужно (YAGNI на старте)
- Neo4j / graph DB — SQLite + joins
- Embedding pipeline — начать без, добавить позже
- Zep Cloud — платный
- Frontend визуализация — Telegram достаточно

**Зависимости:** Фазы 0-3 (done), migrate.py (done), json_guard (done)
**Когда:** после Фазы 7 (Docker deploy), когда система стабильна
**Оценка:** ~3-4 модуля (schema migration, knowledge_worker CLAUDE.md, graph_query.py, integration в промпт)

## Заметки для ретроспективы

- После каждой фазы: re-design review на основе опыта
- Рефакторинг после Фазы 2 (из заметок пользователя)
- /simplify нашёл 2 бага (dead param, double coercion) — исправлены
- /code-check: все инварианты соблюдены, 3 minor замечания записаны в backlog
