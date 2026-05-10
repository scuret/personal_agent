"""Relay dispatcher — picks the right transport at startup.

Reads `RELAY_TRANSPORT` from .env and delegates to the matching daemon.
This is what the LaunchAgent invokes (rather than calling iMessage or
Telegram directly), so switching transports is a one-line .env change
plus a daemon restart — no plist edit needed.

Run modes (forwarded to the chosen transport):
    python -m relay.run               # run the daemon
    python -m relay.run --check       # diagnostics for the chosen transport
"""

from __future__ import annotations

import sys

from dotenv import load_dotenv

load_dotenv()

from relay.sender import (  # noqa: E402
    TRANSPORT_IMESSAGE,
    TRANSPORT_TELEGRAM,
    current_transport,
)


def main() -> None:
    transport = current_transport()
    if transport == TRANSPORT_IMESSAGE:
        from relay.imessage_relay import main as run_imessage

        print(f"[relay.run] dispatching to imessage transport")
        run_imessage()
    elif transport == TRANSPORT_TELEGRAM:
        from relay.telegram_relay import main as run_telegram

        print(f"[relay.run] dispatching to telegram transport")
        run_telegram()
    else:
        print(
            f"error: unknown RELAY_TRANSPORT={transport!r} "
            "(expected 'imessage' or 'telegram')",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
