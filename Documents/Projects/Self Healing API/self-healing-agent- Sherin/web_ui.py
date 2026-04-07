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
import dynamic_discovery

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
        "cascade_summary": None,
        "cascaded_tools": [],
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
            # Check if this question likely needs live/real-time data
            # If not, the LLM's direct answer is probably fine
            _LIVE_DATA_KEYWORDS = [
                "current", "right now", "today", "latest", "live", "real-time",
                "price of", "weather", "trending", "status", "track",
                "random", "generate", "show me", "give me a",
            ]
            # Topics where no free/no-auth API exists — skip discovery
            _SKIP_DISCOVERY = [
                "email", "sms", "send", "login", "password", "auth",
            ]
            needs_api = any(kw in question.lower() for kw in _LIVE_DATA_KEYWORDS)
            skip = any(kw in question.lower() for kw in _SKIP_DISCOVERY)
            needs_api = any(kw in question.lower() for kw in _LIVE_DATA_KEYWORDS)

            if needs_api and not skip:
                step("discovery", "No registered tool matched — discovering API...")
                log.info("No tool match, trying dynamic discovery for: %s", question[:80])
                discovery = dynamic_discovery.discover_and_call(question)
                if discovery and "error" not in discovery.get("result", {}):
                    api = discovery["api_info"]
                    tool_name = discovery["tool_name"]
                    result["tool_used"] = tool_name
                    raw_args = api.get("extract_from_question", {})
                    result["raw_args"] = raw_args
                    result["dynamic_api"] = {
                        "name": api.get("api_name"),
                        "url": api.get("base_url"),
                        "cached": discovery.get("cached", False),
                        "elapsed_ms": discovery.get("elapsed_ms"),
                        "schema_source": dynamic_discovery._dynamic_tools.get(tool_name, {}).get("schema_source", "unknown"),
                    }

                    # Route through interceptor for full SBSA pipeline if available
                    tool_data = None
                    if use_interceptor:
                        try:
                            step("tool_call", f"Calling {tool_name} via SBSA pipeline...")
                            resp = transport.send({
                                "jsonrpc": "2.0", "id": _next_id(), "method": "tools/call",
                                "params": {"name": tool_name, "arguments": raw_args},
                            })
                            if "result" in resp:
                                tool_data = resp["result"].get("structuredContent", resp["result"])
                                result["healing_steps"].append("🛡️ Routed through SBSA pipeline")
                        except Exception as e:
                            log.warning("Interceptor routing failed for %s: %s, using direct result", tool_name, e)

                    # Fallback to direct discovery result
                    if tool_data is None:
                        tool_data = discovery["result"]

                    step("summarize", "LLM generating answer from discovered API...")
                    clean_data = tool_data if isinstance(tool_data, dict) else tool_data
                    result["raw_tool_data"] = clean_data
                    summary_prompt = f"""Question: {question}

API result from {api.get('api_name', 'discovered API')}:
{json.dumps(clean_data, indent=2)}

Answer naturally using the data. Be concise but complete."""
                    result["answer"] = adapter.summarize(summary_prompt, {})
                    result["healing_steps"].append(f"🔍 Dynamic discovery: {api.get('api_name')} ({api.get('base_url')})")

                    # Result integrity check
                    from interceptor import check_result_integrity
                    if isinstance(clean_data, dict) and isinstance(result["answer"], str):
                        integrity_warning = check_result_integrity(clean_data, result["answer"])
                        if integrity_warning:
                            result["warnings"].append(integrity_warning)
                    return result

            # LLM's own knowledge is sufficient, or discovery failed
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

        # ═══════════════════════════════════════════════════════════
        # MULTI-STEP TOOL EXECUTION LOOP
        # LLM keeps calling tools until it has enough info to answer
        # ═══════════════════════════════════════════════════════════
        MAX_TOOL_STEPS = 8
        all_tool_results = {}
        tools_called = []
        current_tool = tool_name
        current_args = raw_args

        for step_num in range(1, MAX_TOOL_STEPS + 1):
            step("tool_call", f"Step {step_num}: {current_tool}({json.dumps(current_args)[:50]})")
            tools_called.append(current_tool)

            resp = transport.send({
                "jsonrpc": "2.0", "id": _next_id(), "method": "tools/call",
                "params": {"name": current_tool, "arguments": current_args},
            })

            if "error" in resp:
                error = resp["error"]
                etype = error.get("error_type", "unknown")
                msg = error.get("message", "Unknown error")
                if etype == "schema" and "Missing required field" in msg:
                    info = TOOL_REGISTRY.get(current_tool, {})
                    required = list(info.get("schema", {}).keys())
                    sent = list(current_args.keys())
                    missing = [f for f in required if f not in current_args]
                    result["error"] = {"type": "llm_incomplete", "code": error.get("code"),
                        "message": f"LLM forgot parameter(s): {missing}. Sent: {sent}. Requires: {required}."}
                else:
                    result["error"] = {"type": etype, "code": error.get("code"), "message": msg}
                # Don't break — feed error to LLM, let it try something else
                step("tool_call", f"Step {step_num} failed: {msg[:60]}")
                break

            tool_data = resp["result"].get("structuredContent", resp["result"])

            # Collect metadata
            if isinstance(tool_data, dict):
                if "_interceptor_warning" in tool_data:
                    result["warnings"].append(tool_data["_interceptor_warning"])
                drift_note = tool_data.get("_sbsa_drift_note") or tool_data.get("_drift_healed")
                if drift_note:
                    result["healing_steps"].append(f"🔧 Step {step_num}: {drift_note}")

                clean = {k: v for k, v in tool_data.items() if not k.startswith("_")}
                all_tool_results[current_tool] = clean
            else:
                all_tool_results[current_tool] = tool_data

            if use_interceptor:
                result["healing_steps"].insert(0, f"✓ Step {step_num}: SBSA healed {current_tool}")

            # Ask LLM: do you need another tool call?
            collected_summary = json.dumps(all_tool_results, indent=1)[:800]
            next_decision = adapter.route(
                f"Original question: {question}\n\n"
                f"Data collected so far:\n{collected_summary}\n\n"
                f"Do you need to call another tool to fully answer the question? "
                f"If yes, call the next tool. If you have enough data, give a final text answer.",
                tools,
            )

            if "final" in next_decision:
                # LLM has enough data — break and summarize
                break
            elif "tool" in next_decision:
                current_tool = next_decision["tool"]
                current_args = next_decision.get("arguments", {})
                result["tool_used"] = ", ".join(tools_called + [current_tool])
            else:
                break

        # Collect SBSA report from last log entry
        if use_interceptor:
            try:
                log_dir = os.path.join(os.path.dirname(__file__), "benchmark_logs")
                if os.path.isdir(log_dir):
                    logs = sorted(f for f in os.listdir(log_dir) if f.endswith(".jsonl"))
                    if logs:
                        with open(os.path.join(log_dir, logs[-1])) as f:
                            lines = f.readlines()
                        if lines:
                            last = json.loads(lines[-1])
                            result["sbsa_report"] = {
                                "similarity_scores": last.get("similarity_scores") or last.get("sbsa_similarities", {}),
                                "elapsed_ms": last.get("sbsa_elapsed_ms", 0),
                                "mapping": last.get("mapping") or last.get("sbsa_mapping", {}),
                            }
            except Exception:
                pass

        result["tool_used"] = ", ".join(tools_called)
        result["raw_args"] = raw_args
        result["raw_tool_data"] = all_tool_results

        # Step 3: LLM summarizes ALL collected results
        step("summarize", f"LLM summarizing {len(all_tool_results)} tool results...")
        clean_data = all_tool_results

        summary_prompt = f"""Question: {question}

Tool result (includes cascade chain if executed):
{json.dumps(clean_data, indent=2)}

Answer naturally using all available information, including any cascaded results.
Include key facts. Be concise but complete."""
        
        result["answer"] = adapter.summarize(summary_prompt, {})  # Use prompt directly

        # Stage 12: Result integrity check (gaslighting detection)
        from interceptor import check_result_integrity
        if isinstance(clean_data, dict) and isinstance(result["answer"], str):
            integrity_warning = check_result_integrity(clean_data, result["answer"])
            if integrity_warning:
                result["warnings"].append(integrity_warning)
                result["healing_steps"].append(f"⚠️ {integrity_warning}")

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
