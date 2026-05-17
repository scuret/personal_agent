"""Learning MCP server — captures user corrections to the agent's triggers.

When the user texts feedback like "this email should have pinged me" or
"the morning brief shouldn't have included weather," the agent calls these
tools to persist the correction. At the next trigger fire,
`scheduler.trigger_prompts.render_examples_block` reads the corrections back
into the prompt as in-context examples.

In-process SDK MCP server — shares the MemoryStore with the relay daemon,
no IPC. Wired in from agent_host.build_options.

Tools:

  learning_record_trigger_example(trigger_name, polarity, input_payload,
                                  expected_output?, note?)
      Persist one correction. Polarity must be 'positive' (should have
      fired / output was correct) or 'negative' (shouldn't have / wrong).

  learning_list_trigger_examples(trigger_name?, polarity?, limit?, archived?)
      Inspect existing examples. Archived=True surfaces soft-deleted rows.

  learning_delete_trigger_example(example_id)
      Soft-delete (is_active=0) so it stops influencing the prompt but
      remains visible in /learning for audit.

  learning_get_last_trigger_fire(trigger_name)
      Pull the most recent fire's assembled prompt from api_events. Used
      when the user gives brief / weekly_review feedback referring to
      'the last brief' — the agent needs the actual input it was given.
"""

from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from memory.store import MemoryStore


def create_learning_mcp_server(store: MemoryStore) -> McpSdkServerConfig:
    """Build the in-process MCP server with closure access to the shared store."""

    trigger_names_csv = ", ".join(store.TRIGGER_NAMES)

    record_schema = {
        "type": "object",
        "properties": {
            "trigger_name": {
                "type": "string",
                "description": (
                    "Which trigger this correction applies to. One of: "
                    + trigger_names_csv
                ),
            },
            "polarity": {
                "type": "string",
                "enum": list(store.TRIGGER_EXAMPLE_POLARITIES),
                "description": (
                    "'positive' = the trigger should have fired / output "
                    "was correct. 'negative' = it fired but shouldn't have / "
                    "the output was wrong."
                ),
            },
            "input_payload": {
                "type": "string",
                "description": (
                    "The raw input the trigger would see for this case. "
                    "For email_triage: the From / Subject / body. For "
                    "morning_brief / weekly_review: the assembled prompt "
                    "from the last fire (call learning_get_last_trigger_fire "
                    "first to retrieve it). Keep it concrete — the next "
                    "trigger call sees this verbatim."
                ),
            },
            "expected_output": {
                "type": "string",
                "description": (
                    "Optional. The user's stated correct outcome — e.g. for "
                    "email_triage: 'ping; chair sent thursday agenda, needs "
                    "your read by tomorrow'. For brief: 'should have "
                    "mentioned the P0 from yesterday' or 'should NOT have "
                    "included the weather section'."
                ),
            },
            "note": {
                "type": "string",
                "description": (
                    "Optional. Short explanation in the user's voice — "
                    "why this case is what it is. Helps the model generalize. "
                    "e.g. 'chair messages always need same-day read'."
                ),
            },
        },
        "required": ["trigger_name", "polarity", "input_payload"],
    }

    @tool(
        "learning_record_trigger_example",
        (
            "Persist a user correction for one of the agent's triggers. "
            "Call this when the user gives feedback about a trigger's "
            "behavior: 'this email should have pinged me', 'the brief "
            "shouldn't have included weather', 'forget that rule about X' "
            "(use delete instead for forget). The correction is injected "
            "into the trigger's prompt on the next fire."
        ),
        record_schema,
    )
    async def learning_record_trigger_example(args: dict[str, Any]) -> dict[str, Any]:
        try:
            new_id = store.record_trigger_example(
                trigger_name=args["trigger_name"],
                polarity=args["polarity"],
                input_payload=args["input_payload"],
                expected_output=args.get("expected_output"),
                note=args.get("note"),
            )
            return {
                "content": [{
                    "type": "text",
                    "text": (
                        f"recorded example #{new_id} for "
                        f"{args['trigger_name']} ({args['polarity']}). "
                        f"Will take effect on next {args['trigger_name']} fire."
                    ),
                }],
            }
        except ValueError as e:
            return {
                "content": [{"type": "text", "text": f"validation error: {e}"}],
                "is_error": True,
            }
        except Exception as e:  # noqa: BLE001
            return {
                "content": [{"type": "text", "text": f"error: {e}"}],
                "is_error": True,
            }

    list_schema = {
        "type": "object",
        "properties": {
            "trigger_name": {
                "type": "string",
                "description": (
                    "Filter to one trigger. Omit to list across all triggers. "
                    "Valid: " + trigger_names_csv
                ),
            },
            "polarity": {
                "type": "string",
                "enum": list(store.TRIGGER_EXAMPLE_POLARITIES),
                "description": "Optional filter to 'positive' or 'negative'.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "Max examples to return. Default 20.",
            },
            "archived": {
                "type": "boolean",
                "description": (
                    "If true, include soft-deleted examples. Default false "
                    "(active only)."
                ),
            },
        },
        "required": [],
    }

    @tool(
        "learning_list_trigger_examples",
        (
            "Inspect the corrections the user has recorded for one or all "
            "triggers. Use when the user asks 'what rules have I taught "
            "you' or before calling delete to find the matching id."
        ),
        list_schema,
    )
    async def learning_list_trigger_examples(args: dict[str, Any]) -> dict[str, Any]:
        rows = store.list_trigger_examples(
            trigger_name=args.get("trigger_name"),
            polarity=args.get("polarity"),
            limit=int(args.get("limit", 20)),
            active_only=not bool(args.get("archived", False)),
        )
        if not rows:
            return {"content": [{"type": "text", "text": "no examples found."}]}
        lines = []
        for r in rows:
            note = (r.get("note") or "").strip()
            expected = (r.get("expected_output") or "").strip()
            payload_preview = (r.get("input_payload") or "").strip().replace("\n", " ")
            if len(payload_preview) > 100:
                payload_preview = payload_preview[:100] + "…"
            active_marker = "" if r.get("is_active") else " [archived]"
            lines.append(
                f"#{r['id']} [{r['trigger_name']}] {r['polarity']}"
                f"{active_marker}\n"
                f"   input: {payload_preview}\n"
                + (f"   expected: {expected}\n" if expected else "")
                + (f"   note: {note}\n" if note else "")
            )
        return {"content": [{"type": "text", "text": "\n".join(lines).strip()}]}

    delete_schema = {
        "type": "object",
        "properties": {
            "example_id": {
                "type": "integer",
                "description": (
                    "The numeric id from learning_list_trigger_examples."
                ),
            },
        },
        "required": ["example_id"],
    }

    @tool(
        "learning_delete_trigger_example",
        (
            "Soft-delete one example (sets is_active=0). It stops "
            "influencing the trigger's prompt but stays visible at "
            "/learning. Call when the user says 'forget that rule about X' "
            "— look up the id with learning_list_trigger_examples first."
        ),
        delete_schema,
    )
    async def learning_delete_trigger_example(args: dict[str, Any]) -> dict[str, Any]:
        ok = store.soft_delete_trigger_example(int(args["example_id"]))
        if ok:
            return {"content": [{
                "type": "text",
                "text": f"archived example #{args['example_id']}. Won't influence the next fire.",
            }]}
        return {
            "content": [{
                "type": "text",
                "text": (
                    f"no active example #{args['example_id']} found "
                    f"(may already be archived; check with archived=true)."
                ),
            }],
            "is_error": True,
        }

    last_fire_schema = {
        "type": "object",
        "properties": {
            "trigger_name": {
                "type": "string",
                "description": (
                    "The trigger to look up. One of: " + trigger_names_csv
                ),
            },
        },
        "required": ["trigger_name"],
    }

    @tool(
        "learning_get_last_trigger_fire",
        (
            "Fetch the most recent fire's assembled prompt for a trigger. "
            "Use this when the user gives feedback about brief / weekly "
            "review behavior ('the last brief should have included X') — "
            "you need to capture the actual input the trigger saw before "
            "calling learning_record_trigger_example. Returns the prompt "
            "string + timestamp; the prompt is what to pass as "
            "input_payload to the record call."
        ),
        last_fire_schema,
    )
    async def learning_get_last_trigger_fire(args: dict[str, Any]) -> dict[str, Any]:
        trigger_name = args["trigger_name"]
        # api_events stores payload as JSON; we filter to the trigger_fire
        # kind and pull the most recent matching one. SQLite JSON1 isn't
        # guaranteed in all builds — easier to pull the rows and match in
        # Python for the small volume we expect (1-10 fires per day).
        conn = store._conn()
        rows = conn.execute(
            """SELECT timestamp, payload
                 FROM api_events
                WHERE kind = 'trigger_fire'
             ORDER BY timestamp DESC
                LIMIT 20"""
        ).fetchall()
        for r in rows:
            try:
                pl = json.loads(r["payload"])
            except (TypeError, ValueError):
                continue
            if pl.get("trigger_name") == trigger_name:
                prompt = pl.get("prompt", "")
                return {"content": [{
                    "type": "text",
                    "text": (
                        f"Last {trigger_name} fire at {r['timestamp']}.\n"
                        f"Assembled prompt was:\n\n{prompt}"
                    ),
                }]}
        return {
            "content": [{
                "type": "text",
                "text": (
                    f"no recent {trigger_name} fire found in audit log "
                    f"(checked the most recent 20 trigger_fire events)."
                ),
            }],
            "is_error": True,
        }

    return create_sdk_mcp_server(
        name="learning",
        version="1.0.0",
        tools=[
            learning_record_trigger_example,
            learning_list_trigger_examples,
            learning_delete_trigger_example,
            learning_get_last_trigger_fire,
        ],
    )


def main() -> None:
    raise NotImplementedError(
        "learning_server is in-process; instantiate via "
        "create_learning_mcp_server(store) from agent_host."
    )


if __name__ == "__main__":
    main()
