"""Модуль сортировок: bubble_sort, merge_sort, quick_sort.

Каждая функция принимает список (числа или строки),
возвращает новый отсортированный список, не мутируя входной.
Используется только стандартная библиотека.
"""


def bubble_sort(lst: list) -> list:
    """Сортировка пузырьком с ранним выходом."""
    result = list(lst)
    n = len(result)
    for i in range(n):
        swapped = False
        for j in range(0, n - i - 1):
            if result[j] > result[j + 1]:
                result[j], result[j + 1] = result[j + 1], result[j]
                swapped = True
        if not swapped:
            break
    return result


def merge_sort(lst: list) -> list:
    """Рекурсивная сортировка слиянием."""
    result = list(lst)
    if len(result) <= 1:
        return result

    mid = len(result) // 2
    left = merge_sort(result[:mid])
    right = merge_sort(result[mid:])

    return _merge(left, right)


def _merge(left: list, right: list) -> list:
    """Слияние двух отсортированных списков."""
    merged = []
    i = j = 0
    while i < len(left) and j < len(right):
        if left[i] <= right[j]:
            merged.append(left[i])
            i += 1
        else:
            merged.append(right[j])
            j += 1
    merged.extend(left[i:])
    merged.extend(right[j:])
    return merged


def quick_sort(lst: list) -> list:
    """Рекурсивный quicksort с median-of-three pivot."""
    result = list(lst)
    if len(result) <= 1:
        return result

    pivot = _median_of_three(result)
    less = [x for x in result if x < pivot]
    equal = [x for x in result if x == pivot]
    greater = [x for x in result if x > pivot]

    return quick_sort(less) + equal + quick_sort(greater)


def _median_of_three(lst: list):
    """Выбор pivot как медианы первого, среднего и последнего элементов."""
    if len(lst) <= 2:
        return lst[0]
    first = lst[0]
    mid = lst[len(lst) // 2]
    last = lst[-1]
    return sorted([first, mid, last])[1]
