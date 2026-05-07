"""iMessage relay — polls Messages.app for incoming texts and sends replies.

Polls the macOS Messages.app via AppleScript on a configurable cadence.
For each new message from `TARGET_PHONE_NUMBER`, invokes the agent host
and sends the reply back to the same chat.

Tracks processed message IDs in a local SQLite DB (`data/imessage_relay.sqlite`)
to avoid double-processing across restarts.

Built in step 5 of the v1 plan.
"""


def main() -> None:
    raise NotImplementedError("iMessage relay not yet implemented — see step 5 of the v1 plan.")


if __name__ == "__main__":
    main()
