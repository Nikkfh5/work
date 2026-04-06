---
name: self-qa-loop
description: "Self-controlling QA infrastructure: autonomous feedback loop that tests, finds bugs, proposes fixes, and verifies corrections. Use when setting up debug/testing infrastructure, when user mentions 'autonomous testing', 'self-testing', 'debug loop', 'QA automation', or wants to systematically find bugs in their system."
---

# Self-QA Loop — Self-Controlling Debug Infrastructure

## Что это

Методология и тулкит для создания **автономных QA-петель** в любом проекте.
Система сама генерирует тесты, инжектирует их, мониторит, анализирует результаты,
находит баги, предлагает фиксы, и верифицирует исправления — без ручного управления.

**Ключевая инновация:** append-only институциональная память
(experiments → findings → proposals → fixes → verification)
создаёт порочный цикл улучшений, где каждая сессия дебага строится на всех предыдущих.

```
┌─────────────────────────────────────────────┐
│               FEEDBACK LOOP                 │
│                                             │
│  Generate tests ──→ Inject ──→ Monitor      │
│       ↑                           │         │
│       │                           ↓         │
│  Learn from past ←── Analyze ←── Results    │
│       │                           │         │
│       ↓                           ↓         │
│  experiments.md    findings.md  proposals.md │
│       │                           │         │
│       └───── fixes.md ←── Fix ←───┘         │
│                  │                          │
│                  └──→ Re-test (verify) ──→↑ │
│                                             │
└─────────────────────────────────────────────┘
```

## Когда триггерится

### Автоматические триггеры (ПРОАКТИВНЫЕ)

1. **Пользователь просит настроить тестирование/дебаг** — "хочу автоматически тестировать", "настрой дебаг", "как найти баги"
2. **Пользователь упоминает QA/debug loop** — "self-testing", "autonomous QA", "debug infrastructure"
3. **Есть debug/ директория** — перейти в режим выполнения протокола (Phase 3+)
4. **Пользователь хочет перенести паттерн** — "как в том проекте", "такой же дебаг"

### НЕ триггерится

- При обычном написании unit-тестов (pytest)
- При ручном дебаге одного бага (→ systematic-debugging)
- При CI/CD настройке (→ gh-fix-ci)

## Архитектура

Self-QA Loop состоит из 5 компонентов:

### 1. Протокол (CLAUDE.md в debug/)

Инструкции для автономного выполнения 6-фазного цикла:

| Фаза | Действие | Время |
|------|----------|-------|
| ПОДГОТОВКА | Health check, прочитать прошлые findings/fixes | ~1 мин |
| ЗАПУСК | Стартовать систему под тестом | ~30 сек |
| ТЕСТИРОВАНИЕ | Инжекция задач, мониторинг | 10-15 мин |
| АНАЛИЗ | Метрики, паттерны ошибок | ~2 мин |
| ОТЧЁТ | Записать experiment, обновить proposals | ~2 мин |
| CLEANUP | Остановить систему, сбросить state | ~30 сек |

Шаблон: `references/protocol-template.md`

### 2. Boss Engine (boss.py)

Автономный "босс-тестировщик" с функциями:
- **generate** — генерирует тест-кейсы (палитра + LLM)
- **inject** — отправляет в систему под тестом
- **monitor** — следит за выполнением (polling/events)
- **analyze** — извлекает метрики и паттерны
- **report** — записывает findings и proposals

Использует `qa_core.py` (универсальная библиотека) + project-specific backend.

### 3. Monitor Tool (inject/monitor)

CLI для ручного взаимодействия:
- Инжекция отдельных задач
- Live dashboard с auto-refresh
- Health check системы
- Reset зависших задач

### 4. Институциональная память (append-only logs)

| Файл | ID формат | Кто пишет | Назначение |
|------|-----------|-----------|------------|
| experiments.md | EXP-001 | Boss/Claude | Полный лог каждого запуска |
| findings.md | BUG-001 | Boss автоматически | Найденные баги и паттерны |
| proposals.md | PROP-001 | Boss автоматически | Предложения по улучшению |
| fixes.md | FIX-001 | Разработчик | Отчёт о фиксах + re-test |

Форматы: `references/log-formats.md`

### 5. qa_core.py — Универсальная библиотека

Не зависит от проекта. Обеспечивает:
- LogManager: append-only CRUD для всех 4 типов логов
- IDManager: автоинкремент EXP/BUG/PROP/FIX
- CoverageTracker: какие фичи покрыты, какие нет
- ContextBuilder: генерирует контекст из прошлых запусков для LLM
- ReportGenerator: markdown-отчёты

## Как использовать

### Сценарий A: Scaffold с нуля

Пользователь хочет добавить Self-QA Loop в свой проект:

```bash
python .claude/skills/self-qa-loop/scripts/scaffold.py [--project-dir /path] [--type web|cli|api|ai]
```

Scaffold создаст:
```
debug/
├── CLAUDE.md           # Протокол (из шаблона, адаптирован под проект)
├── boss.py             # Starter boss с TODO для project-specific частей
├── monitor.py          # CLI инструмент мониторинга
├── qa_core.py          # Копия универсальной библиотеки
├── config.yaml         # Конфигурация (что/как тестировать)
├── experiments.md      # Пустой лог
├── findings.md         # Пустой лог
├── proposals.md        # Пустой лог
└── fixes.md            # Пустой лог
```

### Сценарий B: Запуск существующего цикла

Если debug/ уже существует:
1. Прочитать `debug/CLAUDE.md`
2. Выполнять 6-фазный протокол автономно
3. Записать результаты в experiments.md

### Сценарий C: Адаптация под другой тип проекта

Читать `references/adaptation-guide.md` — матрица адаптации для:
- **Web API** — inject = HTTP requests, monitor = response codes
- **CLI tool** — inject = command args, monitor = exit codes
- **AI system** — inject = task descriptions, monitor = DB/state
- **Library** — inject = test cases, monitor = assertions
- **Microservices** — inject = events, monitor = logs/traces

## Правила скилла

### Iron Laws

1. **Append-only логи** — НИКОГДА не редактировать/удалять записи. Только дописывать.
2. **Observe-only сессии** — Во время QA-сессии НЕ чинить баги. Только находить и документировать.
3. **Re-test обязателен** — Каждый FIX должен быть верифицирован через повторный тест.
4. **Контекст из прошлого** — Каждая сессия ОБЯЗАНА прочитать findings + fixes перед стартом.
5. **Новизна тестов** — Не повторять одинаковые тесты. Читать experiments для избежания дублей.

### Anti-patterns

| Рационализация | Почему неправильно |
|---|---|
| "Я и так знаю где баги" | Self-QA находит баги которые ты не ожидал. Автоматика > интуиция |
| "Один прогон достаточно" | Каждый прогон — данные для следующего. Петля ценнее одиночного теста |
| "Findings можно не записывать" | Без записи — нет памяти. Без памяти — нет улучшений |
| "Починю прямо в debug-сессии" | Mixing find + fix → теряешь фокус и пропускаешь другие баги |
| "Re-test не нужен, я уверен" | 30% фиксов ломают что-то ещё. Re-test — единственное доказательство |

## Адаптация

### Что универсально (qa_core.py)

- Append-only log management
- ID generation (EXP/BUG/PROP/FIX)
- Coverage tracking
- Context building for LLM
- Report generation
- 6-phase protocol structure

### Что нужно адаптировать (boss.py)

| Компонент | Что менять | Пример |
|-----------|-----------|--------|
| Task Backend | Как создать/отправить задачу | DB insert, HTTP POST, CLI call |
| Monitor Backend | Как проверить статус | DB poll, API check, log tail |
| Health Check | Что проверять | Process alive, port open, DB connected |
| System Control | Как старт/стоп | systemctl, docker, kill+start |
| Task Palettes | Что тестировать | Feature-specific test cases |
| Pass/Fail Logic | Что считать успехом | exit_code==0, status==200, assertion pass |

### Quick Start по типу проекта

Подробная матрица: `references/adaptation-guide.md`

```
Web API:
  inject: curl/httpx POST to endpoints
  monitor: response status + body validation
  health: GET /health + DB ping
  palettes: CRUD operations, auth, edge cases, load

CLI Tool:
  inject: subprocess.run(["tool", ...args])
  monitor: exit code + stdout parsing
  health: tool --version
  palettes: valid args, invalid args, edge cases, large input

AI System:
  inject: DB insert / API call with task description
  monitor: DB poll for status change
  health: process check + model availability
  palettes: easy/medium/hard/security/edge-case tasks
```

## Файлы скилла

```
.claude/skills/self-qa-loop/
├── SKILL.md                 ← ты здесь
├── scripts/
│   ├── qa_core.py           ← универсальная библиотека (копируется в debug/)
│   └── scaffold.py          ← генератор debug/ для нового проекта
└── references/
    ├── protocol-template.md ← шаблон CLAUDE.md для debug/
    ├── log-formats.md       ← форматы experiments/findings/proposals/fixes
    └── adaptation-guide.md  ← адаптация под web/cli/api/ai/library
```
