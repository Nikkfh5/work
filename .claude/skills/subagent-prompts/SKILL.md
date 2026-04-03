---
name: subagent-prompts
description: "Structured prompt templates for conductor mode agents (Builder, Spec Reviewer, Code Quality Reviewer). Use ONLY inside conductor.md workflow at steps 3, 4, 5, 7. NOT a standalone process."
---

# Subagent Prompt Templates

**Когда:** только в режиме conductor.md (шаги 3, 4, 5, 7).
**НЕ когда:** одиночные задачи, дебаг, security review.

## Маршрутизация

| Шаг conductor | Шаблон | Файл |
|---------------|--------|------|
| 4 BUILD | Implementer | `references/implementer-prompt.md` |
| 3 CRITIC plan | Spec Reviewer | `references/spec-reviewer-prompt.md` |
| 5 CRITIC review | Spec → Quality (2 прохода) | оба файла выше + `references/code-quality-prompt.md` |
| 7 CHECK | Quality на diff | `references/code-quality-prompt.md` |

## Двухэтапное ревью

```
BUILD → Spec Review → PASS? → Code Quality → PASS? → SAVE
            ↓ FAIL                 ↓ FAIL
      Fix + re-review        Fix + re-review
```

Code quality НИКОГДА не начинается до прохождения spec review.

## Статусы implementer'а

| Статус | Действие |
|--------|----------|
| DONE | → Spec Review |
| DONE_WITH_CONCERNS | Оценить concerns → review |
| NEEDS_CONTEXT | Дать контекст, перезапустить |
| BLOCKED | Контекст / разбить / эскалация к пользователю |

Мелкие фиксы (<20 строк) → только code quality, без spec review.
