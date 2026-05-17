"""Google Drive MCP server.

Search, browse, read, and share Drive files. Uses the Drive API v3 via
the shared google_auth helper. OAuth scope is `drive` (full read/write)
because the agent needs to surface files the user already owns — the
narrower `drive.file` scope only sees app-created files, which makes
"find my taxes spreadsheet" impossible.

Tools exposed (namespaced as mcp__drive__<name>):

  drive_search(query, max_results?, mime_type?)
      Search Drive for files matching a name or content query. Optional
      mime_type narrows to docs / sheets / pdfs / folders / etc.

  drive_list_folder(folder_id, max_results?)
      List the direct children of a folder by ID. Use 'root' for "My Drive".

  drive_get_metadata(file_id)
      Return id, name, mimeType, size, modified time, owners, web link.

  drive_read_text(file_id, max_chars?)
      Read text content. Handles Google Docs (export as plain text),
      plain-text files, markdown, csv. Returns an error for binary
      types like images / pdfs / sheets (sheets has its own server).

  drive_create_share_link(file_id, role?)
      Add an "anyone with the link" permission and return the share URL.
      Default role is 'reader'; pass 'writer' for edit access.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig
from googleapiclient.errors import HttpError

from mcp_servers._untrusted import wrap_untrusted
from mcp_servers.google_auth import build_service


def _err(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "is_error": True}


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _drive():
    return build_service("drive", "v3")


# Friendly mime aliases the agent can pass as `mime_type` to narrow searches.
_MIME_ALIASES = {
    "doc": "application/vnd.google-apps.document",
    "docs": "application/vnd.google-apps.document",
    "document": "application/vnd.google-apps.document",
    "sheet": "application/vnd.google-apps.spreadsheet",
    "sheets": "application/vnd.google-apps.spreadsheet",
    "spreadsheet": "application/vnd.google-apps.spreadsheet",
    "slides": "application/vnd.google-apps.presentation",
    "presentation": "application/vnd.google-apps.presentation",
    "folder": "application/vnd.google-apps.folder",
    "pdf": "application/pdf",
}

# Text-extractable mime types. Google Docs uses export(); the rest are
# fetched via get_media() and decoded as utf-8.
_GDOC_MIME = "application/vnd.google-apps.document"
_PLAINTEXT_MIMES = {
    "text/plain",
    "text/markdown",
    "text/csv",
    "text/html",
    "application/json",
    "text/x-python",
}


def _format_file(f: dict[str, Any]) -> str:
    return (
        f"- [{f.get('id', '?')}] {f.get('name', '(no name)')}"
        f" ({f.get('mimeType', '?')})"
        f" mod={f.get('modifiedTime', '?')}"
    )


def create_drive_mcp_server() -> McpSdkServerConfig:
    @tool(
        "drive_search",
        (
            "Search Google Drive by name or content. Returns matches with "
            "id, name, mimeType, modifiedTime. Optional mime_type narrows "
            "results — accepts friendly aliases like 'doc', 'sheet', "
            "'folder', 'pdf' or a raw mime string."
        ),
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search term. Matched against name and full-text content.",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Default 20.",
                },
                "mime_type": {
                    "type": "string",
                    "description": "Optional. Friendly alias or raw mime string.",
                },
            },
            "required": ["query"],
        },
    )
    async def drive_search(args: dict[str, Any]) -> dict[str, Any]:
        # Drive's `q` operator escapes single quotes by doubling them.
        raw_q = args["query"].replace("'", "\\'")
        clauses = [f"(name contains '{raw_q}' or fullText contains '{raw_q}')", "trashed = false"]
        mime = args.get("mime_type")
        if mime:
            mime_resolved = _MIME_ALIASES.get(mime.lower(), mime)
            clauses.append(f"mimeType = '{mime_resolved}'")
        q = " and ".join(clauses)
        try:
            resp = (
                _drive()
                .files()
                .list(
                    q=q,
                    pageSize=int(args.get("max_results", 20)),
                    fields="files(id,name,mimeType,modifiedTime,owners(emailAddress)),nextPageToken",
                    orderBy="modifiedTime desc",
                )
                .execute()
            )
        except HttpError as e:
            return _err(f"drive search failed: {e}")
        files = resp.get("files", [])
        if not files:
            return _ok("no matching files.")
        return _ok("\n".join(_format_file(f) for f in files))

    @tool(
        "drive_list_folder",
        (
            "List direct children of a Drive folder. Pass 'root' for the "
            "top-level My Drive folder. Returns id, name, mimeType, "
            "modifiedTime for each child."
        ),
        {
            "type": "object",
            "properties": {
                "folder_id": {"type": "string", "description": "Folder ID or 'root'."},
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "description": "Default 50.",
                },
            },
            "required": ["folder_id"],
        },
    )
    async def drive_list_folder(args: dict[str, Any]) -> dict[str, Any]:
        folder_id = args["folder_id"]
        q = f"'{folder_id}' in parents and trashed = false"
        try:
            resp = (
                _drive()
                .files()
                .list(
                    q=q,
                    pageSize=int(args.get("max_results", 50)),
                    fields="files(id,name,mimeType,modifiedTime),nextPageToken",
                    orderBy="folder,name",
                )
                .execute()
            )
        except HttpError as e:
            return _err(f"drive list_folder failed: {e}")
        files = resp.get("files", [])
        if not files:
            return _ok("(empty folder)")
        return _ok("\n".join(_format_file(f) for f in files))

    @tool(
        "drive_get_metadata",
        "Get full metadata for a Drive file by ID.",
        {
            "type": "object",
            "properties": {"file_id": {"type": "string"}},
            "required": ["file_id"],
        },
    )
    async def drive_get_metadata(args: dict[str, Any]) -> dict[str, Any]:
        try:
            f = (
                _drive()
                .files()
                .get(
                    fileId=args["file_id"],
                    fields="id,name,mimeType,size,modifiedTime,createdTime,owners(emailAddress,displayName),webViewLink,parents",
                )
                .execute()
            )
        except HttpError as e:
            return _err(f"drive get_metadata failed: {e}")
        owners = ", ".join(
            o.get("emailAddress", "?") for o in (f.get("owners") or [])
        )
        text = (
            f"id: {f.get('id', '')}\n"
            f"name: {f.get('name', '')}\n"
            f"mime: {f.get('mimeType', '')}\n"
            f"size: {f.get('size', '(google-native, no size)')}\n"
            f"created: {f.get('createdTime', '')}\n"
            f"modified: {f.get('modifiedTime', '')}\n"
            f"owners: {owners}\n"
            f"link: {f.get('webViewLink', '')}\n"
            f"parents: {', '.join(f.get('parents') or [])}"
        )
        return _ok(text)

    @tool(
        "drive_read_text",
        (
            "Read text content from a Drive file. Handles Google Docs "
            "(exports as plain text), plain text, markdown, csv, html, "
            "json. Returns an error for binary formats like images, "
            "PDFs (use a separate OCR), or Sheets (use the sheets server)."
        ),
        {
            "type": "object",
            "properties": {
                "file_id": {"type": "string"},
                "max_chars": {
                    "type": "integer",
                    "minimum": 100,
                    "maximum": 200000,
                    "description": "Truncate to this many chars. Default 50000.",
                },
            },
            "required": ["file_id"],
        },
    )
    async def drive_read_text(args: dict[str, Any]) -> dict[str, Any]:
        svc = _drive()
        try:
            meta = (
                svc.files()
                .get(fileId=args["file_id"], fields="id,name,mimeType")
                .execute()
            )
        except HttpError as e:
            return _err(f"drive read_text (metadata) failed: {e}")
        mime = meta.get("mimeType", "")
        try:
            if mime == _GDOC_MIME:
                data = svc.files().export(fileId=args["file_id"], mimeType="text/plain").execute()
            elif mime in _PLAINTEXT_MIMES or mime.startswith("text/"):
                data = svc.files().get_media(fileId=args["file_id"]).execute()
            else:
                return _err(
                    f"drive read_text: mime '{mime}' is not text-readable. "
                    "Use drive_get_metadata for details or a different tool for binary types."
                )
        except HttpError as e:
            return _err(f"drive read_text failed: {e}")
        text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
        cap = int(args.get("max_chars", 50000))
        if len(text) > cap:
            text = text[:cap] + f"\n\n[truncated at {cap} chars]"
        # Drive files may be authored by anyone the file was shared
        # with — treat content as untrusted.
        name = meta.get("name", "")
        return _ok(
            f"name: {name}\nmime: {mime}\n\n"
            + wrap_untrusted(f"Google Drive file {name!r} (mime {mime})", text)
        )

    @tool(
        "drive_create_share_link",
        (
            "Add an 'anyone with the link' permission to a Drive file "
            "and return the share URL. Default role is reader (view "
            "only); pass role='writer' for edit access. The principal "
            "should be the one asking for this — don't share files "
            "unprompted."
        ),
        {
            "type": "object",
            "properties": {
                "file_id": {"type": "string"},
                "role": {
                    "type": "string",
                    "enum": ["reader", "commenter", "writer"],
                    "description": "Default 'reader'.",
                },
            },
            "required": ["file_id"],
        },
    )
    async def drive_create_share_link(args: dict[str, Any]) -> dict[str, Any]:
        svc = _drive()
        try:
            svc.permissions().create(
                fileId=args["file_id"],
                body={"type": "anyone", "role": args.get("role", "reader")},
            ).execute()
            meta = (
                svc.files()
                .get(fileId=args["file_id"], fields="webViewLink,name")
                .execute()
            )
        except HttpError as e:
            return _err(f"drive create_share_link failed: {e}")
        return _ok(
            f"shared {meta.get('name', '')} as "
            f"{args.get('role', 'reader')}\nlink: {meta.get('webViewLink', '')}"
        )

    return create_sdk_mcp_server(
        name="drive",
        version="1.0.0",
        tools=[
            drive_search,
            drive_list_folder,
            drive_get_metadata,
            drive_read_text,
            drive_create_share_link,
        ],
    )


def main() -> None:
    raise NotImplementedError(
        "drive_server is in-process; instantiate via create_drive_mcp_server() from agent_host."
    )


if __name__ == "__main__":
    main()
