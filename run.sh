#!/bin/bash
# Wrapper script for cron — sets PATH and runs the apartment finder agent.
# Logs output to apartment_finder.log in the same directory.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$SCRIPT_DIR/apartment_finder.log"
PYTHON="/opt/homebrew/bin/python3.11"

cd "$SCRIPT_DIR"

echo "--- $(date '+%Y-%m-%d %H:%M:%S') ---" >> "$LOG"
"$PYTHON" main.py >> "$LOG" 2>&1
echo "" >> "$LOG"
