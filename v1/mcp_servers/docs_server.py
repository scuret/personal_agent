"""Google Docs MCP server.

Read and edit Google Docs by ID. Uses the Docs API v1 via the shared
google_auth helper. OAuth scope is `documents` (full read/write to docs
the user owns or has access to).

Tools exposed (namespaced as mcp__docs__<name>):

  docs_read(document_id, max_chars?)
      Pull the document's plain-text content. Walks the structured
      body.content tree and concatenates paragraph runs.

  docs_append_text(document_id, text)
      Append text to the end of the document. Adds a newline before
      the new text so it lands on its own paragraph.

  docs_replace_text(document_id, find, replace, match_case?)
      Global find-and-replace across the document.

  docs_create(title, initial_content?)
      Create a new Doc with optional initial text. Returns the new
      document's id and a link.

Docs don't have a "save" step — every batchUpdate is durable. There is
no undo via API; tell the principal before destructive edits.
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


def _docs():
    return build_service("docs", "v1")


def _extract_text(doc: dict[str, Any]) -> str:
    """Walk a Docs body.content tree and return concatenated text.

    Each top-level element may be a paragraph, table, sectionBreak, or
    tableOfContents. We handle paragraphs and tables (recursively); the
    others contribute no text.
    """

    def runs_from_paragraph(p: dict[str, Any]) -> list[str]:
        out = []
        for el in p.get("elements") or []:
            tr = el.get("textRun")
            if tr and "content" in tr:
                out.append(tr["content"])
        return out

    def walk(elements: list[dict[str, Any]]) -> list[str]:
        chunks: list[str] = []
        for el in elements:
            if "paragraph" in el:
                chunks.extend(runs_from_paragraph(el["paragraph"]))
            elif "table" in el:
                for row in el["table"].get("tableRows") or []:
                    for cell in row.get("tableCells") or []:
                        chunks.extend(walk(cell.get("content") or []))
        return chunks

    body = (doc.get("body") or {}).get("content") or []
    return "".join(walk(body))


def _end_index(doc: dict[str, Any]) -> int:
    """Return the document's end index (1-based; first valid insertion point).

    Used by append: we insert at end_index - 1 (the index *before* the
    final newline that Docs adds implicitly), which puts text inside the
    body rather than past it.
    """
    body = (doc.get("body") or {}).get("content") or []
    for el in reversed(body):
        if "endIndex" in el:
            return int(el["endIndex"])
    return 1


def create_docs_mcp_server() -> McpSdkServerConfig:
    @tool(
        "docs_read",
        (
            "Read the plain-text content of a Google Doc. Concatenates "
            "paragraphs and table cells in document order. Inline images, "
            "drawings, and other non-text elements are skipped."
        ),
        {
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "max_chars": {
                    "type": "integer",
                    "minimum": 100,
                    "maximum": 200000,
                    "description": "Truncate to this many chars. Default 50000.",
                },
            },
            "required": ["document_id"],
        },
    )
    async def docs_read(args: dict[str, Any]) -> dict[str, Any]:
        try:
            doc = _docs().documents().get(documentId=args["document_id"]).execute()
        except HttpError as e:
            return _err(f"docs read failed: {e}")
        text = _extract_text(doc)
        cap = int(args.get("max_chars", 50000))
        if len(text) > cap:
            text = text[:cap] + f"\n\n[truncated at {cap} chars]"
        return _ok(f"title: {doc.get('title', '')}\n\n{text}")

    @tool(
        "docs_append_text",
        (
            "Append text to the end of a Google Doc. A newline is "
            "prepended so the new text starts on its own line. Use this "
            "for journal-style appends, log entries, action items."
        ),
        {
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "text": {
                    "type": "string",
                    "description": "Text to append. Newline prepended automatically.",
                },
            },
            "required": ["document_id", "text"],
        },
    )
    async def docs_append_text(args: dict[str, Any]) -> dict[str, Any]:
        try:
            doc = _docs().documents().get(documentId=args["document_id"]).execute()
        except HttpError as e:
            return _err(f"docs append (load) failed: {e}")
        # Insert at endIndex - 1 (just before the final implicit newline).
        # Prepend our own \n so the new text lands on a fresh paragraph.
        insert_at = max(1, _end_index(doc) - 1)
        body = {
            "requests": [
                {
                    "insertText": {
                        "location": {"index": insert_at},
                        "text": "\n" + args["text"],
                    }
                }
            ]
        }
        try:
            _docs().documents().batchUpdate(documentId=args["document_id"], body=body).execute()
        except HttpError as e:
            return _err(f"docs append failed: {e}")
        return _ok(f"appended {len(args['text'])} chars to {doc.get('title', args['document_id'])}.")

    @tool(
        "docs_replace_text",
        (
            "Global find-and-replace across a Google Doc. Replaces every "
            "occurrence of `find` with `replace`. Returns the number of "
            "replacements made."
        ),
        {
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "find": {"type": "string"},
                "replace": {"type": "string"},
                "match_case": {
                    "type": "boolean",
                    "description": "If true, find is case-sensitive. Default true.",
                },
            },
            "required": ["document_id", "find", "replace"],
        },
    )
    async def docs_replace_text(args: dict[str, Any]) -> dict[str, Any]:
        body = {
            "requests": [
                {
                    "replaceAllText": {
                        "containsText": {
                            "text": args["find"],
                            "matchCase": bool(args.get("match_case", True)),
                        },
                        "replaceText": args["replace"],
                    }
                }
            ]
        }
        try:
            resp = (
                _docs()
                .documents()
                .batchUpdate(documentId=args["document_id"], body=body)
                .execute()
            )
        except HttpError as e:
            return _err(f"docs replace failed: {e}")
        replies = resp.get("replies") or []
        occurrences = 0
        if replies and "replaceAllText" in replies[0]:
            occurrences = int(replies[0]["replaceAllText"].get("occurrencesChanged", 0))
        return _ok(f"replaced {occurrences} occurrence(s) of '{args['find']}'.")

    @tool(
        "docs_create",
        (
            "Create a new Google Doc. Optional initial_content is "
            "inserted as plain text. Returns the new doc's id and a "
            "shareable link."
        ),
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "initial_content": {"type": "string"},
            },
            "required": ["title"],
        },
    )
    async def docs_create(args: dict[str, Any]) -> dict[str, Any]:
        try:
            doc = _docs().documents().create(body={"title": args["title"]}).execute()
        except HttpError as e:
            return _err(f"docs create failed: {e}")
        doc_id = doc["documentId"]
        if args.get("initial_content"):
            try:
                _docs().documents().batchUpdate(
                    documentId=doc_id,
                    body={
                        "requests": [
                            {
                                "insertText": {
                                    "location": {"index": 1},
                                    "text": args["initial_content"],
                                }
                            }
                        ]
                    },
                ).execute()
            except HttpError as e:
                return _err(
                    f"docs create succeeded but initial_content insert failed: {e}\n"
                    f"empty doc id: {doc_id}"
                )
        return _ok(
            f"created doc {doc_id}\n"
            f"title: {doc.get('title', '')}\n"
            f"link: https://docs.google.com/document/d/{doc_id}/edit"
        )

    return create_sdk_mcp_server(
        name="docs",
        version="1.0.0",
        tools=[
            docs_read,
            docs_append_text,
            docs_replace_text,
            docs_create,
        ],
    )


def main() -> None:
    raise NotImplementedError(
        "docs_server is in-process; instantiate via create_docs_mcp_server() from agent_host."
    )


if __name__ == "__main__":
    main()
