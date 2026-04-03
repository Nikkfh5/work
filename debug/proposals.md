# Improvement Proposals

Append-only лог предложений по улучшению системы.
Босс анализирует результаты экспериментов и пишет сюда идеи.
При следующем запуске — читает и проверяет, были ли они реализованы.

---

### PROP-001: Фоновый lease renewal (BUG-014 fix)
- **Приоритет:** CRITICAL
- **Описание:** Добавить asyncio task который продлевает lease каждые TTL/2 секунд (150с) пока pipeline stage выполняется
- **Где:** `supervisor/pipeline.py` — обернуть execute/review/deliver в context manager с фоновым renew
- **Альтернатива:** увеличить TTL до 3600с (1 час), но это маскирует проблему

### PROP-002: Atomic ensure_mirror с lock (BUG-018 fix)
- **Приоритет:** MEDIUM
- **Описание:** `ensure_mirror` вызывается параллельно из двух tasks → race condition
- **Решение:** asyncio.Lock на worker_id + repo_alias, или retry с backoff при "already exists"

### PROP-003: Clarification → blocked status (BUG-016 fix)
- **Приоритет:** HIGH
- **Описание:** При clarification задача должна переходить в status='blocked' (не 'running')
- **Проблема:** renew_lease проверяет `AND status = 'running'`, при clarification status может быть другим
- **Решение:** либо убрать status check из renew_lease, либо правильно управлять status transitions

### PROP-004: Partial result recovery при lease_stale (BUG-015 fix)
- **Приоритет:** MEDIUM
- **Описание:** Если worker создал файлы но lease истёк, сохранять partial_result и path к worktree
- **Решение:** в release_stale записывать partial_result с worktree path для возможного recovery

### PROP-005: Диагностика git failures в deliver stage (BUG-019)
- **Приоритет:** MEDIUM
- **Описание:** `git add` и `git commit` возвращают rc=1 без видимой причины в логах
- **Решение:** логировать stderr от git commands в deliver stage для диагностики

### PROP-006: Автоответ на clarification из debug boss
- **Приоритет:** LOW
- **Описание:** debug boss мог бы автоматически отвечать на clarification questions через resolve_escalation()
- **Решение:** добавить в boss.py возможность auto-resolve escalations для тестирования

### PROP-007: Supervisor PID lock file
- **Приоритет:** MEDIUM
- **Описание:** Два supervisor-а запускаются одновременно → TG 409 Conflict + race conditions
- **Решение:** PID lock file при старте supervisor, fail-fast если уже запущен
