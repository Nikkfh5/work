# Property-Driven Development

Design features by defining properties upfront as executable specifications, before implementation.

## When to Use

- Designing a new feature from scratch
- Building something with clear algebraic properties (serialization, validation, transformations)
- Complex domain where edge cases are likely
- User wants to think through requirements rigorously before coding

## Process

### Phase 1: Understand the Feature

Gather information:
- **Purpose**: What problem does this solve?
- **Inputs**: What data does it accept? What makes inputs valid?
- **Outputs**: What does it produce? What guarantees?
- **Constraints**: What must always be true?
- **Edge cases**: Boundary conditions?
- **Relationships**: Inverse operations? Compositions?

### Phase 2: Identify Candidate Properties

| Question | Property Type | Example |
|----------|---------------|---------|
| Does it have an inverse operation? | Roundtrip | `decode(encode(x)) == x` |
| Is applying it twice the same as once? | Idempotence | `f(f(x)) == f(x)` |
| What quantities are preserved? | Invariants | Length, sum, count |
| Is order of arguments irrelevant? | Commutativity | `f(a, b) == f(b, a)` |
| Can operations be regrouped? | Associativity | `f(f(a,b), c) == f(a, f(b,c))` |
| Is there a neutral element? | Identity | `f(x, 0) == x` |
| Is there an oracle/reference impl? | Oracle | `new(x) == old(x)` |
| Can output be easily verified? | Hard/Easy | `is_sorted(sort(x))` |

### Phase 3: Define Input Domain

Specify valid inputs as strategies. The strategy IS the specification.

**Key principle**: Build constraints INTO the strategy, not via `assume()`.

```python
@st.composite
def valid_task_descriptions(draw):
    """Generate valid task descriptions - this documents the domain."""
    text = draw(st.text(min_size=5, max_size=2000))
    task_type = draw(st.sampled_from(["code", "review", "knowledge_ingest"]))
    priority = draw(st.integers(min_value=1, max_value=5))
    return {"description": text, "type": task_type, "priority": priority}
```

### Phase 4: Write Property Tests (Before Implementation)

```python
class TestFeatureSpec:
    """Property-based specification - should FAIL until implemented."""

    @given(valid_inputs())
    def test_core_property(self, x):
        """[What this guarantees]."""
        result = feature(x)
        assert property_holds(result)
```

### Phase 5: Iterate on Design

Properties reveal design questions:
- "What about edge cases in JSON parsing?"
- "Is task state machine deterministic?"
- "What if two workers claim same task?"

Surface these questions early, before implementation.

## Property Strength Hierarchy

Build properties incrementally from weak to strong:

### Level 1: Basic (Weak)
```python
@given(valid_inputs())
def test_no_crash(x):
    process(x)  # Just don't crash
```

### Level 2: Type Preservation
```python
@given(valid_inputs())
def test_returns_type(x):
    assert isinstance(process(x), ExpectedType)
```

### Level 3: Invariants
```python
@given(valid_inputs())
def test_invariant(x):
    result = process(x)
    assert invariant_holds(result)
```

### Level 4: Full Specification (Strong)
```python
@given(valid_inputs())
def test_complete(x):
    result = process(x)
    assert satisfies_all_requirements(result)
```

## Red Flags

- **Writing tautological properties**: Don't reimplement the function logic in the test
- **Starting too strong**: Build from weak to strong properties
- **Ignoring design questions**: Properties that feel awkward often reveal design gaps
- **Overly complex strategies**: If your input strategy is 50 lines, the domain model might need simplification

## Checklist

- [ ] Properties are not tautological
- [ ] At least one strong property defined
- [ ] Input strategy documents valid inputs
- [ ] Design questions have been surfaced
- [ ] Tests will actually FAIL without implementation
