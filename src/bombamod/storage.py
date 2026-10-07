"""Small async SQLAlchemy store for policy, audit labels, and expiring strikes."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, delete, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from bombamod.schemas import GuildPolicy


class Base(DeclarativeBase):
    pass


class GuildConfigRow(Base):
    __tablename__ = "guild_config"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    values_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class StrikeRow(Base):
    __tablename__ = "strikes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    categories: Mapped[str] = mapped_column(String(500), default="")
    action: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class FeedbackRow(Base):
    __tablename__ = "moderator_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    moderator_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    corrected_action: Mapped[str] = mapped_column(String(32))
    category_labels: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class Store:
    """Persistence interface; message text and image data are never stored."""

    def __init__(self, database_url: str) -> None:
        if database_url.startswith("sqlite+"):
            connect_args = {"check_same_thread": False}
        else:
            connect_args = {}
        self.engine: AsyncEngine = create_async_engine(
            database_url, pool_pre_ping=True, connect_args=connect_args
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    async def initialize(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()

    async def policy(self, guild_id: int) -> GuildPolicy:
        async with self.sessions() as session:
            row = await session.get(GuildConfigRow, guild_id)
            values = json.loads(row.values_json) if row else {}
            return GuildPolicy.from_mapping(guild_id, values)

    async def update_policy(self, guild_id: int, **changes: Any) -> GuildPolicy:
        async with self.sessions.begin() as session:
            row = await session.get(GuildConfigRow, guild_id)
            values = json.loads(row.values_json) if row else {}
            values.update(changes)
            # Validate before committing and persist only settings/rules, never message content.
            policy = GuildPolicy.from_mapping(guild_id, values)
            encoded = json.dumps(
                {
                    "rules": policy.rules,
                    "monitored_channel_ids": list(policy.monitored_channel_ids),
                    "moderation_enabled": policy.moderation_enabled,
                    "openrouter_text_opt_in": policy.openrouter_text_opt_in,
                    "image_scan_enabled": policy.image_scan_enabled,
                    "auto_actions_enabled": policy.auto_actions_enabled,
                    "ban_opt_in": policy.ban_opt_in,
                    "max_timeout_minutes": policy.max_timeout_minutes,
                    "min_confidence": policy.min_confidence,
                    "review_channel_id": policy.review_channel_id,
                    "strikes_enabled": policy.strikes_enabled,
                    "strike_retention_days": policy.strike_retention_days,
                    "feedback_retention_days": policy.feedback_retention_days,
                }
            )
            if row:
                row.values_json = encoded
                row.updated_at = datetime.now(UTC)
            else:
                session.add(GuildConfigRow(guild_id=guild_id, values_json=encoded))
        return policy

    async def add_strike(
        self, guild_id: int, user_id: int, categories: list[str], action: str, retention_days: int
    ) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        async with self.sessions.begin() as session:
            await session.execute(delete(StrikeRow).where(StrikeRow.created_at < cutoff))
            session.add(
                StrikeRow(
                    guild_id=guild_id,
                    user_id=user_id,
                    categories=",".join(categories)[:500],
                    action=action[:32],
                )
            )
            result = await session.execute(
                select(StrikeRow.id).where(
                    StrikeRow.guild_id == guild_id,
                    StrikeRow.user_id == user_id,
                    StrikeRow.created_at >= cutoff,
                )
            )
            return len(result.scalars().all())

    async def strikes_for(self, guild_id: int, user_id: int, retention_days: int) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        async with self.sessions.begin() as session:
            await session.execute(delete(StrikeRow).where(StrikeRow.created_at < cutoff))
            result = await session.execute(
                select(StrikeRow.id).where(
                    StrikeRow.guild_id == guild_id,
                    StrikeRow.user_id == user_id,
                    StrikeRow.created_at >= cutoff,
                )
            )
            return len(result.scalars().all())

    async def save_feedback(
        self,
        guild_id: int,
        moderator_id: int,
        message_id: int,
        corrected_action: str,
        categories: list[str],
    ) -> None:
        async with self.sessions.begin() as session:
            cutoff = datetime.now(UTC) - timedelta(days=365)
            await session.execute(delete(FeedbackRow).where(FeedbackRow.created_at < cutoff))
            session.add(
                FeedbackRow(
                    guild_id=guild_id,
                    moderator_id=moderator_id,
                    message_id=message_id,
                    corrected_action=corrected_action[:32],
                    category_labels=",".join(categories)[:500],
                )
            )

    async def recent_feedback_count(self, guild_id: int, days: int = 30) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        async with self.sessions() as session:
            result = await session.execute(
                select(FeedbackRow.id).where(
                    FeedbackRow.guild_id == guild_id,
                    FeedbackRow.created_at >= cutoff,
                )
            )
            return len(result.scalars().all())
