# Fix Report

Лог исправлений после debug-сессий. Пишет тот, кто чинил баги.
Debug boss читает этот файл в Фазе 1 (ПОДГОТОВКА) чтобы знать:
- Что починили с прошлого раза
- Как именно починили (какие файлы, какой подход)
- Что нужно перепроверить (re-test)
- Что ещё не починено

---

## Формат записи

```
### FIX-XXX: BUG-YYY — краткое описание
- **Дата:** YYYY-MM-DD
- **Баги:** BUG-YYY, BUG-ZZZ (какие баги закрывает)
- **Proposals:** PROP-YYY (какие proposals реализует, если есть)
- **Изменённые файлы:**
  - `path/to/file.py` — что именно изменилось (1 строка)
- **Подход:** 1-2 предложения как починили
- **Re-test:** Конкретный сценарий для проверки боссом
- **Риски:** Что могло сломаться / на что обратить внимание
```

---

## FIX-001: BUG-005, BUG-006, BUG-007, BUG-008, BUG-009 — post EXP-002 fixes
- **Дата:** 2026-03-28
- **Баги:** BUG-005 (worker CWD), BUG-006 (symlink cleanup), BUG-007 (returncode=None), BUG-008 (email spam), BUG-009 (TG timeout level)
- **Proposals:** —
- **Изменённые файлы:**
  - `supervisor/repo_manager.py` — `ensure_agent_symlink` для reviewer
  - `supervisor/stages/review.py` — вызов symlink + returncode=0
  - `supervisor/stages/execute.py` — returncode=0 в log_run
  - `integrations/email_handler.py` — exponential backoff для IMAP errors
  - `integrations/telegram_handler.py` — TimeoutError → DEBUG level
- **Подход:** Прямые фиксы по диагностике из EXP-002
- **Re-test:** Запустить задачу >5 мин, проверить что reviewer видит файлы, returncode записывается, email не спамит
- **Риски:** symlink cleanup на Windows может вести себя иначе (junctions vs symlinks)

## FIX-002: BUG-014, BUG-016, BUG-019 — post EXP-005 lease & git fixes
- **Дата:** 2026-04-03
- **Баги:** BUG-014 (lease не продлевается), BUG-016 (renew_lease status check), BUG-019 (git diagnostics)
- **Proposals:** PROP-001 (фоновый lease renewal), PROP-003 (status check в renew_lease)
- **Изменённые файлы:**
  - `supervisor/pipeline.py` — background lease renewal task
  - `supervisor/lease_manager.py` — renew_lease без жёсткой проверки status
  - `supervisor/stages/deliver.py` — логирование stderr от git commands
- **Подход:** Фоновый asyncio task продлевает lease каждые TTL/2, renew_lease ослаблен, git stderr логируется
- **Re-test:** Задача >5 мин должна завершиться без lease_stale. Clarification не должна падать. Git errors должны показывать stderr.
- **Риски:** Фоновый renewal может маскировать зависшие задачи — нужен hard timeout
- **Verified:** YES (EXP-006, EXP-007)

## FIX-003: BUG-020, BUG-021, BUG-022 — worktree path + Windows env + pytest isolation
- **Дата:** 2026-04-03
- **Баги:** BUG-020 (git add fails in worktree), BUG-021 (worktree on main), BUG-022 (WinError 267)
- **Proposals:** PROP-008 (git add в worktree), PROP-009 (WinError 267)
- **Изменённые файлы:**
  - `supervisor/repo_manager.py` — `wt_path.resolve()` (абсолютный путь в `git worktree add`)
  - `supervisor/stages/deliver.py` — `os.path.abspath()` для cwd, pytest `--rootdir` isolation
  - `supervisor/safe_exec.py` — Windows env vars (SYSTEMROOT, TEMP, COMSPEC и др.)
- **Подход:** Root cause: relative path в `git worktree add` создавал worktree внутри mirror, а не в top-level. Worker писал файлы в top-level (без .git файла), git fallback-ил на основной проект. Fix: абсолютные пути + Windows env + pytest rootdir isolation.
- **Re-test:** Задача с тестами (greet/hello/calculator) должна пройти полный E2E до git push. Проверить что коммит в тестовом репо (не в основном).
- **Риски:** Нет значительных
- **Verified:** YES (EXP-007 — task abf60a52 DONE, commit 1e21297 pushed)
