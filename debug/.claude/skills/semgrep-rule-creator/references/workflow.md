# Semgrep Rule Creation Workflow

## Step 1: Analyze the Problem

1. Understand the exact bug pattern to detect
2. Identify the target language
3. Determine approach: **Pattern matching** (syntactic) or **Taint mode** (data flow)

### When to Use Taint Mode
- Track data flow across multiple variables
- Find injection vulnerabilities (SQL, command, XSS)
- Write rules resilient to nesting in if/loops/try

## Step 2: Write Tests First

Create directory and test file:
```
<rule-id>/
  <rule-id>.yaml
  <rule-id>.py
```

Include test cases for:
- Clear vulnerable cases (must match)
- Clear safe cases (must not match)
- Edge cases and variations
- Different coding styles
- Sanitized/validated input (must not match)
- Unrelated code (must not match)
- Nested structures (if/loops/try/callbacks)

**CRITICAL**: `# ruleid:` must be on the line IMMEDIATELY BEFORE the finding.

## Step 3: Analyze AST

```bash
semgrep --dump-ast --lang python <rule-id>.py
```

Understanding AST structure prevents patterns that miss syntactic variations.

## Step 4: Write the Rule

Write YAML, then validate and test:
```bash
semgrep --validate --config <rule-id>.yaml
semgrep --test --config <rule-id>.yaml <rule-id>.py
```

Expected: `1/1: All tests passed`

## Step 5: Iterate Until Tests Pass

Each change -> re-run tests:
```bash
semgrep --test --config <rule-id>.yaml <rule-id>.py
```

For taint debugging:
```bash
semgrep --dataflow-traces --config <rule-id>.yaml <rule-id>.py
```

| Problem | Solution |
|---------|----------|
| Too many matches | Add `pattern-not` exclusions |
| Missing matches | Add `pattern-either` variants |
| Wrong line matched | Adjust `focus-metavariable` |
| Taint not flowing | Check sanitizers aren't too broad |

## Step 6: Optimize

After all tests pass:
1. Remove patterns differing only in quote style
2. Remove patterns that are subsets of `...` patterns
3. Consolidate similar patterns using metavariable-regex
4. **Re-run tests after each optimization**

## Step 7: Final Run

```bash
semgrep --config <rule-id>.yaml <rule-id>.py
```

Ensure message has no uninterpolated metavariables.
