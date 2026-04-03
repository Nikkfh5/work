---
name: semgrep-rule-creator
description: "Creates custom Semgrep rules for detecting security vulnerabilities, bug patterns, and code patterns. Use when writing Semgrep rules or building custom static analysis detections. Also includes pre-built rules for project invariants."
---

# Semgrep Rule Creator

Create production-quality Semgrep rules with proper testing and validation.

## When to Use

- Writing Semgrep rules for specific bug patterns
- Writing rules to detect security vulnerabilities in our codebase
- Writing taint mode rules for data flow vulnerabilities
- Enforcing project invariants automatically

## When NOT to Use

- Running existing Semgrep rulesets -> use `static-analysis` skill
- General static analysis without custom rules

## Approach Selection

- **Taint mode** (prioritize): Data flow issues where untrusted input reaches dangerous sinks
- **Pattern matching**: Simple syntactic patterns without data flow requirements

## Output Structure

Exactly 2 files per rule in a directory named after the rule-id:
```
<rule-id>/
  <rule-id>.yaml     # Semgrep rule
  <rule-id>.py       # Test file with ruleid/ok annotations
```

## Workflow

```
Semgrep Rule Progress:
- [ ] Step 1: Analyze the Problem
- [ ] Step 2: Write Tests First
- [ ] Step 3: Analyze AST structure
- [ ] Step 4: Write the rule
- [ ] Step 5: Iterate until all tests pass (semgrep --test)
- [ ] Step 6: Optimize the rule (remove redundancies, re-test)
- [ ] Step 7: Final Run
```

### Step 1: Analyze the Problem
- Understand the exact bug pattern to detect
- Determine if taint mode or pattern matching is appropriate

### Step 2: Write Tests First (MANDATORY)
Test annotations (ONLY allowed annotations):
```python
# ruleid: rule-id
vulnerable_code()              # This line MUST match

# ok: rule-id
safe_code()                    # This line MUST NOT match
```

Include test cases for: vulnerable cases, safe cases, edge cases, different coding styles, sanitized input, unrelated code.

### Step 3: Analyze AST
```bash
semgrep --dump-ast --lang python <rule-id>.py
```

### Step 4: Write the Rule
```yaml
rules:
  - id: rule-id
    languages: [python]
    severity: HIGH
    message: Description
    pattern: code(...)  # OR taint mode
```

### Step 5: Iterate Until Tests Pass
```bash
semgrep --test --config <rule-id>.yaml <rule-id>.py
```

### Step 6: Optimize
- Remove quote variants (Semgrep normalizes quotes)
- Remove ellipsis subsets (`func($X, ...)` covers `func($X)`)
- Consolidate with metavariable-regex
- **Re-run tests after each optimization**

### Step 7: Final Run
```bash
semgrep --config <rule-id>.yaml <rule-id>.py
```

## Quick Reference

See `references/quick-reference.md` for pattern operators, taint mode syntax, and debugging commands.
See `references/workflow.md` for detailed step-by-step workflow.

## Pre-Built Rules for Project Invariants

See `.semgrep/rules/` directory for rules enforcing our 10 invariants. Run all:
```bash
semgrep --metrics=off --config .semgrep/rules/ .
```

## Anti-Patterns

**Too broad**:
```yaml
# BAD
pattern: $FUNC(...)
# GOOD
pattern: eval(...)
```

**Missing safe cases in tests**:
```python
# BAD: Only tests vulnerable case
# ruleid: my-rule
dangerous(user_input)

# GOOD: Include safe cases
# ruleid: my-rule
dangerous(user_input)
# ok: my-rule
dangerous(sanitize(user_input))
```
