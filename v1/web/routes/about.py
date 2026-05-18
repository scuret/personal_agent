"""Static disclosure pages. /about/privacy mirrors the README's
'Privacy & security profile' section. /about/setup mirrors the
repo-root SETUP.md so users can read the install walkthrough in the
browser (and the wizard can deep-link into specific sections)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from web.templating import templates

router = APIRouter(prefix="/about")

# SETUP.md lives at the repo root (one level above v1/).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_SETUP_MD_PATH = _REPO_ROOT / "SETUP.md"
_SETUP_IMAGES_DIR = _REPO_ROOT / "docs" / "setup"


def _render_markdown(text: str) -> str:
    """Minimal Markdown → HTML for SETUP.md.

    Avoids a `markdown-it-py` dep. Handles the subset SETUP.md uses:
    h2/h3 headings with `<a id>` anchors, fenced code blocks, inline
    backtick code, bold, lists, images, and links. Anything fancier
    falls through as escaped text.
    """
    import html
    import re

    out: list[str] = []
    in_code = False
    in_list = False
    in_table = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    def close_table() -> None:
        nonlocal in_table
        if in_table:
            out.append("</table>")
            in_table = False

    def inline(s: str) -> str:
        # Escape first, then re-introduce the markup we want to render.
        s = html.escape(s)
        # Images: ![alt](url) — must run BEFORE links.
        # Rewrite `docs/setup/<name>.png` to the served route. Keeps
        # SETUP.md's image paths portable (GitHub's preview renders
        # them as-is from the repo) AND lets the web UI serve them
        # via /about/setup-image/<name>.
        def _img_repl(m: re.Match[str]) -> str:
            alt = m.group(1)
            url = m.group(2)
            if url.startswith("docs/setup/"):
                url = "/about/setup-image/" + url[len("docs/setup/"):]
            return f'<img alt="{alt}" src="{url}" loading="lazy" class="rounded border border-zinc-800 my-3 max-w-full">'

        s = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", _img_repl, s)
        # Links: [text](url)
        s = re.sub(
            r"\[([^\]]+)\]\(([^)]+)\)",
            r'<a href="\2" class="underline hover:text-zinc-50">\1</a>',
            s,
        )
        # Inline code
        s = re.sub(
            r"`([^`]+)`",
            r'<code class="mono text-emerald-300 bg-zinc-900 px-1 rounded">\1</code>',
            s,
        )
        # Bold
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        return s

    for raw in text.splitlines():
        line = raw.rstrip()
        # Fenced code blocks
        if line.startswith("```"):
            close_list()
            if not in_code:
                out.append('<pre class="mono text-xs text-zinc-300 bg-zinc-950 border border-zinc-800 rounded p-3 overflow-x-auto whitespace-pre">')
                in_code = True
            else:
                out.append("</pre>")
                in_code = False
            continue
        if in_code:
            out.append(html.escape(raw))
            continue
        # Headings
        m = re.match(r"^(#{1,6})\s+(.+)$", line)
        if m:
            close_list()
            close_table()
            level = len(m.group(1))
            content = m.group(2)
            # Extract anchor if present: <a id="x"></a>
            anchor_match = re.match(r'^<a id="([^"]+)"></a>\s*(.+)$', content)
            if anchor_match:
                aid = anchor_match.group(1)
                content = anchor_match.group(2)
                out.append(f'<h{level} id="{aid}" class="mt-6 mb-3 font-semibold {"text-2xl" if level <= 2 else "text-lg"}">{inline(content)}</h{level}>')
            else:
                out.append(f'<h{level} class="mt-6 mb-3 font-semibold {"text-2xl" if level <= 2 else "text-lg"}">{inline(content)}</h{level}>')
            continue
        # Horizontal rule
        if line.strip() == "---":
            close_list()
            close_table()
            out.append('<hr class="my-6 border-zinc-800">')
            continue
        # Tables (very simple: detect `|...|` rows; treat first row as header)
        if line.startswith("|") and line.endswith("|"):
            close_list()
            if not in_table:
                out.append('<table class="w-full text-sm border border-zinc-800 rounded my-3"><tbody>')
                in_table = True
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(c.strip("- ") == "" for c in cells):
                continue  # the `|---|---|` separator row
            row = "".join(f'<td class="px-3 py-1.5 border-b border-zinc-800">{inline(c)}</td>' for c in cells)
            out.append(f"<tr>{row}</tr>")
            continue
        else:
            close_table()
        # Bullet lists
        m = re.match(r"^(\s*)[-*]\s+(.+)$", line)
        if m:
            if not in_list:
                out.append('<ul class="list-disc list-inside space-y-1 my-2 text-sm">')
                in_list = True
            out.append(f"<li>{inline(m.group(2))}</li>")
            continue
        # Numbered lists — fold into the same bullet styling for simplicity.
        m = re.match(r"^(\s*)(\d+)\.\s+(.+)$", line)
        if m:
            if not in_list:
                out.append('<ol class="list-decimal list-inside space-y-1 my-2 text-sm">')
                in_list = True
            out.append(f"<li>{inline(m.group(3))}</li>")
            continue
        # Blank line
        if not line:
            close_list()
            close_table()
            out.append("")
            continue
        # Paragraph
        close_list()
        close_table()
        out.append(f'<p class="my-2 text-sm">{inline(line)}</p>')

    close_list()
    close_table()
    if in_code:
        out.append("</pre>")
    return "\n".join(out)


@router.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "about/privacy.html", {})


@router.get("/setup", response_class=HTMLResponse)
async def setup(request: Request) -> HTMLResponse:
    """Render SETUP.md as in-browser HTML.

    SETUP.md is the canonical install walkthrough. The wizard deep-
    links into anchors here (e.g. /about/setup#dropbox) so users can
    read the provider click-path while filling in fields.
    """
    if not _SETUP_MD_PATH.exists():
        raise HTTPException(404, f"SETUP.md not found at {_SETUP_MD_PATH}")
    raw = _SETUP_MD_PATH.read_text()
    return templates.TemplateResponse(
        request, "about/setup.html",
        {"rendered_html": _render_markdown(raw)},
    )


# Serve image references in SETUP.md (e.g. /about/setup-image/discord-bot-create.png)
# without exposing the rest of the docs tree.
@router.get("/setup-image/{name}")
async def setup_image(name: str):
    """Serve a SETUP.md image. Restricted to `docs/setup/*.png` for safety.

    Security batch 5 (C3): the suffix + substring checks alone don't
    prevent a `.png` symlink inside `docs/setup/` from pointing outside
    the directory (theoretical, but trivial to defend against). Add a
    canonical-path containment check so the resolved file MUST be a
    direct child of `_SETUP_IMAGES_DIR`.
    """
    from fastapi.responses import FileResponse

    if "/" in name or ".." in name or not name.endswith(".png"):
        raise HTTPException(400, "bad filename")
    base = _SETUP_IMAGES_DIR.resolve()
    try:
        path = (_SETUP_IMAGES_DIR / name).resolve()
    except (OSError, RuntimeError):
        raise HTTPException(400, "bad path") from None
    if path.parent != base:
        # A symlink escaped the directory, or some other path-traversal
        # trick. Reject — anything inside docs/setup must literally be
        # inside docs/setup.
        raise HTTPException(400, "bad path (escapes docs/setup)")
    if not path.is_file():
        raise HTTPException(404, f"image not found: {name}")
    return FileResponse(path)
