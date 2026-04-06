# Log Formats — Self-QA Loop Institutional Memory

All log files are **append-only**. Never edit or delete existing entries.
Each entry has a sequential ID and timestamp.

---

## experiments.md — Experiment Log

Each EXP block documents a complete QA session.

```markdown
---

## EXP-001 — 2026-04-01 14:30 UTC

**Strategy:** smoke test basic features (basic, auth, edge_cases)

**Status:** IN PROGRESS

### EXP-001 Results — 2026-04-01 14:45 UTC

- **Passed:** 3/5
- **Failed:** 2/5
- **Cost:** $0.0234
- **Duration:** 847s
- **Bugs found:** BUG-001, BUG-002

**Notes:**
First smoke test. Server crashed on empty POST body (BUG-001).
Auth endpoint returned 200 instead of 401 for invalid password (BUG-002).
Basic CRUD operations work fine.

**Status:** DONE
```

### Key fields:
- **Strategy** — what was the testing approach (focus, batch size, type)
- **Passed/Failed** — quantitative results
- **Bugs found** — links to BUG-XXX in findings.md
- **Notes** — qualitative observations, surprises, patterns
- **Status** — IN PROGRESS or DONE

---

## findings.md — Bug Reports

Each BUG entry documents a discovered issue.
Positive observations are also logged.

```markdown
### BUG-001 [CRITICAL] — 2026-04-01 14:35 UTC

Server crashes with unhandled exception on POST /api/users with empty body.
Stack trace shows NoneType in request.json parsing.

**Details:**
  curl -X POST http://localhost:8000/api/users -d ''
  → 500 Internal Server Error
  → TypeError: 'NoneType' object is not subscriptable
  File "app/views.py", line 42, in create_user
    name = request.json["name"]

### BUG-002 [HIGH] [auth, security] — 2026-04-01 14:37 UTC

POST /api/login with invalid password returns 200 OK instead of 401.
Comparison uses `==` instead of `hmac.compare_digest`.

### [OK] 2026-04-01 14:40 UTC

SQL injection attempt "'; DROP TABLE users; --" correctly rejected.
Parameterized queries in all database operations confirmed.
```

### Severity levels:
- **CRITICAL** — system crash, data loss, security vulnerability
- **HIGH** — major feature broken, wrong results
- **MEDIUM** — feature works but with issues, edge case failures
- **LOW** — cosmetic, minor, non-blocking

### Tags (optional):
`[auth, security]`, `[performance]`, `[data-integrity]`, `[ux]`

---

## proposals.md — Improvement Proposals

Each PROP entry suggests an improvement based on findings.

```markdown
### PROP-001 [CRITICAL] — 2026-04-01 14:42 UTC

**Add input validation middleware**

All endpoints accept any POST body without validation.
BUG-001 (empty body crash) is a symptom of missing validation layer.

Suggestion: Add Pydantic/marshmallow request validation decorator
that returns 422 with details before handler code runs.

Related: BUG-001, BUG-003

### PROP-002 [HIGH] — 2026-04-01 14:43 UTC

**Replace password comparison with constant-time check**

Current `==` comparison is vulnerable to timing attack.
Use `hmac.compare_digest()` or `secrets.compare_digest()`.

Related: BUG-002
```

### Priority levels:
- **CRITICAL** — security fix, crash prevention
- **HIGH** — major improvement, reliability
- **MEDIUM** — nice to have, code quality
- **LOW** — cosmetic, optimization

---

## fixes.md — Fix Reports

Each FIX entry documents what was changed and how to verify.
Written by the developer (main project session), not by QA boss.

```markdown
---

## FIX-001 — 2026-04-02 10:00 UTC

**Bugs closed:** BUG-001, BUG-003

**What changed:**
Added Pydantic request validation to all POST/PUT endpoints.
Empty body now returns 422 with validation error details.
Added `RequestValidator` middleware class.

**Files changed:**
- `app/middleware.py` (new)
- `app/views.py` (added @validate decorator)
- `tests/test_validation.py` (new, 12 tests)

**Re-test procedure:**
1. POST /api/users with empty body → expect 422
2. POST /api/users with missing "name" → expect 422 with field error
3. POST /api/users with valid JSON → expect 201 (regression check)

**Verified:** NOT YET

**Risks:**
Middleware might reject valid requests with unexpected content-type.
Monitor 4xx rates after deploy.
```

### After verification:
```markdown
**Verified:** YES (EXP-003 — all 3 re-test cases passed)
```

or:

```markdown
**Verified:** NO (EXP-003 — test 2 still returns 500, see BUG-008)
```

---

## Cross-referencing

The four files form a connected graph:

```
EXP-001 finds BUG-001, BUG-002
    ↓
PROP-001 suggests fix for BUG-001
    ↓
FIX-001 closes BUG-001 (with re-test procedure)
    ↓
EXP-003 re-tests → Verified: YES
```

Always use exact IDs (BUG-001, PROP-001, FIX-001, EXP-001) for traceability.

---

## qa_core.py API for log management

```python
from qa_core import QALoop

qa = QALoop(Path("debug/"))

# Experiments
exp_id = qa.start_experiment("smoke test basic features")
qa.finish_experiment(exp_id, {
    "passed": 3, "failed": 1, "total": 4,
    "cost_usd": 0.0234, "duration_sec": 847
}, bugs_found=["BUG-001"], notes="First smoke test")

# Findings
bug_id = qa.log_finding("CRITICAL", "Server crashes on empty POST", 
                        details="Stack trace...", tags=["validation"])
qa.log_positive("SQL injection correctly rejected")

# Proposals
prop_id = qa.log_proposal("CRITICAL", "Add input validation middleware",
                          description="All endpoints accept any body...",
                          related_bugs=["BUG-001"])

# Fixes
fix_id = qa.log_fix(
    bugs_closed=["BUG-001", "BUG-003"],
    description="Added Pydantic validation middleware",
    files_changed=["app/middleware.py", "app/views.py"],
    retest_procedure="1. POST empty body → 422\n2. POST valid → 201",
    risks="Might reject unexpected content-types"
)

# Queries
open_bugs = qa.get_open_bugs()          # BUG IDs not in fixes.md
unverified = qa.get_unverified_fixes()  # FIX IDs with "Verified: NOT YET"
actionable = qa.get_actionable_for_main_project()  # Markdown summary
```
