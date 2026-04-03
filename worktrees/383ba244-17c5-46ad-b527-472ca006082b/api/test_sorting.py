"""Тесты для модуля sorting: bubble_sort, merge_sort, quick_sort.

15+ тест-кейсов с edge cases: пустой список, один элемент, дубликаты,
уже отсортированный, обратный порядок, строки, отрицательные числа и т.д.
"""

import pytest

from sorting import bubble_sort, merge_sort, quick_sort

SORT_FUNCTIONS = [bubble_sort, merge_sort, quick_sort]


@pytest.fixture(params=SORT_FUNCTIONS, ids=lambda f: f.__name__)
def sort_fn(request):
    return request.param


# --- Edge cases ---


def test_empty_list(sort_fn):
    """Пустой список."""
    assert sort_fn([]) == []


def test_single_element(sort_fn):
    """Один элемент."""
    assert sort_fn([42]) == [42]


def test_two_elements(sort_fn):
    """Два элемента в обратном порядке."""
    assert sort_fn([5, 3]) == [3, 5]


def test_two_elements_sorted(sort_fn):
    """Два элемента уже отсортированы."""
    assert sort_fn([1, 2]) == [1, 2]


def test_already_sorted(sort_fn):
    """Уже отсортированный список."""
    assert sort_fn([1, 2, 3, 4, 5]) == [1, 2, 3, 4, 5]


def test_reverse_order(sort_fn):
    """Обратный порядок."""
    assert sort_fn([5, 4, 3, 2, 1]) == [1, 2, 3, 4, 5]


def test_duplicates(sort_fn):
    """Список с дубликатами."""
    assert sort_fn([3, 1, 3, 2, 1]) == [1, 1, 2, 3, 3]


def test_all_same(sort_fn):
    """Все элементы одинаковые."""
    assert sort_fn([5, 5, 5, 5]) == [5, 5, 5, 5]


def test_negative_numbers(sort_fn):
    """Отрицательные числа."""
    assert sort_fn([-3, -1, -5, 0, 2]) == [-5, -3, -1, 0, 2]


def test_mixed_int_float(sort_fn):
    """Смешанные int и float."""
    assert sort_fn([3.5, 1, 2.5, 0, 4]) == [0, 1, 2.5, 3.5, 4]


def test_strings(sort_fn):
    """Сортировка строк."""
    assert sort_fn(["banana", "apple", "cherry"]) == ["apple", "banana", "cherry"]


def test_strings_with_duplicates(sort_fn):
    """Строки с дубликатами."""
    assert sort_fn(["b", "a", "c", "a"]) == ["a", "a", "b", "c"]


def test_single_char_strings(sort_fn):
    """Одиночные символы."""
    assert sort_fn(["z", "a", "m"]) == ["a", "m", "z"]


def test_large_list(sort_fn):
    """Большой список (100 элементов)."""
    import random

    rng = random.Random(42)
    data = list(range(100))
    rng.shuffle(data)
    assert sort_fn(data) == list(range(100))


def test_does_not_mutate_input(sort_fn):
    """Функция не мутирует входной список."""
    original = [3, 1, 4, 1, 5]
    copy = original.copy()
    sort_fn(original)
    assert original == copy


def test_stability_duplicates(sort_fn):
    """Корректная обработка множественных дубликатов."""
    assert sort_fn([2, 2, 1, 1, 3, 3]) == [1, 1, 2, 2, 3, 3]


def test_all_negative(sort_fn):
    """Все отрицательные числа."""
    assert sort_fn([-1, -5, -3, -2]) == [-5, -3, -2, -1]
