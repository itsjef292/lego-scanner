#!/bin/bash
# Wrapper so launchd/cron run the refresh with the right cwd and interpreter.
# Self-locating: cd to this script's own directory (the project root), so it
# works regardless of where the project lives or which user runs it.
cd "$(cd "$(dirname "$0")" && pwd)" || exit 1
exec /usr/bin/python3 refresh_catalog.py "$@"
