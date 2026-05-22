#!/bin/bash
# Start LEGO Scanner — Flask server + ngrok tunnel
cd "$(dirname "$0")"

# Kill any existing instances
pkill -f "python3 app.py" 2>/dev/null
pkill -f "ngrok http" 2>/dev/null
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

# Start ngrok tunnel (static domain)
ngrok http 5001 --domain=neon-monument-cursive.ngrok-free.dev --log=stdout > /tmp/ngrok.log 2>&1 &
NGROK_PID=$!

sleep 2

echo "  ✅ Running!"
echo ""
echo "  Local:   http://$(ipconfig getifaddr en0):5001"
echo "  Public:  https://neon-monument-cursive.ngrok-free.dev"
echo ""
echo "  Press Ctrl+C to stop."
echo ""

# Stop both on exit
trap "kill $FLASK_PID $NGROK_PID 2>/dev/null; echo ''; echo '  Stopped.'; exit" INT TERM

wait
