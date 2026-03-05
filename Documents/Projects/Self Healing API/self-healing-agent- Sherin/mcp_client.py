"""
MCP Client — Self-Healing Agent
================================
Auto-detects whether the interceptor is running and adjusts behaviour.

HOW TO RUN
──────────
  # With interceptor (self-healing active)
  Terminal 1:  python interceptor.py
  Terminal 2:  python mcp_client.py

  # Without interceptor (raw errors surface)
              python mcp_client.py    ← just run it, no interceptor needed

WHAT YOU WILL SEE
─────────────────
  🛡 WITH interceptor
    • LLM heals misnamed/missing args before they reach the server
    • Timeouts are retried with exponential back-off
    • Results are reassessed; suspicious data is annotated with a warning
    • All healing steps are logged visibly

  ⚡ WITHOUT interceptor
    • Args go straight from LLM → server, exactly as produced
    • Schema errors surface immediately with a clear ❌ explanation
    • Timeouts fail straight to the user with no retry
    • No result reassessment

Good prompts to see the contrast:
    "what's the weather in london"       → works both ways
    "tell me the weather at my location" → LLM may omit city; interceptor heals it
    "convert GBP to JPY"                 → LLM may send from/to not base/target
    "how much is apple stock"            → LLM may say ticker not symbol
    "bitcoin price"                      → no args, works both ways
"""

import json
import logging
import socket
import subprocess
import sys
import time

import ollama

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════

INTERCEPTOR_HOST = "127.0.0.1"
INTERCEPTOR_PORT = 6010
LLM_MODEL        = "llama3"

logging.basicConfig(
    level=logging.INFO,
    format="[client] %(levelname)s  %(message)s",
)
log = logging.getLogger("client")

# Methods that are notifications — server sends NO response for these
_NOTIFICATIONS = {"initialized", "notifications/initialized", "notifications/cancelled"}

# ═══════════════════════════════════════════════════════════════
# REQUEST ID COUNTER  (monotonically increasing, shared globally)
# ═══════════════════════════════════════════════════════════════

import itertools as _itertools
_req_id = _itertools.count(1)   # 1, 2, 3, …

def _next_id() -> int:
    return next(_req_id)


# ═══════════════════════════════════════════════════════════════
# TRANSPORT — SocketTransport (via interceptor)
# ═══════════════════════════════════════════════════════════════

class SocketTransport:
    """
    Sends JSON-RPC over TCP to the interceptor.
    Correctly distinguishes requests (expects a response) from
    notifications (fire-and-forget).
    """

    def __init__(self, host: str, port: int):
        self._sock = socket.create_connection((host, port), timeout=5)
        self._sock.settimeout(None)
        self._buf  = ""
        log.info("connected to interceptor  addr=%s:%d", host, port)

    def send(self, payload: dict) -> dict:
        """Send a request and block until the response arrives."""
        self._write(payload)
        return self._read_response()

    def notify(self, payload: dict):
        """Send a notification — do NOT wait for a response."""
        self._write(payload)
        # intentionally no read here

    def _write(self, payload: dict):
        self._sock.sendall((json.dumps(payload) + "\n").encode())

    def _read_response(self) -> dict:
        while True:
            if "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                line = line.strip()
                if line.startswith("{"):
                    return json.loads(line)
                # skip empty / non-JSON lines and keep reading
                continue
            chunk = self._sock.recv(4096)
            if not chunk:
                raise RuntimeError("Interceptor closed the connection unexpectedly")
            self._buf += chunk.decode(errors="replace")

    def close(self):
        try:
            self._sock.close()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
# TRANSPORT — PipeTransport (direct to server)
# ═══════════════════════════════════════════════════════════════

class PipeTransport:
    """
    Spawns mcp_server.py directly and communicates via stdin/stdout.
    No healing, no retry — raw behaviour for comparison.
    """

    def __init__(self):
        self._proc = subprocess.Popen(
            [sys.executable, "mcp_server.py"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,   # server logs kept separate
            text=True,
            bufsize=1,
        )
        log.info("spawned mcp_server.py directly  pid=%d", self._proc.pid)

    def send(self, payload: dict) -> dict:
        self._proc.stdin.write(json.dumps(payload) + "\n")
        self._proc.stdin.flush()
        return self._read_response()

    def notify(self, payload: dict):
        """Send a notification — do NOT wait for a response."""
        self._proc.stdin.write(json.dumps(payload) + "\n")
        self._proc.stdin.flush()
        # intentionally no read here

    def _read_response(self) -> dict:
        while True:
            line = self._proc.stdout.readline()
            if not line:
                raise RuntimeError("mcp_server.py exited unexpectedly")
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)

    def close(self):
        try:
            self._proc.terminate()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
# AUTO-DETECT TRANSPORT
# ═══════════════════════════════════════════════════════════════

def _connect():
    """
    Probe port 6010.
    If interceptor answers → SocketTransport (healed mode).
    Otherwise              → PipeTransport   (raw mode — client talks directly
                             to mcp_server.py via stdin/stdout subprocess).
    Returns (transport, using_interceptor: bool).
    """
    try:
        t = SocketTransport(INTERCEPTOR_HOST, INTERCEPTOR_PORT)
        log.info("interceptor detected — using healed mode")
        return t, True
    except (ConnectionRefusedError, OSError):
        log.info(
            "interceptor not found on port %d — connecting directly to mcp_server.py",
            INTERCEPTOR_PORT,
        )
        return PipeTransport(), False


# ═══════════════════════════════════════════════════════════════
# MCP HANDSHAKE
# ═══════════════════════════════════════════════════════════════

def _handshake(transport) -> list:
    """
    Run the MCP initialize → initialized → tools/list sequence.
    'initialized' is a notification — notify() is used, NOT send(),
    so we never block waiting for a response that will never come.
    """
    log.info("starting MCP handshake …")

    # 1. initialize (has id → expects a response)
    resp = transport.send({
        "jsonrpc": "2.0", "id": _next_id(), "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities":    {},
            "clientInfo":      {"name": "ollama-mcp-client", "version": "2.0"},
        },
    })
    log.info("initialize OK  server=%s", resp.get("result", {}).get("serverInfo", {}))

    # 2. initialized (notification — no id, NO response expected)
    transport.notify({"jsonrpc": "2.0", "method": "initialized"})

    # 3. tools/list (has id → expects a response)
    resp  = transport.send({"jsonrpc": "2.0", "id": _next_id(), "method": "tools/list", "params": {}})
    tools = resp.get("result", {}).get("tools", [])
    log.info("tools/list OK  tools=%s", [t["name"] for t in tools])
    return tools


# ═══════════════════════════════════════════════════════════════
# LLM SYSTEM PROMPTS
# ═══════════════════════════════════════════════════════════════

# NOTE: The decision prompt is intentionally permissive about field names.
# Without the interceptor, whatever the LLM guesses goes straight to the server.
# With the interceptor, wrong field names are silently repaired.
# This makes the contrast in error handling very visible.

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
  weather / temperature / rain  →  get_weather          (argument: location)
  country / capital / region    →  get_country_info     (argument: country)
  currency / exchange rate      →  get_exchange_rate    (arguments: base, target)
  stock / share / ticker        →  get_stock_price      (argument: symbol)
"""

_ANSWER_SYSTEM = """
You are a helpful assistant. Answer the user's question using ONLY the tool
result provided. Do not recompute or invent any values. Be concise.
"""


# ═══════════════════════════════════════════════════════════════
# DISPLAY HELPERS
# ═══════════════════════════════════════════════════════════════

def _fmt_mode(using_interceptor: bool) -> str:
    return "🛡 [interceptor]" if using_interceptor else "⚡ [direct]"


def _print_step(label: str, using_interceptor: bool, *lines):
    mode = _fmt_mode(using_interceptor)
    print(f"\n  {mode}  {label}")
    for line in lines:
        print(f"    {line}")


def _print_error_detail(error: dict, using_interceptor: bool):
    """
    Print a clear, human-readable breakdown of what went wrong and why.
    """
    code       = error.get("code", "?")
    message    = error.get("message", "unknown error")
    error_type = error.get("error_type", "unknown")

    # Map error_type to a plain-English cause and advice
    explanations = {
        "schema":  (
            "The tool received the wrong argument names or missing required fields.",
            "The LLM produced argument keys the server didn't recognise."
            if not using_interceptor else
            "The interceptor attempted to heal the arguments but could not extract "
            "enough information from the question.",
        ),
        "timeout": (
            "The upstream API did not respond in time.",
            "No retries were attempted." if not using_interceptor else
            f"The interceptor retried the request but the API remained unreachable.",
        ),
        "network": (
            "A network or connection error occurred reaching the upstream API.",
            "No retries were attempted." if not using_interceptor else
            "The interceptor attempted recovery but the network issue persisted.",
        ),
        "api": (
            "The upstream API returned an error status (e.g. 404, 429, 500).",
            "Check that the tool arguments are valid (correct symbol, country name, etc.).",
        ),
        "parse": (
            "The API returned an unexpected response shape.",
            "This may be a temporary API change or outage.",
        ),
        "unknown": (
            "An unexpected internal error occurred.",
            "Check the server logs for more detail.",
        ),
    }

    cause, advice = explanations.get(error_type, explanations["unknown"])

    mode = _fmt_mode(using_interceptor)
    print(f"\n  {mode}  ❌  Tool call failed")
    print(f"    Error type : {error_type}")
    print(f"    Error code : {code}")
    print(f"    What failed: {cause}")
    print(f"    Detail     : {message}")
    print(f"    Advice     : {advice}")

    if not using_interceptor and error_type in ("schema", "timeout", "network"):
        print(f"\n    💡 Running with the interceptor would handle this automatically.")
        print(f"       Start it with:  python interceptor.py")


def _print_result(data, using_interceptor: bool):
    if not isinstance(data, dict):
        _print_step("Result", using_interceptor, str(data))
        return

    warn  = data.get("_interceptor_warning")
    clean = {k: v for k, v in data.items() if not k.startswith("_")}

    if warn:
        _print_step("⚠️  Interceptor warning", using_interceptor,
                    f"The result may be unreliable: {warn}",
                    f"Data: {clean}")
    else:
        _print_step("✅  Tool result", using_interceptor, str(clean))


# ═══════════════════════════════════════════════════════════════
# AGENT
# ═══════════════════════════════════════════════════════════════

def _fmt_tool_list(tools: list) -> str:
    return "\n".join(f"  - {t['name']}: {t.get('description', '')}" for t in tools)


def ask_agent(question: str, transport, tools: list, using_interceptor: bool) -> str:

    system = _DECISION_SYSTEM.format(tool_list=_fmt_tool_list(tools))

    # ── Step 1: LLM routing decision ─────────────────────────
    raw = ollama.chat(
        model=LLM_MODEL,
        format="json",
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": question},
        ],
    )
    decision = json.loads(raw["message"]["content"])
    _print_step("LLM decision", using_interceptor, str(decision))

    # ── No-tool path ──────────────────────────────────────────
    if "final" in decision:
        return decision["final"]

    if "tool" not in decision:
        raise RuntimeError(
            f"LLM returned an unexpected JSON structure: {decision}\n"
            f"Expected either {{\"tool\": ..., \"arguments\": ...}} "
            f"or {{\"final\": ...}}"
        )

    tool_name = decision["tool"]
    raw_args  = decision.get("arguments", {})

    # ── Step 2: Describe what's about to happen ───────────────
    if using_interceptor:
        _print_step(
            "Sending to interceptor for healing …", using_interceptor,
            f"tool    = {tool_name}",
            f"raw args = {raw_args}  ← interceptor will repair these if needed",
        )
    else:
        _print_step(
            "Sending directly to server (no healing)", using_interceptor,
            f"tool    = {tool_name}",
            f"args    = {raw_args}  ← sent exactly as the LLM produced them",
        )

    # ── Step 3: Make the tool call ────────────────────────────
    resp = transport.send({
        "jsonrpc": "2.0",
        "id":      _next_id(),
        "method":  "tools/call",
        "params":  {"name": tool_name, "arguments": raw_args},
    })

    # ── Step 4: Handle errors clearly ─────────────────────────
    if "error" in resp:
        _print_error_detail(resp["error"], using_interceptor)
        return _error_answer(resp["error"], using_interceptor)

    tool_data = resp["result"].get("structuredContent", resp["result"])
    _print_result(tool_data, using_interceptor)

    # ── Step 5: LLM formulates the final answer ───────────────
    # Strip internal interceptor keys before passing to LLM
    clean_data = (
        {k: v for k, v in tool_data.items() if not k.startswith("_")}
        if isinstance(tool_data, dict) else tool_data
    )
    final = ollama.chat(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": _ANSWER_SYSTEM},
            {"role": "user",   "content":
                f"Question: {question}\n\nTool result: {json.dumps(clean_data)}"},
        ],
    )
    return final["message"]["content"]


def _error_answer(error: dict, using_interceptor: bool) -> str:
    error_type = error.get("error_type", "unknown")
    message    = error.get("message", "unknown error")

    if using_interceptor:
        return (
            f"I was unable to complete that request. "
            f"The interceptor attempted automatic recovery but the {error_type} error "
            f"could not be resolved: {message}"
        )
    else:
        return (
            f"Request failed with a {error_type} error: {message}\n"
            f"Running with the interceptor (python interceptor.py) would attempt "
            f"automatic recovery for this type of error."
        )


# ═══════════════════════════════════════════════════════════════
# STARTUP BANNER
# ═══════════════════════════════════════════════════════════════

def _print_banner(tools: list, using_interceptor: bool):
    w = 65
    print("\n" + "═" * w)
    print("  MCP Self-Healing Agent")
    if using_interceptor:
        print("  🛡  INTERCEPTOR MODE")
        print("      Schema healing • Timeout retry • Result reassessment")
    else:
        print("  ⚡  DIRECT MODE  (raw — no healing, no retry)")
        print("      💡 Run  python interceptor.py  to enable self-healing")
    print("─" * w)
    print(f"  Tools: {[t['name'] for t in tools]}")
    print("─" * w)
    print("  Prompts to see the contrast:")
    print('    "weather in london"              → works both ways')
    print('    "what is the weather here"       → may omit city; interceptor heals')
    print('    "convert GBP to JPY"             → may send from/to; interceptor heals')
    print('    "how much is apple stock"        → may say ticker; interceptor heals')
    print('    "bitcoin price"                  → no args, works both ways')
    print("═" * w + "\n")


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    transport, using_interceptor = _connect()

    try:
        time.sleep(0.3)   # let subprocess settle on PipeTransport
        tools = _handshake(transport)
        _print_banner(tools, using_interceptor)

        while True:
            try:
                question = input("❓ Ask: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not question or question.lower() in {"exit", "quit", "q"}:
                break

            try:
                answer = ask_agent(question, transport, tools, using_interceptor)
                print(f"\n🤖 Answer:\n   {answer}\n")
            except Exception as exc:
                print(f"\n❌ Client error: {type(exc).__name__}: {exc}\n")

    finally:
        transport.close()
        print("Goodbye.")


if __name__ == "__main__":
    main()