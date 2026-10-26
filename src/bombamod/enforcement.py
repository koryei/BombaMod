"""Deterministic action guardrails around untrusted model recommendations."""

from __future__ import annotations

from dataclasses import replace

from bombamod.schemas import Action, Decision, GuildPolicy, ModerationResult

MAX_ABSOLUTE_TIMEOUT_MINUTES = 40_320  # Discord's 28-day timeout ceiling.

# Plain insults ("harassment") and profanity are treated as banter and never punished.
# Only these categories can trigger an action, each with a short user-facing description.
SEVERE_CATEGORIES: dict[str, str] = {
    "hate": "hateful content",
    "hate/threatening": "hateful threats",
    "harassment/threatening": "threats",
    "self-harm": "encouraging self-harm",
    "self-harm/intent": "encouraging self-harm",
    "self-harm/instructions": "encouraging self-harm",
    "violence": "threats of violence",
    "violence/graphic": "graphic violence",
    "sexual/minors": "sexual content involving minors",
    "illicit/violent": "violent illicit content",
}


def severe_reasons(result: ModerationResult) -> list[str]:
    """Return de-duplicated, user-facing descriptions of the severe categories that were flagged."""
    reasons: list[str] = []
    for category, description in SEVERE_CATEGORIES.items():
        if result.categories.get(category) and description not in reasons:
            reasons.append(description)
    return reasons


def constrain_decision(decision: Decision, policy: GuildPolicy) -> Decision:
    """Clamp/deny risky actions regardless of Nemotron's response."""
    if decision.confidence < policy.min_confidence and decision.action != Action.ESCALATE:
        return Decision(
            action=Action.ESCALATE,
            reason="Model confidence is below the server's automatic-action threshold.",
            confidence=decision.confidence,
        )

    if decision.action == Action.BAN and not policy.ban_opt_in:
        return Decision(
            action=Action.ESCALATE,
            reason="A ban was recommended, but an administrator has not explicitly enabled bans.",
            confidence=decision.confidence,
        )

    if decision.action == Action.TIMEOUT:
        duration = min(
            max(decision.timeout_minutes, 1),
            policy.max_timeout_minutes,
            MAX_ABSOLUTE_TIMEOUT_MINUTES,
        )
        return replace(decision, timeout_minutes=duration)

    if decision.timeout_minutes:
        return replace(decision, timeout_minutes=0)
    return decision


def require_admin_opt_in(decision: Decision, policy: GuildPolicy) -> Decision:
    """Escalate instead of punishing when autonomous actions are disabled."""
    if not policy.auto_actions_enabled and decision.action not in {Action.ALLOW, Action.ESCALATE}:
        return Decision(
            action=Action.ESCALATE,
            reason="Automatic enforcement is disabled; moderator review is required.",
            confidence=decision.confidence,
        )
    return constrain_decision(decision, policy)
