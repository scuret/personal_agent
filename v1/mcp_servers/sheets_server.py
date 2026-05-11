"""Google Sheets MCP server.

Read and write Google Sheets by ID. Uses the Sheets API v4 via the
shared google_auth helper. OAuth scope is `spreadsheets` (full
read/write to sheets the user owns or has access to).

Tools exposed (namespaced as mcp__sheets__<name>):

  sheets_read_range(spreadsheet_id, range)
      Read values from a range (A1 notation: 'Sheet1!A1:D20' or just
      'A1:D20' for the first sheet). Returns a list of rows.

  sheets_append_rows(spreadsheet_id, range, rows)
      Append rows to the bottom of a table. range identifies the table
      (any cell in it works); the API finds the first empty row after.

  sheets_update_range(spreadsheet_id, range, rows)
      Overwrite a specific range with the given rows. Rows shorter or
      longer than the range are accepted but Google may complain.

  sheets_create(title, initial_sheet_name?)
      Create a new spreadsheet. Returns the new id and a link.

Notes:
  • `rows` is always a 2D array of strings/numbers. Even single-cell
    writes pass [[value]].
  • The agent should ask the principal before destructive overwrites.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig
from googleapiclient.errors import HttpError

from mcp_servers.google_auth import build_service


def _err(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "is_error": True}


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _sheets():
    return build_service("sheets", "v4")


def _format_rows(rows: list[list[Any]]) -> str:
    if not rows:
        return "(empty range)"
    # Render as tab-separated lines. Good enough for an LLM to parse.
    return "\n".join("\t".join(str(c) for c in row) for row in rows)


def create_sheets_mcp_server() -> McpSdkServerConfig:
    @tool(
        "sheets_read_range",
        (
            "Read values from a Google Sheets range. A1 notation: pass "
            "'Sheet1!A1:D20' for a specific sheet, or just 'A1:D20' to "
            "default to the first sheet. Returns a tab-separated grid."
        ),
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "range": {
                    "type": "string",
                    "description": "A1 notation. e.g. 'Sheet1!A1:D20' or 'A:A'.",
                },
            },
            "required": ["spreadsheet_id", "range"],
        },
    )
    async def sheets_read_range(args: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = (
                _sheets()
                .spreadsheets()
                .values()
                .get(spreadsheetId=args["spreadsheet_id"], range=args["range"])
                .execute()
            )
        except HttpError as e:
            return _err(f"sheets read_range failed: {e}")
        rows = resp.get("values", [])
        return _ok(f"range: {resp.get('range', args['range'])}\n\n{_format_rows(rows)}")

    @tool(
        "sheets_append_rows",
        (
            "Append rows to the bottom of a Sheets table. Pass `range` "
            "as any cell within the table (e.g. 'Sheet1!A1'); the API "
            "scans down to find the first empty row and starts writing "
            "there. `rows` is a 2D array — each inner array is one row."
        ),
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "range": {
                    "type": "string",
                    "description": "A cell or range identifying the table. e.g. 'Sheet1!A1'.",
                },
                "rows": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {},
                    },
                    "description": "2D array of values to append.",
                },
            },
            "required": ["spreadsheet_id", "range", "rows"],
        },
    )
    async def sheets_append_rows(args: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = (
                _sheets()
                .spreadsheets()
                .values()
                .append(
                    spreadsheetId=args["spreadsheet_id"],
                    range=args["range"],
                    valueInputOption="USER_ENTERED",
                    insertDataOption="INSERT_ROWS",
                    body={"values": args["rows"]},
                )
                .execute()
            )
        except HttpError as e:
            return _err(f"sheets append_rows failed: {e}")
        updates = resp.get("updates") or {}
        return _ok(
            f"appended {len(args['rows'])} row(s).\n"
            f"updated range: {updates.get('updatedRange', '?')}\n"
            f"updated cells: {updates.get('updatedCells', '?')}"
        )

    @tool(
        "sheets_update_range",
        (
            "Overwrite a specific range with new values. Pass `range` "
            "in A1 notation (e.g. 'Sheet1!B2:D5') and `rows` as a 2D "
            "array sized to match. Existing values in the range are "
            "replaced — confirm with the principal before destructive "
            "writes."
        ),
        {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "range": {"type": "string"},
                "rows": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {},
                    },
                },
            },
            "required": ["spreadsheet_id", "range", "rows"],
        },
    )
    async def sheets_update_range(args: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = (
                _sheets()
                .spreadsheets()
                .values()
                .update(
                    spreadsheetId=args["spreadsheet_id"],
                    range=args["range"],
                    valueInputOption="USER_ENTERED",
                    body={"values": args["rows"]},
                )
                .execute()
            )
        except HttpError as e:
            return _err(f"sheets update_range failed: {e}")
        return _ok(
            f"updated range: {resp.get('updatedRange', '?')}\n"
            f"updated cells: {resp.get('updatedCells', '?')}"
        )

    @tool(
        "sheets_create",
        (
            "Create a new Google Sheets spreadsheet. Returns the new "
            "spreadsheet id and a link. Optional initial_sheet_name "
            "renames the default 'Sheet1' tab."
        ),
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "initial_sheet_name": {"type": "string"},
            },
            "required": ["title"],
        },
    )
    async def sheets_create(args: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {"properties": {"title": args["title"]}}
        if args.get("initial_sheet_name"):
            body["sheets"] = [{"properties": {"title": args["initial_sheet_name"]}}]
        try:
            ss = _sheets().spreadsheets().create(body=body).execute()
        except HttpError as e:
            return _err(f"sheets create failed: {e}")
        sid = ss["spreadsheetId"]
        return _ok(
            f"created spreadsheet {sid}\n"
            f"title: {ss.get('properties', {}).get('title', '')}\n"
            f"link: https://docs.google.com/spreadsheets/d/{sid}/edit"
        )

    return create_sdk_mcp_server(
        name="sheets",
        version="1.0.0",
        tools=[
            sheets_read_range,
            sheets_append_rows,
            sheets_update_range,
            sheets_create,
        ],
    )


def main() -> None:
    raise NotImplementedError(
        "sheets_server is in-process; instantiate via create_sheets_mcp_server() from agent_host."
    )


if __name__ == "__main__":
    main()
