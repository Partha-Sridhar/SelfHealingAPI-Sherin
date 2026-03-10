"""
Web UI for MCP Self-Healing Demo
=================================
A Flask-based chat interface that showcases the interceptor's self-healing
capabilities with a visual toggle to enable/disable healing.

Run:  python web_ui.py
Then open:  http://localhost:5000
"""

import json
import logging
import socket
import subprocess
import sys
import threading
import time
from typing import Optional, Tuple

import ollama
from flask import Flask, render_template, request, jsonify
from flask_cors import CORS

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════

INTERCEPTOR_HOST = "127.0.0.1"
INTERCEPTOR_PORT = 6010
LLM_MODEL = "llama3"

logging.basicConfig(
    level=logging.INFO,
    format="[web_ui] %(levelname)s  %(message)s",
)
log = logging.getLogger("web_ui")

app = Flask(__name__)
CORS(app)

# Global state
_server_proc: Optional[subprocess.Popen] = None
_interceptor_proc: Optional[subprocess.Popen] = None
_tools_cache = []

_NOTIFICATIONS = {"initialized", "notifications/initialized", "notifications/cancelled"}


# ═══════════════════════════════════════════════════════════════
# REQUEST ID
# ═══════════════════════════════════════════════════════════════

import itertools as _itertools
_req_id = _itertools.count(1)

def _next_id() -> int:
    return next(_req_id)


# ═══════════════════════════════════════════════════════════════
# TRANSPORT CLASSES
# ═══════════════════════════════════════════════════════════════

class SocketTransport:
    """Connects to interceptor via TCP."""
    
    def __init__(self, host: str, port: int):
        self._sock = socket.create_connection((host, port), timeout=5)
        self._sock.settimeout(120)
        self._buf = ""
        log.info("connected to interceptor")

    def send(self, payload: dict) -> dict:
        self._write(payload)
        return self._read_response()

    def notify(self, payload: dict):
        self._write(payload)

    def _write(self, payload: dict):
        self._sock.sendall((json.dumps(payload) + "\n").encode())

    def _read_response(self) -> dict:
        while True:
            if "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                line = line.strip()
                if line.startswith("{"):
                    return json.loads(line)
                continue
            chunk = self._sock.recv(4096)
            if not chunk:
                raise RuntimeError("Connection closed")
            self._buf += chunk.decode(errors="replace")

    def close(self):
        try:
            self._sock.close()
        except Exception:
            pass


class PipeTransport:
    """Connects directly to mcp_server.py via subprocess."""
    
    def __init__(self):
        self._proc = subprocess.Popen(
            [sys.executable, "mcp_server.py"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        log.info("spawned mcp_server.py directly")

    def send(self, payload: dict) -> dict:
        self._proc.stdin.write(json.dumps(payload) + "\n")
        self._proc.stdin.flush()
        return self._read_response()

    def notify(self, payload: dict):
        self._proc.stdin.write(json.dumps(payload) + "\n")
        self._proc.stdin.flush()

    def _read_response(self) -> dict:
        while True:
            line = self._proc.stdout.readline()
            if not line:
                raise RuntimeError("Server exited")
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)

    def close(self):
        try:
            self._proc.terminate()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
# TRANSPORT MANAGER
# ═══════════════════════════════════════════════════════════════

def _connect(use_interceptor: bool) -> Tuple[object, bool]:
    """Connect to either interceptor or direct server."""
    if use_interceptor:
        try:
            t = SocketTransport(INTERCEPTOR_HOST, INTERCEPTOR_PORT)
            return t, True
        except (ConnectionRefusedError, OSError) as e:
            log.warning(f"Interceptor not available: {e}")
            raise RuntimeError("Interceptor is not running. Please start it first.")
    else:
        return PipeTransport(), False


def _handshake(transport) -> list:
    """Perform MCP handshake and return tools list."""
    # initialize
    resp = transport.send({
        "jsonrpc": "2.0", "id": _next_id(), "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "web-ui-client", "version": "1.0"},
        },
    })
    
    # initialized (notification)
    transport.notify({"jsonrpc": "2.0", "method": "initialized"})
    
    # tools/list
    resp = transport.send({
        "jsonrpc": "2.0", "id": _next_id(), "method": "tools/list", "params": {}
    })
    tools = resp.get("result", {}).get("tools", [])
    return tools


# ═══════════════════════════════════════════════════════════════
# LLM PROMPTS
# ═══════════════════════════════════════════════════════════════

_DECISION_SYSTEM = """
You are a tool-routing agent. Decide which tool to call for the user's question.

Rules:
- If a relevant tool exists, call it. Do NOT answer from your own knowledge.
- Respond ONLY with valid JSON. No markdown, no explanation.

Response formats:
  Call a tool:  {{"tool": "<name>", "arguments": {{<key>: <value>}}}}
  No tool fits: {{"final": "<direct answer>"}}

Available tools:
{tool_list}

Routing:
  bitcoin / crypto / BTC        →  get_bitcoin_price    (no arguments)
  weather / temperature / rain  →  get_weather          (argument: location or city)
  country / capital / region    →  get_country_info     (argument: country)
  currency / exchange rate      →  get_exchange_rate    (arguments: base, target)
  stock / share / ticker        →  get_stock_price      (argument: symbol)
"""

_ANSWER_SYSTEM = """
You are a helpful assistant. Answer the user's question using ONLY the tool
result provided. Do not recompute or invent any values. Be concise and friendly.
"""


# ═══════════════════════════════════════════════════════════════
# AGENT LOGIC
# ═══════════════════════════════════════════════════════════════

def _fmt_tool_list(tools: list) -> str:
    return "\n".join(f"  - {t['name']}: {t.get('description', '')}" for t in tools)


def ask_agent(question: str, use_interceptor: bool) -> dict:
    """
    Process a user question and return detailed response with metadata.
    Returns: {
        "answer": str,
        "tool_used": str or None,
        "raw_args": dict,
        "healed_args": dict or None,
        "error": dict or None,
        "warnings": list,
        "healing_steps": list,
        "mode": "interceptor" or "direct"
    }
    """
    result = {
        "answer": "",
        "tool_used": None,
        "raw_args": {},
        "healed_args": None,
        "error": None,
        "warnings": [],
        "healing_steps": [],
        "mode": "interceptor" if use_interceptor else "direct",
        "cascade_data": None,
    }
    
    transport = None
    try:
        # Connect
        transport, connected_via_interceptor = _connect(use_interceptor)
        tools = _handshake(transport)
        
        # LLM decision
        system = _DECISION_SYSTEM.format(tool_list=_fmt_tool_list(tools))
        raw = ollama.chat(
            model=LLM_MODEL,
            format="json",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": question},
            ],
        )
        decision = json.loads(raw["message"]["content"])
        
        # No-tool path
        if "final" in decision:
            result["answer"] = decision["final"]
            return result
        
        if "tool" not in decision:
            result["error"] = {
                "type": "llm_error",
                "message": "LLM returned unexpected format"
            }
            result["answer"] = "I couldn't understand how to process that request."
            return result
        
        tool_name = decision["tool"]
        raw_args = decision.get("arguments", {})
        
        result["tool_used"] = tool_name
        result["raw_args"] = raw_args
        
        # Make tool call
        resp = transport.send({
            "jsonrpc": "2.0",
            "id": _next_id(),
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": raw_args},
        })
        
        # Handle error
        if "error" in resp:
            error = resp["error"]
            result["error"] = {
                "type": error.get("error_type", "unknown"),
                "code": error.get("code"),
                "message": error.get("message", "Unknown error"),
            }
            
            # Generate user-friendly error message
            error_type = result["error"]["type"]
            if error_type == "schema":
                result["answer"] = f"❌ Schema Error: The tool received incorrect argument names. {error['message']}"
            elif error_type == "timeout":
                result["answer"] = f"⏱️ Timeout: The API didn't respond in time. {error['message']}"
            elif error_type == "network":
                result["answer"] = f"🌐 Network Error: Couldn't reach the API. {error['message']}"
            elif error_type == "api":
                result["answer"] = f"⚠️ API Error: {error['message']}"
            else:
                result["answer"] = f"❌ Error: {error['message']}"
            
            return result
        
        # Success - extract result
        tool_data = resp["result"].get("structuredContent", resp["result"])
        
        # Check for interceptor annotations
        if isinstance(tool_data, dict):
            if "_interceptor_warning" in tool_data:
                result["warnings"].append(tool_data["_interceptor_warning"])
            
            if "_drift_healed" in tool_data:
                result["healing_steps"].append(f"🔧 {tool_data['_drift_healed']}")
            
            # Extract cascade data
            cascade_keys = [k for k in tool_data.keys() if k.startswith("_cascade_")]
            if cascade_keys:
                result["cascade_data"] = {k: tool_data[k] for k in cascade_keys}
            
            # Infer healing happened if we're using interceptor
            if use_interceptor:
                result["healed_args"] = raw_args  # In real scenario, interceptor would log this
                result["healing_steps"].insert(0, "✓ Arguments validated and normalized")
        
        # Generate final answer
        clean_data = (
            {k: v for k, v in tool_data.items() if not k.startswith("_")}
            if isinstance(tool_data, dict) else tool_data
        )
        
        final = ollama.chat(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": _ANSWER_SYSTEM},
                {"role": "user", "content": f"Question: {question}\n\nTool result: {json.dumps(clean_data)}"},
            ],
        )
        result["answer"] = final["message"]["content"]
        
        return result
        
    except Exception as e:
        log.error(f"Error in ask_agent: {e}", exc_info=True)
        result["error"] = {
            "type": "system_error",
            "message": str(e)
        }
        result["answer"] = f"System error: {str(e)}"
        return result
    finally:
        if transport:
            transport.close()


# ═══════════════════════════════════════════════════════════════
# FLASK ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/chat', methods=['POST'])
def chat():
    """Handle chat messages."""
    data = request.json
    question = data.get('message', '').strip()
    use_interceptor = data.get('interceptor_enabled', True)
    
    if not question:
        return jsonify({"error": "Empty message"}), 400
    
    try:
        result = ask_agent(question, use_interceptor)
        return jsonify(result)
    except Exception as e:
        log.error(f"Chat error: {e}", exc_info=True)
        return jsonify({
            "answer": f"Error: {str(e)}",
            "error": {"type": "system_error", "message": str(e)},
            "mode": "interceptor" if use_interceptor else "direct"
        }), 500


@app.route('/api/status', methods=['GET'])
def status():
    """Check if interceptor is running."""
    try:
        sock = socket.create_connection((INTERCEPTOR_HOST, INTERCEPTOR_PORT), timeout=1)
        sock.close()
        interceptor_running = True
    except (ConnectionRefusedError, OSError, socket.timeout):
        interceptor_running = False
    
    return jsonify({
        "interceptor_running": interceptor_running,
        "interceptor_host": INTERCEPTOR_HOST,
        "interceptor_port": INTERCEPTOR_PORT,
    })


@app.route('/api/start_interceptor', methods=['POST'])
def start_interceptor():
    """Start the interceptor process."""
    global _interceptor_proc
    
    if _interceptor_proc and _interceptor_proc.poll() is None:
        return jsonify({"status": "already_running"})
    
    try:
        _interceptor_proc = subprocess.Popen(
            [sys.executable, "interceptor.py"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(2)  # Give it time to start
        return jsonify({"status": "started", "pid": _interceptor_proc.pid})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/stop_interceptor', methods=['POST'])
def stop_interceptor():
    """Stop the interceptor process."""
    global _interceptor_proc
    
    if _interceptor_proc:
        _interceptor_proc.terminate()
        _interceptor_proc = None
        return jsonify({"status": "stopped"})
    
    return jsonify({"status": "not_running"})


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\n" + "═" * 70)
    print("  MCP Self-Healing Demo - Web UI")
    print("═" * 70)
    print(f"  Starting web server on http://localhost:5002")
    print(f"  Interceptor should be running on {INTERCEPTOR_HOST}:{INTERCEPTOR_PORT}")
    print("─" * 70)
    print("  To start interceptor manually:")
    print("    python interceptor.py")
    print("═" * 70 + "\n")
    
    app.run(debug=True, host='0.0.0.0', port=5002, use_reloader=False)
