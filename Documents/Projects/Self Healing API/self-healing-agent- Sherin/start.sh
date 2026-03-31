#!/bin/bash
# Start SBSA Self-Healing Middleware — interceptor + web UI
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# Load API keys from .env if it exists
if [ -f .env ]; then
    echo "Loading API keys from .env ..."
    set -a; source .env; set +a
fi

cleanup() {
    echo ""
    echo "Shutting down..."
    kill $INTERCEPTOR_PID 2>/dev/null
    kill $WEBUI_PID 2>/dev/null
    wait 2>/dev/null
    echo "Done."
}
trap cleanup EXIT INT TERM

echo "═══════════════════════════════════════════════════"
echo "  🛡️  SBSA Self-Healing Middleware"
echo "═══════════════════════════════════════════════════"
echo "  Providers:"
echo "    Ollama : ✅ local"
[ -n "$GROQ_API_KEY" ]   && echo "    Groq   : ✅ key loaded" || echo "    Groq   : ⚠️  set GROQ_API_KEY in .env"
[ -n "$GEMINI_API_KEY" ] && echo "    Gemini : ✅ key loaded" || echo "    Gemini : ⚠️  set GEMINI_API_KEY in .env"
echo "═══════════════════════════════════════════════════"

# 1. Start interceptor (background)
echo "[1/2] Starting interceptor on :6010..."
python3 interceptor.py &
INTERCEPTOR_PID=$!
sleep 2

if ! kill -0 $INTERCEPTOR_PID 2>/dev/null; then
    echo "❌ Interceptor failed to start"
    exit 1
fi
echo "  ✓ Interceptor running (PID $INTERCEPTOR_PID)"

# 2. Start web UI (foreground)
echo "[2/2] Starting web UI on http://localhost:5002 ..."
echo "═══════════════════════════════════════════════════"
echo ""
python3 web_ui.py &
WEBUI_PID=$!

wait
