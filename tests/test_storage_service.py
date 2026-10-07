from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from bombamod.providers import ProviderError
from bombamod.schemas import Action, Decision, ModerationResult
from bombamod.service import ModerationService
from bombamod.storage import Store


class FakeOpenAI:
    def __init__(self, result: ModerationResult | None = None) -> None:
        self.result = result or ModerationResult(True, {"harassment": True}, {"harassment": 0.99})
        self.image_fetches = 0
        self.moderated_text: list[str] = []
        self.moderated_images: list[list[str]] = []

    async def fetch_image_data_url(self, url: str, *, max_bytes: int | None = None) -> str:
        self.image_fetches += 1
        if "bad" in url:
            raise ProviderError("skip")
        return "data:image/png;base64,AA=="

    async def moderate(
        self, text: str, image_data_urls: list[str] | None = None
    ) -> ModerationResult:
        self.moderated_text.append(text)
        self.moderated_images.append(image_data_urls or [])
        return self.result


class FakeOpenRouter:
    def __init__(self, decision: Decision | None = None) -> None:
        self.decision = decision or Decision(Action.ESCALATE, "Review required", confidence=0.9)
        self.calls: list[dict[str, Any]] = []

    async def decide(self, **kwargs: Any) -> Decision:
        self.calls.append(kwargs)
        return self.decision


class FailingOpenRouter:
    async def decide(self, **kwargs: Any) -> Decision:
        raise ProviderError("upstream error")


class FakeAlerts:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.case_urls: list[str | None] = []

    async def alert(
        self, guild_id: int, channel_id: int, reason: str, case_url: str | None = None
    ) -> None:
        self.messages.append(reason)
        self.case_urls.append(case_url)


class FakeActions:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.calls = 0

    async def apply(
        self,
        message_id: int,
        guild_id: int,
        channel_id: int,
        user_id: int,
        source_fingerprint: str,
        decision: Decision,
    ) -> bool:
        self.calls += 1
        return self.result


def make_service(
    store: Store,
    openai: Any,
    openrouter: Any,
    alerts: FakeAlerts,
    actions: FakeActions | None = None,
    *,
    allow_openrouter_text: bool = False,
    disable_openai_text: bool = False,
    max_message_chars: int = 6000,
    max_image_bytes: int = 8_000_000,
) -> ModerationService:
    return ModerationService(
        store=store,  # type: ignore[arg-type]
        openai=openai,  # type: ignore[arg-type]
        openrouter=openrouter,  # type: ignore[arg-type]
        alerts=alerts,
        actions=actions or FakeActions(),
        semaphore=asyncio.Semaphore(1),
        allow_openrouter_text=allow_openrouter_text,
        disable_openai_text=disable_openai_text,
        max_message_chars=max_message_chars,
        max_image_bytes=max_image_bytes,
    )


@pytest.mark.asyncio
async def test_store_persists_policy_and_feedback_without_raw_message(tmp_path: Path) -> None:
    db = tmp_path / "bot.db"
    store = Store(f"sqlite+aiosqlite:///{db}")
    await store.initialize()
    await store.update_policy(55, moderation_enabled=True, rules="Be respectful")
    policy = await store.policy(55)
    assert policy.moderation_enabled
    assert policy.rules == "Be respectful"
    await store.add_strike(55, 77, ["harassment"], "delete", 30)
    await store.save_feedback(55, 88, 99, "allow", "delete", ["harassment"])
    labels = await store.feedback_rows(55)
    assert len(labels) == 1 and labels[0].predicted_action == "delete"
    with pytest.raises(ValueError):
        await store.strikes_for(55, 77, 0)
    with pytest.raises(ValueError):
        await store.add_strike(55, 77, [], "warn", 366)
    await store.close()
    data = db.read_bytes()
    assert b"Be respectful" in data
    assert b"harassment" in data
    assert b"PRIVATE RAW MESSAGE" not in data


@pytest.mark.asyncio
async def test_provider_failure_escalates_without_action_or_content_in_alert(
    tmp_path: Path,
) -> None:
    store = Store(f"sqlite+aiosqlite:///{tmp_path / 'failure.db'}")
    await store.initialize()
    await store.update_policy(
        1,
        moderation_enabled=True,
        auto_actions_enabled=True,
        rules="No harassment",
        review_channel_id=9,
    )
    alerts = FakeAlerts()
    actions = FakeActions()
    service = make_service(store, FakeOpenAI(), FailingOpenRouter(), alerts, actions)
    outcome = await service.moderate(
        guild_id=1,
        channel_id=2,
        message_id=3,
        user_id=4,
        content="PRIVATE RAW MESSAGE",
    )
    assert outcome.provider_failed
    assert outcome.decision.action is Action.ESCALATE
    assert alerts.messages
    assert actions.calls == 0
    assert "PRIVATE RAW MESSAGE" not in alerts.messages[0]
    assert alerts.case_urls[0] == "https://discord.com/channels/1/2/3"
    await store.close()


@pytest.mark.asyncio
async def test_openrouter_gets_no_content_without_both_opt_ins(tmp_path: Path) -> None:
    store = Store(f"sqlite+aiosqlite:///{tmp_path / 'privacy.db'}")
    await store.initialize()
    await store.update_policy(
        2,
        moderation_enabled=True,
        rules="No harassment",
        openrouter_text_opt_in=True,
        review_channel_id=99,
    )
    openai = FakeOpenAI()
    openrouter = FakeOpenRouter()
    service = make_service(store, openai, openrouter, FakeAlerts(), allow_openrouter_text=False)
    await service.moderate(
        guild_id=2,
        channel_id=4,
        message_id=5,
        user_id=6,
        content="secret account number 1234",
    )
    assert len(openrouter.calls) == 1
    assert openrouter.calls[0]["message_text"] is None
    await store.close()


@pytest.mark.asyncio
async def test_no_rules_escalates_without_calling_decision_model(tmp_path: Path) -> None:
    store = Store(f"sqlite+aiosqlite:///{tmp_path / 'rules.db'}")
    await store.initialize()
    await store.update_policy(3, moderation_enabled=True, review_channel_id=99)
    router = FakeOpenRouter()
    alerts = FakeAlerts()
    service = make_service(store, FakeOpenAI(), router, alerts)
    result = await service.moderate(
        guild_id=3, channel_id=2, message_id=30, user_id=4, content="flagged sample"
    )
    assert result.decision.action is Action.ESCALATE
    assert not router.calls
    assert alerts.messages
    await store.close()


@pytest.mark.asyncio
async def test_failed_enabled_image_scan_escalates_and_never_shares_text(tmp_path: Path) -> None:
    store = Store(f"sqlite+aiosqlite:///{tmp_path / 'image-failure.db'}")
    await store.initialize()
    await store.update_policy(
        5,
        moderation_enabled=True,
        image_scan_enabled=True,
        openrouter_text_opt_in=True,
        rules="No harassment",
        review_channel_id=55,
    )
    openai = FakeOpenAI()
    router = FakeOpenRouter()
    result = await make_service(
        store, openai, router, FakeAlerts(), allow_openrouter_text=True
    ).moderate(
        guild_id=5,
        channel_id=10,
        message_id=11,
        user_id=12,
        content="flagged text",
        image_urls=["https://cdn.discordapp.com/bad.png"],
    )
    assert result.decision.action is Action.ESCALATE
    assert not router.calls
    await store.close()


@pytest.mark.asyncio
async def test_openai_disabled_routes_to_review_without_provider_call(tmp_path: Path) -> None:
    store = Store(f"sqlite+aiosqlite:///{tmp_path / 'no-ai.db'}")
    await store.initialize()
    await store.update_policy(6, moderation_enabled=True, review_channel_id=77)
    openai = FakeOpenAI()
    alerts = FakeAlerts()
    result = await make_service(
        store, openai, FakeOpenRouter(), alerts, disable_openai_text=True
    ).moderate(guild_id=6, channel_id=8, message_id=9, user_id=10, content="private")
    assert result.decision.action is Action.ESCALATE
    assert not openai.moderated_text
    assert alerts.messages
    await store.close()


@pytest.mark.asyncio
async def test_message_scan_size_is_bounded_and_not_marked_safe(tmp_path: Path) -> None:
    store = Store(f"sqlite+aiosqlite:///{tmp_path / 'long-message.db'}")
    await store.initialize()
    await store.update_policy(7, moderation_enabled=True, review_channel_id=88)
    openai = FakeOpenAI(ModerationResult(False, {}, {}))
    alerts = FakeAlerts()
    result = await make_service(
        store, openai, FakeOpenRouter(), alerts, max_message_chars=1000
    ).moderate(
        guild_id=7,
        channel_id=9,
        message_id=10,
        user_id=11,
        content="x" * 1500,
    )
    assert len(openai.moderated_text[0]) == 1000
    assert result.decision.action is Action.ESCALATE
    assert alerts.messages
    await store.close()


@pytest.mark.asyncio
async def test_image_fetch_is_never_called_without_guild_opt_in(tmp_path: Path) -> None:
    store = Store(f"sqlite+aiosqlite:///{tmp_path / 'images.db'}")
    await store.initialize()
    await store.update_policy(12, moderation_enabled=True, image_scan_enabled=False)
    openai = FakeOpenAI()
    service = make_service(store, openai, FakeOpenRouter(), FakeAlerts())
    await service.moderate(
        guild_id=12,
        channel_id=2,
        message_id=3,
        user_id=4,
        content="",
        image_urls=["https://cdn.discordapp.com/image.png"],
    )
    assert openai.image_fetches == 0
    await store.close()
