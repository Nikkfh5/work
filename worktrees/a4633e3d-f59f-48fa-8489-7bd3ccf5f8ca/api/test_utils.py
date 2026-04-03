"""Tests for utils.is_even."""

import pytest

from utils import is_even


class TestIsEven:
    """Tests for the is_even function."""

    def test_even_positive(self):
        assert is_even(2) is True
        assert is_even(4) is True
        assert is_even(100) is True

    def test_odd_positive(self):
        assert is_even(1) is False
        assert is_even(3) is False
        assert is_even(99) is False

    def test_zero(self):
        assert is_even(0) is True

    def test_negative_even(self):
        assert is_even(-2) is True
        assert is_even(-4) is True

    def test_negative_odd(self):
        assert is_even(-1) is False
        assert is_even(-3) is False

    def test_large_numbers(self):
        assert is_even(10**18) is True
        assert is_even(10**18 + 1) is False

    def test_type_error_on_float(self):
        with pytest.raises(TypeError):
            is_even(2.0)

    def test_type_error_on_string(self):
        with pytest.raises(TypeError):
            is_even("2")

    def test_type_error_on_bool(self):
        with pytest.raises(TypeError):
            is_even(True)

    def test_type_error_on_none(self):
        with pytest.raises(TypeError):
            is_even(None)
