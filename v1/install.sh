#!/bin/bash
# Bootstrap installer for personal_agent.
#
# What this does:
#   1. Verify Python 3.11+ is available (preferring 3.13).
#   2. Create a .venv if one doesn't exist.
#   3. Install dependencies from pyproject.toml.
#   4. Hand off to tools/install.py — the interactive configurator that
#      walks you through sub-agent selection, API key entry, Google OAuth,
#      iMessage relay setup, and LaunchAgent install.
#
# Idempotent: safe to re-run to add new sub-agents or re-do parts of the
# config. Existing .env values are preserved unless you explicitly change
# them in the configurator.
#
# Usage:
#   ./install.sh                  # full bootstrap + interactive setup
#   ./install.sh --skip-deps      # skip venv/dep install (reuse existing)
#   ./install.sh --configure-only # alias for --skip-deps
#   ./install.sh --help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

skip_deps=false
for arg in "$@"; do
    case "$arg" in
        --help|-h)
            sed -n '2,18p' "${BASH_SOURCE[0]}"
            exit 0
            ;;
        --skip-deps|--configure-only)
            skip_deps=true
            ;;
        *)
            echo "unknown arg: $arg (use --help)" >&2
            exit 1
            ;;
    esac
done

echo "═══ personal_agent installer ═══"
echo "working dir: $SCRIPT_DIR"
echo

if ! $skip_deps; then
    # Python 3.11+ is required. Prefer the newest available so type-syntax
    # features (`int | None`, `list[str]`) just work.
    PY=""
    for candidate in python3.13 python3.12 python3.11; do
        if command -v "$candidate" >/dev/null 2>&1; then
            PY="$candidate"
            break
        fi
    done
    if [[ -z "$PY" ]]; then
        echo "✗ no compatible Python found (need 3.11+, prefer 3.13)" >&2
        echo "  on macOS: brew install python@3.13" >&2
        exit 1
    fi
    echo "✓ Python: $($PY --version) at $(command -v "$PY")"

    if [[ -d .venv ]]; then
        echo "✓ .venv already exists — reusing"
    else
        echo "→ creating .venv with $PY..."
        if command -v uv >/dev/null 2>&1; then
            uv venv --python "$PY" .venv
        else
            "$PY" -m venv .venv
        fi
        echo "✓ .venv created"
    fi

    echo "→ installing dependencies..."
    if command -v uv >/dev/null 2>&1; then
        uv pip install -e ".[dev]" 2>&1 | tail -3
    else
        ./.venv/bin/pip install -e ".[dev]" 2>&1 | tail -3
    fi
    echo "✓ deps installed"
    echo
fi

# Hand off to the interactive Python configurator. Use the venv's Python
# so all imports resolve.
exec ./.venv/bin/python -m tools.install
