"""Validated, fail-closed application configuration."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    AliasChoices,
    AnyHttpUrl,
    BeforeValidator,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _parse_csv(value: object) -> tuple[str, ...]:
    """Parse a comma-delimited environment value into unique, ordered names."""
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = [str(item) for item in value]
    else:
        raise ValueError("must be a comma-delimited string or sequence")

    result: list[str] = []
    seen: set[str] = set()
    for raw_value in values:
        item = raw_value.strip()
        if not item:
            continue
        normalized = item.casefold()
        if normalized not in seen:
            seen.add(normalized)
            result.append(item)
    if not result:
        raise ValueError("must contain at least one non-empty value")
    return tuple(result)


ProtectedTags = Annotated[tuple[str, ...], NoDecode, BeforeValidator(_parse_csv)]


class Settings(BaseSettings):
    """Application settings loaded from environment variables or an optional `.env`.

    Safety capabilities are independent and disabled by default. A taxonomy policy
    file may further restrict these settings, but may never enable a capability.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
        populate_by_name=True,
        validate_default=True,
    )

    paperless_url: AnyHttpUrl = Field(
        validation_alias=AliasChoices("PAPERLESS_URL", "paperless_url")
    )
    paperless_api_token: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("PAPERLESS_API_TOKEN", "paperless_api_token"),
        repr=False,
    )
    paperless_api_token_file: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("PAPERLESS_API_TOKEN_FILE", "paperless_api_token_file"),
    )

    write_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("PAPERLESS_MCP_WRITE_ENABLED", "write_enabled"),
    )
    delete_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("PAPERLESS_MCP_DELETE_ENABLED", "delete_enabled"),
    )
    allow_taxonomy_creation: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "PAPERLESS_MCP_ALLOW_TAXONOMY_CREATION", "allow_taxonomy_creation"
        ),
    )
    max_batch_size: int = Field(
        default=25,
        ge=1,
        le=1_000,
        validation_alias=AliasChoices("PAPERLESS_MCP_MAX_BATCH_SIZE", "max_batch_size"),
    )
    max_page_size: int = Field(
        default=100,
        ge=1,
        le=1_000,
        validation_alias=AliasChoices("PAPERLESS_MCP_MAX_PAGE_SIZE", "max_page_size"),
    )
    max_content_characters: int = Field(
        default=12_000,
        ge=256,
        le=1_000_000,
        validation_alias=AliasChoices(
            "PAPERLESS_MCP_MAX_CONTENT_CHARACTERS", "max_content_characters"
        ),
    )
    max_notes: int = Field(
        default=25,
        ge=0,
        le=1_000,
        validation_alias=AliasChoices("PAPERLESS_MCP_MAX_NOTES", "max_notes"),
    )
    max_note_characters: int = Field(
        default=2_000,
        ge=64,
        le=100_000,
        validation_alias=AliasChoices("PAPERLESS_MCP_MAX_NOTE_CHARACTERS", "max_note_characters"),
    )
    audit_dir: Path = Field(
        default=Path("./data/audit"),
        validation_alias=AliasChoices("PAPERLESS_MCP_AUDIT_DIR", "audit_dir"),
    )
    request_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        le=300,
        validation_alias=AliasChoices(
            "PAPERLESS_MCP_REQUEST_TIMEOUT_SECONDS", "request_timeout_seconds"
        ),
    )
    verify_tls: bool = Field(
        default=True,
        validation_alias=AliasChoices("PAPERLESS_MCP_VERIFY_TLS", "verify_tls"),
    )
    log_level: Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"] = Field(
        default="INFO",
        validation_alias=AliasChoices("PAPERLESS_MCP_LOG_LEVEL", "log_level"),
    )
    protected_tags: ProtectedTags = Field(
        default=("Inbox", "Needs Review", "Important", "Retain Original"),
        validation_alias=AliasChoices("PAPERLESS_MCP_PROTECTED_TAGS", "protected_tags"),
    )
    default_review_tag: str = Field(
        default="Needs Review",
        min_length=1,
        validation_alias=AliasChoices("PAPERLESS_MCP_DEFAULT_REVIEW_TAG", "default_review_tag"),
    )
    retry_attempts: int = Field(
        default=3,
        ge=0,
        le=10,
        validation_alias=AliasChoices("PAPERLESS_MCP_RETRY_ATTEMPTS", "retry_attempts"),
    )
    taxonomy_policy_file: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("PAPERLESS_MCP_TAXONOMY_POLICY_FILE", "taxonomy_policy_file"),
    )

    @field_validator("paperless_url")
    @classmethod
    def validate_paperless_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.username is not None or value.password is not None:
            raise ValueError("must not contain embedded credentials")
        if value.query is not None or value.fragment is not None:
            raise ValueError("must not contain a query string or fragment")
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("default_review_tag")
    @classmethod
    def normalize_review_tag(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("audit_dir")
    @classmethod
    def validate_audit_dir(cls, value: Path) -> Path:
        if not str(value).strip():
            raise ValueError("must not be empty")
        return value

    @model_validator(mode="after")
    def load_token_file(self) -> Settings:
        """Load a token file only when the direct token is absent.

        The token is immediately wrapped in ``SecretStr`` so model repr and normal
        serialization cannot reveal it.
        """
        if self.paperless_api_token is not None:
            if not self.paperless_api_token.get_secret_value().strip():
                raise ValueError("PAPERLESS_API_TOKEN must not be blank")
            return self

        token_file = self.paperless_api_token_file
        if token_file is None:
            raise ValueError("PAPERLESS_API_TOKEN or PAPERLESS_API_TOKEN_FILE is required")
        try:
            if not token_file.is_file():
                raise ValueError("PAPERLESS_API_TOKEN_FILE must name a regular file")
            token = token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(f"could not read PAPERLESS_API_TOKEN_FILE: {exc}") from exc
        if not token:
            raise ValueError("PAPERLESS_API_TOKEN_FILE must not be empty")
        self.paperless_api_token = SecretStr(token)
        return self

    @property
    def api_token(self) -> str:
        """Return the token only at the authentication boundary."""
        token = self.paperless_api_token
        if token is None:  # Defensive; validation prevents this.
            raise RuntimeError("API token is unavailable")
        return token.get_secret_value()

    @property
    def base_url(self) -> str:
        """Return a normalized base URL without trailing slashes."""
        return str(self.paperless_url).rstrip("/")

    def safe_summary(self) -> dict[str, Any]:
        """Return configuration suitable for health output and logs."""
        return {
            "paperless_url": self.base_url,
            "paperless_api_token": "[REDACTED]",
            "paperless_api_token_file": (
                str(self.paperless_api_token_file)
                if self.paperless_api_token_file is not None
                else None
            ),
            "write_enabled": self.write_enabled,
            "delete_enabled": self.delete_enabled,
            "allow_taxonomy_creation": self.allow_taxonomy_creation,
            "max_batch_size": self.max_batch_size,
            "max_page_size": self.max_page_size,
            "max_content_characters": self.max_content_characters,
            "max_notes": self.max_notes,
            "max_note_characters": self.max_note_characters,
            "audit_dir": str(self.audit_dir),
            "request_timeout_seconds": self.request_timeout_seconds,
            "verify_tls": self.verify_tls,
            "log_level": self.log_level,
            "protected_tags": list(self.protected_tags),
            "default_review_tag": self.default_review_tag,
            "retry_attempts": self.retry_attempts,
            "taxonomy_policy_file": (
                str(self.taxonomy_policy_file) if self.taxonomy_policy_file is not None else None
            ),
        }


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of HTTP headers with credentials redacted."""
    sensitive = {"authorization", "proxy-authorization", "x-api-key", "cookie", "set-cookie"}
    return {
        key: "[REDACTED]" if key.casefold() in sensitive else value
        for key, value in headers.items()
    }
