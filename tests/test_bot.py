from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bombamod.bot import BombaModBot, BombaModCommands, install_commands
from bombamod.config import Settings
from bombamod.schemas import Action, Decision, ModerationResult


class DummyStore:
    async def close(self) -> None:
        return None


@pytest.fixture
def settings() -> Settings:
    return Settings(
        discord_token="token",
        openai_api_key="openai",
        openrouter_api_key="router",
        openrouter_model="nvidia/nemotron-3-super-120b-a12b:free",
        openrouter_base_url="https://openrouter.ai/api/v1",
        database_url="sqlite+aiosqlite:///:memory:",
        discord_guild_id=None,
        max_concurrent_moderation=1,
        http_timeout_seconds=12,
        max_image_bytes=8000000,
        max_message_chars=6000,
        image_scan_enabled_by_default=False,
        allow_openrouter_text=False,
        disable_openai_text=False,
        log_level="INFO",
    )


def test_slash_command_tree_has_management_and_feedback_commands(settings: Settings) -> None:
    bot = BombaModBot(settings, DummyStore())  # type: ignore[arg-type]
    install_commands(bot)
    group = bot.tree.get_command("bm")
    assert group is not None
    names = {command.name for command in group.commands}
    assert {
        "setup",
        "rules",
        "enforcement",
        "privacy",
        "review-channel",
        "feedback",
        "export-feedback",
        "retention",
        "status",
        "pause",
    } <= names
    assert bot.intents.message_content
    assert bot.intents.guild_messages
    assert not bot.intents.members


def test_message_fingerprint_changes_when_source_is_edited(settings: Settings) -> None:
    bot = BombaModBot(settings, DummyStore())  # type: ignore[arg-type]
    first = SimpleNamespace(content="original", attachments=[SimpleNamespace(id=100, size=12)])
    edited = SimpleNamespace(content="edited", attachments=[SimpleNamespace(id=100, size=12)])
    reuploaded = SimpleNamespace(content="original", attachments=[SimpleNamespace(id=101, size=12)])
    assert bot.message_fingerprint(first) != bot.message_fingerprint(edited)
    assert bot.message_fingerprint(first) != bot.message_fingerprint(reuploaded)


def test_action_uses_the_original_message_snapshot(settings: Settings) -> None:
    bot = BombaModBot(settings, DummyStore())  # type: ignore[arg-type]
    message = SimpleNamespace(content="original", attachments=[])
    fingerprint = bot.message_fingerprint(message)
    message.content = "edited after AI review"
    assert bot.message_fingerprint(message) != fingerprint


@pytest.mark.asyncio
async def test_moderator_requires_actual_permission_not_role_id(settings: Settings) -> None:
    bot = BombaModBot(settings, DummyStore())  # type: ignore[arg-type]

    class Member:
        guild_permissions = SimpleNamespace(
            administrator=False, manage_guild=False, manage_messages=False, moderate_members=False
        )
        roles = [
            SimpleNamespace(
                permissions=SimpleNamespace(manage_messages=False, moderate_members=False)
            )
        ]

    interaction = SimpleNamespace(guild=object(), user=Member())
    assert not await bot._moderator(interaction)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_bot_close_handles_unstarted_workers(settings: Settings) -> None:
    bot = BombaModBot(settings, DummyStore())  # type: ignore[arg-type]
    await bot.close()


@pytest.mark.asyncio
async def test_feedback_only_accepts_known_same_guild_case(settings: Settings) -> None:
    class FeedbackStore(DummyStore):
        saved: list[tuple[object, ...]]

        def __init__(self) -> None:
            self.saved = []

        async def policy(self, guild_id: int) -> object:
            return SimpleNamespace(feedback_retention_days=90)

        async def save_feedback(self, *args: object, **kwargs: object) -> None:
            self.saved.append((*args, kwargs))

    store = FeedbackStore()
    bot = BombaModBot(settings, store)  # type: ignore[arg-type]
    bot._moderator = AsyncMock(return_value=True)  # type: ignore[method-assign]
    group = BombaModCommands(bot)
    command = group.get_command("feedback")
    assert command is not None
    response = SimpleNamespace(send_message=AsyncMock())
    interaction = SimpleNamespace(
        guild_id=123,
        user=SimpleNamespace(id=456),
        response=response,
    )
    await command.callback(
        group,
        interaction,
        "789",
        discord.app_commands.Choice(name="allow", value="allow"),
    )
    assert not store.saved
    assert "unavailable or expired" in response.send_message.await_args.args[0]

    bot.cases.add(
        message_id=789,
        guild_id=123,
        user_id=456,
        channel_id=321,
        result=ModerationResult(True, {"harassment": True}, {}),
        decision=Decision(Action.DELETE, "Flagged", confidence=0.99),
    )
    await command.callback(
        group,
        interaction,
        "789",
        discord.app_commands.Choice(name="allow", value="allow"),
    )
    assert len(store.saved) == 1
    assert store.saved[0][0:5] == (123, 456, 789, "allow", "delete")
    assert store.saved[0][5] == ["harassment"]


@pytest.mark.asyncio
async def test_alert_delivery_retries_transient_discord_failure(
    settings: Settings, monkeypatch
) -> None:
    bot = BombaModBot(settings, DummyStore())  # type: ignore[arg-type]
    channel = MagicMock(spec=discord.TextChannel)
    failure_response = SimpleNamespace(status=500, reason="server error", text="error", headers={})
    channel.send = AsyncMock(
        side_effect=[discord.HTTPException(failure_response, "temporary"), None]
    )
    monkeypatch.setattr(bot, "get_channel", lambda channel_id: channel)
    monkeypatch.setattr("bombamod.bot.asyncio.sleep", AsyncMock(return_value=None))
    bot._pending_alerts[12] = [("manual review", "https://discord.com/channels/12/2/3")]
    bot._alert_channels[12] = 88
    guild = MagicMock(spec=discord.Guild)
    guild.id = 12
    guild.me = object()
    channel.guild = guild
    channel.permissions_for.return_value.view_channel = True
    channel.permissions_for.return_value.send_messages = True
    default_role = object()
    guild.default_role = default_role
    everyone_permissions = SimpleNamespace(view_channel=False)
    channel.permissions_for.side_effect = lambda subject: (
        everyone_permissions
        if subject is default_role
        else SimpleNamespace(view_channel=True, send_messages=True)
    )
    monkeypatch.setattr(bot, "get_guild", lambda guild_id: guild)

    await bot._flush_alerts(12)

    assert channel.send.await_count == 2
    assert "manual review" in channel.send.await_args.args[0]
    assert 12 not in bot._pending_alerts
    assert 12 not in bot._alert_tasks


@pytest.mark.asyncio
async def test_alert_refuses_public_channel(settings: Settings, monkeypatch) -> None:
    bot = BombaModBot(settings, DummyStore())  # type: ignore[arg-type]
    channel = MagicMock(spec=discord.TextChannel)
    guild = MagicMock(spec=discord.Guild)
    guild.id = 12
    guild.me = object()
    guild.default_role = object()
    channel.guild = guild
    channel.permissions_for.side_effect = lambda subject: SimpleNamespace(
        view_channel=True, send_messages=True
    )
    monkeypatch.setattr(bot, "get_channel", lambda channel_id: channel)
    monkeypatch.setattr(bot, "get_guild", lambda guild_id: guild)
    monkeypatch.setattr(bot, "fetch_channel", AsyncMock(return_value=channel))
    monkeypatch.setattr("bombamod.bot.asyncio.sleep", AsyncMock(return_value=None))
    bot._pending_alerts[12] = [("private details", None)]
    bot._alert_channels[12] = 88
    bot._alert_tasks[12] = AsyncMock()

    await bot._flush_alerts(12)

    channel.send.assert_not_awaited()
    assert bot._pending_alerts[12] == [("private details", None)]
    bot._alert_tasks.clear()
    bot._alert_channels[12] = 89
    guild.default_role = object()
    default_role = guild.default_role
    channel.permissions_for.side_effect = lambda subject: (
        SimpleNamespace(view_channel=False, send_messages=True)
        if subject is default_role
        else SimpleNamespace(view_channel=True, send_messages=True)
    )
    bot._start_alert_flush(12)
    task = bot._alert_tasks[12]
    await task
    channel.send.assert_awaited_once()
    assert 12 not in bot._pending_alerts
    assert 12 not in bot._alert_tasks
