# Property-Based Test Generation

## Test Structure (Python/Hypothesis)

```python
from hypothesis import given, example, settings, strategies as st

@given(st.text())
def test_my_property(input_val):
    """Describe what property this tests."""
    result = function_under_test(input_val)
    assert property_holds(result)
```

## Common Patterns for This Project

### 1. JSON Parsing Roundtrip (json_guard)

```python
from hypothesis import given, strategies as st
import json

@st.composite
def valid_worker_json(draw):
    """Generate valid worker output JSON."""
    return {
        "status": draw(st.sampled_from(["done", "error", "blocked"])),
        "summary": draw(st.text(min_size=1, max_size=500)),
        "files_changed": draw(st.lists(st.text(min_size=1, max_size=100), max_size=10)),
    }

@given(valid_worker_json())
def test_extract_json_roundtrip(payload):
    """extract_json should parse any valid JSON wrapped in markers."""
    raw = f"noise<<<JSON>>>{json.dumps(payload)}<<<END>>>noise"
    result = extract_json(raw)
    assert result == payload

@given(st.text())
def test_extract_json_never_crashes(raw):
    """extract_json should never raise on arbitrary input."""
    result = extract_json(raw)
    assert result is None or isinstance(result, dict)
```

### 2. Path Traversal Safety (safe_exec)

```python
@st.composite
def traversal_paths(draw):
    """Generate paths that attempt directory traversal."""
    base = draw(st.sampled_from(["/tmp/test", "/app/data"]))
    traversal = draw(st.lists(
        st.sampled_from(["..", ".", "~", "symlink"]),
        min_size=0, max_size=5,
    ))
    normal = draw(st.lists(
        st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=('L','N'))),
        min_size=1, max_size=3,
    ))
    segments = []
    for t, n in zip(traversal, normal):
        segments.extend([t, n])
    segments.extend(normal[len(traversal):])
    return base + "/" + "/".join(segments)

@given(traversal_paths())
def test_realpath_stays_in_allowed_root(path):
    """safe_exec path validation should never allow escape from allowed roots."""
    allowed = ["/tmp/test", "/app/data"]
    # resolved path should either be within allowed roots or rejected
    result = validate_path(path, allowed)
    if result is not None:
        assert any(result.startswith(root) for root in allowed)
```

### 3. Lease Manager Idempotence

```python
@given(st.uuids().map(str), st.uuids().map(str))
def test_lease_claim_idempotent(task_id, worker_id):
    """Claiming same task twice by same worker should be idempotent."""
    # first claim succeeds
    token1 = claim_task(task_id, worker_id)
    # second claim by same worker should return same token or succeed
    token2 = claim_task(task_id, worker_id)
    assert token1 == token2 or token2 is not None

@given(st.uuids().map(str), st.uuids().map(str), st.uuids().map(str))
def test_lease_mutual_exclusion(task_id, worker_a, worker_b):
    """Two different workers cannot hold lease on same task."""
    assume(worker_a != worker_b)
    token_a = claim_task(task_id, worker_a)
    token_b = claim_task(task_id, worker_b)
    assert not (token_a is not None and token_b is not None)
```

### 4. Redact Invariant (log_utils)

```python
@given(st.text(max_size=1000))
def test_redact_idempotent(text):
    """Redacting twice should be same as once."""
    assert redact(redact(text)) == redact(text)

@given(st.text(max_size=1000))
def test_redact_no_secrets_leak(text):
    """After redaction, no known secret patterns should remain."""
    result = redact(text)
    for pattern in SECRET_PATTERNS:
        assert not re.search(pattern, result)
```

## Tips

- Use `@example(...)` to pin known edge cases alongside property tests
- Use `@settings(max_examples=200)` for critical properties
- Use `@settings(deadline=None)` for tests that involve I/O
- Use `assume()` sparingly — prefer constrained strategies
- Keep properties independent — each tests one thing
