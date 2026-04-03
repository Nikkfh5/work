# Input Strategy Reference (Python/Hypothesis)

## Basic Strategies

| Type | Strategy |
|------|----------|
| `int` | `st.integers()` |
| `float` | `st.floats(allow_nan=False)` |
| `str` | `st.text()` |
| `bytes` | `st.binary()` |
| `bool` | `st.booleans()` |
| `list[T]` | `st.lists(strategy_for_T)` |
| `dict[K, V]` | `st.dictionaries(key_strategy, value_strategy)` |
| `set[T]` | `st.frozensets(strategy_for_T)` |
| `tuple[T, ...]` | `st.tuples(strategy_for_T, ...)` |
| `Optional[T]` | `st.none() \| strategy_for_T` |
| `Union[A, B]` | `st.one_of(strategy_a, strategy_b)` |
| Custom class | `st.builds(ClassName, field1=..., field2=...)` |
| Enum | `st.sampled_from(EnumClass)` |
| Constrained int | `st.integers(min_value=0, max_value=100)` |
| Email | `st.emails()` |
| UUID | `st.uuids()` |
| DateTime | `st.datetimes()` |
| Regex match | `st.from_regex(r"pattern")` |

## Composite Strategies

For complex types, use `@st.composite`:

```python
@st.composite
def valid_users(draw):
    name = draw(st.text(min_size=1, max_size=50))
    age = draw(st.integers(min_value=0, max_value=150))
    email = draw(st.emails())
    return User(name=name, age=age, email=email)
```

## Best Practices

1. **Constrain early**: Build constraints into strategy, not `assume()`
   ```python
   # GOOD
   st.integers(min_value=1, max_value=100)

   # BAD
   st.integers().filter(lambda x: 1 <= x <= 100)
   ```

2. **Size limits**: Use `max_size` to prevent slow tests
   ```python
   st.lists(st.integers(), max_size=100)
   st.text(max_size=1000)
   ```

3. **Realistic data**: Make strategies match real-world constraints
   ```python
   # Real user ages, not arbitrary integers
   st.integers(min_value=0, max_value=150)
   ```

4. **Reuse strategies**: Define once, use across tests
   ```python
   valid_users = st.builds(User, ...)

   @given(valid_users)
   def test_one(user): ...

   @given(valid_users)
   def test_two(user): ...
   ```

## Project-Specific Strategies

Useful strategies for this AI orchestration project:

```python
# Task IDs
task_ids = st.uuids().map(str)

# Worker IDs
worker_ids = st.sampled_from(["job1_worker", "job1_reviewer", "health_monitor"])

# Task states
task_states = st.sampled_from(["pending", "assigned", "running", "done", "failed", "review"])

# JSON-like stdout from Claude
@st.composite
def claude_json_output(draw):
    """Generate JSON that looks like Claude CLI stdout with markers."""
    payload = draw(st.dictionaries(
        st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=('L',))),
        st.one_of(st.text(max_size=100), st.integers(), st.booleans()),
        max_size=10,
    ))
    noise_before = draw(st.text(max_size=200))
    noise_after = draw(st.text(max_size=200))
    import json
    return f"{noise_before}<<<JSON>>>{json.dumps(payload)}<<<END>>>{noise_after}"

# File paths for safe_exec
@st.composite
def safe_file_paths(draw, root="/tmp/test"):
    """Generate paths that stay within allowed root."""
    segments = draw(st.lists(
        st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=('L', 'N'))),
        min_size=1, max_size=5,
    ))
    return root + "/" + "/".join(segments)

# agents.yaml config fragments
@st.composite
def agent_configs(draw):
    name = draw(st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=('L',))))
    return {
        name: {
            "model": draw(st.sampled_from(["claude-opus-4-6", "claude-sonnet-4-6"])),
            "active": draw(st.booleans()),
            "max_attempts": draw(st.integers(min_value=1, max_value=10)),
        }
    }
```
