"""Ephemeral case context for moderator feedback; no message text is stored."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from bombamod.schemas import Action, Decision, ModerationResult


@dataclass(frozen=True, slots=True)
class CaseRecord:
    message_id: int
    guild_id: int
    user_id: int
    channel_id: int
    categories: tuple[str, ...]
    action: Action
    reason: str
    created_at: datetime


class CaseRegistry:
    """Bounded in-memory metadata to connect a moderator correction to a case."""

    def __init__(self, max_cases: int = 5000, max_age_days: int = 14) -> None:
        self.max_cases = max_cases
        self.max_age_days = max_age_days
        self._cases: dict[int, CaseRecord] = {}

    def add(
        self,
        *,
        message_id: int,
        guild_id: int,
        user_id: int,
        channel_id: int,
        result: ModerationResult,
        decision: Decision,
    ) -> None:
        now = datetime.now(UTC)
        cutoff = now.timestamp() - self.max_age_days * 86_400
        self._cases = {
            case_id: record
            for case_id, record in self._cases.items()
            if record.created_at.timestamp() >= cutoff
        }
        self._cases[message_id] = CaseRecord(
            message_id=message_id,
            guild_id=guild_id,
            user_id=user_id,
            channel_id=channel_id,
            categories=tuple(key for key, flagged in result.categories.items() if flagged)[:50],
            action=decision.action,
            reason=decision.reason[:500],
            created_at=now,
        )
        if len(self._cases) > self.max_cases:
            oldest = sorted(self._cases.values(), key=lambda item: item.created_at)
            for record in oldest[: len(self._cases) - self.max_cases]:
                self._cases.pop(record.message_id, None)

    def get(self, message_id: int) -> CaseRecord | None:
        record = self._cases.get(message_id)
        if record is None:
            return None
        cutoff = datetime.now(UTC).timestamp() - self.max_age_days * 86_400
        if record.created_at.timestamp() < cutoff:
            self._cases.pop(message_id, None)
            return None
        return record
