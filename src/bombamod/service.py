"""End-to-end moderation pipeline with fail-safe review on provider errors."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

from bombamod.enforcement import require_admin_opt_in
from bombamod.providers import OpenAIProvider, OpenRouterProvider, ProviderError
from bombamod.schemas import Action, Decision, GuildPolicy, ModerationResult
from bombamod.storage import Store

logger = logging.getLogger(__name__)


class AlertSink(Protocol):
    async def alert(
        self, guild_id: int, channel_id: int, reason: str, case_url: str | None = None
    ) -> None: ...


class MessageActionSink(Protocol):
    async def apply(
        self,
        message_id: int,
        guild_id: int,
        channel_id: int,
        user_id: int,
        decision: Decision,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class CaseOutcome:
    result: ModerationResult | None
    decision: Decision
    provider_failed: bool = False


class ModerationService:
    def __init__(
        self,
        *,
        store: Store,
        openai: OpenAIProvider,
        openrouter: OpenRouterProvider,
        alerts: AlertSink,
        actions: MessageActionSink,
        semaphore: asyncio.Semaphore,
        allow_openrouter_text: bool,
        case_registry: object | None = None,
    ) -> None:
        self.store = store
        self.openai = openai
        self.openrouter = openrouter
        self.alerts = alerts
        self.actions = actions
        self.semaphore = semaphore
        self.allow_openrouter_text = allow_openrouter_text
        self.case_registry = case_registry

    async def moderate(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        user_id: int,
        content: str,
        image_urls: list[str] | None = None,
    ) -> CaseOutcome:
        policy = await self.store.policy(guild_id)
        if not policy.moderation_enabled:
            return CaseOutcome(
                None, Decision(Action.ALLOW, "BombaMod moderation is not configured.")
            )
        if policy.monitored_channel_ids and channel_id not in policy.monitored_channel_ids:
            return CaseOutcome(None, Decision(Action.ALLOW, "Channel is not monitored."))

        async with self.semaphore:
            try:
                image_data: list[str] = []
                if image_urls and policy.image_scan_enabled:
                    # Any unsupported/oversized/unavailable attachment is skipped; never fetched
                    # unless image scanning is explicitly enabled for this guild.
                    for url in image_urls[:4]:
                        try:
                            image_data.append(await self.openai.fetch_image_data_url(url))
                        except ProviderError:
                            logger.info("Skipped an ineligible image attachment")
                image_scan_incomplete = bool(image_urls and policy.image_scan_enabled) and (
                    len(image_data) < len(image_urls[:4])
                )
                result = await self.openai.moderate(content or "", image_data)
                if image_scan_incomplete:
                    await self._alert(
                        policy,
                        guild_id,
                        channel_id,
                        "An enabled image scan could not process every attachment; moderator review is required.",
                        message_id=message_id,
                    )
                    return CaseOutcome(
                        result,
                        Decision(Action.ESCALATE, "Image moderation was incomplete; review required."),
                    )
                if not result.flagged:
                    return CaseOutcome(
                        result, Decision(Action.ALLOW, "No moderation category was flagged.")
                    )

                if not policy.rules.strip():
                    decision = Decision(
                        Action.ESCALATE,
                        "No moderator-authored policy is configured; review required.",
                    )
                    await self._alert(
                        policy,
                        guild_id,
                        channel_id,
                        self._review_reason(result, decision),
                        message_id=message_id,
                    )
                    return CaseOutcome(result, decision)

                prior_strikes = 0
                if policy.strikes_enabled:
                    prior_strikes = await self.store.strikes_for(
                        guild_id, user_id, policy.strike_retention_days
                    )

                share_text = self.allow_openrouter_text and policy.openrouter_text_opt_in
                decision = await self.openrouter.decide(
                    rules=policy.rules,
                    categories=result.categories,
                    scores=result.scores,
                    message_text=content if share_text else None,
                    strike_count=prior_strikes,
                    minimum_confidence=policy.min_confidence,
                )
                decision = self._sanitize_decision(decision, result)
                decision = require_admin_opt_in(decision, policy)
                if decision.action == Action.BAN and not policy.ban_opt_in:
                    decision = Decision(
                        Action.ESCALATE,
                        "A ban requires explicit administrator opt-in.",
                        confidence=decision.confidence,
                    )
                if policy.strikes_enabled and decision.action not in {
                    Action.ALLOW,
                    Action.ESCALATE,
                }:
                    await self.store.add_strike(
                        guild_id,
                        user_id,
                        [key for key, flagged in result.categories.items() if flagged],
                        decision.action.value,
                        policy.strike_retention_days,
                    )
                if decision.action == Action.ESCALATE or not policy.auto_actions_enabled:
                    await self._alert(
                        policy,
                        guild_id,
                        channel_id,
                        self._review_reason(result, decision),
                        message_id=message_id,
                    )
                elif decision.action != Action.ALLOW:
                    applied = await self.actions.apply(
                        message_id, guild_id, channel_id, user_id, decision
                    )
                    if not applied:
                        await self._alert(
                            policy,
                            guild_id,
                            channel_id,
                            "Discord action failed (permission or role hierarchy); review required.",
                            message_id=message_id,
                        )

                if self.case_registry is not None:
                    self.case_registry.add(
                        message_id=message_id,
                        guild_id=guild_id,
                        user_id=user_id,
                        channel_id=channel_id,
                        result=result,
                        decision=decision,
                    )
                return CaseOutcome(result, decision)
            except (TimeoutError, ProviderError):
                # Don't log exception messages; they could contain remote response content.
                logger.warning(
                    "A moderation provider was unavailable; moderator review is required"
                )
                await self._alert(
                    policy,
                    guild_id,
                    channel_id,
                    "AI moderation unavailable; please review manually.",
                    message_id=message_id,
                )
                return CaseOutcome(
                    None,
                    Decision(
                        Action.ESCALATE, "AI moderation unavailable; moderator review required."
                    ),
                    provider_failed=True,
                )

    @staticmethod
    def _sanitize_decision(decision: Decision, result: ModerationResult) -> Decision:
        """Never trust generated reasons for audit logs: they may quote the source message."""
        categories = [name for name, flagged in result.categories.items() if flagged][:5]
        summary = ", ".join(categories) or "moderation signals"
        return Decision(
            action=decision.action,
            reason=f"Nemotron recommended {decision.action.value} based on {summary}.",
            timeout_minutes=decision.timeout_minutes,
            confidence=decision.confidence,
        )

    @staticmethod
    def _review_reason(result: ModerationResult, decision: Decision) -> str:
        categories = [name for name, flagged in result.categories.items() if flagged][:8]
        category_summary = ", ".join(categories) or "unspecified category"
        return (
            f"Flagged categories: {category_summary}. "
            f"Suggested action: {decision.action.value}; moderator review required."
        )

    async def _alert(
        self,
        policy: GuildPolicy,
        guild_id: int,
        channel_id: int,
        reason: str,
        *,
        message_id: int | None = None,
    ) -> None:
        case_url = (
            f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
            if message_id is not None
            else None
        )
        await self.alerts.alert(
            guild_id, policy.review_channel_id or channel_id, reason[:500], case_url
        )
