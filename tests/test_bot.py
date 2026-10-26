from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bombamod.bot import BombaModBot, BombaModCommands, DiscordActionSink, install_commands
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


@pytest.mark.asyncio
async def test_spam_deletes_share_one_bulk_request(settings: Settings, monkeypatch) -> None:
    bot = BombaModBot(settings, DummyStore())  # type: ignore[arg-type]
    sink = DiscordActionSink(bot)
    monkeypatch.setattr("bombamod.bot.DELETE_BATCH_SECONDS", 0)
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 5
    channel.delete_messages = AsyncMock()
    messages = [MagicMock(spec=discord.Message) for _ in range(8)]

    results = await asyncio.gather(*(sink._delete(channel, m) for m in messages))

    assert all(results)
    channel.delete_messages.assert_awaited_once()
    assert len(channel.delete_messages.await_args.args[0]) == 8
    for message in messages:
        message.delete.assert_not_called()


@pytest.mark.asyncio
async def test_failed_bulk_delete_falls_back_to_single_deletes(
    settings: Settings, monkeypatch
) -> None:
    bot = BombaModBot(settings, DummyStore())  # type: ignore[arg-type]
    sink = DiscordActionSink(bot)
    monkeypatch.setattr("bombamod.bot.DELETE_BATCH_SECONDS", 0)
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 5
    response = SimpleNamespace(status=404, reason="missing", text="", headers={})
    channel.delete_messages = AsyncMock(side_effect=discord.NotFound(response, "gone"))
    first, second = MagicMock(spec=discord.Message), MagicMock(spec=discord.Message)
    first.delete = AsyncMock(side_effect=discord.NotFound(response, "gone"))
    second.delete = AsyncMock()

    results = await asyncio.gather(sink._delete(channel, first), sink._delete(channel, second))

    assert results == [False, True]


@pytest.mark.asyncio
async def test_punished_user_gets_one_dm_per_cooldown(settings: Settings) -> None:
    bot = BombaModBot(settings, DummyStore())  # type: ignore[arg-type]
    sink = DiscordActionSink(bot)
    guild = MagicMock(spec=discord.Guild)
    guild.id, guild.name = 1, "Test Server"
    member = MagicMock(spec=discord.Member)
    member.id = 2
    member.send = AsyncMock()
    decision = Decision(Action.DELETE, "flagged for hateful content", confidence=0.99)

    for _ in range(5):
        await sink._notify(guild, member, decision, "Your message was removed")

    member.send.assert_awaited_once()
    text = member.send.await_args.args[0]
    assert "Test Server" in text and "hateful content" in text


@pytest.mark.asyncio
async def test_dm_failure_is_swallowed(settings: Settings) -> None:
    bot = BombaModBot(settings, DummyStore())  # type: ignore[arg-type]
    sink = DiscordActionSink(bot)
    guild = MagicMock(spec=discord.Guild)
    guild.id, guild.name = 1, "Test Server"
    member = MagicMock(spec=discord.Member)
    member.id = 2
    response = SimpleNamespace(status=403, reason="closed", text="", headers={})
    member.send = AsyncMock(side_effect=discord.Forbidden(response, "closed"))

    await sink._notify(guild, member, Decision(Action.DELETE, "x", confidence=1), "Removed")


@pytest.mark.asyncio
async def test_bad_guild_id_falls_back_to_global_sync(settings: Settings, monkeypatch) -> None:
    from dataclasses import replace

    bot = BombaModBot(replace(settings, discord_guild_id=123), DummyStore())  # type: ignore[arg-type]
    response = SimpleNamespace(status=403, reason="forbidden", text="", headers={})
    calls: list[object] = []

    async def fake_sync(*, guild: object = None) -> list[object]:
        calls.append(guild)
        if guild is not None:
            raise discord.Forbidden(response, "Missing Access")
        return []

    monkeypatch.setattr(bot.tree, "sync", fake_sync)

    await bot._sync_commands()

    assert len(calls) == 2 and calls[0] is not None and calls[1] is None


@pytest.mark.asyncio
async def test_global_mode_removes_leftover_guild_commands(settings: Settings, monkeypatch) -> None:
    bot = BombaModBot(settings, DummyStore())  # type: ignore[arg-type]
    stale, clean = MagicMock(spec=discord.Guild), MagicMock(spec=discord.Guild)
    stale.id, clean.id = 1, 2
    monkeypatch.setattr(BombaModBot, "guilds", property(lambda self: [stale, clean]))
    monkeypatch.setattr(
        bot.tree,
        "fetch_commands",
        AsyncMock(side_effect=lambda guild=None: [object()] if guild is stale else []),
    )
    synced: list[object] = []

    async def fake_sync(*, guild: object = None) -> list[object]:
        synced.append(guild)
        return []

    monkeypatch.setattr(bot.tree, "sync", fake_sync)

    await bot._prune_duplicate_commands()

    assert synced == [stale]


@pytest.mark.asyncio
async def test_guild_mode_removes_leftover_global_commands(settings: Settings, monkeypatch) -> None:
    bot = BombaModBot(settings, DummyStore())  # type: ignore[arg-type]
    bot._commands_scope = "guild"
    monkeypatch.setattr(bot.tree, "fetch_commands", AsyncMock(return_value=[object()]))
    synced: list[object] = []

    async def fake_sync(*, guild: object = None) -> list[object]:
        synced.append(guild)
        return []

    monkeypatch.setattr(bot.tree, "sync", fake_sync)

    await bot._prune_duplicate_commands()

    assert synced == [None]


@pytest.mark.asyncio
async def test_fallback_to_global_keeps_global_commands(settings: Settings, monkeypatch) -> None:
    from dataclasses import replace

    bot = BombaModBot(replace(settings, discord_guild_id=123), DummyStore())  # type: ignore[arg-type]
    response = SimpleNamespace(status=403, reason="forbidden", text="", headers={})

    async def fake_sync(*, guild: object = None) -> list[object]:
        if guild is not None:
            raise discord.Forbidden(response, "Missing Access")
        return []

    monkeypatch.setattr(bot.tree, "sync", fake_sync)
    fetch = AsyncMock(return_value=[object()])
    monkeypatch.setattr(bot.tree, "fetch_commands", fetch)
    monkeypatch.setattr(BombaModBot, "guilds", property(lambda self: []))

    await bot._sync_commands()
    await bot._prune_duplicate_commands()

    assert bot._commands_scope == "global"


@pytest.mark.asyncio
async def test_audit_notice_includes_excerpt_when_content_given(settings: Settings) -> None:
    class Store:
        async def policy(self, guild_id: int) -> SimpleNamespace:
            return SimpleNamespace(review_channel_id=88)

        async def close(self) -> None:
            return None

    bot = BombaModBot(settings, Store())  # type: ignore[arg-type]
    bot.alert = AsyncMock()  # type: ignore[method-assign]
    sink = DiscordActionSink(bot)
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1
    member = MagicMock(spec=discord.Member)
    member.id = 2
    decision = Decision(Action.DELETE, "flagged for hateful content", confidence=0.99)

    await sink._audit(guild, member, decision, case_url="https://x/1", content="bad  words\nhere")
    await sink._audit(guild, member, decision, case_url="https://x/1")

    with_text, without_text = (call.args[2] for call in bot.alert.await_args_list)
    assert with_text.endswith("`bad words here`")
    assert "`" not in without_text
