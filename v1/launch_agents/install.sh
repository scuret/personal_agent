#!/bin/bash
# Install the relay + scheduler as LaunchAgents that auto-start on login.
#
# Usage: ./install.sh
#
# What this does:
#   1. Substitutes __V1_DIR__ in each plist with the absolute path to v1/
#   2. Copies the rendered plists to ~/Library/LaunchAgents/
#   3. Loads them with launchctl, which both registers and starts them
#
# Logs land in v1/data/relay.{log,err.log} and v1/data/scheduler.{log,err.log}.
#
# Run ./uninstall.sh to remove and stop everything.

set -euo pipefail

# Resolve absolute path to v1/ regardless of where install.sh is invoked from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
V1_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"

mkdir -p "$LAUNCH_AGENTS_DIR"

UID_DOMAIN="gui/$(id -u)"

for label in com.personal-agent.relay com.personal-agent.scheduler; do
    src="$SCRIPT_DIR/${label}.plist"
    dst="$LAUNCH_AGENTS_DIR/${label}.plist"

    if [[ ! -f "$src" ]]; then
        echo "error: missing template $src" >&2
        exit 1
    fi

    # Render the plist by substituting __V1_DIR__ with the actual path.
    # Using | as the sed delimiter avoids escaping issues with paths
    # containing slashes.
    sed "s|__V1_DIR__|$V1_DIR|g" "$src" > "$dst"
    echo "installed: $dst"

    # bootout/bootstrap is the modern way to (un)register a service. The
    # legacy load/unload silently caches the old plist contents in some
    # macOS versions, so plist edits don't actually take effect — bootout
    # tears the registration down completely so bootstrap loads fresh.
    if launchctl print "$UID_DOMAIN/$label" >/dev/null 2>&1; then
        launchctl bootout "$UID_DOMAIN/$label" 2>/dev/null || true
        sleep 1  # give launchd a moment to fully release the slot
    fi
    launchctl bootstrap "$UID_DOMAIN" "$dst"
    echo "  loaded:    $label"
done

echo
echo "done. tail logs to verify:"
echo "  tail -f $V1_DIR/data/relay.log $V1_DIR/data/relay.err.log"
echo "  tail -f $V1_DIR/data/scheduler.log $V1_DIR/data/scheduler.err.log"
echo
echo "the daemons will auto-start every time you log in. to remove:"
echo "  $SCRIPT_DIR/uninstall.sh"
