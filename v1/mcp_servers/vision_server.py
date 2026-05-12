"""Vision MCP server — analyze images sent via iMessage.

The relay extracts attachment file paths from chat.db and injects them
into the user's turn (see relay.imessage_relay). The agent then calls
this server's `analyze_image` tool with the file path and a question.

The tool:
  1. Resolves `~`-prefixed paths.
  2. If the file is HEIC (Apple's iPhone default), converts it to JPEG
     via `sips` (built into macOS, no Python deps).
  3. Reads the image, base64-encodes it, sends it to Claude with a
     vision-capable model.
  4. Logs the call to the audit log (`api_events` table) so the privacy
     invariant still holds — every Claude API event is captured locally.
  5. Returns the model's text reply.

Tool exposed (namespaced as mcp__vision__<name>):
  analyze_image(image_path, query)
"""

from __future__ import annotations

import base64
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import anthropic
from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from memory.store import MemoryStore

# Vision-capable model. Default to Haiku 4.5: image-description quality
# matches Sonnet for the everyday cases (screenshots, photos, receipts,
# whiteboards, album shots) at roughly 5-10× lower cost. Override via
# CLAUDE_VISION_MODEL for tasks that need denser reasoning over images
# (complex diagrams, multi-step OCR + interpretation).
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 1024
SIPS_PATH = "/usr/bin/sips"

# Claude vision accepts JPEG, PNG, GIF, WEBP. Anything else needs conversion.
_DIRECTLY_SUPPORTED = {"image/jpeg", "image/png", "image/gif", "image/webp"}
_NEEDS_CONVERSION = {"image/heic", "image/heif"}


def _err(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _resolve_path(p: str) -> Path:
    return Path(os.path.expanduser(p)).resolve()


def _detect_mime(path: Path) -> str:
    """Best-effort MIME guess from extension. Caller can override."""
    ext = path.suffix.lower().lstrip(".")
    return {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "webp": "image/webp",
        "heic": "image/heic",
        "heif": "image/heif",
    }.get(ext, "application/octet-stream")


def _convert_heic_to_jpeg(src: Path) -> Path:
    """Convert HEIC/HEIF to JPEG via macOS `sips`. Returns the JPEG path.

    Output goes to a tempfile that the OS will eventually clean up; we
    don't bother manually deleting it because the relay process is long-
    running and tempfiles are tiny enough to ignore.
    """
    if not Path(SIPS_PATH).exists():
        raise RuntimeError(f"sips not found at {SIPS_PATH}; can't convert HEIC")
    fd, dst_path = tempfile.mkstemp(suffix=".jpg", prefix="vision_")
    os.close(fd)
    subprocess.run(
        [SIPS_PATH, "-s", "format", "jpeg", str(src), "--out", dst_path],
        check=True,
        capture_output=True,
    )
    return Path(dst_path)


def create_vision_mcp_server(store: MemoryStore) -> McpSdkServerConfig:
    """Build the in-process vision MCP server.

    Closes over `store` so each Anthropic vision call appends an
    `api_events` row to the audit log.
    """
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    model = os.environ.get("CLAUDE_VISION_MODEL", os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL))

    @tool(
        "analyze_image",
        (
            "Analyze an image attached to an iMessage. The relay surfaces "
            "image attachment paths in the user's turn as `[attachment: "
            "image at PATH (mime)]`. Pass that exact path here along with "
            "the principal's question — or 'Describe this image briefly' "
            "if they sent the image with no caption."
        ),
        {
            "type": "object",
            "properties": {
                "image_path": {
                    "type": "string",
                    "description": "Filesystem path to the image (`~`-prefixed paths are expanded).",
                },
                "query": {
                    "type": "string",
                    "description": "Question or instruction about the image.",
                },
            },
            "required": ["image_path", "query"],
        },
    )
    async def analyze_image(args: dict[str, Any]) -> dict[str, Any]:
        path = _resolve_path(args["image_path"])
        query = args["query"]

        if not path.exists():
            return _err(f"file not found: {path}")
        if not path.is_file():
            return _err(f"not a regular file: {path}")

        mime = _detect_mime(path)
        if mime in _NEEDS_CONVERSION:
            try:
                path = _convert_heic_to_jpeg(path)
                mime = "image/jpeg"
            except subprocess.CalledProcessError as e:
                return _err(f"HEIC→JPEG conversion failed: {e.stderr.decode(errors='replace')[:200]}")
        elif mime not in _DIRECTLY_SUPPORTED:
            return _err(
                f"unsupported image type: {mime} (path={path.name}). "
                "Claude vision accepts JPEG/PNG/GIF/WEBP; HEIC is auto-converted."
            )

        try:
            data = path.read_bytes()
        except OSError as e:
            return _err(f"couldn't read image: {e}")

        # Hard cap so we don't push a gigantic payload at Claude — the API
        # rejects images over ~5MB anyway. If we hit this in practice we'll
        # add a downscale step.
        if len(data) > 5 * 1024 * 1024:
            return _err(
                f"image too large ({len(data) / 1024 / 1024:.1f}MB; max ~5MB). "
                "Resize on the iPhone before sending."
            )

        b64 = base64.standard_b64encode(data).decode()

        try:
            resp = client.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": mime,
                                    "data": b64,
                                },
                            },
                            {"type": "text", "text": query},
                        ],
                    }
                ],
            )
        except anthropic.APIError as e:
            return _err(f"vision API error: {e}")

        text = "".join(b.text for b in resp.content if hasattr(b, "text"))

        # Audit-log the vision call so the privacy invariant ("every Claude
        # API event is locally visible") still holds. We don't have a
        # conversation_id here — the api_events row gets one only via the
        # main agent loop; a NULL is fine for tool-internal calls.
        store.log_api_event(
            kind="vision_request",
            payload={
                "model": model,
                "image_path": str(path),
                "image_bytes": len(data),
                "query": query,
            },
            metadata={
                "input_tokens": resp.usage.input_tokens if resp.usage else None,
                "output_tokens": resp.usage.output_tokens if resp.usage else None,
            },
        )
        store.log_api_event(
            kind="vision_response",
            payload=text,
        )

        return _ok(text or "(empty response)")

    return create_sdk_mcp_server(
        name="vision",
        version="1.0.0",
        tools=[analyze_image],
    )


def main() -> None:
    raise NotImplementedError(
        "vision_server is in-process; instantiate via create_vision_mcp_server(store) from agent_host."
    )


if __name__ == "__main__":
    main()
