#!/bin/bash
# Install (or refresh) the Brick Scanner launchd agents for THIS machine.
#
# The committed .plist files are TEMPLATES containing __PROJECT_DIR__. This
# script substitutes the real project path (wherever this repo lives, under
# whatever username) and installs the result into ~/Library/LaunchAgents,
# then (re)loads each agent. Re-running it is safe — it reloads in place.
#
#   ./install_agents.sh            # install/refresh both agents
#
# Agents installed:
#   com.brickscanner.app             — keeps the Flask server up (RunAtLoad + KeepAlive)
#   com.brickscanner.catalog-refresh — daily offline-catalog refresh at 07:30 local
#
# Uninstall:
#   launchctl unload ~/Library/LaunchAgents/com.brickscanner.app.plist
#   launchctl unload ~/Library/LaunchAgents/com.brickscanner.catalog-refresh.plist
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
LA="$HOME/Library/LaunchAgents"
mkdir -p "$LA"

for label in com.brickscanner.app com.brickscanner.catalog-refresh; do
  src="$DIR/$label.plist"
  dst="$LA/$label.plist"
  if [ ! -f "$src" ]; then
    echo "  ✗ missing template: $src" >&2
    continue
  fi
  # Fill in this machine's project path. Also rewrites any previously-baked
  # absolute path, so an old installed copy gets corrected too.
  sed -e "s|__PROJECT_DIR__|$DIR|g" \
      -e "s|/Users/[^/]*/Claude/Brick Scanner|$DIR|g" \
      "$src" > "$dst"
  launchctl unload "$dst" 2>/dev/null || true
  launchctl load "$dst"
  echo "  ✓ installed + loaded: $label"
done

echo ""
echo "  Done. The app autostarts at login; the catalog refreshes daily at 07:30 local."
echo "  Reach it over Tailscale — run ./start.sh once to print the URL, or use the"
echo "  always-on agent and open  http://<this-mac>.<tailnet>.ts.net:5001  on your phone."
