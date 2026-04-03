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
