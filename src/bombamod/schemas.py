"""Typed domain objects and strict validation for model responses."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Action(StrEnum):
    ALLOW = "allow"
    DELETE = "delete"
    WARN = "warn"
    TIMEOUT = "timeout"
    ESCALATE = "escalate"
    BAN = "ban"


@dataclass(frozen=True, slots=True)
class ModerationResult:
    flagged: bool
    categories: dict[str, bool] = field(default_factory=dict)
    scores: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_openai(cls, payload: dict[str, Any]) -> ModerationResult:
        """Validate and normalize the first result from OpenAI's Moderations API."""
        results = payload.get("results")
        if not isinstance(results, list) or not results or not isinstance(results[0], dict):
            raise ValueError("Moderation API returned no result")
        result = results[0]
        categories_raw = result.get("categories")
        scores_raw = result.get("category_scores")
        if not isinstance(categories_raw, dict) or not isinstance(scores_raw, dict):
            raise ValueError("Moderation result is missing categories or category_scores")
        if any(not isinstance(value, bool) for value in categories_raw.values()):
            raise ValueError("Moderation category values must be booleans")
        categories = {str(k): value for k, value in categories_raw.items()}
        scores: dict[str, float] = {}
        for key, value in scores_raw.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"Invalid moderation score for {key}")
            score = float(value)
            if not 0 <= score <= 1:
                raise ValueError(f"Moderation score for {key} is out of range")
            scores[str(key)] = score
        flagged = result.get("flagged")
        if not isinstance(flagged, bool):
            raise ValueError("Moderation result is missing a boolean flagged value")
        return cls(flagged=flagged, categories=categories, scores=scores)


@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    reason: str
    timeout_minutes: int = 0
    confidence: float = 0.0

    @classmethod
    def from_json(cls, payload: Any) -> Decision:
        """Parse the constrained JSON response; do not trust model output."""
        if not isinstance(payload, dict):
            raise ValueError("Decision must be a JSON object")
        if set(payload) - {"action", "reason", "timeout_minutes", "confidence"}:
            raise ValueError("Decision contains unexpected keys")
        try:
            action = Action(payload["action"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("Decision has an unsupported action") from exc
        reason = payload.get("reason")
        minutes = payload.get("timeout_minutes", 0)
        confidence = payload.get("confidence", 0.0)
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
            raise ValueError("Decision reason must contain 1-500 characters")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 0 <= minutes <= 40320:
            raise ValueError("timeout_minutes must be an integer between 0 and 40320")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ValueError("confidence must be numeric")
        confidence = float(confidence)
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if action != Action.TIMEOUT and minutes != 0:
            raise ValueError("Only timeout decisions may specify timeout_minutes")
        return cls(
            action=action, reason=reason.strip(), timeout_minutes=minutes, confidence=confidence
        )


@dataclass(frozen=True, slots=True)
class GuildPolicy:
    guild_id: int
    rules: str = ""
    monitored_channel_ids: tuple[int, ...] = ()
    moderation_enabled: bool = False
    openrouter_text_opt_in: bool = False
    image_scan_enabled: bool = False
    auto_actions_enabled: bool = False
    ban_opt_in: bool = False
    max_timeout_minutes: int = 1440
    min_confidence: float = 0.80
    review_channel_id: int | None = None
    strikes_enabled: bool = True
    strike_retention_days: int = 30
    feedback_retention_days: int = 90

    @classmethod
    def from_mapping(cls, guild_id: int, values: dict[str, Any]) -> GuildPolicy:
        def parse_bool(key: str, default: bool = False) -> bool:
            value = values.get(key, default)
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be a boolean")
            return value

        timeout_raw = values.get("max_timeout_minutes", 1440)
        if isinstance(timeout_raw, bool) or not isinstance(timeout_raw, int):
            raise ValueError("Maximum timeout must be an integer")
        timeout = timeout_raw
        if not 1 <= timeout <= 40320:
            raise ValueError("Maximum timeout must be from 1 to 40320 minutes")
        confidence_raw = values.get("min_confidence", 0.80)
        if isinstance(confidence_raw, bool) or not isinstance(confidence_raw, (int, float)):
            raise ValueError("Minimum confidence must be numeric")
        confidence = float(confidence_raw)
        if not 0 <= confidence <= 1:
            raise ValueError("Minimum confidence must be from 0 to 1")
        rules = str(values.get("rules", ""))
        if len(rules) > 6000:
            raise ValueError("Rules may contain at most 6000 characters")
        retention_raw = values.get("strike_retention_days", 30)
        feedback_retention_raw = values.get("feedback_retention_days", 90)
        if (
            isinstance(retention_raw, bool)
            or not isinstance(retention_raw, int)
            or isinstance(feedback_retention_raw, bool)
            or not isinstance(feedback_retention_raw, int)
        ):
            raise ValueError("Retention periods must be integers")
        retention = retention_raw
        feedback_retention = feedback_retention_raw
        if not 1 <= retention <= 365 or not 1 <= feedback_retention <= 365:
            raise ValueError("Retention must be from 1 to 365 days")
        raw_channels = values.get("monitored_channel_ids", [])
        if not isinstance(raw_channels, (list, tuple)) or len(raw_channels) > 100:
            raise ValueError("At most 100 monitored channels may be configured")
        channels = tuple(sorted({int(channel) for channel in raw_channels}))
        if any(channel <= 0 for channel in channels):
            raise ValueError("Monitored channel IDs must be positive")
        channel = values.get("review_channel_id")
        if channel is not None and int(channel) <= 0:
            raise ValueError("Review channel ID must be positive")
        return cls(
            guild_id=guild_id,
            rules=rules,
            monitored_channel_ids=channels,
            moderation_enabled=parse_bool("moderation_enabled"),
            openrouter_text_opt_in=parse_bool("openrouter_text_opt_in"),
            image_scan_enabled=parse_bool("image_scan_enabled"),
            auto_actions_enabled=parse_bool("auto_actions_enabled"),
            ban_opt_in=parse_bool("ban_opt_in"),
            max_timeout_minutes=timeout,
            min_confidence=confidence,
            review_channel_id=int(channel) if channel is not None else None,
            strikes_enabled=parse_bool("strikes_enabled", True),
            strike_retention_days=retention,
            feedback_retention_days=feedback_retention,
        )
