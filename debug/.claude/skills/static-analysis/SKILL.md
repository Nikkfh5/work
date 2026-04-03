---
name: static-analysis
description: "Run Semgrep static analysis scan on the codebase. Use when asked to scan code for vulnerabilities, run a security audit, find bugs, or perform static analysis. Detects Python-specific issues, security vulnerabilities, and code quality problems."
---

# Semgrep Security Scan

Run Semgrep scan with language detection, configurable rulesets, and structured output.

## Essential Principles

1. **Always use `--metrics=off`** — Every `semgrep` command must include `--metrics=off` to prevent telemetry.
2. **User must approve the scan plan before execution** — Present rulesets and scope, wait for explicit approval.
3. **Include third-party rulesets** — Trail of Bits rules catch vulnerabilities absent from the official registry.

## When to Use

- Security audit of the codebase
- Finding vulnerabilities before code review
- Scanning for known bug patterns
- Pre-deploy security check

## When NOT to Use

- Creating custom Semgrep rules -> Use `semgrep-rule-creator` skill
- Binary analysis
- Already have Semgrep CI configured

## Quick Start (Our Project)

```bash
# Install semgrep
pip install semgrep

# Basic Python security scan
semgrep --metrics=off --config p/python --config p/security-audit --severity MEDIUM --severity HIGH --severity CRITICAL .

# With Trail of Bits rules
semgrep --metrics=off --config p/python --config p/trailofbits-python --config p/security-audit .

# JSON output for automation
semgrep --metrics=off --config p/python --json -o results.json .
```

## Recommended Rulesets for This Project

### Core (always include)
| Ruleset | What it catches |
|---------|----------------|
| `p/python` | Python-specific bugs and anti-patterns |
| `p/security-audit` | Cross-language security vulnerabilities |
| `p/trailofbits-python` | Trail of Bits Python security rules |

### Extended (when doing thorough scan)
| Ruleset | What it catches |
|---------|----------------|
| `p/owasp-top-ten` | OWASP Top 10 vulnerabilities |
| `p/command-injection` | Command injection patterns (critical for our subprocess usage) |
| `p/insecure-transport` | Insecure HTTP, missing TLS |
| `p/secrets` | Hardcoded secrets, API keys |

### Project-Specific Focus Areas
| Area | Why | Key files |
|------|-----|-----------|
| Subprocess execution | We run Claude CLI via subprocess | `supervisor/claude_runner.py`, `supervisor/safe_exec.py` |
| Path traversal | Worktree paths, file operations | `supervisor/repo_manager.py`, `supervisor/hooks/guard_bash.py` |
| SQL injection | SQLite queries | `storage/db.py`, `storage/migrate.py` |
| Secret handling | .env, tokens, TG bot token | `supervisor/log_utils.py`, `supervisor/config_validator.py` |
| Input validation | Telegram/Email input parsing | `integrations/telegram_handler.py`, `integrations/email_handler.py` |

## Scan Modes

| Mode | Command | Use case |
|------|---------|----------|
| **Important only** | Add `--severity MEDIUM --severity HIGH --severity CRITICAL` | Quick security check |
| **Run all** | No severity filter | Full audit |

## Workflow

### 1) Detect scope
```bash
# Count Python files
find . -name "*.py" -not -path "./venv/*" -not -path "./.git/*" | wc -l

# Check for semgrep
semgrep --version
```

### 2) Select rulesets based on project
- Python project -> `p/python` + `p/trailofbits-python`
- Has subprocess calls -> `p/command-injection`
- Has SQL -> `p/security-audit`
- Has secrets -> `p/secrets`

### 3) Present plan to user
Show: target directory, rulesets, mode. Wait for approval.

### 4) Execute scan
```bash
mkdir -p scan_results

semgrep --metrics=off \
  --config p/python \
  --config p/trailofbits-python \
  --config p/security-audit \
  --config p/command-injection \
  --severity MEDIUM --severity HIGH --severity CRITICAL \
  --json -o scan_results/results.json \
  --sarif -o scan_results/results.sarif \
  --exclude="venv" --exclude=".git" --exclude="__pycache__" \
  .
```

### 5) Report results
- Group by severity (CRITICAL > HIGH > MEDIUM)
- For each finding: file, line, rule, message, suggested fix
- Summary table with counts

## Output Format

```
## Scan Results Summary

| Severity | Count |
|----------|-------|
| CRITICAL | X |
| HIGH | Y |
| MEDIUM | Z |

## Findings

### CRITICAL
1. **rule-id** — `file:line` — Description + suggested fix

### HIGH
...
```

## References

- Semgrep rulesets: https://semgrep.dev/r
- Trail of Bits rules: https://github.com/trailofbits/semgrep-rules
