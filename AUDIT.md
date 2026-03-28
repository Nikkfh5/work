# AI Orchestration System — Audit Document

**Дата:** 2026-03-28
**Автор:** система + владелец
**Цель:** Полная и честная оценка системы для внешнего аудита. Каждый модуль, каждое решение, каждый известный баг.

---

## 1. Что это и зачем

### 1.1 Суть проекта

Python-система (asyncio, 24/7) которая принимает задачи через Telegram/Email, маршрутизирует на AI-воркеров (Claude CLI subprocess), прогоняет через автоматический code review, и пушит результат в git.

Это **personal automation tool** — один пользователь, один VPS, несколько воркеров. Не enterprise platform.

### 1.2 Ключевое ограничение

Подписка **Claude Code Max**. Нет API ключей Anthropic. LLM вызывается ТОЛЬКО через `claude --print <prompt>` как subprocess. Это определяет всю архитектуру:
- Нет streaming, нет function calling
- Output — plaintext, JSON извлекается regex-ом из stdout
- Нет retry на уровне API (только retry целого subprocess)
- Claude запускается с cwd=worker_dir и имеет доступ к файловой системе (Read/Write/Edit tools)

### 1.3 Полный data flow

```
┌────────────────────────────────────────────────────────────────────────────┐
│                              ВХОД                                          │
│                                                                            │
│  Telegram (@work_aitool_bot)          Email (IMAP polling)                │
│  ┌──────────────────────┐            ┌──────────────────────┐            │
│  │ Long polling 30s     │            │ Poll every 30s       │            │
│  │ Dedup: offset +      │            │ Dedup: Message-ID +  │            │
│  │  processed_tg_updates│            │  processed_emails    │            │
│  └──────────┬───────────┘            └──────────┬───────────┘            │
│             │                                    │                        │
│             ▼                                    ▼                        │
│  ┌──────────────────────────────────────────────────────────┐            │
│  │  Router: resolve_worker(source, contact) → worker_id    │            │
│  │  agents.yaml: @voidnyan → job1_worker                   │            │
│  └──────────────────────────┬───────────────────────────────┘            │
│                             │                                             │
│                             ▼                                             │
│  ┌──────────────────────────────────────────────────────────┐            │
│  │  DB: INSERT INTO tasks (status='pending')                │            │
│  │  Ответ пользователю: "✓ Задача принята: #abc12345"      │            │
│  └──────────────────────────┬───────────────────────────────┘            │
└─────────────────────────────┼──────────────────────────────────────────────┘
                              │
┌─────────────────────────────┼──────────────────────────────────────────────┐
│                       DISPATCHER                                           │
│                             │                                              │
│  dispatch_pending_tasks() — каждые 30 секунд:                             │
│  1. Очищаем завершённые asyncio.Task из _running_tasks                    │
│  2. slots_free = max_concurrent(2) - len(_running_tasks)                  │
│  3. SELECT * FROM tasks WHERE status='pending' LIMIT {slots_free}         │
│  4. Для каждой: asyncio.create_task(run_worker_cycle(task))               │
│                             │                                              │
└─────────────────────────────┼──────────────────────────────────────────────┘
                              │
┌─────────────────────────────┼──────────────────────────────────────────────┐
│                    PIPELINE (4 stages)                                      │
│                             │                                              │
│  ┌──────────────────────────▼───────────────────────────────────┐          │
│  │  STAGE 1: prepare_stage                                      │          │
│  │                                                               │          │
│  │  1. acquire_lease(task_id, worker_id, ttl=300s)              │          │
│  │     SQL: UPDATE tasks SET status='running',                  │          │
│  │          locked_by=?, locked_until=?, lease_token=UUID        │          │
│  │          WHERE id=? AND (status IN ('pending','blocked')     │          │
│  │                     OR (status='running' AND locked_until<now))│         │
│  │     → Если rowcount=0: raise LeaseConflict (silent exit)     │          │
│  │     → Если rowcount=1: token = UUID, продолжаем              │          │
│  │                                                               │          │
│  │  2. ensure_mirror(job, alias, url, strategy)                 │          │
│  │     → git clone --bare (один раз, кешируется в repos_cache/) │          │
│  │                                                               │          │
│  │  3. prepare_worktree(task_id, alias, branch)                 │          │
│  │     → git worktree add worktrees/{task_id}/{alias}           │          │
│  │     → symlink: workers/{worker}/workspace/{task_id}/         │          │
│  │                → worktrees/{task_id}/                        │          │
│  │                                                               │          │
│  │  Результат: ctx.token, ctx.worktree_aliases, ctx.branch     │          │
│  └──────────────────────────┬───────────────────────────────────┘          │
│                             │                                              │
│  ┌──────────────────────────▼───────────────────────────────────┐          │
│  │  STAGE 2: execute_stage                                      │          │
│  │                                                               │          │
│  │  Retry loop: до max_attempts(3) попыток                      │          │
│  │                                                               │          │
│  │  1. Построить промпт:                                        │          │
│  │     _build_worker_prompt(description, repos_context, branch) │          │
│  │     Содержит: задание + workspace пути + JSON шаблон         │          │
│  │                                                               │          │
│  │  2. Запустить Claude CLI:                                    │          │
│  │     await run_claude(prompt, cwd="workers/job1_worker",      │          │
│  │                      timeout=1800s)                          │          │
│  │     → asyncio.create_subprocess_exec(                        │          │
│  │         "claude", "--print", prompt,                         │          │
│  │         cwd=worker_dir, stdout=PIPE, stderr=PIPE)            │          │
│  │                                                               │          │
│  │  3. Парсинг stdout:                                          │          │
│  │     extract_json(stdout):                                    │          │
│  │       Stage 1: <<<JSON>>>...<<<END>>> маркеры                │          │
│  │       Stage 2: regex fallback r"\{.*\}"                      │          │
│  │     validate_worker_schema(parsed):                          │          │
│  │       status ∈ {done, blocked, error}                        │          │
│  │       confidence ∈ [0, 100]                                  │          │
│  │       result.repos[].alias, result.repos[].changed_files     │          │
│  │                                                               │          │
│  │  4. Запись в task_runs (append-only):                        │          │
│  │     log_run(task_id, phase="worker", stdout_path, json_valid)│          │
│  │                                                               │          │
│  │  На ошибку:                                                  │          │
│  │  - TimeoutError → fail immediately (не ретраить)             │          │
│  │  - ClaudeRunnerError → retry с delay                         │          │
│  │  - json_invalid → retry с коррекционным промптом             │          │
│  │  - json_schema_invalid → retry с коррекционным промптом      │          │
│  │                                                               │          │
│  │  Результат: ctx.parsed, ctx.worker_status, ctx.confidence   │          │
│  └──────────────────────────┬───────────────────────────────────┘          │
│                             │                                              │
│  ┌──────────────────────────▼───────────────────────────────────┐          │
│  │  STAGE 3: review_stage                                       │          │
│  │                                                               │          │
│  │  Ветвление по результату execute:                            │          │
│  │                                                               │          │
│  │  IF status="done" AND confidence >= threshold(75):           │          │
│  │    IF reviewer configured (reviewer_id в agents.yaml):       │          │
│  │      → _run_review_cycle() (см. ниже)                       │          │
│  │    ELSE:                                                     │          │
│  │      → passthrough к deliver stage                           │          │
│  │                                                               │          │
│  │  IF status="blocked" OR confidence < threshold:              │          │
│  │    → handle_worker_blocked() (escalation)                    │          │
│  │    → supervisor_reasoning() пытается auto-resolve            │          │
│  │    → если не может: create_escalation() + TG                 │          │
│  │    → release_lease(..., "blocked")                           │          │
│  │                                                               │          │
│  │  IF status="error":                                          │          │
│  │    → release_lease(..., "requires_manual")                   │          │
│  │    → TG notification                                        │          │
│  │                                                               │          │
│  │  ─── Review Cycle ───                                        │          │
│  │                                                               │          │
│  │  ensure_agent_symlink(task_id, reviewer_id)                  │          │
│  │  → symlink: workers/job1_reviewer/workspace/{task_id}/       │          │
│  │             → worktrees/{task_id}/                           │          │
│  │                                                               │          │
│  │  for iteration in 1..max_review_iterations(3):               │          │
│  │    1. _build_reviewer_prompt(description, worker_result)     │          │
│  │       На iter 2+: "проверь ТОЛЬКО предыдущие замечания,      │          │
│  │                    НЕ ищи новых проблем"                     │          │
│  │    2. run_claude(prompt, cwd="workers/job1_reviewer")        │          │
│  │    3. extract_json → validate_reviewer_schema                │          │
│  │       verdict ∈ {APPROVED, NEEDS_CHANGES}                    │          │
│  │                                                               │          │
│  │    IF APPROVED:                                              │          │
│  │      ctx.worker_status = "reviewed_approved"                 │          │
│  │      → переход к deliver stage                               │          │
│  │                                                               │          │
│  │    IF NEEDS_CHANGES (не последняя итерация):                 │          │
│  │      1. _build_worker_retry_prompt(issues, prev_notes)       │          │
│  │         "Прочитай файлы. Исправь точечно. Попытка N/M."      │          │
│  │      2. run_claude(retry_prompt, cwd=worker_dir)             │          │
│  │      3. extract_json → validate_worker_schema                │          │
│  │      4. Следующая итерация review                            │          │
│  │                                                               │          │
│  │    IF NEEDS_CHANGES (последняя итерация):                    │          │
│  │      → release_lease(..., "requires_manual")                 │          │
│  │      → TG: "review exhausted (3 iterations)"                │          │
│  │                                                               │          │
│  │  Максимум Claude CLI вызовов на задачу:                      │          │
│  │    1 worker + 3×(reviewer + worker_retry) = 7 вызовов        │          │
│  └──────────────────────────┬───────────────────────────────────┘          │
│                             │                                              │
│  ┌──────────────────────────▼───────────────────────────────────┐          │
│  │  STAGE 4: deliver_stage                                      │          │
│  │                                                               │          │
│  │  Пропускается если worker_status содержит "_handled"         │          │
│  │                                                               │          │
│  │  Для каждого repo в repos_context:                           │          │
│  │    _run_ci_and_push():                                       │          │
│  │      1. Style: ruff format . (через safe_exec, 120s)        │          │
│  │      2. Git:   git add . + git commit -m "ai: task {id}"    │          │
│  │      3. CI:    pytest --tb=short -q (через safe_exec, 300s) │          │
│  │                ruff check . (через safe_exec, 300s)          │          │
│  │      4. Push:  git push origin HEAD (через safe_exec, 120s) │          │
│  │                                                               │          │
│  │  IF ci_policy.required_pass=true AND CI fail:                │          │
│  │    → release_lease("requires_manual", reason="git_push_failed")│         │
│  │    → TG notification                                        │          │
│  │                                                               │          │
│  │  IF all pass:                                                │          │
│  │    → release_lease("done")                                   │          │
│  │    → TG: "task#abc: DONE ✓ (confidence=85)"                │          │
│  └──────────────────────────┬───────────────────────────────────┘          │
│                             │                                              │
│  ┌──────────────────────────▼───────────────────────────────────┐          │
│  │  FINALLY (всегда выполняется):                               │          │
│  │    cleanup_worktree(task_id) — удалить worktree + symlinks   │          │
│  └──────────────────────────────────────────────────────────────┘          │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Метрики проекта

### 2.1 Объём кода

| Категория | Файлов | Строк | % |
|-----------|--------|-------|---|
| Production code | 36 | 7,848 | 58.3% |
| Тесты | 21 | 5,606 | 41.7% |
| **Итого** | **57** | **13,454** | **100%** |

### 2.2 Модули по размеру

| Модуль | Строк | Ответственность |
|--------|-------|-----------------|
| supervisor/main.py | 497 | Asyncio event loop, startup, shutdown, scheduling |
| supervisor/health_monitor.py | 475 | Health snapshot, auto-actions, TG alerts |
| integrations/telegram_handler.py | 496 | TG polling, commands, notifications, fallback |
| supervisor/stages/review.py | 468 | Review cycle, reviewer/worker prompts |
| supervisor/repo_manager.py | 425 | Bare mirror, worktree, symlink, cleanup |
| supervisor/safe_exec.py | 389 | Allowlist runner, realpath check |
| storage/db.py | 374 | SQLite transport (CRUD, connections, WAL) |
| supervisor/escalation.py | 315 | Supervisor reasoning, escalation CRUD |
| supervisor/lease_manager.py | 284 | Atomic lease acquire/release/renew/stale |
| supervisor/json_guard.py | 278 | JSON extraction (markers + regex), schema validation |
| supervisor/stages/execute.py | 264 | Worker Claude CLI + retry loop |
| supervisor/pipeline.py | 247 | WorkerContext dataclass, StageError hierarchy, run_pipeline |
| supervisor/hooks/guard_bash.py | 220 | Claude PreToolUse hook, read-only allowlist |
| storage/migrate.py | 219 | 15 миграций, schema versioning |
| integrations/email_handler.py | 263 | IMAP polling, Message-ID dedup |
| supervisor/stages/deliver.py | 168 | ruff + git + CI + push |
| supervisor/config_validator.py | 157 | agents.yaml fail-fast validation |
| supervisor/run_logger.py | 180 | File logging + task_runs INSERT |
| supervisor/claude_runner.py | 114 | Claude CLI subprocess wrapper |
| supervisor/stages/prepare.py | 107 | Lease acquisition + worktree setup |
| supervisor/summarizer.py | 104 | Daily digest builder |
| supervisor/event_handlers.py | 82 | Event → TG notification dispatch |
| supervisor/log_utils.py | 62 | redact() для секретов |
| supervisor/router.py | 97 | Contact → worker_id mapping |

### 2.3 Тесты по модулям

| Тестовый файл | Тестов | Строк | Что покрыто |
|---------------|--------|-------|-------------|
| test_guard_bash.py | 44 | 255 | Allowlist, pipes, semicolons, path traversal, edge cases |
| test_json_guard.py | 38 | 291 | Маркеры, fallback regex, все 3 schema, невалидные данные |
| test_main.py | 28 | 427 | Startup sequence, polling loops, dispatcher, shutdown, scheduling |
| test_telegram_handler.py | 24 | 364 | Commands (/status/approve/retry/cancel), dedup, fallback log |
| test_health_monitor.py | 18 | 399 | Snapshot SQL, GREEN/YELLOW/RED, auto_actions, create_tasks |
| test_email_handler.py | 17 | 309 | IMAP polling, Message-ID dedup, task creation |
| test_safe_exec.py | 16 | 226 | Allowlist, denylist, realpath, timeout, env filtering |
| test_config_validator.py | 15 | 225 | Valid/invalid YAML, missing fields, type errors |
| test_pipeline.py | 14 | 518 | Stage orchestration, error hierarchy, events, WorkerContext |
| test_repo_manager.py | 14 | 351 | Mirror clone, worktree add/cleanup, symlink, ensure_agent |
| test_lease_manager.py | 12 | 282 | Acquire, release, renew, stale, token protection, transitions |
| test_review_cycle.py | 11 | 574 | Approved, retry, exhausted, CI fail, reviewer symlink |
| test_db.py | 10 | 191 | CRUD tasks, connections, WAL mode |
| test_worker_cycle.py | 8 | 282 | Worktree setup/cleanup/error/multi-repo |
| test_escalation.py | 8 | 258 | Reasoning, auto-resolve, escalate, CRUD, routing |
| test_log_utils.py | 8 | 86 | Redact patterns (tokens, emails, paths) |
| test_run_logger.py | 8 | 247 | File write, task_runs INSERT, path construction |
| test_migrate.py | 8 | 146 | Schema versioning, idempotent migrations |
| test_summarizer.py | 6 | 123 | Daily digest: no tasks→skip, done, blocked, errors |

**Итого: 301 passed, 5 skipped, 0 failed**

### 2.4 Зависимости

| Пакет | Версия | Назначение |
|-------|--------|------------|
| python-telegram-bot | >=21.0 | TG Bot API (polling + send) |
| pyyaml | >=6.0 | Парсинг agents.yaml |
| python-dotenv | >=1.0 | Загрузка .env |
| gitpython | >=3.1 | Git операции (используется в repo_manager) |
| aiohttp | >=3.9 | HTTP клиент (для TG bot) |
| pytest | >=8.0 | Тестирование |
| pytest-asyncio | >=0.23 | Async тесты |
| pytest-mock | >=3.12 | Mocker fixture |
| hypothesis | >=6.100 | Property-based тесты |
| semgrep | >=1.60 | Static analysis (кастомные правила) |

**Нет anthropic SDK, нет openai, нет LLM-зависимостей.** Claude вызывается исключительно через CLI subprocess.

---

## 3. Архитектура — модуль за модулем

### 3.1 supervisor/pipeline.py — Pipeline Engine

**Центральный dataclass:**
```python
@dataclass
class WorkerContext:
    # Identity
    task_id: str
    worker_id: str
    task_description: str

    # Dependencies (DI)
    config: dict
    tg_handler: TelegramHandler
    db_path: Optional[str] = None
    runner: Optional[Callable] = None      # заменяемый run_claude
    executor: Optional[Callable] = None    # заменяемый safe_exec
    repo_manager: Optional[RepoManager] = None

    # Worker config (из agents.yaml)
    worker_cfg: dict
    job: str                               # "job1" (без "_worker")
    worker_dir: str                        # "workers/job1_worker"

    # Lease
    token: str = ""
    lease_ttl: int = 300                   # 5 мин

    # Worktrees
    repos: list                            # из agents.yaml workers.repos
    worktree_aliases: list                 # ["api"]
    branch: str = ""                       # "ai/task-{uuid}"
    branch_pattern: str = "ai/task-{task_id}"
    base_branch: str = "main"
    repos_context: Optional[list] = None   # [{"alias":"api","path":"workspace/..."}]

    # Execution limits
    max_attempts: int = 3
    worker_timeout: int = 1800             # 30 мин
    retry_delay: int = 30
    conf_threshold: int = 70               # минимальный confidence для auto-push

    # Results (мутируются stages)
    parsed: Optional[dict] = None          # JSON из stdout
    worker_status: str = ""                # done/blocked/error
    confidence: int = 0

    # Event bus
    _events: list = field(default_factory=list)

    def emit(self, event_type: str, **kwargs) -> None
    def drain_events(self) -> list[dict]
```

**Иерархия ошибок:**
```
StageError (reason, message, notify=True)
├── LeaseConflict (notify=False)     — задача занята другим worker'ом
├── WorktreeError                     — git clone/worktree fail
├── WorkerCrash                       — timeout/crash/invalid JSON
├── ReviewExhausted                   — 3 итерации NEEDS_CHANGES
└── CIFailed                          — pytest/ruff/push fail
```

**run_pipeline(ctx, stages):**
1. Для каждого stage: вызвать stage(ctx), затем handle_events(ctx)
2. LeaseConflict → silent exit
3. StageError → _fail_final() + TG notify (если e.notify)
4. Finally → cleanup_worktree (всегда)

### 3.2 supervisor/lease_manager.py — Конкурентный доступ к задачам

**State machine:**
```
pending ──────→ running (acquire_lease)
pending_approval → pending (/approve) | rejected (/reject)
running ───────→ done | blocked | error | requires_manual
blocked ───────→ running (re-acquire после escalation)
requires_manual → running (/approve) | cancelled (/cancel)
done, cancelled, rejected, error — терминальные
```

**acquire_lease() — атомарный захват:**
```sql
UPDATE tasks
SET status='running', locked_by=?, locked_until=?, lease_token=UUID,
    worker_attempt = worker_attempt + 1
WHERE id=?
  AND (status IN ('pending','blocked')
       OR (status='running' AND locked_until < ?))
```
Если rowcount=0 — задача уже занята (race condition защита через WHERE). Если rowcount=1 — возвращает UUID token.

**release_lease() — проверка владения:**
```sql
UPDATE tasks SET status=?, locked_by=NULL, locked_until=NULL, lease_token=NULL
WHERE id=? AND locked_by=? AND lease_token=?
```
Только владелец (по locked_by + lease_token) может освободить. Защита от stale owner release.

**release_stale() — ночной cleanup:**
```sql
UPDATE tasks SET status='error', locked_by=NULL, ..., last_error_reason='lease_stale'
WHERE status='running' AND locked_until < ?
```

**renew_lease() — продление TTL:**
```sql
UPDATE tasks SET locked_until=?
WHERE id=? AND locked_by=? AND lease_token=? AND status='running'
```

### 3.3 supervisor/json_guard.py — Парсинг LLM output

**Проблема:** Claude `--print` возвращает произвольный текст. Нужно извлечь JSON.

**Двухстадийный алгоритм extract_json(raw):**

1. **Маркеры:** Ищем `<<<JSON>>>` ... `<<<END>>>`. Если найдены — парсим содержимое между ними.
2. **Fallback regex:** Если маркеров нет — ищем первый `\{.*\}` (DOTALL). Парсим через json.loads.
3. Если оба метода fail — return None.

**Три схемы валидации:**

Worker schema:
```json
{
  "status": "done|blocked|error",
  "confidence": 0-100,
  "result": {
    "repos": [{"alias": "api", "changed_files": ["file.py"]}],
    "notes": "что сделано"
  },
  "question": null | "текст вопроса"
}
```

Reviewer schema:
```json
{
  "verdict": "APPROVED|NEEDS_CHANGES",
  "feedback": "комментарий",
  "issues": [
    {"repo": "api", "file": "path.py", "line": 42, "type": "bug|style|logic|test", "message": "описание"}
  ]
}
```

Health schema:
```json
{
  "status": "done",
  "confidence": 0-100,
  "health": {
    "overall": "GREEN|YELLOW|RED",
    "findings": [{"severity": "low|med|high", "title": "...", "evidence": "...", "suggestion": "..."}],
    "auto_actions": ["cleanup_worktrees", "release_stale_leases"],
    "create_tasks": [{"title": "...", "summary": "...", "priority": "P1|P2|P3"}]
  }
}
```

### 3.4 supervisor/safe_exec.py — Sandbox для команд

**Принцип:** Whitelist-only. Команда должна быть явно разрешена в COMMAND_PROFILES.

**Профили команд:**
```python
COMMAND_PROFILES = {
    "git": {
        "allowed_subcommands": ["clone","fetch","worktree","add","commit","push",
                                 "diff","log","status","checkout","branch",...],
        "blocked_flags": ["-c", "--upload-pack", "--exec"]
    },
    "pytest": {"allowed_forms": ["pytest", "python -m pytest"]},
    "ruff":   {"allowed_subcommands": ["check", "format"]},
    "pip":    {"allowed_subcommands": ["install"]},
    "npm":    {"allowed_subcommands": ["test","run"], "allowed_run_scripts": ["lint","build","test"]},
    "go":     {"allowed_subcommands": ["test","vet","build"]},
    "python": {"allowed_subcommands": ["-m"], "allowed_m_modules": ["pytest"]},
}
```

**Denylist (всегда блокируется):**
```python
DENYLIST_COMMANDS = {"rm","sudo","chmod","chown","dd","kill","shutdown",
                     "wget","curl","nc","ssh","scp","rsync","docker","kubectl"}
```

**Проверка CWD:** `os.path.realpath(cwd)` — предотвращает escape через symlinks.

**Фильтрация env:** Передаются ТОЛЬКО ключи из `SAFE_ENV_KEYS` (PATH, HOME, LANG...). Ключи содержащие TOKEN/SECRET/PASSWORD/KEY — отбрасываются.

**safe_exec(cmd, cwd, timeout=300):**
1. `_check_cmd(cmd)` — проверка по профилям и denylist
2. `_check_cwd(cwd, allowed_roots)` — realpath check
3. `_build_safe_env()` — фильтрация переменных
4. `subprocess.run(cmd, cwd, env, timeout, capture_output=True)` — **без shell=True**

### 3.5 supervisor/hooks/guard_bash.py — Claude Code hook

**Как работает:** Claude Code (при работе в worker directory) вызывает этот скрипт через PreToolUse hook перед каждым Bash-вызовом. Скрипт получает JSON на stdin, проверяет команду, возвращает exit(0) или exit(2).

**Разрешено (read-only):**
```python
ALLOWED_PROGRAMS = {"pwd","ls","find","cat","head","tail","grep","rg","wc","echo","stat","file","git"}
ALLOWED_GIT_SUBCOMMANDS = {"status","diff","log","show","ls-files","branch","rev-parse","cat-file","describe"}
```

**Заблокировано:**
```python
BLOCKED_PROGRAMS = {
    # Git write
    "git add","git commit","git push","git reset","git checkout","git merge","git rebase",
    # Executors
    "pytest","npm","go","make","pip","python","node","ruby",
    # Destructive
    "rm","mv","cp","chmod","sudo","curl","wget","ssh","docker","kubectl",
    # System
    "kill","shutdown","systemctl","touch","mkdir",
}
BLOCKED_SHELL_OPERATORS = ["&&","||",";","|",">","<","`","$(","${"]
```

**Ограничения (честно):** Парсит команду через string.split(), не через shell parser. Теоретически возможен bypass через heredoc или сложные shell конструкции. Для trusted environment (свои воркеры) — достаточно. Для adversarial — нет.

### 3.6 supervisor/repo_manager.py — Git-инфраструктура

**Три уровня:**

1. **Bare mirror** (persistent): `repos_cache/{job}/{alias}.git` — клонируется один раз, используется всеми задачами.
2. **Worktree** (per-task): `worktrees/{task_id}/{alias}/` — отдельная ветка, изолированная от других задач.
3. **Symlink** (per-agent): `workers/{agent}/workspace/{task_id}/` → `worktrees/{task_id}/` — чтобы Claude видел файлы в своём CWD.

**ensure_mirror(job, alias, url, strategy):**
- Если `repos_cache/{job}/{alias}.git` существует → git fetch
- Иначе → git clone (strategy: shallow/mirror/partial/sparse)

**prepare_worktree(task_id, alias, branch):**
- `git worktree add worktrees/{task_id}/{alias} -b {branch}`
- Создаёт symlink в `workers/{worker_id}/workspace/{task_id}/`

**ensure_agent_symlink(task_id, agent_id):**
- Создаёт symlink `workers/{agent_id}/workspace/{task_id}/` → `worktrees/{task_id}/`
- Используется для reviewer: он запускается в `workers/job1_reviewer/` но должен видеть worktree файлы

**cleanup_worktree(task_id):**
- Удаляет worktree через `git worktree remove`
- Удаляет symlinks у всех агентов
- Windows-совместимость: `os.rmdir()` для junction points вместо `shutil.rmtree()`

### 3.7 integrations/telegram_handler.py — TG бот

**Polling:** Ручной long polling через `Bot.get_updates(offset, timeout=30)`. Не webhooks — проще в development, не требует HTTPS.

**Двухслойная dedup:**
1. **offset** в kv_store — запрашиваем только updates > last_offset
2. **processed_tg_updates** таблица — пропускаем уже обработанные update_id (защита от replay)

**Команды:**

| Команда | Что делает | SQL |
|---------|-----------|-----|
| /status | Показать активные задачи + requires_manual + error count | SELECT WHERE status IN ('pending','running','blocked',...) |
| /errors | Список задач с ошибками | SELECT WHERE status IN ('error','requires_manual') |
| /approve `<id>` | pending_approval/requires_manual → pending | UPDATE SET status='pending' WHERE status IN (...) |
| /reject `<id>` | pending_approval → rejected | UPDATE SET status='rejected' |
| /retry `<id>` | error/blocked/requires_manual → pending | UPDATE SET status='pending' |
| /cancel `<id>` | Любой (кроме done) → cancelled | UPDATE SET status='cancelled' WHERE status!='done' |
| Свободный текст | Создать задачу через router | create_task(..., assigned_worker=router.resolve()) |

**Fallback:** Если TG API недоступен (3 retry) — пишет в `logs/tg_fallback_{date}.log`.

### 3.8 storage/db.py — Database layer

**Принцип:** Только транспорт. Никакой бизнес-логики. Это инвариант #1 проекта.

**Подключение:**
```python
@contextmanager
def get_conn(db_path=None):
    # PRAGMA journal_mode = WAL
    # PRAGMA busy_timeout = 10000
    # PRAGMA foreign_keys = ON
    # row_factory = sqlite3.Row (dict-like доступ)
    # auto-commit on success, rollback on exception
```

**Таблицы (из schema.sql + 15 миграций):**

| Таблица | Назначение | Ключевые поля |
|---------|-----------|---------------|
| tasks | Задачи (state machine) | id UUID, status, assigned_worker, description, locked_by, locked_until, lease_token, worker_attempt, last_error_reason |
| messages | Переписка по задаче | task_id, role (client/supervisor/worker), content |
| agent_context | Append-only knowledge base | agent_id, entry_type, content, importance |
| worker_updates | Worker → Supervisor канал | worker_id, task_id, update_type, payload JSON |
| supervisor_directives | Supervisor → Worker канал | worker_id, task_id, directive_type, payload JSON |
| escalations | Ручное вмешательство | task_id, reason, question, context, resolved |
| meetings | Транскрипты встреч | title, summary, participants JSON |
| daily_summaries | Дайджесты | content, sent |
| task_runs | Execution history | task_id, phase, attempt, stdout_path, returncode, json_valid |
| kv_store | Key-value хранилище | key (tg_last_update_id), value |
| processed_tg_updates | TG dedup | update_id, task_id |
| processed_emails | Email dedup | message_id, task_id |
| schema_migrations | Версионирование | version, applied_at |

### 3.9 storage/migrate.py — Миграции

**15 миграций**, идемпотентные (повторный запуск безопасен):

| Version | Что создаёт/меняет |
|---------|-------------------|
| 1 | schema_migrations таблица |
| 2 | kv_store таблица |
| 3 | tasks.locked_by колонка |
| 4 | tasks.locked_until колонка |
| 5 | tasks.lease_token колонка |
| 6 | tasks.worker_attempt (int, default 0) |
| 7 | tasks.review_iteration (int, default 0) |
| 8 | tasks.last_error_code колонка |
| 9 | tasks.last_error_reason колонка |
| 10 | tasks.notion_page_url колонка |
| 11 | tasks.partial_result колонка |
| 12 | task_runs таблица (полная) |
| 13 | processed_tg_updates таблица |
| 14 | processed_emails таблица |
| 15 | Индексы: idx_tasks_status_v2, idx_tasks_locked_until, idx_task_runs_task_phase |

### 3.10 Остальные модули

**supervisor/escalation.py:**
- `supervisor_reasoning(question, context)` — вызывает Claude CLI для auto-resolve blocked задач
- `handle_worker_blocked(task, output, config)` — если confidence >= threshold: auto-resolve, иначе: create_escalation + TG
- `route_owner_reply(text)` — привязать ответ владельца к open escalation

**supervisor/health_monitor.py:**
- `build_snapshot()` — собирает SQL статистику + disk usage + последние логи
- `run_health_check()` — snapshot → Claude CLI (health_monitor worker) → parse JSON → execute auto_actions
- Auto-actions (allowlist): cleanup_worktrees, release_stale_leases, rotate_logs, gc_mirrors
- create_tasks: здоровье агент может предложить создать задачи (через pending_approval)

**supervisor/summarizer.py:**
- `build_daily_summary()` — SQL: done/blocked/error counts за 24 часа + top-3 ошибки
- `run_daily_summary()` — если есть задачи: save + TG notify, иначе: skip

**supervisor/config_validator.py:**
- `load_and_validate()` — парсит agents.yaml, проверяет: reviewer_id существует, git_token_env в env, clone_strategy валидный, delivery_mode валидный
- Fail-fast: sys.exit(1) при ошибке (supervisor не запускается)

**supervisor/event_handlers.py:**
- Маппинг: task_done → TG "DONE ✓", task_failed → TG "⚠️ + /retry", task_blocked → TG "BLOCKED + question"

---

## 4. 10 инвариантов проекта

Критические правила которые никогда нельзя нарушать. Автоматически проверяются 7 Semgrep правилами.

| # | Инвариант | Semgrep правило | Severity |
|---|-----------|-----------------|----------|
| 1 | `db.py` — только транспорт, без бизнес-логики | no-logic-in-db | MEDIUM |
| 2 | `json_guard` — только парсинг/валидация, не пишет файлы/DB | — | — |
| 3 | `run_logger` — только запись файлов + task_runs | — | — |
| 4 | Воркеры не запускают команды (только файлы + JSON) | no-direct-subprocess-in-workers | HIGH |
| 5 | Все внешние команды через `safe_exec`, никогда `shell=True` | no-shell-true | CRITICAL |
| 6 | Все логи через `redact()`, raw stdout только в файлах | no-raw-stdout-in-logs | HIGH |
| 7 | В DB только пути к логам, не сами строки | no-log-content-in-db | HIGH |
| 8 | Все изменения схемы только через `storage/migrate.py` | no-schema-changes-outside-migrate | CRITICAL |
| 9 | Bash hook воркеров: read-only allowlist | — | — |
| 10 | Supervisor — единственный кто пишет клиенту (TG/Email) | no-direct-client-write | HIGH |

---

## 5. Архитектурные решения и почему

### 5.1 Claude CLI subprocess вместо API

**Решение:** `claude --print <prompt>` через `asyncio.create_subprocess_exec`.

**Почему:** Подписка Claude Code Max, нет API ключей. Это единственный способ вызвать LLM.

**Что это определяет:**
- Нет streaming, нет function calling, нет structured output
- Output — plaintext, JSON извлекается regex (json_guard)
- Нет retry на уровне API — retry целого subprocess в execute_stage
- Claude запускается с доступом к файловой системе (Read/Write/Edit)
- Worker реально создаёт файлы в worktree (это подтверждено debug-сессией)

**Трейдофф:** Медленнее API (subprocess overhead ~2-5s). Но для задач которые выполняются 30-60 секунд — незначительно.

### 5.2 SQLite WAL вместо Postgres

**Почему:** Один процесс supervisor, max_concurrent=2. SQLite с WAL поддерживает concurrent reads + sequential writes. Postgres — overhead без пользы.

**Трейдофф:** Не масштабируется горизонтально. При переходе к multi-supervisor нужен Postgres. Текущая нагрузка: единицы задач в день.

### 5.3 Lease-based concurrency вместо message queue

**Почему:** Нет Redis/RabbitMQ. SQLite — единственный shared state. Lease с TTL + UUID token обеспечивает:
- Атомарность через WHERE clause
- Защита от stale owner (token must match)
- Liveness через release_stale() + TTL

**Трейдофф:** Не exactly-once. При crash возможен double-execute после TTL expiry. Допустимо — воркер идемпотентен.

### 5.4 Worktrees вместо clone per task

**Почему:** Экономия. Bare mirror clone один раз (может быть 500MB), worktree — мгновенно (~1ms). Каждая задача в изолированной ветке.

**Трейдофф:** На Windows symlinks требуют developer mode. Worktrees привязаны к одному bare repo (не проблема).

### 5.5 `--print` вместо interactive mode

**Почему:** Предсказуемость. Один промпт → один ответ → один JSON. Interactive mode сложнее парсить.

**Трейдофф:** Worker не может задавать уточняющие вопросы в процессе. Компенсация: status="blocked" + question → escalation → TG → owner ответ.

### 5.6 Reviewer как отдельный subprocess

**Почему:** Изоляция контекста. Worker и reviewer не видят промпты друг друга. Reviewer получает только результат (через чтение файлов в worktree).

**Трейдофф:** Дорого — до 7 Claude CLI вызовов на задачу (1 worker + 3×(reviewer + retry)). При $0 (Max subscription) — не проблема.

### 5.7 Semgrep вместо ручных code reviews для инвариантов

**Почему:** Человек забудет проверить shell=True. CI не забудет. 7 правил автоматически ловят нарушения.

**Трейдофф:** AST pattern matching, не формальная верификация. Могут быть false positive/negative. Для текущих правил — работает надёжно.

### 5.8 Guard bash hook вместо sandbox (nsjail/bubblewrap)

**Почему:** Простота. Hook — это Python скрипт, 220 строк. nsjail требует Linux capabilities, root, сложную настройку.

**Трейдофф:** Guard парсит команды regex-ом, не через shell parser. Теоретически возможен bypass. Для trusted environment — достаточно. Для production с adversarial inputs — нужен настоящий sandbox.

### 5.9 Event system вместо прямых вызовов

**Почему:** Декаплинг. Stages emit events, pipeline dispatches. Stage не знает о TG — только emit("task_done").

**Трейдофф:** Небольшой overhead. Для текущего масштаба — не заметен.

---

## 6. Файлы которые нельзя трогать (и почему)

| Файл | Строк | Причина |
|------|-------|---------|
| `storage/db.py` | 374 | Низкоуровневый транспорт. Любая бизнес-логика = нарушение инварианта #1. Исключение: PRAGMA WAL/busy_timeout |
| `supervisor/router.py` | 97 | Жёсткая привязка contact → worker. Простой и стабильный |
| `supervisor/claude_runner.py` | 114 | Тонкая обёртка subprocess. Менять = ломать все вызовы Claude |
| `tests/test_db.py` | 191 | 10 базовых тестов DB layer. Canary — если падают, всё сломано |

---

## 7. Что НЕ работает / известные проблемы

### 7.1 Deliver stage на Windows с Unicode путями (BUG-012, BUG-013)

**Проблема:** Проект в `OneDrive/Рабочий стол/` (кириллица). `git commit` падает с `WinError 267`, `pip install -e` не может URL-decode кириллицу в file:// URL.

**Влияние:** Pipeline работает до deliver stage: ruff format ✅, git add ✅, git commit ❌.

**Решение:** Перенести в ASCII путь или VPS. Не баг системы — ограничение Windows path handling.

### 7.2 Reviewer спираль (пофикшено 2026-03-28)

**Проблема:** На каждой итерации reviewer находил НОВЫЙ баг вместо проверки предыдущего фикса. 3 итерации → 3 разных бага → exhausted.

**Пример:** validators.py: iter1 "trailing dot в email" → iter2 "consecutive dots в domain" → iter3 "ещё один edge case" → exhausted.

**Фикс:** Промпт reviewer'а на итерации 2+: "Проверь ТОЛЬКО что предыдущие замечания исправлены. НЕ ищи новых проблем." Worker retry: "Прочитай файлы. Исправь точечно. Попытка N/M."

**Статус:** Не тестировано в production.

### 7.3 Email не настроен

Email handler написан (263 строки), покрыт тестами (17 тестов), но credentials в .env — placeholder. Не блокирует работу через TG.

### 7.4 Error recovery при crash supervisor'а

Если supervisor падает между acquire_lease и release_lease, задача зависает в `running` до:
- TTL expiry (5 мин) + следующий dispatch цикл
- release_stale() в nightly_housekeeping (0:00 UTC)

**Проблема:** Между crash и recovery может пройти от 5 минут до 24 часов.

**Нужно:** Запускать release_stale() чаще (каждые 5-10 мин) или watchdog process.

### 7.5 Нет реальной нагрузки

Система тестирована ТОЛЬКО debug-задачами. Реальных production задач не было.

---

## 8. Debug-сессия — полные результаты

### 8.1 Обзор

4 эксперимента, 8 задач, 13 багов найдено, 10 пофикшено:

### 8.2 Все баги

| # | Severity | Статус | Описание | Root cause | Fix |
|---|----------|--------|----------|------------|-----|
| 1 | LOW | Fixed | TG 409 Conflict при двух экземплярах | Предыдущий процесс не убит | pkill перед стартом |
| 2 | LOW | Expected | Email IMAP AUTHENTICATIONFAILED | Placeholder credentials | Ожидаемо |
| 3 | LOW | Fixed | Repo URL placeholder → git clone висит | `https://github.com/org/api` в agents.yaml | Заменён на реальный URL |
| 4 | LOW | Fixed | GIT_TOKEN_JOB1 placeholder | `ghp_...` в .env | Заменён на gh auth token |
| 5 | **CRITICAL** | **Fixed** | Worker файлы вне worktree — reviewer не видит | Worker CWD = workers/job1_worker/, файлы в workspace/, reviewer ищет в worktree/ | ensure_agent_symlink для reviewer |
| 6 | MEDIUM | Fixed | Symlink cleanup fail на Windows | shutil.rmtree() не работает на symlinks/junctions | os.rmdir() для junctions |
| 7 | LOW | Fixed | returncode=None в task_runs | log_run() не получал returncode | Явный returncode=0 |
| 8 | LOW | Fixed | Email polling спам каждые 30s | Нет backoff при IMAP error | Exponential backoff |
| 9 | LOW | Fixed | TG timeout как WARNING | Long polling timeout = нормальное поведение | asyncio.TimeoutError → DEBUG |
| 10 | HIGH | Fixed | Claude CLI без permissions | settings.json не содержал allow для Write/Edit | Добавлены permissions |
| 11 | HIGH | Fixed | pytest crash — нет зависимостей | Worktree не содержит pip packages | Глобальная установка |
| 12 | MEDIUM | Known | pip install fail с Unicode path | file:// URL с кириллицей | Отложено (VPS решит) |
| 13 | MEDIUM | Known | git commit WinError 267 | CWD с кириллицей | Отложено (VPS решит) |

### 8.3 Timeline лучшего прогона (task 13920949, utils/add)

```
02:30:16  dispatch: started task_id=13920949
02:30:16  lease_acquired (token=UUID, ttl=300s)
02:30:16  ensure_mirror: job1/api ready (cached)
02:30:18  prepare_worktree: branch=ai/task-13920949, alias=api
02:30:18  prepare_stage: DONE
02:30:18  execute_stage: attempt 1/3
02:30:18  Running claude in workers/job1_worker (timeout=1800s)
02:31:25  claude finished (output: 1200 chars)
02:31:25  extract_json: found markers, schema valid
02:31:25  Worker: status=done, confidence=92, files=[utils.py, test_utils.py]
02:31:25  review_stage: iteration 1/3
02:31:25  ensure_agent_symlink: reviewer → worktree ✅
02:31:25  Running claude in workers/job1_reviewer (timeout=1800s)
02:31:51  Reviewer: verdict=APPROVED, feedback="Код чистый, тесты покрывают..."
02:31:51  deliver_stage: ruff format ✅
02:31:51  deliver_stage: git add ✅
02:31:51  deliver_stage: git commit ✅
02:31:51  deliver_stage: pytest ❌ (rc=1, зависимости не установлены)
02:31:53  → requires_manual (git_push_failed)
02:31:53  TG: "⚠️ task#13920949: CI failed"
02:31:53  cleanup_worktree: done
```

**Полное время pipeline:** 1 мин 37 сек (от dispatch до cleanup).

### 8.4 Ключевой результат

**7 из 8 задач** прошли worker + reviewer APPROVED на 1-й итерации. Worker генерирует рабочий и качественный код. Pipeline обрывается только на deliver stage из-за окружения (Unicode paths).

---

## 9. Фазы реализации

| Фаза | Название | Статус | Модули | Тестов |
|------|----------|--------|--------|--------|
| 0 | Safety & Determinism | ✅ Done | log_utils, migrate, config_validator, lease_manager, json_guard, run_logger, safe_exec, repo_manager, guard_bash | 163 |
| 1 | Telegram/Email + main.py | ✅ Done | telegram_handler, email_handler, main.py | 69 |
| 2 | Worker pipeline | ✅ Done | worker CLAUDE.md, pipeline stages, WorkerContext | 22 |
| 3 | Code review | ✅ Done | reviewer CLAUDE.md, review cycle, CI+push | 11 |
| 4 | Escalation | ✅ Done | escalation.py, pending_approval flow | 8 |
| 5 | Summarizer | ✅ Done | daily digest в TG | 6 |
| 6 | Health monitor | ✅ Done | snapshot + auto_actions + create_tasks | 18 |
| 7 | Deploy | ⚠️ Частично | systemd + deploy.sh готовы, VPS деплой отложен | — |
| 8 | GraphRAG Memory | Planned | Схема спроектирована (entities, relations, chunks), не реализована | — |

---

## 10. Что НЕ покрыто тестами (честно)

| Область | Почему не покрыта | Риск |
|---------|-------------------|------|
| E2E с реальным Claude CLI | Все тесты используют mock run_claude | Промпт может быть плохо сформулирован, JSON маркеры могут не парситься из реального output |
| Deliver stage git push | Mock safe_exec | git push может fail по причинам не предусмотренным в mock |
| Concurrent задачи | Нет stress test | Race condition в lease acquire (теоретически защищён SQL, но не проверен под нагрузкой) |
| Windows-specific | Unicode paths, symlinks | BUG-012/013 найдены только при debug |
| Real IMAP/SMTP | Credentials placeholder | Возможны баги при реальном IMAP |
| Claude CLI timeout/crash | Mock возвращает instantly | Реальный процесс может зависнуть между stdout и exit |
| Disk full | Нет тестов | worktree clone может fail при нехватке места |
| Large stdout | MAX_OUTPUT_BYTES=10MB в safe_exec | Claude может вернуть больше |

---

## 11. Что стоит улучшить (приоритизировано)

### Высокий приоритет

1. **Реальная нагрузка.** Подключить рабочий репо и дать системе реальные задачи. Debug-задачи не покрывают edge cases реального кода.

2. **Deliver stage.** Перенести в ASCII путь или VPS. Без этого pipeline не может завершить задачу.

3. **Error recovery.** release_stale() запускать каждые 5-10 мин, не только ночью.

### Средний приоритет

4. **`SELECT *` в dispatcher.** `SELECT id, assigned_worker, description` достаточно. Мелочь, но гигиена.

5. **repo_manager._run_git — синхронный.** Блокирует asyncio event loop при git clone. При concurrent=4+ будет проблема. Нужен `run_in_executor`.

6. **Reviewer prompt tuning верификация.** Фикс "не ищи новых проблем" не тестирован в production.

### Низкий приоритет

7. **Guard bash → sandbox.** Текущий regex-парсинг достаточен для trusted environment. Для adversarial inputs нужен nsjail/bubblewrap.

8. **Metrics/observability.** Нет Prometheus/Grafana. Логи в файле. Для personal project — ок.

9. **TG graceful degradation.** Fallback в файл есть, но нет retry queue.

---

## 12. Стек

```
Runtime:       Python 3.12 / asyncio / SQLite 3 (WAL mode)
LLM:           Claude CLI (subprocess --print), Claude Code Max subscription
Integrations:  python-telegram-bot 21+ / imaplib (stdlib) / GitPython 3.1+
Testing:       pytest 8+ / pytest-asyncio / pytest-mock / hypothesis 6.100+
Linting:       ruff (format + check) / semgrep 1.60+ (7 custom rules)
Deploy:        systemd / rsync / Makefile (без Docker)
```

---

## 13. Вопросы для аудитора

1. **Guard bash** — regex-based command parsing. Достаточно ли для изоляции? Стоит ли nsjail/bubblewrap?

2. **Lease TTL = 5 мин.** При crash задача висит до ночного cleanup. Оптимальный TTL? Watchdog?

3. **Review cycle** — reviewer находит реальные баги, worker не фиксит за 3 итерации. Ограничивать reviewer ("не ищи новых") или увеличить max_iterations?

4. **SQLite** — при каком объёме переходить на Postgres? Текущее: единицы задач/день.

5. **Claude CLI subprocess** — процесс может зависнуть. Timeout = единственная защита. Нужен ли heartbeat/watchdog для subprocess?

6. **safe_exec env filtering** — отбрасываем ключи с TOKEN/SECRET/PASSWORD/KEY. Достаточно? Можно ли leak через другие env vars?

7. **Worktree isolation** — разные задачи используют разные worktrees одного bare repo. Может ли задача A повлиять на задачу B через bare repo state?

8. **json_guard fallback regex** — `\{.*\}` (DOTALL) берёт первый JSON из stdout. Если Claude выведет два JSON объекта — возьмёт неправильный. Это риск?

9. **Event system** — events накапливаются в памяти и dispatched после каждого stage. При crash между stage и dispatch — events теряются. Нужна ли персистенция?

10. **Общая оценка** — для personal automation tool с одним пользователем и десятком задач в день — это overengineered, underengineered, или в самый раз?
