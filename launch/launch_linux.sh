#!/bin/bash
# Onion Shell — Linux launcher
# Starts watcher and prints status

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

# Start watcher in background if not running
if ! pgrep -f "onion_shell.py _daemon" > /dev/null; then
    python3 onion_shell.py start
    echo "Watcher started."
fi

echo "🧅 Onion Shell watcher is running. Use 'python3 onion_shell.py status' to check."
