from pathlib import Path

import pytest
from pydantic import ValidationError

from paperless_mcp.config import Settings, redact_headers


def test_required_configuration_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PAPERLESS_URL", raising=False)
    monkeypatch.delenv("PAPERLESS_API_TOKEN", raising=False)
    monkeypatch.delenv("PAPERLESS_API_TOKEN_FILE", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_defaults_are_read_only_and_redacted() -> None:
    settings = Settings(
        PAPERLESS_URL="https://paperless.example.test",
        PAPERLESS_API_TOKEN="super-secret-token",
    )

    assert settings.write_enabled is False
    assert settings.delete_enabled is False
    assert settings.allow_taxonomy_creation is False
    assert settings.verify_tls is True
    assert settings.api_token == "super-secret-token"
    assert settings.base_url == "https://paperless.example.test"

    summary = settings.safe_summary()
    assert summary["paperless_api_token"] == "[REDACTED]"
    assert "super-secret-token" not in repr(settings)
    assert "super-secret-token" not in repr(summary)


def test_token_can_be_loaded_from_file(tmp_path: Path) -> None:
    token_file = tmp_path / "paperless-token"
    token_file.write_text("from-file\n", encoding="utf-8")

    settings = Settings(
        PAPERLESS_URL="https://paperless.example.test/paperless/",
        PAPERLESS_API_TOKEN_FILE=token_file,
    )

    assert settings.api_token == "from-file"
    assert settings.base_url == "https://paperless.example.test/paperless"


def test_direct_token_takes_precedence_over_token_file(tmp_path: Path) -> None:
    token_file = tmp_path / "paperless-token"
    token_file.write_text("from-file\n", encoding="utf-8")

    settings = Settings(
        PAPERLESS_URL="https://paperless.example.test",
        PAPERLESS_API_TOKEN="from-environment",
        PAPERLESS_API_TOKEN_FILE=token_file,
    )

    assert settings.api_token == "from-environment"


def test_missing_token_file_fails_without_leaking_a_token(tmp_path: Path) -> None:
    missing_token_file = tmp_path / "missing-token"

    with pytest.raises(ValidationError) as error:
        Settings(
            PAPERLESS_URL="https://paperless.example.test",
            PAPERLESS_API_TOKEN_FILE=missing_token_file,
        )

    assert "regular file" in str(error.value)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PAPERLESS_URL", "ftp://paperless.example.test"),
        ("PAPERLESS_URL", "https://user:password@paperless.example.test"),
        ("PAPERLESS_URL", "https://paperless.example.test?token=bad"),
        ("PAPERLESS_MCP_MAX_BATCH_SIZE", "0"),
        ("PAPERLESS_MCP_REQUEST_TIMEOUT_SECONDS", "-1"),
    ],
)
def test_invalid_settings_fail_validation(name: str, value: str) -> None:
    values = {
        "PAPERLESS_URL": "https://paperless.example.test",
        "PAPERLESS_API_TOKEN": "secret",
        name: value,
    }

    with pytest.raises(ValidationError):
        Settings(**values)  # type: ignore[arg-type]


def test_protected_tags_parse_csv_case_insensitively() -> None:
    settings = Settings(
        PAPERLESS_URL="https://paperless.example.test",
        PAPERLESS_API_TOKEN="secret",
        PAPERLESS_MCP_PROTECTED_TAGS=" Inbox,Needs Review,inbox, Important ",
    )

    assert settings.protected_tags == ("Inbox", "Needs Review", "Important")


def test_authorization_headers_are_redacted() -> None:
    result = redact_headers(
        {
            "Authorization": "Token secret",
            "X-Request-ID": "request-1",
            "Cookie": "session=secret",
        }
    )

    assert result == {
        "Authorization": "[REDACTED]",
        "X-Request-ID": "request-1",
        "Cookie": "[REDACTED]",
    }
