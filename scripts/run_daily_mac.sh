#!/bin/bash
# Local alternative to GitHub Actions: runs the bot once and opens nothing.
# Schedule it with the launchd file in this folder (see README).
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
/usr/bin/env python3 -m tradebot run >> logs/bot.log 2>&1
echo "---- $(date)" >> logs/bot.log
