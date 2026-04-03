# Code Quality Reviewer Prompt Template

**Запускать ТОЛЬКО ПОСЛЕ прохождения Spec Review.**

```
Agent(
  description="Code quality: {module_name}",
  subagent_type="general-purpose",
  prompt="""
  Проверь КАЧЕСТВО КОДА (соответствие спецификации уже проверено).

  ## Изменённые файлы
  {Список — прочитай их}

  ## Чеклист

  **БЛОКИРУЮЩИЕ:**
  - Защищённые файлы НЕ изменены
  - Нет shell=True
  - Логи через redact(), 10 инвариантов CLAUDE.md
  - Все тесты проходят

  **ВАЖНЫЕ:**
  - Одна ответственность на файл
  - Логирование с контекстом (task_id, phase)
  - DI через параметры, стандартные коды ошибок
  - Тесты проверяют поведение, не моки

  **MINOR:**
  - Docstring, нет дублирования, нет print()

  ## Формат ответа
  Strengths / Issues (Critical/Important/Minor) / Assessment: APPROVED | NEEDS_CHANGES
  """
)
```
