---
name: "gh-fix-ci"
description: "Use when a user asks to debug or fix failing GitHub PR checks that run in GitHub Actions; use `gh` to inspect checks and logs, summarize failure context, draft a fix plan, and implement only after explicit approval."
---

# GitHub PR Checks Fix

## Overview

Use gh to locate failing PR checks, fetch GitHub Actions logs for actionable failures, summarize the failure snippet, then propose a fix plan and implement after explicit approval.

Prereq: authenticate with `gh auth login` (repo + workflow scopes required).

## Inputs

- `repo`: path inside the repo (default `.`)
- `pr`: PR number or URL (optional; defaults to current branch PR)
- `gh` authentication for the repo host

## Quick Start

```bash
python .claude/skills/gh-fix-ci/scripts/inspect_pr_checks.py --repo "." --pr "<number-or-url>"
# Add --json for machine-friendly output
```

## Workflow

### 1) Verify gh authentication
```bash
gh auth status
```
If unauthenticated, ask user to run `gh auth login`.

### 2) Resolve the PR
```bash
# Current branch PR
gh pr view --json number,url
# Or use user-provided PR number/URL
```

### 3) Inspect failing checks (GitHub Actions only)
**Preferred**: Run bundled script:
```bash
python .claude/skills/gh-fix-ci/scripts/inspect_pr_checks.py --repo "." --pr "<number>"
```

**Manual fallback**:
```bash
# List checks
gh pr checks <pr> --json name,state,conclusion,detailsUrl,startedAt,completedAt

# For each failing check, extract run ID and fetch logs
gh run view <run_id> --json name,workflowName,conclusion,status,url
gh run view <run_id> --log
```

### 4) Scope non-GitHub Actions checks
If `detailsUrl` is not a GitHub Actions run, label as external and only report URL. Do not attempt Buildkite or other providers.

### 5) Summarize failures
- Failing check name
- Run URL
- Concise log snippet (error lines)
- Missing logs noted explicitly

### 6) Create a fix plan
Draft concise plan with:
- Root cause analysis
- Proposed changes
- Files to modify
- Request explicit approval before implementing

### 7) Implement after approval
- Apply the approved plan
- Summarize diffs/tests
- Ask about opening a PR

### 8) Recheck status
```bash
gh pr checks <pr>
```

## Failure Markers

The script searches for these patterns in logs:
`error`, `fail`, `failed`, `traceback`, `exception`, `assert`, `panic`, `fatal`, `timeout`, `segmentation fault`

## Common CI Failures in Our Project

| Failure | Likely Cause | Fix |
|---------|-------------|-----|
| `pytest` failures | Broken test or code regression | Read traceback, fix code/test |
| `ruff check` | Linting violation | Run `ruff check --fix .` |
| `ruff format` | Formatting issue | Run `ruff format .` |
| Import error | Missing dependency | Add to requirements.txt |
| Timeout | Test hanging on I/O | Check for missing mocks |
