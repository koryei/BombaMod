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
    async def fetch_image_data_url(self, url: str) -> str:
        raise ProviderError("skip")

    async def moderate(
        self, text: str, image_data_urls: list[str] | None = None
    ) -> ModerationResult:
        return ModerationResult(True, {"harassment": True}, {"harassment": 0.99})


class FailingOpenRouter:
    async def decide(self, **kwargs: Any) -> Decision:
        raise ProviderError("upstream error")


class FakeAlerts:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def alert(
        self, guild_id: int, channel_id: int, reason: str, case_url: str | None = None
    ) -> None:
        self.messages.append(reason)


class FakeActions:
    async def apply(
        self,
        message_id: int,
        guild_id: int,
        channel_id: int,
        user_id: int,
        decision: Decision,
    ) -> bool:
        raise AssertionError("No action may run after provider failure")


@pytest.mark.asyncio
async def test_store_persists_policy_and_metadata_not_message_text(tmp_path: Path) -> None:
    db = tmp_path / "bot.db"
    store = Store(f"sqlite+aiosqlite:///{db}")
    await store.initialize()
    await store.update_policy(55, moderation_enabled=True, rules="Be respectful")
    policy = await store.policy(55)
    assert policy.moderation_enabled
    assert policy.rules == "Be respectful"
    await store.add_strike(55, 77, ["harassment"], "delete", 30)
    await store.save_feedback(55, 88, 99, "allow", ["harassment"])
    await store.close()
    data = db.read_bytes()
    assert b"Be respectful" in data
    assert b"harassment" in data
    assert b"PRIVATE RAW MESSAGE" not in data


@pytest.mark.asyncio
async def test_provider_failure_notifies_and_never_executes_action(tmp_path: Path) -> None:
    store = Store(f"sqlite+aiosqlite:///{tmp_path / 'failure.db'}")
    await store.initialize()
    await store.update_policy(
        1, moderation_enabled=True, auto_actions_enabled=True, rules="No harassment"
    )
    alerts = FakeAlerts()
    service = ModerationService(
        store=store,
        openai=FakeOpenAI(),  # type: ignore[arg-type]
        openrouter=FailingOpenRouter(),  # type: ignore[arg-type]
        alerts=alerts,
        actions=FakeActions(),
        semaphore=asyncio.Semaphore(1),
        allow_openrouter_text=False,
    )
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
    await store.close()


@pytest.mark.asyncio
async def test_image_fetch_is_never_called_without_guild_opt_in(tmp_path: Path) -> None:
    class CountingOpenAI(FakeOpenAI):
        image_fetches = 0

        async def fetch_image_data_url(self, url: str) -> str:
            self.image_fetches += 1
            return "data:image/png;base64,AA=="

    store = Store(f"sqlite+aiosqlite:///{tmp_path / 'images.db'}")
    await store.initialize()
    await store.update_policy(5, moderation_enabled=True, image_scan_enabled=False)
    openai = CountingOpenAI()
    alerts = FakeAlerts()
    service = ModerationService(
        store=store,
        openai=openai,  # type: ignore[arg-type]
        openrouter=FailingOpenRouter(),  # type: ignore[arg-type]
        alerts=alerts,
        actions=FakeActions(),
        semaphore=asyncio.Semaphore(1),
        allow_openrouter_text=False,
    )
    await service.moderate(
        guild_id=5,
        channel_id=2,
        message_id=3,
        user_id=4,
        content="",
        image_urls=["https://cdn.discordapp.com/image.png"],
    )
    assert openai.image_fetches == 0
    await store.close()
