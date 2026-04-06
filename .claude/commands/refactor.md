# /refactor — Full Module Refactoring

Analyse and refactor the specified Python module: $ARGUMENTS

## Steps

1. **Analyse complexity**
   - Count functions, classes, lines per function
   - Identify functions > 30 lines or cyclomatic complexity > 10
   - Identify duplicated logic, dead code, unclear naming

2. **Run linting**
   ```bash
   ruff check $ARGUMENTS --output-format text
   ruff format --check $ARGUMENTS
   ```

3. **Plan refactoring**
   Present a numbered list of proposed changes:
   - Extract long functions into smaller helpers
   - Rename unclear variables/functions to descriptive names
   - Remove dead code (unused imports, unreachable branches)
   - Simplify complex conditionals (early returns, guard clauses)
   - Apply DRY (extract shared logic into helpers)
   - Fix any ruff violations found in step 2

4. **Wait for approval**
   Ask: "Proceed with these changes? (y/n/partial — specify numbers)"

5. **Execute refactoring**
   - Apply changes incrementally (one logical change at a time)
   - After each change, run `ruff check $ARGUMENTS` to verify no regressions
   - Run `python -m pytest tests/ -x -q` after each significant change

6. **Final verification**
   ```bash
   ruff format $ARGUMENTS
   ruff check $ARGUMENTS
   python -m pytest tests/ -x -q
   ```
   Summarise: what changed, why, lines before/after.

## Project Conventions
- `snake_case` for functions/variables, `PascalCase` for classes
- Logger per module: `logger = logging.getLogger(__name__)`
- DI pattern: inject `db_path`, `now_fn`, `uuid_fn`, `runner` via params (no globals)
- Respect invariants from CLAUDE.md (no shell=True, no raw stdout in logs, etc.)
- NEVER modify protected files: `storage/db.py`, `supervisor/router.py`, `supervisor/claude_runner.py`, `tests/test_db.py`
