#!/bin/bash
# Stop and remove the relay + scheduler LaunchAgents.
#
# Usage: ./uninstall.sh

set -euo pipefail

LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"

UID_DOMAIN="gui/$(id -u)"

for label in com.personal-agent.relay com.personal-agent.scheduler; do
    plist="$LAUNCH_AGENTS_DIR/${label}.plist"
    if launchctl print "$UID_DOMAIN/$label" >/dev/null 2>&1; then
        launchctl bootout "$UID_DOMAIN/$label" 2>/dev/null || true
    fi
    if [[ -f "$plist" ]]; then
        rm "$plist"
        echo "removed: $plist"
    else
        echo "not installed: $label (skipping)"
    fi
done

echo
echo "done. the daemons will no longer start on login."
