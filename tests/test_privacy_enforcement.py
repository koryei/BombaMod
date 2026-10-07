from __future__ import annotations

from bombamod.enforcement import constrain_decision, require_admin_opt_in
from bombamod.privacy import redact_text
from bombamod.schemas import Action, Decision, GuildPolicy


def test_redaction_removes_common_identifiers_and_limits_size() -> None:
    text = "Email a@b.com or ping <@123456789012345678>; call +1 (555) 234-5678"
    redacted = redact_text(text)
    assert "a@b.com" not in redacted
    assert "123456789012345678" not in redacted
    assert "555" not in redacted
    assert len(redact_text("x" * 100, max_chars=20)) == 20


def test_low_confidence_escalates() -> None:
    policy = GuildPolicy.from_mapping(1, {"min_confidence": 0.8})
    decision = constrain_decision(Decision(Action.DELETE, "Flagged", confidence=0.79), policy)
    assert decision.action is Action.ESCALATE


def test_bans_require_explicit_opt_in_and_enabled_autonomy() -> None:
    decision = Decision(Action.BAN, "Severe", confidence=0.99)
    no_bans = GuildPolicy.from_mapping(1, {"auto_actions_enabled": True})
    assert require_admin_opt_in(decision, no_bans).action is Action.ESCALATE
    allowed = GuildPolicy.from_mapping(1, {"auto_actions_enabled": True, "ban_opt_in": True})
    assert require_admin_opt_in(decision, allowed).action is Action.BAN


def test_disabled_automatic_actions_escalate_punitive_recommendations() -> None:
    policy = GuildPolicy.from_mapping(1, {"auto_actions_enabled": False})
    decision = require_admin_opt_in(
        Decision(Action.DELETE, "Clear violation", confidence=0.99), policy
    )
    assert decision.action is Action.ESCALATE


def test_timeout_is_clamped_and_only_timeout_can_have_duration() -> None:
    policy = GuildPolicy.from_mapping(1, {"auto_actions_enabled": True, "max_timeout_minutes": 60})
    result = constrain_decision(Decision(Action.TIMEOUT, "Repeated abuse", 40320, 0.99), policy)
    assert result.timeout_minutes == 60


def test_rules_privacy_opt_in_is_off_by_default() -> None:
    policy = GuildPolicy.from_mapping(987, {})
    assert policy.openrouter_text_opt_in is False
