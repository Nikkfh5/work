"""Utility functions."""


def is_even(n: int) -> bool:
    """Check if a number is even.

    Args:
        n: An integer to check.

    Returns:
        True if n is even, False otherwise.

    Raises:
        TypeError: If n is not an integer.
    """
    if not isinstance(n, (int, bool)):
        raise TypeError(f"Expected int, got {type(n).__name__}")
    if isinstance(n, bool):
        raise TypeError("Expected int, got bool")
    return n % 2 == 0
