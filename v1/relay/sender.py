"""Transport-agnostic sender factory.

Both the relay daemon and the scheduler need to send outgoing messages.
We abstract the sender behind a tiny interface so callers don't care
whether the transport is iMessage or Telegram.

Each transport's `Sender` exposes:
    .send(text: str) -> tuple[bool, str]   # returns (ok, error_msg)

The factory `make_sender()` reads the `RELAY_TRANSPORT` env var and
returns the right one. Defaults to `imessage` for backward compat.
"""

from __future__ import annotations

import os

# Transport names — keep in sync with relay/run.py.
TRANSPORT_IMESSAGE = "imessage"
TRANSPORT_TELEGRAM = "telegram"
TRANSPORT_DISCORD = "discord"
TRANSPORT_SLACK = "slack"
TRANSPORT_SMS = "sms"


def current_transport() -> str:
    return os.environ.get("RELAY_TRANSPORT", TRANSPORT_IMESSAGE).strip().lower()


def make_sender():
    """Return a Sender appropriate for the configured RELAY_TRANSPORT.

    Imports happen lazily so we don't pay the cost of importing all
    transport deps when only one is in use (notably py-applescript is
    macOS-only and isn't needed for the network-based transports;
    discord.py and slack-bolt similarly aren't needed for iMessage).
    """
    transport = current_transport()
    if transport == TRANSPORT_TELEGRAM:
        from relay.telegram_relay import TelegramSender, _resolve_telegram_chat_id

        return TelegramSender(_resolve_telegram_chat_id())
    if transport == TRANSPORT_DISCORD:
        from relay.discord_relay import DiscordSender, _resolve_discord_recipient

        return DiscordSender(_resolve_discord_recipient())
    if transport == TRANSPORT_SLACK:
        from relay.slack_relay import SlackSender, _resolve_slack_recipient

        return SlackSender(_resolve_slack_recipient())
    if transport == TRANSPORT_SMS:
        from relay.sms_relay import SMSSender, _resolve_sms_recipient

        return SMSSender(_resolve_sms_recipient())
    if transport == TRANSPORT_IMESSAGE:
        from relay.imessage_relay import ChatSender, _resolve_send_handle

        return ChatSender(_resolve_send_handle())
    raise RuntimeError(
        f"unknown RELAY_TRANSPORT: {transport!r} "
        "(expected 'imessage', 'telegram', 'discord', 'slack', or 'sms')"
    )
