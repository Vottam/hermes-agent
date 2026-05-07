"""Minimal e2e tests for Discord mention stripping + /command detection.

Covers the fix for slash commands not being recognized when sent via
@mention in a channel, especially after auto-threading.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.e2e.conftest import (
    BOT_USER_ID,
    E2E_MESSAGE_SETTLE_DELAY,
    get_response_text,
    make_discord_message,
    make_fake_dm_channel,
    make_fake_thread,
)

pytestmark = pytest.mark.asyncio


async def dispatch(adapter, msg):
    await adapter._handle_message(msg)
    await asyncio.sleep(E2E_MESSAGE_SETTLE_DELAY)


class TestMentionStrippedCommandDispatch:
    async def test_mention_then_command(self, discord_adapter, bot_user):
        """<@BOT> /help → mention stripped, /help dispatched."""
        msg = make_discord_message(
            content=f"<@{BOT_USER_ID}> /help",
            mentions=[bot_user],
        )
        await dispatch(discord_adapter, msg)
        response = get_response_text(discord_adapter)
        assert response is not None
        assert "/new" in response

    async def test_nickname_mention_then_command(self, discord_adapter, bot_user):
        """<@!BOT> /help → nickname mention also stripped, /help works."""
        msg = make_discord_message(
            content=f"<@!{BOT_USER_ID}> /help",
            mentions=[bot_user],
        )
        await dispatch(discord_adapter, msg)
        response = get_response_text(discord_adapter)
        assert response is not None
        assert "/new" in response

    async def test_text_before_command_not_detected(self, discord_adapter, bot_user):
        """'<@BOT> something else /help' → mention stripped, but 'something else /help'
        doesn't start with / so it's treated as text, not a command."""
        msg = make_discord_message(
            content=f"<@{BOT_USER_ID}> something else /help",
            mentions=[bot_user],
        )
        await dispatch(discord_adapter, msg)
        # Message is accepted (not dropped by mention gate), but since it doesn't
        # start with / it's routed as text — no command output, and no agent in this
        # mock setup means no send call either.
        response = get_response_text(discord_adapter)
        assert response is None or "/new" not in response

    async def test_no_mention_in_channel_dropped(self, discord_adapter):
        """Message without @mention in server channel → silently dropped."""
        msg = make_discord_message(content="/help", mentions=[])
        await dispatch(discord_adapter, msg)
        assert get_response_text(discord_adapter) is None

    async def test_dm_no_mention_needed(self, discord_adapter):
        """DMs don't require @mention — /help works directly."""
        dm = make_fake_dm_channel()
        msg = make_discord_message(content="/help", channel=dm, mentions=[])
        await dispatch(discord_adapter, msg)
        response = get_response_text(discord_adapter)
        assert response is not None
        assert "/new" in response


class TestAutoThreadingPreservesCommand:
    async def test_command_detected_after_auto_thread(self, discord_adapter, bot_user, monkeypatch):
        """@mention /help in channel with auto-thread → thread created AND command dispatched."""
        monkeypatch.setenv("DISCORD_AUTO_THREAD", "true")
        fake_thread = make_fake_thread(thread_id=90001, name="help")
        msg = make_discord_message(
            content=f"<@{BOT_USER_ID}> /help",
            mentions=[bot_user],
        )

        # Simulate discord.py restoring the original raw content (with mention)
        # after create_thread(), which undoes any prior mention stripping.
        original_content = msg.content

        async def clobber_content(**kwargs):
            msg.content = original_content
            return fake_thread

        msg.create_thread = AsyncMock(side_effect=clobber_content)
        await dispatch(discord_adapter, msg)

        msg.create_thread.assert_awaited_once()
        response = get_response_text(discord_adapter)
        assert response is not None
        assert "/new" in response


class TestHupSlashBypassesStringDispatch:
    async def test_hup_slash_calls_gateway_handler_directly(self, discord_adapter):
        """/hup should call GatewayRunner._handle_hup_command directly."""
        sent_event = object()
        gateway_runner = MagicMock()
        gateway_runner._handle_hup_command = AsyncMock(return_value="ok")
        discord_adapter.gateway_runner = gateway_runner
        discord_adapter.handle_message = AsyncMock()
        discord_adapter._check_slash_authorization = AsyncMock(return_value=True)
        discord_adapter._build_slash_event = MagicMock(return_value=sent_event)

        interaction = MagicMock()
        interaction.user = SimpleNamespace(name="alice", id=1)
        interaction.channel = SimpleNamespace(id=123)
        interaction.channel_id = 123
        interaction.guild_id = 456
        interaction.response.defer = AsyncMock()
        interaction.edit_original_response = AsyncMock()
        interaction.delete_original_response = AsyncMock()

        await discord_adapter._run_hup_slash(interaction, "/hup", "Update initiated~")

        gateway_runner._handle_hup_command.assert_awaited_once_with(sent_event)
        discord_adapter.handle_message.assert_not_awaited()
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.edit_original_response.assert_awaited_once_with(content="Update initiated~")
        interaction.delete_original_response.assert_not_awaited()

    async def test_update_safe_alias_uses_same_hup_handler(self, discord_adapter):
        """/update-safe should route through the same internal HUP handler."""
        sent_event = object()
        gateway_runner = MagicMock()
        gateway_runner._handle_hup_command = AsyncMock(return_value="ok")
        discord_adapter.gateway_runner = gateway_runner
        discord_adapter.handle_message = AsyncMock()
        discord_adapter._check_slash_authorization = AsyncMock(return_value=True)
        discord_adapter._build_slash_event = MagicMock(return_value=sent_event)

        interaction = MagicMock()
        interaction.user = SimpleNamespace(name="alice", id=1)
        interaction.channel = SimpleNamespace(id=123)
        interaction.channel_id = 123
        interaction.guild_id = 456
        interaction.response.defer = AsyncMock()
        interaction.edit_original_response = AsyncMock()
        interaction.delete_original_response = AsyncMock()

        await discord_adapter._run_hup_slash(interaction, "/update-safe", None)

        gateway_runner._handle_hup_command.assert_awaited_once_with(sent_event)
        discord_adapter.handle_message.assert_not_awaited()
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.delete_original_response.assert_awaited_once()
