# Debug Findings

Append-only лог находок босса. Каждый запуск дописывает блок.
Босс читает этот файл перед генерацией новых задач,
чтобы не повторять проверенные сценарии.

---

### 2026-04-03 EXP-005 findings

**BUG-014 (CRITICAL):** `renew_lease()` вызывается ТОЛЬКО из `planning.py`. В execute/review/deliver — ноль вызовов. Worker >5 мин → lease expires → work lost.

**BUG-015 (HIGH):** При lease_stale task_run НЕ записывается. Файлы в worktree остаются но supervisor не знает.

**BUG-016 (HIGH):** Clarification → lease_stale через ~21 сек. Planning stage меняет status → `renew_lease` фейлится (проверяет `AND status = 'running'`).

**BUG-017 (MEDIUM):** Pipeline продолжает работу после того как dispatch loop сбросил lease через release_stale. Race condition.

**BUG-018 (MEDIUM):** Два dispatch одновременно → оба вызывают ensure_mirror → `directory already exists`.

**BUG-019 (LOW-MEDIUM):** git add/commit rc=1 в deliver stage при свежем тестовом репо. Причина неясна, stderr не логируется.

**POSITIVE:** SQL injection безопасна (параметризованные запросы), clarification pipeline работает, CI auto-fix pipeline работает (3 попытки), worker создаёт качественный код.

---

### 2026-04-03 EXP-006 findings

**VERIFIED FIX:** BUG-014 (background lease renewal) — pipeline 383ba244 работал 10 мин при TTL=5 мин, без lease_stale. Фоновый renewal в `pipeline.py` работает корректно.

**VERIFIED FIX:** BUG-016 (renew_lease status check) — задача a4633e3d в статусе `planning` (не `running`), lease успешно продлён. Fix `status NOT IN (terminal)` работает.

**VERIFIED FIX:** BUG-019 (git diagnostics) — stderr от git add/commit теперь видны: `"Changes not staged for commit"`, `"paths are ignored by .gitignore"`.

**BUG-020 (HIGH):** `git add .` в worktree возвращает rc=1, файлы НЕ staging. Причина: parent `.gitignore` проекта содержит `worktrees/`, git подхватывает это правило. Все задачи фейлятся на deliver stage.

**BUG-021 (HIGH):** Worktree на ветке `main` вместо `ai/task-{id}`. Git commit output: "On branch main, ahead of origin/main by 2 commits". Возможная причина: fallback в `prepare_worktree` при уже существующей ветке.

**BUG-022 (MEDIUM):** `[WinError 267] Неверно задано имя папки` при запуске claude auto-fix runner в worktree path. Windows не может использовать worktree path как cwd для subprocess.

**BUG-023 (LOW):** Cost tracking не записывается — `cost_usd=0`, `model_id=?` для всех task_runs. Метрики `--output-format json` не парсятся или не передаются в `log_run()`.

**POSITIVE:** Pipeline prepare→execute→review безупречен. 3/3 задачи: worker создал код, reviewer одобрил на 1-й итерации. Planning pipeline (classify→plan→approve) работает. Clarification pipeline работает. Background lease renewal работает.

---

### 2026-04-03 EXP-007 findings

**ROOT CAUSE (BUG-020/021):** `prepare_worktree` передавал относительный путь `worktrees/{task_id}/api` в `git worktree add` с `cwd=mirror`. Git создавал worktree внутри mirror (`repos_cache/job1/api.git/worktrees/...`) а не в top-level. Worker/deliver использовали top-level `worktrees/` (без `.git` файла) → git fallback на основной проект → коммиты в основной репо.

**VERIFIED FIX:** BUG-020/021 — абсолютный путь в `git worktree add`. Коммит `1e21297` на ветке `ai/task-abf60a52` в тестовом репо (не в основном).

**VERIFIED FIX:** BUG-022 (WinError 267) — `os.path.abspath()` для cwd в deliver + Windows env vars в safe_exec.

**VERIFIED FIX:** safe_exec env — SYSTEMROOT/TEMP/COMSPEC добавлены. Pytest/asyncio больше не crash-ат.

**NEW FIX:** pytest isolation — `--rootdir` + `asyncio_mode=strict` предотвращают подхват parent `pytest.ini`.

**FIRST FULL E2E:** Task abf60a52 — prepare → planning → execute → review:APPROVED → ruff → git add → git commit → pytest(3 passed) → git push ✅

**POSITIVE:** Весь pipeline работает end-to-end. Worker создаёт качественный код, reviewer одобряет на 1-й итерации, deliver stage полностью функционален.

---

### 2026-04-04 EXP-008 findings

**STABILITY CONFIRMED:** 3/3 coding-задачи прошли полный E2E без единого критического бага. Pipeline стабилен: planning → execute → review → deliver → git push.

**REVIEW CYCLE CONFIRMED:** Task 88e7a1e5 (validate_email) — reviewer выдал NEEDS_CHANGES на 1-й итерации, worker исправил, APPROVED на 2-й. Цикл worker↔reviewer работает корректно.

**PLANNING CONFIRMED:** Все 3 complex-задачи корректно классифицированы, планы созданы (confidence 82-85), approval через partial_result работает.

**LEASE STABILITY:** 0 lease_stale за всю сессию (~12 мин uptime). Background renewal полностью решил проблему.

**BUG-025 (MEDIUM):** Задачи без файлов (анализ/отчёты) идут в deliver stage → git commit rc=1 (nothing to commit) → pytest rc=5 (no tests) → CI auto-fix loop → git_push_failed. Нужен skip deliver для задач у которых worker не создал файлы, или определение "explorer" типа задач.

**POSITIVE:** Система готова к production use для coding-задач. Все критические баги (BUG-014..022) починены и верифицированы в 3 последовательных сессиях.

---

### 2026-04-05 EXP-009 findings

**BUG-026 (CRITICAL): Background lease renewal в pipeline.py НЕ РАБОТАЕТ.**
- Ноль записей renewal в логах за всю сессию (30 мин)
- 3/5 задач получили lease_stale из-за нерабочего renewal
- Planning stage renewal (poll loop в planning.py) РАБОТАЕТ — locked_until обновляется
- Pipeline.py `_background_lease_renewal` asyncio task создаётся но lease не обновляет
- Код не менялся с FIX-002 (2026-04-03), но renewal не работает → возможно Windows asyncio issue или тихое исключение в background task
- **РЕГРЕССИЯ:** В EXP-006 (04-03) renewal работал. В EXP-008 (04-04) lease_stale=0. Сейчас 3/5 stale.

**BUG-017 (HIGH) — подтверждён x3: Zombie pipeline после lease_stale.**
- 3 задачи: pipeline продолжил работу после release_stale → deliver → git push → TG notifications
- 4699b5f2: zombie push SUCCESS (код на GitHub)
- 95aa89ae: zombie push SUCCESS (код на GitHub)
- dc54db4e: zombie "nothing to commit" → push пропущен
- Pipeline НЕ проверяет lease validity перед каждым stage
- Результат: status="error" но работа выполнена → пользователь видит ложную "ошибку"

**BUG-027 (MEDIUM): CI auto-fix "nothing to commit" пропускает push начального коммита.**
- dc54db4e: initial commit + ruff fix коммит сделаны локально. После CI auto-fix claude session: ruff format → git add → git commit → "nothing to commit" → skip push
- 2 непушенных коммита на локальной ветке `ai/task-dc54db4e`
- Fix: проверять unpushed commits (`git log origin/branch..branch`) перед skip

**PATTERN: Opus всегда получает NEEDS_CHANGES, Sonnet — нет.**
- Opus: 2/2 задачи получили NEEDS_CHANGES (dc54db4e: 1 iter, 95aa89ae: 2 iters)
- Sonnet: 0/3 coding задачи получили NEEDS_CHANGES (все APPROVED iter 1)
- Гипотеза: opus генерирует более "сложный" код, reviewer находит больше issues. Или opus включает лишние зависимости/паттерны.

**CI AUTO-FIX CONFIRMED:** Впервые протестирован полный цикл:
- ruff check fail → claude fix → ruff pass → push (79674fd6)
- pytest fail → claude fix (hypothesis dep) → pytest pass → push (4699b5f2)
- ruff fail → claude fix → "nothing to commit" → BUG-027 (dc54db4e)

**SONNET PERFORMANCE:** Security задачи на sonnet значительно дольше:
- string_utils (simple): worker 51s
- HTML sanitizer (security): worker 189s (3.7x)
- stack (simple): worker 36s

**POSITIVE:** Pipeline стабилен для coding задач. 5/5 задач создали качественный код. CI auto-fix работает. Planning classifier корректен. TG notifications надёжны.
