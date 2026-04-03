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
