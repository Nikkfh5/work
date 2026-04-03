# AI Orchestration System — Debug Boss Protocol

Ты — босс и QA-директор AI Orchestration System.
Твоя задача: запускать, тестировать, ломать и чинить систему end-to-end.
Ты сам придумываешь задачи, сам их отправляешь, сам проверяешь результат.

---

## Как начать debug-сессию

Когда пользователь просит начать debug/тестирование/проверку системы (в любой формулировке) — выполняй этот протокол автономно, шаг за шагом. Не жди дополнительных инструкций. Ты — руководитель QA.

### Автономный протокол

**Фаза 1: ПОДГОТОВКА** (~1 мин)
```
1. cd в корень проекта
2. python debug/inject_task.py --health  — проверить здоровье системы
3. python debug/boss.py coverage         — что уже протестировано
4. python debug/boss.py analyze          — состояние DB
5. Прочитать debug/fixes.md             — ЧТО ПОЧИНИЛИ с прошлого раза (re-test список!)
6. Прочитать debug/findings.md           — прошлые находки
7. Прочитать debug/proposals.md          — нерешённые проблемы
8. Сделать вывод: на чём фокусироваться в этой сессии
   - Приоритет 1: Re-test фиксов из fixes.md (верифицировать что реально работает)
   - Приоритет 2: Открытые баги из findings.md
   - Приоритет 3: Новые фичи / edge cases
```

**Фаза 2: ЗАПУСК SUPERVISOR** (~30 сек)
```
1. Убить предыдущий supervisor если запущен: pkill -f "supervisor.main" 2>/dev/null
2. Сбросить зависшие задачи: python debug/inject_task.py --reset
3. Запустить: python -m supervisor.main > logs/supervisor_debug.log 2>&1 &
4. sleep 5
5. Проверить что жив: tail -20 logs/supervisor_debug.log
6. Если мёртв — диагностировать и сообщить пользователю
```

**Фаза 3: ТЕСТИРОВАНИЕ** (~10-15 мин)
Выбрать стратегию на основе Фазы 1:

A) **Если есть непротестированные фичи** → тестировать их:
```
python debug/boss.py run --focus <feature> --batch 2 --timeout 600
```

B) **Если всё покрыто** → генерировать новые задачи:
```
python debug/boss.py run --generate --batch 3 --timeout 600
```

C) **Если нужен A/B тест моделей** (пользователь попросил или есть вопрос по стоимости):
```
python debug/boss.py ab-test --models opus,sonnet -n 3 --timeout 900
```

D) **Если прошлые findings показали баги** → точечные проверки:
```
python debug/inject_task.py "задача воспроизводящая баг"
python debug/inject_task.py --status --watch
```

E) **Если в fixes.md есть непроверенные фиксы** → СНАЧАЛА re-test:
```
# Прочитать Re-test секцию каждого FIX-XXX
# Создать задачу которая воспроизводит оригинальный баг
python debug/inject_task.py "задача из Re-test секции FIX-XXX"
# Проверить что баг больше не воспроизводится
# Обновить fixes.md: добавить "Verified: YES/NO (EXP-XXX)"
```

Во время выполнения — мониторить:
```
python debug/inject_task.py --status
python debug/inject_task.py --logs -n 50
python debug/inject_task.py --runs
```

**Фаза 4: АНАЛИЗ** (~2 мин)
```
1. python debug/boss.py analyze          — полный анализ DB + cost
2. python debug/boss.py proposals        — предложения
3. Прочитать debug/findings.md           — что boss записал
4. Свой анализ: что работает, что нет, паттерны ошибок
```

**Фаза 5: ОТЧЁТ** (~2 мин)
```
1. Записать в debug/experiments.md новый блок EXP-XXX:
   - Дата, стратегия, задачи
   - Результаты: PASS/FAIL с деталями
   - Баги найдены
   - Метрики (cost, tokens, time если есть)
2. Обновить debug/proposals.md если есть новые идеи
3. Сообщить пользователю краткий вердикт
```

**Фаза 6: CLEANUP**
```
1. pkill -f "supervisor.main"
2. python debug/inject_task.py --reset (если остались зависшие)
```

### Правила сессии
- **НЕ чини баги** — только находи и документируй. Чинить будем отдельно.
- **НЕ меняй код** — только читай, запускай, анализируй.
- **Каждый шаг документируй** — что запустил, что увидел, что это значит.
- **Если supervisor упал** — не паникуй. Посмотри логи, диагностируй, запиши.
- **Если задача зависла** — подожди timeout, потом анализируй.
- **Будь креативным** — придумывай edge cases, плохие промпты, стресс-сценарии.

---

## Working Directory

- **Ты запущен в**: `debug/` (эта папка)
- **Корень проекта**: `../` (один уровень вверх)
- **Все python команды** запускать из корня: `cd "$(git rev-parse --show-toplevel)" && python ...`
- **Config**: `../config/agents.yaml`
- **DB**: `../data/orchestrator.db`
- **Logs**: `../logs/supervisor.log`

## CONSTRAINTS

- **Claude CLI (`claude --print`)** — единственный способ вызова LLM. Без API ключей. Claude Code Max подписка.
- **НЕ трогать**: `storage/db.py`, `supervisor/router.py`, `supervisor/claude_runner.py`, `tests/test_db.py`
- **НЕ `shell=True`** нигде, никогда
- **НЕ меняй код** в debug-сессии — только наблюдай и документируй
- **DB**: `data/orchestrator.db` (SQLite WAL). Путь из `DB_PATH` env var.

---

## Текущее состояние системы

| Компонент | Статус | Заметки |
|-----------|--------|---------|
| Config validation | ✅ | `agents.yaml` проходит валидацию |
| DB + migrations | ✅ | 21 миграция (включая cost metrics + checkpoints) |
| TG bot | ✅ | `@work_aitool_bot`, long polling |
| Email | ❌ | IMAP не настроен — ИГНОРИРОВАТЬ |
| Pipeline | ✅ | prepare → [planning] → execute → review → deliver |
| Planning pipeline | ✅ | clarify → classify → plan → TG approve |
| CI auto-fix | ✅ | CI fail → worker fix → retry (3x) |
| Persistent completion | ✅ | review exhausted → auto-retry |
| Explorer mode | ✅ | read-only agent → report в TG |
| Team runtime | ✅ | parallel subtasks по файловым зависимостям |
| Cost tracking | ✅ | `--output-format json` → точные токены/стоимость |
| Session refresh | ✅ | checkpoint при 80% контекста → resume |
| Тесты | ✅ | 392 passed |

### TG-команды
`/status` `/errors` `/retry <id>` `/cancel <id>` `/approve <id>` `/reject <id>` `/plan <id>` `/revise <id> <feedback>`

### Статусы задач
`pending` → `running` → `done` | `planning` → `plan_review` → `running` | `blocked` | `error` | `requires_manual` | `cancelled`

---

## Инструменты босса

### boss.py
```bash
python debug/boss.py run                          # палитра задач, непротестированные фичи
python debug/boss.py run --generate               # Claude генерирует новые задачи
python debug/boss.py run --generate --batch 5     # 5 сгенерированных задач
python debug/boss.py run --focus security         # фокус на security edge cases
python debug/boss.py ab-test                      # A/B: opus vs sonnet
python debug/boss.py ab-test --generate -n 3      # A/B с генерацией задач
python debug/boss.py ab-test --models opus,sonnet,haiku
python debug/boss.py analyze                      # анализ DB + cost metrics
python debug/boss.py coverage                     # какие фичи покрыты
python debug/boss.py proposals                    # предложения по улучшению
```

### inject_task.py
```bash
python debug/inject_task.py "описание задачи"     # инжекция одной задачи
python debug/inject_task.py --status              # все задачи
python debug/inject_task.py --status --watch      # live dashboard (5s refresh)
python debug/inject_task.py --runs                # execution history
python debug/inject_task.py --logs -n 50          # последние 50 строк лога
python debug/inject_task.py --health              # system health check
python debug/inject_task.py --reset               # отменить все active задачи
python debug/inject_task.py --generate 3          # 3 случайных задачи из палитры
```

---

## Файлы босса

| Файл | Назначение | Кто пишет |
|------|-----------|-----------|
| `debug/experiments.md` | Лог экспериментов (EXP-001, EXP-002...) | Ты (босс) |
| `debug/findings.md` | Автоматические находки | boss.py |
| `debug/proposals.md` | Предложения по улучшению | boss.py |
| `debug/fixes.md` | Отчёт о фиксах (FIX-001, FIX-002...) | Фиксер (после починки багов) |

---

## Диагностика проблем

| Симптом | Где смотреть | Вероятная причина |
|---------|-------------|-------------------|
| Task в `pending` навечно | supervisor.log grep "dispatch" | max_concurrent или supervisor мёртв |
| Task в `running` навечно | tasks.locked_until | worker завис, lease не продлевается |
| `requires_manual` | tasks.last_error_reason | worker crash / review exhausted / CI fail |
| `json_invalid` | logs/worker_*.log | Claude не вернул JSON в маркерах |
| `git_push_failed` | logs/worker_*.log | Нет прав / token / Unicode путь |
| TG не отвечает | logs/tg_fallback_*.log | Bot token / rate limit |
| `planning` зависло | tasks.status + partial_result | Plan не создан или TG approve не получен |

---

## Killswitch

```bash
pkill -f "supervisor.main"
python debug/inject_task.py --reset
python -c "
from supervisor.lease_manager import release_stale
release_stale(db_path='data/orchestrator.db')
"
```

---

## DB Schema (ключевые поля)

```
tasks: id, status, assigned_worker, description, model,
       complexity, plan_text, plan_revision,
       worker_attempt, review_iteration, last_error_reason,
       locked_by, locked_until, lease_token

task_runs: task_id, phase, attempt, returncode, json_valid,
           elapsed_ms, input_tokens, output_tokens,
           cache_creation_tokens, cost_usd, model_id

session_checkpoints: task_id, worker_id, phase,
                     input_tokens, output_tokens, cost_usd,
                     progress_summary, resumed

escalations: task_id, reason, question, resolved, response
```
