"""Agent host — main entry point for the personal agent.

Wraps a Claude Agent SDK reasoning loop. Receives user turns from the
iMessage relay (or stdin in dev mode), invokes Claude with the personality
system prompt and the configured MCP servers (Gmail, Todoist, Calendar,
Memory), and returns the assistant's reply.

Built in step 2 of the v1 plan.
"""


def main() -> None:
    raise NotImplementedError("Agent host not yet implemented — see step 2 of the v1 plan.")


if __name__ == "__main__":
    main()
