"""Environment configuration; never place API keys in source control."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigurationError(ValueError):
    """Raised when required runtime configuration is missing or invalid."""


def _integer(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be a boolean (true/false)")


@dataclass(frozen=True, slots=True)
class Settings:
    discord_token: str
    openai_api_key: str
    openrouter_api_key: str
    openrouter_model: str
    database_url: str
    discord_guild_id: int | None
    max_concurrent_moderation: int
    http_timeout_seconds: int
    max_image_bytes: int
    image_scan_enabled_by_default: bool
    allow_openrouter_text: bool
    log_level: str

    @classmethod
    def from_env(cls) -> Settings:
        """Load `.env` where present and validate configuration before startup."""
        load_dotenv()
        discord_token = os.getenv("DISCORD_TOKEN", "").strip()
        openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
        openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        missing = [
            name
            for name, value in (
                ("DISCORD_TOKEN", discord_token),
                ("OPENAI_API_KEY", openai_api_key),
                ("OPENROUTER_API_KEY", openrouter_api_key),
            )
            if not value
        ]
        if missing:
            raise ConfigurationError(
                f"Missing required environment variables: {', '.join(missing)}"
            )

        model = os.getenv("OPENROUTER_MODEL", "nvidia/nemotron-3-super-120b-a12b:free").strip()
        if not model:
            raise ConfigurationError("OPENROUTER_MODEL cannot be empty")

        raw_guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
        try:
            guild_id = int(raw_guild_id) if raw_guild_id else None
        except ValueError as exc:
            raise ConfigurationError("DISCORD_GUILD_ID must be an integer snowflake") from exc
        if guild_id is not None and guild_id <= 0:
            raise ConfigurationError("DISCORD_GUILD_ID must be positive")

        log_level = os.getenv("LOG_LEVEL", "INFO").upper()
        if log_level not in logging.getLevelNamesMapping():
            raise ConfigurationError("LOG_LEVEL must be a valid Python logging level")

        return cls(
            discord_token=discord_token,
            openai_api_key=openai_api_key,
            openrouter_api_key=openrouter_api_key,
            openrouter_model=model,
            database_url=os.getenv("DATABASE_URL", "").strip(),
            discord_guild_id=guild_id,
            max_concurrent_moderation=_integer(
                "MAX_CONCURRENT_MODERATION", 4, minimum=1, maximum=64
            ),
            http_timeout_seconds=_integer("HTTP_TIMEOUT_SECONDS", 12, minimum=3, maximum=60),
            max_image_bytes=_integer(
                "MAX_IMAGE_BYTES", 8_000_000, minimum=100_000, maximum=20_000_000
            ),
            image_scan_enabled_by_default=_boolean("IMAGE_SCAN_ENABLED_BY_DEFAULT", False),
            allow_openrouter_text=_boolean("ALLOW_OPENROUTER_TEXT", False),
            log_level=log_level,
        )

    def sqlalchemy_database_url(self) -> str:
        """Choose a local SQLite file by default or normalize PostgreSQL URLs."""
        if self.database_url:
            if self.database_url.startswith("postgres://"):
                return self.database_url.replace("postgres://", "postgresql+asyncpg://", 1)
            if self.database_url.startswith("postgresql://"):
                return self.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
            if self.database_url.startswith(("sqlite+aiosqlite:///", "postgresql+asyncpg://")):
                return self.database_url
            raise ConfigurationError(
                "DATABASE_URL must use SQLite (sqlite+aiosqlite) or PostgreSQL (postgresql)"
            )
        Path("data").mkdir(parents=True, exist_ok=True)
        return "sqlite+aiosqlite:///./data/bombamod.db"
