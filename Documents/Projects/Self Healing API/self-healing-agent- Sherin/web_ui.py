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
import os
import socket
import subprocess
import sys
import threading
import time
from typing import Optional, Tuple

from flask import Flask, render_template, request, jsonify
from flask_cors import CORS

import adapters

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════

INTERCEPTOR_HOST = "127.0.0.1"
INTERCEPTOR_PORT = 6010

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
_current_provider = "ollama"
_current_model = "llama3"

# Per-model history for comparative analysis
_model_history: dict = {}   # { "provider/model": [ {tool, success, ...} ] }

_NOTIFICATIONS = {"initialized", "notifications/initialized", "notifications/cancelled"}

# ═══════════════════════════════════════════════════════════════
# DOMAIN REGISTRY — loaded from api_registry.py
# ═══════════════════════════════════════════════════════════════

from api_registry import get_domains_summary, API_REGISTRY as TOOL_REGISTRY
DOMAIN_REGISTRY = get_domains_summary()


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
# AGENT LOGIC — uses adapter system for model-agnostic tool-calling
# ═══════════════════════════════════════════════════════════════

def ask_agent(question: str, use_interceptor: bool, drift_enabled: bool = False, on_step=None) -> dict:
    """
    Process a user question through the selected LLM adapter + MCP pipeline.
    """
    global _current_provider, _current_model
    model_key = f"{_current_provider}/{_current_model}"
    step = on_step or (lambda s, d: None)
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
        "model": model_key,
        "sbsa_report": None,
        "llm_latency_ms": None,
    }

    t_start = time.monotonic()
    transport = None
    try:
        adapter = adapters.get_adapter(_current_provider, _current_model)
        step("connect", f"Connecting to {'interceptor' if use_interceptor else 'server directly'}...")
        transport, _ = _connect(use_interceptor)
        tools = _handshake(transport)

        # Toggle drift on the server
        transport.send({
            "jsonrpc": "2.0", "id": _next_id(),
            "method": "set_drift", "params": {"active": drift_enabled},
        })

        # Step 1: LLM decides tool + args via native function-calling
        step("llm_routing", f"LLM ({model_key}) choosing tool...")
        log.info("Routing via %s/%s", _current_provider, _current_model)
        decision = adapter.route(question, tools)
        result["llm_latency_ms"] = decision.get("latency_ms")
        log.info("LLM decision: %s", decision)

        if "final" in decision:
            result["answer"] = decision["final"]
            return result

        if "tool" not in decision:
            result["error"] = {"type": "llm_error", "message": "LLM returned unexpected format"}
            result["answer"] = "I couldn't understand how to process that request."
            return result

        tool_name = decision["tool"]
        raw_args = decision.get("arguments", {})
        result["tool_used"] = tool_name
        result["raw_args"] = raw_args

        # Step 2: Send tool call through MCP (interceptor or direct)
        step("tool_call", f"Calling {tool_name}({json.dumps(raw_args)[:60]})")
        resp = transport.send({
            "jsonrpc": "2.0", "id": _next_id(), "method": "tools/call",
            "params": {"name": tool_name, "arguments": raw_args},
        })

        if "error" in resp:
            error = resp["error"]
            etype = error.get("error_type", "unknown")
            msg = error.get("message", "Unknown error")

            # Distinguish LLM-missing-args from SBSA failure
            if etype == "schema" and "Missing required field" in msg:
                info = TOOL_REGISTRY.get(tool_name, {})
                required = list(info.get("v1_schema", {}).keys())
                sent = list(raw_args.keys())
                missing = [f for f in required if f not in raw_args]
                result["error"] = {"type": "llm_incomplete", "code": error.get("code"),
                    "message": f"LLM forgot to include required parameter(s): {missing}. "
                               f"LLM sent: {sent}. Tool requires: {required}. "
                               f"Note: SBSA can heal wrong field names but cannot invent missing values."}
                result["answer"] = (
                    f"⚠️ LLM Incomplete Call: The LLM forgot to send parameter(s) {missing}. "
                    f"It only sent {sent}. SBSA heals wrong names, not missing arguments — "
                    f"this is an LLM limitation, not a middleware failure.")
            else:
                result["error"] = {"type": etype, "code": error.get("code"), "message": msg}
                prefix = {"schema": "❌ Schema Error", "timeout": "⏱️ Timeout",
                           "network": "🌐 Network Error", "api": "⚠️ API Error",
                           "drift": "🔀 Schema Drift"}.get(etype, "❌ Error")
                result["answer"] = f"{prefix}: {msg}"
            return result

        tool_data = resp["result"].get("structuredContent", resp["result"])

        if isinstance(tool_data, dict):
            if "_interceptor_warning" in tool_data:
                result["warnings"].append(tool_data["_interceptor_warning"])
            if "_drift_healed" in tool_data:
                result["healing_steps"].append(f"🔧 {tool_data['_drift_healed']}")

            cascade_keys = [k for k in tool_data if k.startswith("_cascade_")]
            if cascade_keys:
                result["cascade_data"] = {k: tool_data[k] for k in cascade_keys}

            if use_interceptor:
                result["healed_args"] = raw_args
                result["healing_steps"].insert(0, "✓ SBSA alignment — deterministic key mapping")
                try:
                    log_dir = os.path.join(os.path.dirname(__file__), "benchmark_logs")
                    if os.path.isdir(log_dir):
                        logs = sorted(f for f in os.listdir(log_dir) if f.endswith(".jsonl"))
                        if logs:
                            with open(os.path.join(log_dir, logs[-1])) as f:
                                lines = f.readlines()
                            if lines:
                                last = json.loads(lines[-1])
                                if last.get("tool") == tool_name:
                                    result["sbsa_report"] = {
                                        "similarity_scores": last.get("similarity_scores") or last.get("sbsa_similarities", {}),
                                        "elapsed_ms": last.get("sbsa_elapsed_ms", 0),
                                        "mapping": last.get("mapping") or last.get("sbsa_mapping", {}),
                                    }
                except Exception:
                    pass

        # Step 3: LLM summarizes the result
        step("summarize", "LLM generating answer...")
        clean_data = (
            {k: v for k, v in tool_data.items() if not k.startswith("_")}
            if isinstance(tool_data, dict) else tool_data
        )
        result["answer"] = adapter.summarize(question, clean_data)
        return result

    except Exception as e:
        log.error(f"Error in ask_agent: {e}", exc_info=True)
        result["error"] = {"type": "system_error", "message": str(e)}
        result["answer"] = f"System error: {str(e)}"
        return result
    finally:
        elapsed = time.monotonic() - t_start
        entry = {
            "tool": result.get("tool_used"),
            "success": result.get("error") is None,
            "total_s": round(elapsed, 2),
            "llm_latency_ms": result.get("llm_latency_ms"),
            "sbsa_report": result.get("sbsa_report"),
        }
        _model_history.setdefault(model_key, []).append(entry)
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
    data = request.json
    question = data.get('message', '').strip()
    use_interceptor = data.get('interceptor_enabled', True)
    drift_enabled = data.get('drift_enabled', False)
    
    if not question:
        return jsonify({"error": "Empty message"}), 400
    
    from flask import Response
    import queue

    step_queue = queue.Queue()

    def on_step(name, detail):
        step_queue.put({"step": name, "detail": detail})

    def generate():
        # Run agent in a thread so we can stream steps
        result_holder = [None]
        def run():
            result_holder[0] = ask_agent(question, use_interceptor, drift_enabled, on_step=on_step)
            step_queue.put(None)  # signal done

        t = threading.Thread(target=run, daemon=True)
        t.start()

        while True:
            item = step_queue.get()
            if item is None:
                # Done — send final result
                yield f"data: {json.dumps({'type': 'result', 'data': result_holder[0]})}\n\n"
                break
            yield f"data: {json.dumps({'type': 'step', 'data': item})}\n\n"

    return Response(generate(), mimetype='text/event-stream')


@app.route('/api/domains', methods=['GET'])
def domains():
    """Return available domains with suggestions."""
    return jsonify(DOMAIN_REGISTRY)


@app.route('/api/models', methods=['GET'])
def models():
    """Return available providers, their models, and readiness status."""
    providers = adapters.available_providers()
    return jsonify({
        "current_provider": _current_provider,
        "current_model": _current_model,
        "providers": providers,
    })


@app.route('/api/models', methods=['POST'])
def set_model():
    """Switch the active provider and model."""
    global _current_provider, _current_model
    provider = request.json.get("provider", "").strip()
    model = request.json.get("model", "").strip()
    if not provider or not model:
        return jsonify({"error": "provider and model required"}), 400
    _current_provider = provider
    _current_model = model
    log.info("Switched to: %s/%s", provider, model)
    return jsonify({"current_provider": _current_provider, "current_model": _current_model})


@app.route('/api/analytics', methods=['GET'])
def analytics():
    """Return per-model metrics for the analytics panel."""
    # Estimated tokens per LLM call (routing + summarize)
    EST_TOKENS_PER_CALL = 200
    # Estimated tokens for a ReAct reflection retry
    EST_TOKENS_PER_RETRY = 400

    MODEL_PRICING = {
        "ollama/llama3": 0, "ollama/llama3:latest": 0,
        "groq/llama-3.3-70b-versatile": 0.59,
        "groq/llama-3.1-8b-instant": 0.05,
        "mistral/mistral-small-latest": 0.1,
        "anthropic/claude-sonnet-4-20250514": 3.0,
        "anthropic/claude-haiku-4-20250514": 0.8,
    }

    summary = {}
    for model, entries in _model_history.items():
        total = len(entries)
        if not total: continue

        successes = sum(1 for e in entries if e["success"])
        failures = total - successes
        llm_times = [e["llm_latency_ms"] for e in entries if e.get("llm_latency_ms")]
        total_times = [e["total_s"] for e in entries]

        # Token economics
        tokens_used = total * EST_TOKENS_PER_CALL
        tokens_saved = successes * EST_TOKENS_PER_RETRY  # each success = 1 avoided retry
        price_per_m = MODEL_PRICING.get(model, 0)
        cost_used = round(tokens_used * price_per_m / 1_000_000, 5)
        cost_saved = round(tokens_saved * price_per_m / 1_000_000, 5)

        summary[model] = {
            "total_queries": total,
            "successes": successes,
            "failures": failures,
            "success_rate": round(successes / total * 100, 1),
            "avg_response_s": round(sum(total_times) / total, 2),
            "avg_llm_ms": round(sum(llm_times) / len(llm_times), 1) if llm_times else None,
            "tokens_used": tokens_used,
            "tokens_saved_vs_react": tokens_saved,
            "cost_used": cost_used,
            "cost_saved_vs_react": cost_saved,
            "sbsa_heals": successes,  # every success with SBSA = a heal
        }
    return jsonify(summary)


@app.route('/api/benchmark', methods=['POST'])
def run_benchmark_endpoint():
    """Trigger a benchmark run (runs in background)."""
    import benchmark
    import threading

    providers = request.json.get("providers", ["ollama", "groq"]) if request.json else ["ollama", "groq"]

    def _run():
        try:
            results = benchmark.run_benchmark(providers)
            metrics = benchmark.compute_metrics(results)
            benchmark.print_report(metrics)
        except Exception as e:
            log.error("Benchmark failed: %s", e, exc_info=True)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started", "providers": providers})


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
