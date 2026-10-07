from __future__ import annotations

import pytest

from bombamod.schemas import Action, Decision, GuildPolicy, ModerationResult


def test_moderation_result_parses_scores_and_categories() -> None:
    result = ModerationResult.from_openai(
        {
            "results": [
                {
                    "flagged": True,
                    "categories": {"harassment": True},
                    "category_scores": {"harassment": 0.91},
                }
            ]
        }
    )
    assert result.flagged is True
    assert result.categories == {"harassment": True}
    assert result.scores["harassment"] == 0.91


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"results": []},
        {"results": [{"flagged": True, "categories": {}, "category_scores": {"bad": 2}}]},
        {"results": [{"flagged": "true", "categories": {}, "category_scores": {}}]},
    ],
)
def test_invalid_moderation_response_fails_closed(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ModerationResult.from_openai(payload)  # type: ignore[arg-type]


def test_decision_requires_valid_schema_and_reason() -> None:
    decision = Decision.from_json(
        {
            "action": "timeout",
            "reason": "Repeated harassment under server rule 2.",
            "timeout_minutes": 30,
            "confidence": 0.92,
        }
    )
    assert decision.action is Action.TIMEOUT
    assert decision.timeout_minutes == 30
    with pytest.raises(ValueError):
        Decision.from_json({"action": "ban", "reason": "", "confidence": 1})
    with pytest.raises(ValueError):
        Decision.from_json({"action": "allow", "reason": "OK", "confidence": 1, "extra": "ignored"})
    with pytest.raises(ValueError):
        Decision.from_json({"action": "delete", "reason": "Bad", "timeout_minutes": 60})


def test_guild_policy_privacy_and_action_defaults_are_off() -> None:
    policy = GuildPolicy.from_mapping(123, {})
    assert not policy.moderation_enabled
    assert not policy.openrouter_text_opt_in
    assert not policy.image_scan_enabled
    assert not policy.auto_actions_enabled
    assert not policy.ban_opt_in
    assert policy.min_confidence == 0.8


def test_guild_policy_enforces_bounds() -> None:
    with pytest.raises(ValueError):
        GuildPolicy.from_mapping(123, {"max_timeout_minutes": 40_321})
    with pytest.raises(ValueError):
        GuildPolicy.from_mapping(123, {"rules": "x" * 6001})
    with pytest.raises(ValueError):
        GuildPolicy.from_mapping(123, {"monitored_channel_ids": list(range(101))})
