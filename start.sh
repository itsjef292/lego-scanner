#!/bin/bash
# Start LEGO Scanner — Flask server, reachable privately over Tailscale.
#
# The app binds 0.0.0.0:5001, so it's available on the tailnet interface.
# From any device signed into the same tailnet (e.g. your phone with the
# Tailscale app on), open the "Tailscale" URL printed below. Traffic is
# end-to-end encrypted over WireGuard and never exposed to the public
# internet — no ngrok, no Render, no port forwarding.
cd "$(dirname "$0")"

# Kill any existing instances
pkill -f "python3 app.py" 2>/dev/null
lsof -ti :5001 | xargs kill -9 2>/dev/null
sleep 1

echo ""
echo "  🧱 Starting LEGO Scanner..."
echo ""

# Start Flask in background
python3 app.py &
FLASK_PID=$!

# Wait for Flask to be ready
sleep 2

# Resolve this machine's Tailscale name (falls back to IP) for the private URL.
TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
TS_NAME=""
if [ -x "$TS" ]; then
  TS_NAME=$("$TS" status --json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('Self',{}).get('DNSName','').rstrip('.'))" 2>/dev/null)
  [ -z "$TS_NAME" ] && TS_NAME=$("$TS" ip -4 2>/dev/null | head -1)
fi

echo "  ✅ Running!"
echo ""
echo "  Local:     http://$(ipconfig getifaddr en0):5001"
if [ -n "$TS_NAME" ]; then
  echo "  Tailscale: http://${TS_NAME}:5001"
else
  echo "  Tailscale: (not detected — is Tailscale running & signed in?)"
fi
echo ""
echo "  Press Ctrl+C to stop."
echo ""

# Stop Flask on exit
trap "kill $FLASK_PID 2>/dev/null; echo ''; echo '  Stopped.'; exit" INT TERM

wait
