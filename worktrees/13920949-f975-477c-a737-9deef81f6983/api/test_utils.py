"""Tests for utils.add function."""

import pytest

from utils import add


class TestAdd:
    """Tests for the add() function."""

    def test_positive_numbers(self):
        assert add(2, 3) == 5

    def test_negative_numbers(self):
        assert add(-1, -1) == -2

    def test_mixed_signs(self):
        assert add(-1, 3) == 2

    def test_zero(self):
        assert add(0, 0) == 0

    def test_zero_identity(self):
        assert add(5, 0) == 5
        assert add(0, 5) == 5

    def test_large_numbers(self):
        assert add(1_000_000, 2_000_000) == 3_000_000

    def test_return_type(self):
        result = add(1, 2)
        assert isinstance(result, int)
