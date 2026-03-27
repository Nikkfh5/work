"""Тесты для csv_parser.parse_csv — TDD: сначала тесты, потом реализация."""

import os
import tempfile
import pytest

from csv_parser import parse_csv


# ─── helpers ───────────────────────────────────────────────────────


def _write_tmp(content: str, suffix: str = ".csv", encoding: str = "utf-8") -> str:
    """Записывает content во временный файл и возвращает путь."""
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
        f.write(content)
    return path


def _write_tmp_bytes(data: bytes, suffix: str = ".csv") -> str:
    """Записывает raw bytes во временный файл."""
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


# ─── 1. Пустой файл ──────────────────────────────────────────────


class TestEmptyFile:
    def test_empty_file_returns_empty_list(self):
        path = _write_tmp("")
        try:
            result = parse_csv(path)
            assert result == []
        finally:
            os.unlink(path)

    def test_whitespace_only_file_returns_empty_list(self):
        path = _write_tmp("   \n  \n\n")
        try:
            result = parse_csv(path)
            assert result == []
        finally:
            os.unlink(path)


# ─── 2. Файл только с заголовком ─────────────────────────────────


class TestHeaderOnly:
    def test_header_only_returns_empty_list(self):
        path = _write_tmp("name,age,city\n")
        try:
            result = parse_csv(path)
            assert result == []
        finally:
            os.unlink(path)

    def test_header_only_no_trailing_newline(self):
        path = _write_tmp("name,age,city")
        try:
            result = parse_csv(path)
            assert result == []
        finally:
            os.unlink(path)


# ─── 3. Кавычки внутри полей (экранирование) ─────────────────────


class TestQuotedFields:
    def test_field_with_comma_inside_quotes(self):
        csv_text = 'name,description\nAlice,"likes cats, dogs"\n'
        path = _write_tmp(csv_text)
        try:
            result = parse_csv(path)
            assert len(result) == 1
            assert result[0]["description"] == "likes cats, dogs"
        finally:
            os.unlink(path)

    def test_field_with_escaped_double_quote(self):
        csv_text = 'name,quote\nBob,"He said ""hello"""\n'
        path = _write_tmp(csv_text)
        try:
            result = parse_csv(path)
            assert len(result) == 1
            assert result[0]["quote"] == 'He said "hello"'
        finally:
            os.unlink(path)

    def test_field_with_newline_inside_quotes(self):
        csv_text = 'name,bio\nCarol,"line1\nline2"\n'
        path = _write_tmp(csv_text)
        try:
            result = parse_csv(path)
            assert len(result) == 1
            assert result[0]["bio"] == "line1\nline2"
        finally:
            os.unlink(path)

    def test_fully_quoted_fields(self):
        csv_text = '"name","age"\n"Alice","30"\n'
        path = _write_tmp(csv_text)
        try:
            result = parse_csv(path)
            assert len(result) == 1
            assert result[0] == {"name": "Alice", "age": "30"}
        finally:
            os.unlink(path)


# ─── 4. Пустые строки между данными ──────────────────────────────


class TestBlankLines:
    def test_blank_lines_between_rows_are_skipped(self):
        csv_text = "name,age\n\nAlice,30\n\n\nBob,25\n\n"
        path = _write_tmp(csv_text)
        try:
            result = parse_csv(path)
            assert len(result) == 2
            assert result[0]["name"] == "Alice"
            assert result[1]["name"] == "Bob"
        finally:
            os.unlink(path)

    def test_blank_lines_at_start(self):
        csv_text = "\n\nname,age\nAlice,30\n"
        path = _write_tmp(csv_text)
        try:
            # Первая непустая строка — заголовок
            result = parse_csv(path)
            assert len(result) == 1
            assert result[0] == {"name": "Alice", "age": "30"}
        finally:
            os.unlink(path)


# ─── 5. Unicode символы ──────────────────────────────────────────


class TestUnicode:
    def test_cyrillic_content(self):
        csv_text = "имя,город\nАлиса,Москва\nБорис,Санкт-Петербург\n"
        path = _write_tmp(csv_text)
        try:
            result = parse_csv(path)
            assert len(result) == 2
            assert result[0]["имя"] == "Алиса"
            assert result[1]["город"] == "Санкт-Петербург"
        finally:
            os.unlink(path)

    def test_emoji_in_fields(self):
        csv_text = "name,mood\nAlice,😊\nBob,🎉🔥\n"
        path = _write_tmp(csv_text)
        try:
            result = parse_csv(path)
            assert result[0]["mood"] == "😊"
            assert result[1]["mood"] == "🎉🔥"
        finally:
            os.unlink(path)

    def test_chinese_characters(self):
        csv_text = "名前,都市\n太郎,東京\n"
        path = _write_tmp(csv_text)
        try:
            result = parse_csv(path)
            assert result[0]["名前"] == "太郎"
        finally:
            os.unlink(path)


# ─── 6. BOM (Byte Order Mark) ────────────────────────────────────


class TestBOM:
    def test_utf8_bom_does_not_corrupt_first_header(self):
        bom = b"\xef\xbb\xbf"
        csv_bytes = bom + "name,age\nAlice,30\n".encode("utf-8")
        path = _write_tmp_bytes(csv_bytes)
        try:
            result = parse_csv(path)
            assert len(result) == 1
            # Ключ должен быть "name", а не "\ufeffname"
            assert "name" in result[0], f"Keys: {list(result[0].keys())}"
            assert result[0]["name"] == "Alice"
            assert result[0]["age"] == "30"
        finally:
            os.unlink(path)


# ─── 7. Разные line endings ──────────────────────────────────────


class TestLineEndings:
    def test_unix_lf(self):
        csv_bytes = b"name,age\nAlice,30\nBob,25\n"
        path = _write_tmp_bytes(csv_bytes)
        try:
            result = parse_csv(path)
            assert len(result) == 2
        finally:
            os.unlink(path)

    def test_windows_crlf(self):
        csv_bytes = b"name,age\r\nAlice,30\r\nBob,25\r\n"
        path = _write_tmp_bytes(csv_bytes)
        try:
            result = parse_csv(path)
            assert len(result) == 2
            assert result[0]["name"] == "Alice"
            assert result[1]["age"] == "25"
        finally:
            os.unlink(path)

    def test_old_mac_cr(self):
        csv_bytes = b"name,age\rAlice,30\rBob,25\r"
        path = _write_tmp_bytes(csv_bytes)
        try:
            result = parse_csv(path)
            assert len(result) == 2
            assert result[0]["name"] == "Alice"
        finally:
            os.unlink(path)

    def test_mixed_line_endings(self):
        csv_bytes = b"name,age\nAlice,30\r\nBob,25\rCarol,28\n"
        path = _write_tmp_bytes(csv_bytes)
        try:
            result = parse_csv(path)
            assert len(result) == 3
        finally:
            os.unlink(path)


# ─── 8. Кастомный delimiter ──────────────────────────────────────


class TestCustomDelimiter:
    def test_semicolon_delimiter(self):
        csv_text = "name;age;city\nAlice;30;Berlin\n"
        path = _write_tmp(csv_text)
        try:
            result = parse_csv(path, delimiter=";")
            assert len(result) == 1
            assert result[0] == {"name": "Alice", "age": "30", "city": "Berlin"}
        finally:
            os.unlink(path)

    def test_tab_delimiter(self):
        csv_text = "name\tage\nAlice\t30\nBob\t25\n"
        path = _write_tmp(csv_text)
        try:
            result = parse_csv(path, delimiter="\t")
            assert len(result) == 2
            assert result[0]["name"] == "Alice"
        finally:
            os.unlink(path)

    def test_pipe_delimiter(self):
        csv_text = "name|age\nAlice|30\n"
        path = _write_tmp(csv_text)
        try:
            result = parse_csv(path, delimiter="|")
            assert result[0] == {"name": "Alice", "age": "30"}
        finally:
            os.unlink(path)


# ─── 9. Edge cases ───────────────────────────────────────────────


class TestEdgeCases:
    def test_trailing_delimiter_creates_empty_field(self):
        csv_text = "a,b,c\n1,2,\n"
        path = _write_tmp(csv_text)
        try:
            result = parse_csv(path)
            assert result[0]["c"] == ""
        finally:
            os.unlink(path)

    def test_single_column(self):
        csv_text = "name\nAlice\nBob\n"
        path = _write_tmp(csv_text)
        try:
            result = parse_csv(path)
            assert len(result) == 2
            assert result[0] == {"name": "Alice"}
        finally:
            os.unlink(path)

    def test_spaces_in_values_preserved(self):
        csv_text = "name,value\nAlice, hello world \n"
        path = _write_tmp(csv_text)
        try:
            result = parse_csv(path)
            assert result[0]["value"] == " hello world "
        finally:
            os.unlink(path)

    def test_nonexistent_file_raises(self):
        with pytest.raises(FileNotFoundError):
            parse_csv("/nonexistent/path/file.csv")

    def test_large_row_count(self):
        lines = ["id,value"] + [f"{i},val_{i}" for i in range(1000)]
        csv_text = "\n".join(lines) + "\n"
        path = _write_tmp(csv_text)
        try:
            result = parse_csv(path)
            assert len(result) == 1000
            assert result[999]["id"] == "999"
        finally:
            os.unlink(path)
