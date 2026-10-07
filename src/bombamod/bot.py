"""Discord client, commands, and enforcement adapter."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import logging
from datetime import timedelta

import discord
import httpx
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
        self.bot = bot

    async def apply(
        self,
        message_id: int,
        guild_id: int,
        channel_id: int,
        user_id: int,
        source_fingerprint: str,
        decision: Decision,
    ) -> bool:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return False
        try:
            policy = await self.bot.store.policy(guild_id)
            review_channel = (
                guild.get_channel(policy.review_channel_id)
                if policy.review_channel_id is not None
                else None
            )
            if not isinstance(review_channel, discord.TextChannel):
                return False
            if not self.bot._is_private_review_channel(guild, review_channel):
                return False
            channel = (
                guild.get_channel(channel_id)
                or guild.get_thread(channel_id)
                or self.bot.get_channel(channel_id)
            )
            if not isinstance(channel, discord.abc.Messageable):
                return False
            message = await channel.fetch_message(message_id)
            if message.guild is None or message.guild.id != guild_id:
                return False
            if message.author.id != user_id or not isinstance(message.author, discord.Member):
                return False
            if self.bot.message_fingerprint(message) != source_fingerprint:
                return False
            member = message.author
            me = guild.me
            if me is None or member.bot or member.id == guild.owner_id:
                return False
            if member.top_role >= me.top_role:
                return False

            if decision.action == Action.DELETE:
                if not channel.permissions_for(me).manage_messages:
                    return False
                await message.delete()
            elif decision.action == Action.WARN:
                if not channel.permissions_for(me).send_messages:
                    return False
                await channel.send(
                    f"{member.mention} Review the server rules; contact a moderator if mistaken.",
                    delete_after=20,
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
            elif decision.action == Action.TIMEOUT:
                if member.guild_permissions.administrator:
                    return False
                if not me.guild_permissions.moderate_members:
                    return False
                minutes = min(max(decision.timeout_minutes, 1), 40_320)
                await member.timeout(
                    timedelta(minutes=minutes),
                    reason=f"BombaMod moderation: {decision.reason[:300]}",
                )
                if channel.permissions_for(me).manage_messages:
                    await message.delete()
            elif decision.action == Action.BAN:
                if member.guild_permissions.administrator:
                    return False
                if not me.guild_permissions.ban_members:
                    return False
                await guild.ban(
                    member,
                    reason=f"BombaMod moderation: {decision.reason[:300]}",
                    delete_message_seconds=0,
                )
            else:
                return False

            try:
                await self._audit(
                    guild,
                    member,
                    decision,
                    case_url=f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}",
                )
            except discord.HTTPException:
                logger.warning("Moderation action succeeded but audit notice could not be sent")
            return True
        except (discord.Forbidden, discord.NotFound, discord.HTTPException, AttributeError):
            return False

    async def _audit(
        self,
        guild: discord.Guild,
        member: discord.Member,
        decision: Decision,
        *,
        case_url: str,
    ) -> None:
        policy = await self.bot.store.policy(guild.id)
        channel = guild.get_channel(policy.review_channel_id) if policy.review_channel_id else None
        if isinstance(channel, discord.TextChannel) and self.bot._is_private_review_channel(
            guild, channel
        ):
            await channel.send(
                f"BombaMod action: **{decision.action.value}** for <@{member.id}>. "
                f"Reason: {decision.reason[:300]}. Case: <{case_url}>",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await self.bot.alert(
                guild.id,
                policy.review_channel_id or 0,
                f"Action {decision.action.value} completed for member ID {member.id}.",
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
        self.provider_http: httpx.AsyncClient | None = None
        self.moderation_service: ModerationService | None = None
        self._moderation_queue: asyncio.Queue[tuple[discord.Message, str]] = asyncio.Queue(
            maxsize=max(8, settings.max_concurrent_moderation * 4)
        )
        self._worker_tasks: list[asyncio.Task[None]] = []
        self._pending_alerts: dict[int, list[tuple[str, str | None]]] = {}
        self._alert_channels: dict[int, int] = {}
        self._alert_tasks: dict[int, asyncio.Task[None]] = {}
        self._dropped_alert_counts: dict[int, int] = {}

    async def setup_hook(self) -> None:
        self.provider_http = httpx.AsyncClient(
            timeout=httpx.Timeout(self.settings.http_timeout_seconds),
            limits=httpx.Limits(max_connections=self.settings.max_concurrent_moderation + 2),
            follow_redirects=False,
        )
        http = self.provider_http
        if http is None:
            raise RuntimeError("Provider HTTP client was not initialized")
        openai = OpenAIProvider(
            self.settings.openai_api_key,
            http,
            max_image_bytes=self.settings.max_image_bytes,
        )
        openrouter = OpenRouterProvider(
            self.settings.openrouter_api_key,
            self.settings.openrouter_model,
            http,
        )
        self.moderation_service = ModerationService(
            store=self.store,
            openai=openai,
            openrouter=openrouter,
            alerts=self,
            actions=DiscordActionSink(self),
            semaphore=self.semaphore,
            allow_openrouter_text=self.settings.allow_openrouter_text,
            disable_openai_text=self.settings.disable_openai_text,
            max_message_chars=self.settings.max_message_chars,
            max_image_bytes=self.settings.max_image_bytes,
            case_registry=self.cases,
        )
        await self.store.initialize()
        self._worker_tasks = [
            asyncio.create_task(self._moderation_worker())
            for _ in range(self.settings.max_concurrent_moderation)
        ]
        if self.settings.discord_guild_id:
            guild_object = discord.Object(id=self.settings.discord_guild_id)
            self.tree.copy_global_to(guild=guild_object)
            await self.tree.sync(guild=guild_object)
        else:
            await self.tree.sync()

    async def close(self) -> None:
        for task in self._worker_tasks:
            task.cancel()
        if self._worker_tasks:
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        for task in self._alert_tasks.values():
            task.cancel()
        if self._alert_tasks:
            await asyncio.gather(*self._alert_tasks.values(), return_exceptions=True)
        if self.provider_http is not None:
            await self.provider_http.aclose()
        await self.store.close()
        await super().close()

    @staticmethod
    def _is_private_review_channel(guild: discord.Guild, channel: object) -> bool:
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id:
            return False
        me = guild.me
        if me is None:
            return False
        bot_permissions = channel.permissions_for(me)
        everyone_permissions = channel.permissions_for(guild.default_role)
        return (
            bot_permissions.view_channel
            and bot_permissions.send_messages
            and not everyone_permissions.view_channel
        )

    async def alert(
        self,
        guild_id: int,
        channel_id: int,
        reason: str,
        case_url: str | None = None,
    ) -> None:
        """Batch review notices while bounding memory and reporting any grouped overflow."""
        if channel_id <= 0:
            logger.error("Moderator alert has no private review-channel destination")
            return
        pending = self._pending_alerts.setdefault(guild_id, [])
        if len(pending) < 32:
            pending.append((reason[:250], case_url))
        else:
            self._dropped_alert_counts[guild_id] = self._dropped_alert_counts.get(guild_id, 0) + 1
        self._alert_channels[guild_id] = channel_id
        if guild_id not in self._alert_tasks:
            self._alert_tasks[guild_id] = asyncio.create_task(self._flush_alerts(guild_id))

    async def _flush_alerts(self, guild_id: int) -> None:
        attempt = 0
        batch_channel_id = 0
        while True:
            await asyncio.sleep(1 if attempt == 0 else min(2**attempt, 30))
            items = self._pending_alerts.pop(guild_id, [])
            channel_id = self._alert_channels.pop(guild_id, 0)
            dropped = self._dropped_alert_counts.pop(guild_id, 0)
            if not items and not dropped:
                self._alert_tasks.pop(guild_id, None)
                return
            if channel_id > 0:
                batch_channel_id = channel_id
            elif batch_channel_id > 0:
                channel_id = batch_channel_id

            guild = self.get_guild(guild_id)
            channel = self.get_channel(channel_id)
            if guild is None or not self._is_private_review_channel(guild, channel):
                try:
                    fetched = await self.fetch_channel(channel_id)
                    channel = fetched if isinstance(fetched, discord.TextChannel) else None
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    channel = None
            private_channel = (
                channel
                if guild is not None
                and isinstance(channel, discord.TextChannel)
                and self._is_private_review_channel(guild, channel)
                else None
            )
            if private_channel is None:
                self._restore_alert_batch(guild_id, batch_channel_id, items, dropped)
                self._alert_tasks.pop(guild_id, None)
                logger.error("Private moderator alert channel is unavailable; review configuration")
                return
            lines = ["⚠️ **BombaMod moderator review queue**"]
            for alert_reason, case_url in items[:12]:
                line = f"• {alert_reason}"
                if case_url:
                    line += f" — <{case_url}>"
                lines.append(line[:250])
            grouped = dropped + max(0, len(items) - 12)
            if grouped:
                lines.append(f"• {grouped} additional cases were grouped; inspect recent messages.")
            failed = False
            try:
                await private_channel.send(
                    "\n".join(lines)[:1900],
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                logger.error("Could not deliver a private moderator review queue")
                failed = True

            if failed:
                self._restore_alert_batch(guild_id, batch_channel_id, items, dropped)
                attempt += 1
                if attempt == 1 or attempt % 5 == 0:
                    logger.error(
                        "Moderator alert delivery is failing; queued cases are retained for retry"
                    )
                continue

            attempt = 0
            if self._pending_alerts.get(guild_id) or self._dropped_alert_counts.get(guild_id):
                continue
            self._alert_tasks.pop(guild_id, None)
            return

    def _restore_alert_batch(
        self,
        guild_id: int,
        channel_id: int,
        items: list[tuple[str, str | None]],
        dropped: int,
    ) -> None:
        """Retain failed notices ahead of newer ones without allowing unbounded growth."""
        pending = items + self._pending_alerts.get(guild_id, [])
        overflow = max(0, len(pending) - 32)
        self._pending_alerts[guild_id] = pending[:32]
        self._dropped_alert_counts[guild_id] = (
            self._dropped_alert_counts.get(guild_id, 0) + dropped + overflow
        )
        if channel_id > 0:
            self._alert_channels.setdefault(guild_id, channel_id)

    def _start_alert_flush(self, guild_id: int) -> None:
        has_pending = (
            guild_id in self._pending_alerts or self._dropped_alert_counts.get(guild_id, 0) > 0
        )
        if has_pending and guild_id not in self._alert_tasks:
            self._alert_tasks[guild_id] = asyncio.create_task(self._flush_alerts(guild_id))

    async def on_ready(self) -> None:
        logger.info("BombaMod connected (guilds=%s)", len(self.guilds))
        for guild_id in set(self._pending_alerts) | set(self._dropped_alert_counts):
            self._start_alert_flush(guild_id)

    async def _moderation_worker(self) -> None:
        while True:
            message, fingerprint = await self._moderation_queue.get()
            try:
                await self._process_message(message, fingerprint)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("Unexpected moderation worker error; case needs manual review")
                try:
                    if message.guild is not None:
                        policy = await self.store.policy(message.guild.id)
                        await self.alert(
                            message.guild.id,
                            policy.review_channel_id or 0,
                            "Internal moderation error; please review this message manually.",
                            f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}",
                        )
                except Exception:
                    logger.error("Could not send a review notice after an internal error")
            finally:
                self._moderation_queue.task_done()

    async def on_message(self, message: discord.Message) -> None:
        if (
            self.is_closed()
            or message.guild is None
            or message.author.bot
            or message.webhook_id is not None
        ):
            return
        policy = await self.store.policy(message.guild.id)
        if not policy.moderation_enabled:
            return
        if policy.monitored_channel_ids and message.channel.id not in policy.monitored_channel_ids:
            return
        image_urls: list[str] = []
        if policy.image_scan_enabled:
            for attachment in message.attachments[:4]:
                content_type = (attachment.content_type or "").split(";", 1)[0].lower()
                if content_type.startswith("image/") or attachment.filename.lower().endswith(
                    (".jpg", ".jpeg", ".png", ".webp", ".gif")
                ):
                    image_urls.append(attachment.url)
        if not message.content and not image_urls:
            return
        try:
            self._moderation_queue.put_nowait((message, self.message_fingerprint(message)))
        except asyncio.QueueFull:
            await self.alert(
                message.guild.id,
                policy.review_channel_id or 0,
                "BombaMod is busy; this message was not scanned. Please review it.",
                f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}",
            )

    @staticmethod
    def message_fingerprint(message: discord.Message) -> str:
        """Hash content/attachments for edit checks; never persist the digest."""
        digest = hashlib.sha256(message.content.encode("utf-8", errors="replace"))
        for attachment in sorted(message.attachments, key=lambda item: item.id):
            digest.update(bytes.fromhex(attachment.id.to_bytes(8, "big", signed=False).hex()))
            digest.update(attachment.size.to_bytes(8, "big", signed=False))
        return digest.hexdigest()

    async def _process_message(self, message: discord.Message, fingerprint: str) -> None:
        if message.guild is None or message.author.bot or message.webhook_id is not None:
            return
        image_urls = [
            attachment.url
            for attachment in message.attachments[:4]
            if (
                (attachment.content_type or "").split(";", 1)[0].lower()
                in {"image/jpeg", "image/png", "image/webp", "image/gif"}
                or attachment.filename.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))
            )
        ]
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
            source_fingerprint=fingerprint,
        )

    async def _admin(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False
        permissions = interaction.user.guild_permissions
        return permissions.administrator or permissions.manage_guild

    async def _moderator(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False
        permissions = interaction.user.guild_permissions
        return (
            await self._admin(interaction)
            or permissions.manage_messages
            or permissions.moderate_members
            or any(
                role.permissions.manage_messages or role.permissions.moderate_members
                for role in interaction.user.roles
            )
        )

    async def _require_admin(self, interaction: discord.Interaction) -> bool:
        if await self._admin(interaction):
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
        if interaction.guild_id is None or interaction.guild is None:
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
        current_policy = await self.bot.store.policy(interaction.guild_id)
        review_channel = (
            interaction.guild.get_channel(current_policy.review_channel_id)
            if current_policy.review_channel_id is not None
            else None
        )
        if not self.bot._is_private_review_channel(interaction.guild, review_channel):
            await interaction.response.send_message(
                "Set a private review channel the bot can access before enabling moderation.",
                ephemeral=True,
            )
            return
        if len(channel_ids) > 100 or any(value <= 0 for value in channel_ids):
            await interaction.response.send_message(
                "Provide at most 100 valid channels.", ephemeral=True
            )
            return
        if any(interaction.guild.get_channel(channel_id) is None for channel_id in channel_ids):
            await interaction.response.send_message(
                "Every selected channel must belong to this server.", ephemeral=True
            )
            return
        await self.bot.store.update_policy(
            interaction.guild_id,
            moderation_enabled=True,
            monitored_channel_ids=channel_ids,
            auto_actions_enabled=(
                current_policy.auto_actions_enabled if current_policy.moderation_enabled else False
            ),
            ban_opt_in=current_policy.ban_opt_in if current_policy.moderation_enabled else False,
            image_scan_enabled=(
                current_policy.image_scan_enabled
                if current_policy.moderation_enabled
                else self.bot.settings.image_scan_enabled_by_default
            ),
        )
        image_enabled = (
            current_policy.image_scan_enabled
            if current_policy.moderation_enabled
            else self.bot.settings.image_scan_enabled_by_default
        )
        image_state = "enabled" if image_enabled else "disabled"
        await interaction.response.send_message(
            "BombaMod enabled. Add `/bm rules`; autonomous actions and bans are off. "
            f"Image scanning is {image_state}; OpenRouter text sharing is unchanged.",
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
            interaction.user.guild_permissions.manage_messages or await self.bot._admin(interaction)
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
            "Policy saved as decision context; this does not train or update model weights.",
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
        current_policy = await self.bot.store.policy(interaction.guild_id)
        if enabled and current_policy.review_channel_id is None:
            await interaction.response.send_message(
                "Set a private moderator review channel before enabling autonomous actions.",
                ephemeral=True,
            )
            return
        if enabled and not current_policy.rules.strip():
            await interaction.response.send_message(
                "Add moderator-authored rules before enabling autonomous actions.", ephemeral=True
            )
            return
        if allow_bans and not enabled:
            await interaction.response.send_message(
                "Enable autonomous actions before enabling bans.", ephemeral=True
            )
            return
        await self.bot.store.update_policy(
            interaction.guild_id,
            auto_actions_enabled=enabled,
            max_timeout_minutes=max_timeout_minutes,
            min_confidence=float(min_confidence),
            ban_opt_in=allow_bans,
        )
        await interaction.response.send_message(
            f"Actions {'enabled' if enabled else 'disabled'}; max={max_timeout_minutes} min; "
            f"confidence {min_confidence:.2f}; bans {'on' if allow_bans else 'off'}.",
            ephemeral=True,
        )

    @app_commands.command(
        name="privacy", description="Opt in to optional model text/image processing"
    )
    @app_commands.describe(
        share_text="Share redacted flagged text with OpenRouter (personal data may remain)",
        scan_images="Send eligible image attachments to OpenAI Omni Moderation",
    )
    async def privacy(
        self, interaction: discord.Interaction, share_text: bool, scan_images: bool
    ) -> None:
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
        host_text_state = (
            "enabled" if self.bot.settings.allow_openrouter_text else "blocked by host config"
        )
        await interaction.response.send_message(
            f"OpenRouter text sharing: {text_state}; host gate is {host_text_state}. "
            "Redaction may miss personal data. "
            f"OpenAI image scans: {image_state}. Never send suspected CSAM (not detected here).",
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
        if interaction.guild_id is None or interaction.guild is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        if channel is not None and channel.guild.id != interaction.guild_id:
            await interaction.response.send_message(
                "Choose a channel from this server.", ephemeral=True
            )
            return
        if channel is not None:
            if not self.bot._is_private_review_channel(interaction.guild, channel):
                await interaction.response.send_message(
                    "The bot must be able to view/send there, and @everyone must not view it.",
                    ephemeral=True,
                )
                return
        if channel is None:
            policy = await self.bot.store.policy(interaction.guild_id)
            if policy.moderation_enabled or policy.auto_actions_enabled:
                await interaction.response.send_message(
                    "Pause moderation and disable actions before clearing this review channel.",
                    ephemeral=True,
                )
                return
        await self.bot.store.update_policy(
            interaction.guild_id,
            review_channel_id=channel.id if channel else None,
        )
        if channel is not None:
            self.bot._alert_channels[interaction.guild_id] = channel.id
            self.bot._start_alert_flush(interaction.guild_id)
        await interaction.response.send_message(
            f"Private review channel set to {channel.mention}."
            if channel
            else "Review channel cleared.",
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
        if not await self.bot._moderator(interaction):
            await interaction.response.send_message(
                "Moderator permissions are required to label cases.", ephemeral=True
            )
            return
        if (
            interaction.guild_id is None
            or not message_id.isascii()
            or not message_id.isdigit()
            or len(message_id) > 20
            or int(message_id) <= 0
        ):
            await interaction.response.send_message(
                "Provide a valid recent case message ID in a server.", ephemeral=True
            )
            return
        case = self.bot.cases.get(int(message_id))
        if case is None:
            await interaction.response.send_message(
                "That case is unavailable or expired. Only recent scanned cases can be labeled.",
                ephemeral=True,
            )
            return
        if case.guild_id != interaction.guild_id:
            await interaction.response.send_message(
                "That case belongs to another server.", ephemeral=True
            )
            return
        predicted_action = case.action.value
        category_labels = list(case.categories)
        moderator_id = interaction.user.id
        await self.bot.store.save_feedback(
            interaction.guild_id,
            moderator_id,
            int(message_id),
            corrected_action.value,
            predicted_action,
            category_labels,
            retention_days=(
                await self.bot.store.policy(interaction.guild_id)
            ).feedback_retention_days,
        )
        await interaction.response.send_message(
            "Label saved without text for offline evaluation, not automatic model training.",
            ephemeral=True,
        )

    @app_commands.command(
        name="export-feedback", description="Export de-identified labels as a CSV attachment"
    )
    async def export_feedback(self, interaction: discord.Interaction) -> None:
        if not await self.bot._admin(interaction):
            await interaction.response.send_message(
                "Manage Server or Administrator permission is required.", ephemeral=True
            )
            return
        if interaction.guild_id is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        policy = await self.bot.store.policy(interaction.guild_id)
        rows = await self.bot.store.feedback_rows(
            interaction.guild_id, policy.feedback_retention_days
        )
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["predicted_action", "corrected_action", "categories", "created_at_utc"])
        for row in rows:
            writer.writerow(
                [
                    row.predicted_action,
                    row.corrected_action,
                    row.category_labels,
                    row.created_at.isoformat(),
                ]
            )
        attachment = discord.File(
            io.BytesIO(output.getvalue().encode("utf-8")), filename="bombamod-feedback.csv"
        )
        await interaction.response.send_message(
            f"Exported {len(rows)} labels without raw text or Discord IDs; protect this file.",
            file=attachment,
            ephemeral=True,
        )

    @app_commands.command(name="pause", description="Disable BombaMod moderation for this server")
    async def pause(self, interaction: discord.Interaction) -> None:
        if not await self.bot._require_admin(interaction):
            return
        if interaction.guild_id is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        await self.bot.store.update_policy(interaction.guild_id, moderation_enabled=False)
        await interaction.response.send_message(
            "BombaMod is paused. Existing queued checks may finish.",
            ephemeral=True,
        )

    @app_commands.command(name="retention", description="Set strike and feedback retention windows")
    async def retention(
        self,
        interaction: discord.Interaction,
        strike_days: app_commands.Range[int, 1, 365],
        feedback_days: app_commands.Range[int, 1, 365],
    ) -> None:
        if not await self.bot._require_admin(interaction):
            return
        if interaction.guild_id is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        await self.bot.store.update_policy(
            interaction.guild_id,
            strike_retention_days=strike_days,
            feedback_retention_days=feedback_days,
        )
        await interaction.response.send_message(
            f"Strike metadata retention: {strike_days} days. "
            f"Feedback label retention: {feedback_days} days.",
            ephemeral=True,
        )

    @app_commands.command(name="status", description="Show BombaMod settings and privacy state")
    async def status(self, interaction: discord.Interaction) -> None:
        if not await self.bot._moderator(interaction):
            await interaction.response.send_message(
                "Moderator permissions are required to view BombaMod settings.", ephemeral=True
            )
            return
        if interaction.guild_id is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        policy = await self.bot.store.policy(interaction.guild_id)
        recent_labels = await self.bot.store.recent_feedback_count(
            interaction.guild_id, min(policy.feedback_retention_days, 365)
        )
        await interaction.response.send_message(
            "BombaMod configuration\n"
            f"Moderation enabled: {policy.moderation_enabled}\n"
            f"OpenAI classification: {not self.bot.settings.disable_openai_text}\n"
            f"Monitored channels: {len(policy.monitored_channel_ids) or 'all'}\n"
            f"Rules configured: {bool(policy.rules)}\n"
            f"Autonomous actions: {policy.auto_actions_enabled}\n"
            f"Bans: {policy.ban_opt_in}\n"
            f"OpenRouter flagged-text sharing: {policy.openrouter_text_opt_in} "
            f"(host gate: {self.bot.settings.allow_openrouter_text})\n"
            f"OpenAI image scanning: {policy.image_scan_enabled}\n"
            f"Strike retention: {policy.strike_retention_days} days\n"
            f"Feedback retention: {policy.feedback_retention_days} days\n"
            f"Max timeout: {policy.max_timeout_minutes} minutes\n"
            f"Minimum confidence: {policy.min_confidence:.2f}\n"
            f"Moderator feedback labels (retention window): {recent_labels}",
            ephemeral=True,
        )


def install_commands(bot: BombaModBot) -> None:
    bot.tree.add_command(BombaModCommands(bot))
