"""
tests/test_log_utils.py — тесты для supervisor/log_utils.py

Запуск: pytest tests/test_log_utils.py -v
"""

from supervisor.log_utils import redact


def test_redact_github_token():
    text = "pushing to https://github.com/org/repo with token ghp_AbCdEfGhIjKlMnOpQrStUvWx123456"
    result = redact(text)
    assert "[GITHUB_TOKEN]" in result
    assert "ghp_" not in result


def test_redact_gitlab_token():
    text = "clone url uses glpat-AbCdEfGhIjKlMnOpQrStUvWxYzAb"
    result = redact(text)
    assert "[GITLAB_TOKEN]" in result
    assert "glpat-" not in result


def test_redact_notion_token():
    text = "notion auth: secret_AbCdEfGhIjKlMnOpQrStUvWxYzAbCdEfGhIjKlMnOpQrSt"
    result = redact(text)
    assert "[NOTION_TOKEN]" in result
    assert "secret_" not in result


def test_redact_telegram_token():
    text = "bot token: AAFAbCdEfGhIjKlMnOpQrStUvWxYzAbCdEfGhIjKl"
    result = redact(text)
    assert "[TG_TOKEN]" in result
    assert "AAF" not in result


def test_redact_https_url_with_credentials():
    text = "git push https://oauth2:ghp_sometoken@gitlab.com/org/repo.git"
    result = redact(text)
    assert "[REDACTED]" in result
    assert "ghp_sometoken" not in result


def test_redact_no_secrets_unchanged():
    text = "Running pytest in /app/worktrees/task-123/api — all tests passed"
    result = redact(text)
    assert result == text


def test_redact_empty_string():
    assert redact("") == ""


def test_redact_multiple_secrets_in_one_string():
    text = (
        "github: ghp_AbCdEfGhIjKlMnOpQrStUvWx123456 "
        "gitlab: glpat-AbCdEfGhIjKlMnOpQrStUvWxYzAb"
    )
    result = redact(text)
    assert "[GITHUB_TOKEN]" in result
    assert "[GITLAB_TOKEN]" in result
    assert "ghp_" not in result
    assert "glpat-" not in result


def test_redact_anthropic_key():
    text = "key=sk-ant-apiAbCdEfGhIjKlMnOpQrStUvWxYzAbCdEfGhIj"
    result = redact(text)
    assert "[ANTHROPIC_KEY]" in result
    assert "sk-ant-api" not in result


def test_redact_preserves_non_secret_url():
    text = "fetching docs from https://docs.python.org/3/library/asyncio.html"
    result = redact(text)
    assert result == text


def test_redact_github_other_token_types():
    """gho_ (OAuth), ghu_ (user-to-server), ghs_ (server-to-server)."""
    for prefix in ["gho_", "ghu_", "ghs_"]:
        text = f"token: {prefix}AbCdEfGhIjKlMnOpQrSt1234"
        result = redact(text)
        assert "[GITHUB_TOKEN]" in result
        assert prefix not in result


def test_redact_telegram_bot_token_full_format():
    """Full Telegram bot token: bot_id:AAtoken format."""
    text = "token is 8783330622:AAGf8ENH8Fxyz123abcdefghijklmno"
    result = redact(text)
    assert "8783330622" not in result
    assert "AAGf8ENH8F" not in result
    assert "[TG_BOT_TOKEN]" in result


def test_redact_git_url_with_embedded_token():
    """Git stderr contains https://token@host URL -> redacted."""
    text = "fatal: unable to access 'https://ghp_AbCdEfGhIjKlMnOpQrStUvWx12@github.com/org/repo.git/'"
    result = redact(text)
    assert "ghp_" not in result
    assert "[GITHUB_TOKEN]" in result or "[REDACTED]" in result
