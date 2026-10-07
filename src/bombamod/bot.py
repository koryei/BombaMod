"""Discord client, commands, and enforcement adapter."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from bombamod.config import Settings
from bombamod.moderator import CaseRegistry
from bombamod.providers import OpenAIProvider, OpenRouterProvider
from bombamod.schemas import Action, Decision
from bombamod.service import ModerationService
from bombamod.storage import Store

logger = logging.getLogger(__name__)


class DiscordActionSink:
    def __init__(self, bot: BombaModBot) -> None:
        self.bot = bot        async def apply(
        self,
        message_id: int,
        guild_id: int,
        channel_id: int,
        user_id: int,
        decision: Decision,
    ) -> bool:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return False
        try:
            channel = guild.get_channel(channel_id)
            if channel is None:
                channel = self.bot.get_channel(channel_id)
            if not isinstance(channel, discord.abc.Messageable):
                return False
            message = await channel.fetch_message(message_id)
            if (
                message.guild is None
                or message.guild.id != guild_id
                or message.author.id != user_id
            ):
                return False
            member = message.author
            if not isinstance(member, discord.Member) or member.bot:
                return False
            me = guild.me
            if me is None or member.top_role >= me.top_role or member.id == guild.owner_id:
                return False

            if decision.action == Action.DELETE:
                if not channel.permissions_for(me).manage_messages:
                    return False
                await message.delete(reason=f"BombaMod moderation: {decision.reason[:350]}")
            elif decision.action == Action.WARN:
                if not channel.permissions_for(me).send_messages:
                    return False
                await channel.send(
                    f"{member.mention} Please review the server rules. If you believe this was a mistake, contact a moderator.",
                    delete_after=20,
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
            elif decision.action == Action.TIMEOUT:
                if not me.guild_permissions.moderate_members:
                    return False
                minutes = min(max(decision.timeout_minutes, 1), 40_320)
                await member.timeout(
                    timedelta(minutes=minutes),
                    reason=f"BombaMod moderation: {decision.reason[:300]}",
                )
                if not message.deleted and channel.permissions_for(guild.me).manage_messages:
                    await message.delete(reason="BombaMod timed out this member")
            elif decision.action == Action.BAN:
                if not guild.me.guild_permissions.ban_members:
                    return False
                if member.top_role >= guild.me.top_role or member.id == guild.owner_id:
                    return False
                await guild.ban(
                    member,
                    reason=f"BombaMod moderation: {decision.reason[:300]}",
                    delete_message_seconds=0,
                )
            else:
                return False
            await self._audit(guild, message.channel, member, decision)
            return True
        except (discord.Forbidden, discord.NotFound, discord.HTTPException, AttributeError):
            return False

    async def _find_message(
        self, guild: discord.Guild, message_id: int
    ) -> tuple[discord.abc.Messageable, discord.Message] | None:
        for channel in guild.text_channels:
            try:
                message = await channel.fetch_message(message_id)
                return channel, message
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                continue
        return None

    async def _audit(
        self,
        guild: discord.Guild,
        source: discord.abc.Messageable,
        member: discord.Member,
        decision: Decision,
    ) -> None:
        policy = await self.bot.store.policy(guild.id)
        channel = guild.get_channel(policy.review_channel_id) if policy.review_channel_id else None
        target = channel if isinstance(channel, discord.abc.Messageable) else source
        if isinstance(target, discord.abc.Messageable):
            await target.send(
                f"BombaMod action: **{decision.action.value}** for <@{member.id}>. "
                f"Reason: {decision.reason[:450]}",
                allowed_mentions=discord.AllowedMentions.none(),
            )


class BombaModBot(commands.Bot):
    def __init__(self, settings: Settings, store: Store) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guild_messages = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.settings = settings
        self.store = store
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_moderation)
        self.cases = CaseRegistry()
        self.http = None
        self.moderation_service: ModerationService | None = None

    def get_channel_for_message(self, message_id: int) -> discord.abc.Messageable | None:
        # Message IDs do not encode the channel ID. The action sink will use guild lookup
        # if this fast-path does not have a channel; the service records cases below.
        return None

    async def setup_hook(self) -> None:
        import httpx

        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(self.settings.http_timeout_seconds),
            limits=httpx.Limits(max_connections=self.settings.max_concurrent_moderation + 2),
            follow_redirects=False,
        )
        openai = OpenAIProvider(
            self.settings.openai_api_key,
            self.http,
            max_image_bytes=self.settings.max_image_bytes,
        )
        openrouter = OpenRouterProvider(
            self.settings.openrouter_api_key,
            self.settings.openrouter_model,
            self.http,
        )
        self.moderation_service = ModerationService(
            store=self.store,
            openai=openai,
            openrouter=openrouter,
            alerts=self,
            actions=DiscordActionSink(self),
            semaphore=self.semaphore,
            allow_openrouter_text=self.settings.allow_openrouter_text,
            case_registry=self.cases,
        )
        await self.store.initialize()
        if self.settings.discord_guild_id:
            guild_object = discord.Object(id=self.settings.discord_guild_id)
            self.tree.copy_global_to(guild=guild_object)
            await self.tree.sync(guild=guild_object)
        else:
            await self.tree.sync()

    async def close(self) -> None:
        if self.http is not None:
            await self.http.aclose()
        await self.store.close()
        await super().close()

    async def alert(
        self,
        guild_id: int,
        channel_id: int,
        reason: str,
        case_url: str | None = None,
    ) -> None:
        channel = self.get_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            guild = self.get_guild(guild_id)
            channel = guild.system_channel if guild else None
        if isinstance(channel, discord.abc.Messageable):
            try:
                case_link = f"\nCase: {case_url}" if case_url else ""
                await channel.send(
                    f"⚠️ BombaMod needs moderator review: {reason[:400]}{case_link}",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                logger.warning("Could not send a moderator review alert")

    async def on_ready(self) -> None:
        logger.info("BombaMod connected (guilds=%s)", len(self.guilds))

    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        policy = await self.store.policy(message.guild.id)
        if not policy.moderation_enabled:
            return
        if policy.monitored_channel_ids and message.channel.id not in policy.monitored_channel_ids:
            return
        # Only Discord-hosted image attachments are eligible for bounded scanning.
        image_urls: list[str] = []
        if policy.image_scan_enabled:
            for attachment in message.attachments[:4]:
                if attachment.content_type and attachment.content_type.lower().startswith("image/"):
                    image_urls.append(attachment.url)
        service = self.moderation_service
        if service is None:
            return
        await service.moderate(
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            message_id=message.id,
            user_id=message.author.id,
            content=message.content,
            image_urls=image_urls,
        )

    async def _admin(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False
        return (
            interaction.user.guild_permissions.administrator
            or interaction.user.guild_permissions.manage_guild
        )

    async def _moderator(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False
        permissions = interaction.user.guild_permissions
        return await self._admin(interaction) or permissions.manage_messages or permissions.moderate_members

    async def _require_admin(self, interaction: discord.Interaction) -> bool:
        if await self.bot._admin(interaction):
            return True
        await interaction.response.send_message(
            "This setting requires Manage Server or Administrator.", ephemeral=True
        )
        return False


# Slash commands are deliberately grouped behind /bm.
class BombaModCommands(app_commands.Group):
    def __init__(self, bot: BombaModBot) -> None:
        super().__init__(name="bm", description="Configure BombaMod AI moderation")
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if await self.bot._moderator(interaction):
            return True
        await interaction.response.send_message(
            "You need moderator permissions to use BombaMod commands.", ephemeral=True
        )
        return False

    @app_commands.command(
        name="setup", description="Enable BombaMod and choose channels to monitor"
    )
    @app_commands.describe(
        channels="Optional comma-separated channel IDs; blank monitors all channels"
    )
    async def setup(self, interaction: discord.Interaction, channels: str = "") -> None:
        if not await self.bot._require_admin(interaction):
            return
        if interaction.guild_id is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        channel_ids: list[int] = []
        try:
            channel_ids = [
                int(item.strip().strip("<#>")) for item in channels.split(",") if item.strip()
            ]
        except ValueError:
            await interaction.response.send_message(
                "Channels must be valid channel IDs or mentions.", ephemeral=True
            )
            return
        if len(channel_ids) > 100 or any(value <= 0 for value in channel_ids):
            await interaction.response.send_message(
                "Provide at most 100 valid channels.", ephemeral=True
            )
            return
        await self.bot.store.update_policy(
            interaction.guild_id,
            moderation_enabled=True,
            monitored_channel_ids=channel_ids,
        )
        await interaction.response.send_message(
            "BombaMod is enabled. Add policy with `/bm rules`; automatic actions, image scanning, and OpenRouter text sharing remain off until separately enabled.",
            ephemeral=True,
        )

    @app_commands.command(
        name="rules", description="Set moderator-authored policy rules for BombaMod"
    )
    @app_commands.describe(text="Server rules and enforcement guidance (up to 6000 characters)")
    async def rules(self, interaction: discord.Interaction, text: str) -> None:
        if interaction.guild_id is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        if not (
            interaction.user.guild_permissions.manage_messages
            or await self.bot._admin(interaction)
        ):
            await interaction.response.send_message(
                "Setting server rules requires Manage Messages or Manage Server.", ephemeral=True
            )
            return
        if len(text.strip()) < 10 or len(text) > 6000:
            await interaction.response.send_message(
                "Rules must be 10-6000 characters and used in a server.", ephemeral=True
            )
            return
        await self.bot.store.update_policy(interaction.guild_id, rules=text.strip())
        await interaction.response.send_message(
            "Policy saved. This supplies context to the AI at decision time; it does not train or update model weights.",
            ephemeral=True,
        )

    @app_commands.command(name="enforcement", description="Configure autonomous action limits")
    @app_commands.describe(
        enabled="Allow validated model actions (otherwise cases are sent for review)",
        max_timeout_minutes="Maximum timeout from 1 to 40320 minutes",
        min_confidence="Minimum confidence from 0 to 1 required for an automatic action",
        allow_bans="Explicit opt-in to AI-issued bans (disabled by default)",
    )
    async def enforcement(
        self,
        interaction: discord.Interaction,
        enabled: bool,
        max_timeout_minutes: app_commands.Range[int, 1, 40320] = 1440,
        min_confidence: app_commands.Range[float, 0.0, 1.0] = 0.80,
        allow_bans: bool = False,
    ) -> None:
        if not await self.bot._require_admin(interaction):
            return
        if interaction.guild_id is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        await self.bot.store.update_policy(
            interaction.guild_id,
            auto_actions_enabled=enabled,
            max_timeout_minutes=max_timeout_minutes,
            min_confidence=float(min_confidence),
            ban_opt_in=allow_bans,
        )
        await interaction.response.send_message(
            f"Automatic actions {'enabled' if enabled else 'disabled'}; max timeout {max_timeout_minutes} minutes; "
            f"minimum confidence {min_confidence:.2f}; bans {'explicitly enabled' if allow_bans else 'disabled'}.",
            ephemeral=True,
        )

    @app_commands.command(
        name="privacy", description="Opt in to optional model text/image processing"
    )
    @app_commands.describe(
        share_text="Send best-effort-redacted flagged text to OpenRouter (residual personal data may remain)",
        scan_images="Send eligible image attachments to OpenAI Omni Moderation",
    )    async def privacy(self, interaction: discord.Interaction, share_text: bool, scan_images: bool) -> None:
        if not await self.bot._require_admin(interaction):
            return
        if interaction.guild_id is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        await self.bot.store.update_policy(
            interaction.guild_id,
            openrouter_text_opt_in=share_text,
            image_scan_enabled=scan_images,
        )
        text_state = "enabled" if share_text else "disabled"
        image_state = "enabled" if scan_images else "disabled"
        await interaction.response.send_message(
            f"OpenRouter flagged-text sharing: {text_state}. Best-effort redaction is not guaranteed to remove all personal data. "
            f"OpenAI image scanning: {image_state}. Never send known or suspected CSAM; the API is not a child-safety detector.",
            ephemeral=True,
        )

    @app_commands.command(
        name="review-channel", description="Set the moderator review/audit channel"
    )
    async def review_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None = None
    ) -> None:
        if not await self.bot._require_admin(interaction):
            return
        if interaction.guild_id is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        await self.bot.store.update_policy(
            interaction.guild_id,
            review_channel_id=channel.id if channel else None,
        )
        await interaction.response.send_message(
            f"Review channel set to {channel.mention}."
            if channel
            else "Review channel cleared; alerts use the message channel.",
            ephemeral=True,
        )

    @app_commands.command(name="feedback", description="Label a recent case for offline evaluation")
    @app_commands.describe(
        message_id="ID of a recent moderated message",
        corrected_action="Your corrected policy label",
    )
    @app_commands.choices(
        corrected_action=[
            app_commands.Choice(name=value.value, value=value.value) for value in Action
        ]
    )
    async def feedback(
        self,
        interaction: discord.Interaction,
        message_id: str,
        corrected_action: app_commands.Choice[str],
    ) -> None:
        if interaction.guild_id is None or not message_id.isdigit():
            await interaction.response.send_message(
                "Provide a valid recent case message ID in a server.", ephemeral=True
            )
            return
        case = self.bot.cases.get(int(message_id))
        if case is None or case.guild_id != interaction.guild_id:
            await interaction.response.send_message(
                "Case metadata is unavailable (cases are memory-only and expire on restart). Feedback was not saved.",
                ephemeral=True,
            )
            return
        moderator_id = interaction.user.id
        await self.bot.store.save_feedback(
            interaction.guild_id,
            moderator_id,
            case.message_id,
            corrected_action.value,
            list(case.categories),
        )
        await interaction.response.send_message(
            "Feedback label saved without message text. It supports offline evaluation, not automatic model training.",
            ephemeral=True,
        )

    @app_commands.command(name="status", description="Show BombaMod settings and privacy state")
    async def status(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        policy = await self.bot.store.policy(interaction.guild_id)
        await interaction.response.send_message(
            "BombaMod configuration\n"
            f"Moderation enabled: {policy.moderation_enabled}\n"
            f"Monitored channels: {len(policy.monitored_channel_ids) or 'all'}\n"
            f"Rules configured: {bool(policy.rules)}\n"
            f"Autonomous actions: {policy.auto_actions_enabled}\n"
            f"Bans: {policy.ban_opt_in}\n"
            f"OpenRouter flagged-text sharing: {policy.openrouter_text_opt_in}\n"
            f"OpenAI image scanning: {policy.image_scan_enabled}\n"
            f"Max timeout: {policy.max_timeout_minutes} minutes\n"
            f"Minimum confidence: {policy.min_confidence:.2f}",
            ephemeral=True,
        )


def install_commands(bot: BombaModBot) -> None:
    bot.tree.add_command(BombaModCommands(bot))
