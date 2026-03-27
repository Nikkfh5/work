"""CSV-парсер с поддержкой кавычек, пустых строк, BOM и разных разделителей.

Использует только stdlib csv модуль. Не зависит от pandas.
"""

import csv
import io


def parse_csv(filepath: str, delimiter: str = ",") -> list[dict]:
    """Парсит CSV файл и возвращает список словарей.

    Args:
        filepath: путь к CSV файлу.
        delimiter: разделитель полей (по умолчанию запятая).

    Returns:
        Список словарей, где ключи — заголовки из первой непустой строки.
        Пустые строки между данными пропускаются. BOM удаляется автоматически.

    Raises:
        FileNotFoundError: если файл не существует.
    """
    # encoding="utf-8-sig" автоматически убирает BOM
    # newline="" — csv модуль сам управляет переводами строк
    with open(filepath, "r", encoding="utf-8-sig", newline="") as f:
        raw = f.read()

    # Нормализуем line endings: \r\n → \n, затем оставшиеся \r → \n
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")

    # Пропускаем ведущие пустые/пробельные строки (до заголовка).
    # НЕ удаляем пустые строки из середины — это делает DictReader сам,
    # а удаление вручную сломало бы multiline quoted fields.
    lines = raw.split("\n")
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1

    if start >= len(lines):
        return []

    content = "\n".join(lines[start:])
    if not content.strip():
        return []

    reader = csv.DictReader(
        io.StringIO(content),
        delimiter=delimiter,
        quotechar='"',
        doublequote=True,
    )

    return [dict(row) for row in reader]
