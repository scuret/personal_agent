#!/bin/bash
# Stop and remove the relay + scheduler LaunchAgents.
#
# Usage: ./uninstall.sh

set -euo pipefail

LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"

for label in com.personal-agent.relay com.personal-agent.scheduler; do
    plist="$LAUNCH_AGENTS_DIR/${label}.plist"
    if [[ -f "$plist" ]]; then
        launchctl unload "$plist" 2>/dev/null || true
        rm "$plist"
        echo "removed: $plist"
    else
        echo "not installed: $label (skipping)"
    fi
done

echo
echo "done. the daemons will no longer start on login."
