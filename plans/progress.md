# Progress Tracker

## Текущий статус

| Параметр | Значение |
|----------|----------|
| Активная фаза | 2 |
| Тесты (всего) | 239 |
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

## Фаза 4 — Эскалация + pending_approval ⏳ В ОЧЕРЕДИ

- [ ] `supervisor/escalation.py` — 5 сценариев эскалации
- [ ] pending_approval flow (/approve, /reject через TG)
- [ ] `tests/test_escalation.py` — тест каждого сценария

## Фаза 5 — Notion + summarizer ⏳ В ОЧЕРЕДИ

- [ ] `integrations/notion_handler.py` — создание/обновление страниц с fallback
- [ ] Обновить уведомления: TG коротко + Notion подробно
- [ ] `supervisor/summarizer.py` — daily summary + scheduler
- [ ] `tests/test_notion_handler.py`, `tests/test_summarizer.py`

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
- [ ] Workspace path в промпте воркера (чтобы знал куда писать код)
- [ ] Git push в worker cycle (ci_policy → commit → push через safe_exec) — задача Фазы 3
- [ ] sequential thinking MCP, filesystem MCP, wcgw MCP
- [ ] Декомпозиция main.py → отдельный `supervisor/worker_cycle.py`

## Заметки для ретроспективы

- После каждой фазы: re-design review на основе опыта
- Рефакторинг после Фазы 2 (из заметок пользователя)
- /simplify нашёл 2 бага (dead param, double coercion) — исправлены
- /code-check: все инварианты соблюдены, 3 minor замечания записаны в backlog
