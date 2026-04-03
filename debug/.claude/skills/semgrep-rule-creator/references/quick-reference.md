# Semgrep Rule Quick Reference

## Required Rule Fields

```yaml
rules:
  - id: rule-id               # Unique identifier (lowercase, hyphens)
    languages: [python]       # Target language(s)
    severity: HIGH            # LOW, MEDIUM, HIGH, CRITICAL
    message: Description      # Shown when rule matches
    pattern: code(...)        # OR use patterns/pattern-either/mode:taint
```

## Pattern Operators

### Basic Matching
```yaml
pattern: foo(...)                          # Basic match
patterns:                                  # AND — all must match
  - pattern: $X
  - pattern-not: safe($X)
pattern-either:                            # OR — any can match
  - pattern: foo(...)
  - pattern: bar(...)
pattern-regex: ^foo.*bar$                  # PCRE2 regex
```

### Metavariables
- `$VAR` — Match single expression (MUST be uppercase)
- `$_` — Anonymous, matches but doesn't bind
- `$...VAR` — Match zero or more arguments
- `...` — Match anything in between
- `<... [pattern] ...>` — Deep expression match

### Scope
```yaml
pattern-inside: |              # Must be inside this
  def $FUNC(...):
    ...
pattern-not-inside: |          # Must NOT be inside this
  with $CTX:
    ...
```

### Negation
```yaml
pattern-not: safe(...)
pattern-not-regex: ^test_
```

### Filters
```yaml
metavariable-regex:
  metavariable: $FUNC
  regex: (unsafe|dangerous).*
metavariable-pattern:
  metavariable: $ARG
  pattern: request.$X
```

## Taint Mode

```yaml
rules:
  - id: taint-rule
    mode: taint
    languages: [python]
    severity: HIGH
    message: Tainted data reaches sink
    pattern-sources:
      - pattern: user_input()
    pattern-sinks:
      - pattern: eval(...)
    pattern-sanitizers:           # Optional
      - pattern: sanitize(...)
```

## Test Annotations

```python
# ruleid: rule-id
vulnerable_code()              # MUST match

# ok: rule-id
safe_code()                    # MUST NOT match
```

## Debugging

```bash
semgrep --test --config rule.yaml rule.py       # Test rules
semgrep --validate --config rule.yaml            # Validate YAML
semgrep --dataflow-traces --config rule.yaml .   # Debug taint
semgrep --dump-ast --lang python file.py         # AST dump
```
