-- AI Orchestration System — SQLite Schema
-- Контекст накапливается (append-only), никогда не удаляется

PRAGMA journal_mode=WAL;  -- безопасные concurrent writes
PRAGMA foreign_keys=ON;

-- ─────────────────────────────────────────
-- Задачи
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,           -- UUID
    status          TEXT NOT NULL DEFAULT 'pending',
                    -- pending | in_progress | waiting_client | done | blocked | cancelled
    source          TEXT NOT NULL,              -- 'telegram' | 'email'
    source_contact  TEXT NOT NULL,              -- @username или email адрес отправителя
    assigned_worker TEXT NOT NULL,              -- worker_id из agents.yaml
    title           TEXT,                       -- краткое название (заполняет supervisor)
    description     TEXT NOT NULL,             -- полный текст задания
    client_contact  TEXT NOT NULL,             -- кому отвечать (может совпадать с source_contact)
    priority        TEXT NOT NULL DEFAULT 'normal', -- low | normal | high | urgent
    git_repo        TEXT,                       -- репо воркера для этой задачи
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tasks_worker  ON tasks(assigned_worker, status);
CREATE INDEX IF NOT EXISTS idx_tasks_status  ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);

-- ─────────────────────────────────────────
-- Сообщения (история переписки по задаче)
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS messages (
    id         TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL REFERENCES tasks(id),
    role       TEXT NOT NULL,   -- 'client' | 'supervisor' | 'worker'
    agent_id   TEXT,            -- worker_id или 'supervisor'
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_messages_task ON messages(task_id, created_at);

-- ─────────────────────────────────────────
-- Контекст агентов (append-only, НИКОГДА не удалять)
-- Воркер видит 90% своих записей, супервайзер — 60%
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS agent_context (
    id          TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL,  -- worker_id или 'supervisor'
    entry_type  TEXT NOT NULL,
                -- 'task_done' | 'task_blocked' | 'decision' | 'meeting' | 'note' | 'client_feedback'
    task_id     TEXT REFERENCES tasks(id),
    content     TEXT NOT NULL,  -- текст знания/события
    importance  TEXT NOT NULL DEFAULT 'normal',  -- low | normal | high
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ctx_agent    ON agent_context(agent_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ctx_type     ON agent_context(agent_id, entry_type);
CREATE INDEX IF NOT EXISTS idx_ctx_task     ON agent_context(task_id);

-- ─────────────────────────────────────────
-- Канал воркер → супервайзер
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS worker_updates (
    id                  TEXT PRIMARY KEY,
    worker_id           TEXT NOT NULL,
    task_id             TEXT NOT NULL REFERENCES tasks(id),
    update_type         TEXT NOT NULL,
                        -- 'progress' | 'ask_client' | 'ask_supervisor' | 'done' | 'blocked'
    payload             TEXT NOT NULL,  -- JSON: { message, code_snippet?, commit_url? }
    read_by_supervisor  INTEGER NOT NULL DEFAULT 0,  -- 0 | 1
    processed_at        TEXT,           -- когда супервайзер обработал
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_wu_unread   ON worker_updates(read_by_supervisor, created_at);
CREATE INDEX IF NOT EXISTS idx_wu_worker   ON worker_updates(worker_id, task_id);

-- ─────────────────────────────────────────
-- Канал супервайзер → воркер
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS supervisor_directives (
    id              TEXT PRIMARY KEY,
    worker_id       TEXT NOT NULL,
    task_id         TEXT NOT NULL REFERENCES tasks(id),
    directive_type  TEXT NOT NULL,
                    -- 'new_task' | 'clarification' | 'priority_change' | 'stop' | 'feedback'
    payload         TEXT NOT NULL,  -- JSON: { instruction, context?, priority? }
    read_by_worker  INTEGER NOT NULL DEFAULT 0,  -- 0 | 1
    read_at         TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sd_unread ON supervisor_directives(worker_id, read_by_worker, created_at);
CREATE INDEX IF NOT EXISTS idx_sd_task   ON supervisor_directives(task_id);

-- ─────────────────────────────────────────
-- Эскалации к владельцу системы
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS escalations (
    id          TEXT PRIMARY KEY,
    task_id     TEXT REFERENCES tasks(id),
    worker_id   TEXT,
    reason      TEXT NOT NULL,  -- 'ask_client' | 'code_review' | 'stuck' | 'decision'
    question    TEXT NOT NULL,
    context     TEXT,           -- дополнительный контекст для владельца
    resolved    INTEGER NOT NULL DEFAULT 0,
    response    TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_esc_unresolved ON escalations(resolved, created_at);

-- ─────────────────────────────────────────
-- Созвоны и суммаризации
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS meetings (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    platform        TEXT,           -- 'zoom' | 'meet' | 'teams'
    external_id     TEXT,           -- ID встречи в tl;dv
    transcript      TEXT,           -- полный транскрипт
    summary         TEXT,           -- суммаризация от супервайзера
    participants    TEXT,           -- JSON: [worker_id, ...]
    meeting_date    TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─────────────────────────────────────────
-- Ежедневные дайджесты
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS daily_summaries (
    id          TEXT PRIMARY KEY,
    content     TEXT NOT NULL,  -- текст дайджеста, отправляется в TG
    sent        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
