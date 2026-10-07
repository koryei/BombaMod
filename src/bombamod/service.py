"""End-to-end moderation pipeline with fail-safe review on provider errors."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

from bombamod.enforcement import require_admin_opt_in
from bombamod.moderator import CaseRegistry
from bombamod.providers import OpenAIProvider, OpenRouterProvider, ProviderError
from bombamod.schemas import Action, Decision, GuildPolicy, ModerationResult
from bombamod.storage import Store

logger = logging.getLogger(__name__)


class AlertSink(Protocol):
    async def alert(
        self,
        guild_id: int,
        channel_id: int,
        reason: str,
        case_url: str | None = None,
    ) -> None: ...


class MessageActionSink(Protocol):
    async def apply(
        self,
        message_id: int,
        guild_id: int,
        channel_id: int,
        user_id: int,
        source_fingerprint: str,
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
        disable_openai_text: bool = False,
        max_message_chars: int = 6000,
        max_image_bytes: int = 8_000_000,
        case_registry: CaseRegistry | None = None,
    ) -> None:
        self.store = store
        self.openai = openai
        self.openrouter = openrouter
        self.alerts = alerts
        self.actions = actions
        self.semaphore = semaphore
        self.allow_openrouter_text = allow_openrouter_text
        self.disable_openai_text = disable_openai_text
        self.max_message_chars = max_message_chars
        self.max_image_bytes = max_image_bytes
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
        source_fingerprint: str = "",
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
                if self.disable_openai_text:
                    decision = Decision(Action.ESCALATE, "OpenAI text classification is disabled.")
                    self._register_case(message_id, guild_id, channel_id, user_id, None, decision)
                    await self._alert(
                        policy,
                        guild_id,
                        channel_id,
                        "OpenAI text classification is disabled; manual review required.",
                        message_id=message_id,
                    )
                    return CaseOutcome(None, decision)
                image_data: list[str] = []
                eligible_image_urls = image_urls or []
                image_scan_incomplete = (
                    bool(eligible_image_urls and policy.image_scan_enabled)
                    and len(eligible_image_urls) > 2
                )
                total_image_bytes = 0
                if eligible_image_urls and policy.image_scan_enabled:
                    # Two images and one configured image-size worth of payload bound per-case RAM.
                    for url in eligible_image_urls[:2]:
                        try:
                            remaining_bytes = self.max_image_bytes - total_image_bytes
                            data_url = await self.openai.fetch_image_data_url(
                                url, max_bytes=remaining_bytes
                            )
                            encoded = data_url.partition(",")[2]
                            estimated_bytes = (len(encoded) * 3) // 4
                            if total_image_bytes + estimated_bytes > self.max_image_bytes:
                                image_scan_incomplete = True
                                continue
                            image_data.append(data_url)
                            total_image_bytes += estimated_bytes
                        except ProviderError:
                            logger.info("Skipped an ineligible image attachment")
                            image_scan_incomplete = True
                image_scan_incomplete = image_scan_incomplete or (
                    bool(eligible_image_urls and policy.image_scan_enabled)
                    and len(image_data) < min(len(eligible_image_urls), 2)
                )
                source_text = content[: self.max_message_chars]
                message_truncated = len(content) > self.max_message_chars
                result = await self.openai.moderate(source_text, image_data)
                if message_truncated:
                    decision = Decision(
                        Action.ESCALATE,
                        "Message exceeded scan limit; manual review is required.",
                    )
                    self._register_case(message_id, guild_id, channel_id, user_id, result, decision)
                    await self._alert(
                        policy,
                        guild_id,
                        channel_id,
                        "Message exceeds scan limit; only the beginning was scanned.",
                        message_id=message_id,
                    )
                    return CaseOutcome(result, decision)
                if image_scan_incomplete:
                    decision = Decision(
                        Action.ESCALATE, "Image moderation was incomplete; review required."
                    )
                    self._register_case(message_id, guild_id, channel_id, user_id, result, decision)
                    await self._alert(
                        policy,
                        guild_id,
                        channel_id,
                        "Enabled image scan incomplete; moderator review is required.",
                        message_id=message_id,
                    )
                    return CaseOutcome(result, decision)
                if not result.flagged:
                    decision = Decision(Action.ALLOW, "No moderation category was flagged.")
                    self._register_case(message_id, guild_id, channel_id, user_id, result, decision)
                    return CaseOutcome(result, decision)
                if not any(result.categories.values()):
                    decision = Decision(
                        Action.ESCALATE,
                        "Classifier returned an inconsistent flagged result; review required.",
                    )
                    self._register_case(message_id, guild_id, channel_id, user_id, result, decision)
                    await self._alert(
                        policy,
                        guild_id,
                        channel_id,
                        "Classifier flagged content without category details; review required.",
                        message_id=message_id,
                    )
                    return CaseOutcome(result, decision)

                if not policy.rules.strip():
                    decision = Decision(
                        Action.ESCALATE,
                        "No moderator-authored policy is configured; review required.",
                    )
                    self._register_case(message_id, guild_id, channel_id, user_id, result, decision)
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
                    message_text=source_text if share_text else None,
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
                self._register_case(message_id, guild_id, channel_id, user_id, result, decision)
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
                if decision.action == Action.ESCALATE:
                    await self._alert(
                        policy,
                        guild_id,
                        channel_id,
                        self._review_reason(result, decision),
                        message_id=message_id,
                    )
                elif decision.action != Action.ALLOW and not policy.auto_actions_enabled:
                    await self._alert(
                        policy,
                        guild_id,
                        channel_id,
                        self._review_reason(result, decision),
                        message_id=message_id,
                    )
                elif decision.action != Action.ALLOW:
                    applied = await self.actions.apply(
                        message_id,
                        guild_id,
                        channel_id,
                        user_id,
                        source_fingerprint,
                        decision,
                    )
                    if not applied:
                        await self._alert(
                            policy,
                            guild_id,
                            channel_id,
                            "Discord action failed; moderator review required.",
                            message_id=message_id,
                        )

                return CaseOutcome(result, decision)
            except (TimeoutError, ProviderError):
                # Don't log exception messages; they could contain remote response content.
                logger.warning(
                    "A moderation provider was unavailable; moderator review is required"
                )
                decision = Decision(
                    Action.ESCALATE, "AI moderation unavailable; moderator review required."
                )
                self._register_case(message_id, guild_id, channel_id, user_id, None, decision)
                await self._alert(
                    policy,
                    guild_id,
                    channel_id,
                    "AI moderation unavailable; please review manually.",
                    message_id=message_id,
                )
                return CaseOutcome(
                    None,
                    decision,
                    provider_failed=True,
                )

    def _register_case(
        self,
        message_id: int,
        guild_id: int,
        channel_id: int,
        user_id: int,
        result: ModerationResult | None,
        decision: Decision,
    ) -> None:
        if self.case_registry is not None:
            self.case_registry.add(
                message_id=message_id,
                guild_id=guild_id,
                user_id=user_id,
                channel_id=channel_id,
                result=result or ModerationResult(False),
                decision=decision,
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
        if policy.review_channel_id is None:
            # Fail closed and never post private case context in a public source channel.
            logger.error("No private review channel configured; case must be handled manually")
            return
        case_url = (
            f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
            if message_id is not None
            else None
        )
        await self.alerts.alert(guild_id, policy.review_channel_id, reason[:500], case_url)
