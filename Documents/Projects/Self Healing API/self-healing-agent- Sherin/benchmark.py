"""
SBSA vs ReAct Benchmark
========================

Compares 3 recovery strategies under schema drift:
  1. BASELINE — no recovery, raw failure
  2. REACT   — LLM sees error, reflects, retries (standard agent pattern)
  3. SBSA    — deterministic Hungarian Algorithm healing (our contribution)

Each strategy is tested with drift ON across the same test suite.
Drift OFF is run once as a sanity check.

Metrics: Recovery Rate, Latency, Token Cost, Step Count.

Usage:
  # Start interceptor first: python interceptor.py
  python benchmark.py
"""

import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
from typing import Any, Dict, List

import adapters

logging.basicConfig(level=logging.INFO, format="[bench] %(levelname)s  %(message)s")
log = logging.getLogger("bench")

# ═══════════════════════════════════════════════════════════════
# TEST SUITE
# ═══════════════════════════════════════════════════════════════

TEST_SUITE = [
    ("What's the weather in London?",    "get_weather",       "Weather"),
    ("Weather in Tokyo right now",       "get_weather",       "Weather"),
    ("Temperature in Mumbai",            "get_weather",       "Weather"),
    ("Tell me about Japan",              "get_country_info",  "Geography"),
    ("Capital of France",                "get_country_info",  "Geography"),
    ("Population of India",              "get_country_info",  "Geography"),
    ("Convert USD to EUR",              "get_exchange_rate", "Finance"),
    ("Exchange rate GBP to JPY",        "get_exchange_rate", "Finance"),
    ("Bitcoin price",                    "get_bitcoin_price", "Finance"),
    ("Apple stock price",               "get_stock_price",   "Finance"),
    ("Tesla stock price",               "get_stock_price",   "Finance"),
    ("Weather in Paris",                "get_weather",       "Weather"),
]

MODEL_PRICING = {
    "ollama/llama3":                 {"input": 0.0,  "output": 0.0},
    "groq/llama-3.3-70b-versatile":  {"input": 0.59, "output": 0.79},
    "groq/llama-3.1-8b-instant":     {"input": 0.05, "output": 0.08},
}

# ═══════════════════════════════════════════════════════════════
# TRANSPORTS
# ═══════════════════════════════════════════════════════════════

class SocketTransport:
    def __init__(self, host="127.0.0.1", port=6010):
        self._sock = socket.create_connection((host, port), timeout=5)
        self._sock.settimeout(120)
        self._buf = ""
    def send(self, p):
        self._sock.sendall((json.dumps(p) + "\n").encode())
        while True:
            if "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip().startswith("{"): return json.loads(line.strip())
                continue
            chunk = self._sock.recv(4096)
            if not chunk: raise RuntimeError("closed")
            self._buf += chunk.decode(errors="replace")
    def notify(self, p):
        self._sock.sendall((json.dumps(p) + "\n").encode())
    def close(self):
        try: self._sock.close()
        except: pass

class PipeTransport:
    def __init__(self):
        self._proc = subprocess.Popen(
            [sys.executable, "mcp_server.py"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1)
    def send(self, p):
        self._proc.stdin.write(json.dumps(p) + "\n"); self._proc.stdin.flush()
        while True:
            line = self._proc.stdout.readline()
            if not line: raise RuntimeError("exited")
            if line.strip().startswith("{"): return json.loads(line.strip())
    def notify(self, p):
        self._proc.stdin.write(json.dumps(p) + "\n"); self._proc.stdin.flush()
    def close(self):
        try: self._proc.terminate()
        except: pass

_rid = 0
def _nid():
    global _rid; _rid += 1; return _rid

def _handshake(t):
    t.send({"jsonrpc":"2.0","id":_nid(),"method":"initialize",
            "params":{"protocolVersion":"2024-11-05","capabilities":{},
                      "clientInfo":{"name":"bench","version":"1"}}})
    t.notify({"jsonrpc":"2.0","method":"initialized"})
    return t.send({"jsonrpc":"2.0","id":_nid(),"method":"tools/list","params":{}}).get("result",{}).get("tools",[])

def _set_drift(t, on):
    t.send({"jsonrpc":"2.0","id":_nid(),"method":"set_drift","params":{"active":on}})

def _tool_call(t, name, args):
    return t.send({"jsonrpc":"2.0","id":_nid(),"method":"tools/call",
                   "params":{"name":name,"arguments":args}})


# ═══════════════════════════════════════════════════════════════
# REACT REFLECTION — the standard LLM self-correction loop
# ═══════════════════════════════════════════════════════════════

def react_reflect(adapter, question, tools, error_msg, prev_tool, prev_args, attempt):
    """
    Standard ReAct reflection: show the LLM its error and ask it to fix.
    This is what GPT/Claude/Llama do natively when a tool call fails.
    Returns a new (tool, args) decision.
    """
    # Build reflection prompt — this is the "Inference Tax"
    tool_desc = "\n".join(f"  - {t['name']}: {t.get('description','')}\n"
                          f"    schema: {json.dumps(t.get('inputSchema',{}))}"
                          for t in tools)

    reflect_prompt = (
        f"Your previous tool call failed.\n\n"
        f"Tool called: {prev_tool}\n"
        f"Arguments sent: {json.dumps(prev_args)}\n"
        f"Error received: {error_msg}\n\n"
        f"Available tools and their EXACT schemas:\n{tool_desc}\n\n"
        f"Original question: {question}\n\n"
        f"Fix your tool call. Respond with ONLY JSON: "
        f'{{\"tool\": \"name\", \"arguments\": {{...}}}}'
    )

    # This is the expensive part — a full LLM inference pass
    decision = adapter.route(reflect_prompt, tools)
    return decision


# ═══════════════════════════════════════════════════════════════
# RUN ONE TEST — supports all 3 strategies
# ═══════════════════════════════════════════════════════════════

def run_test(adapter, question, expected_tool, tools, transport, drift_on, strategy):
    """
    strategy: "baseline" | "react" | "sbsa"
      baseline — single attempt, no recovery
      react    — up to 3 LLM reflection retries on error
      sbsa     — goes through interceptor (SBSA heals transparently)
    """
    MAX_REACT_RETRIES = 3

    r = {
        "question": question,
        "expected_tool": expected_tool,
        "drift_on": drift_on,
        "strategy": strategy,
        "success": False,
        "tool_called": None,
        "raw_args": {},
        "final_args": {},
        "correct_tool": False,
        "attempts": 0,
        "llm_calls": 0,
        "total_input_tokens_est": 0,
        "total_output_tokens_est": 0,
        "llm_route_ms": 0,
        "llm_reflect_ms": 0,
        "total_ms": 0,
        "error": None,
        "reflection_history": [],
    }

    t0 = time.monotonic()
    try:
        _set_drift(transport, drift_on)

        # Initial LLM routing
        decision = adapter.route(question, tools)
        r["llm_route_ms"] = decision.get("latency_ms", 0)
        r["llm_calls"] = 1
        r["total_input_tokens_est"] += 150  # ~avg input for routing
        r["total_output_tokens_est"] += 50   # ~avg output

        if "final" in decision:
            r["error"] = "LLM answered directly"
            r["total_ms"] = (time.monotonic() - t0) * 1000
            return r

        tool_name = decision.get("tool", "")
        raw_args = decision.get("arguments", {}) or {}
        r["tool_called"] = tool_name
        r["raw_args"] = dict(raw_args)
        r["correct_tool"] = (tool_name == expected_tool)
        r["attempts"] = 1

        # Send to server
        resp = _tool_call(transport, tool_name, raw_args)

        # Success on first try
        if "error" not in resp:
            r["success"] = True
            r["final_args"] = raw_args
            r["total_ms"] = (time.monotonic() - t0) * 1000
            return r

        error_msg = resp["error"].get("message", "unknown error")

        # BASELINE: no recovery
        if strategy == "baseline":
            r["error"] = error_msg
            r["total_ms"] = (time.monotonic() - t0) * 1000
            return r

        # SBSA: the interceptor already handled it — if we got an error here,
        # SBSA couldn't fix it either
        if strategy == "sbsa":
            r["error"] = error_msg
            r["total_ms"] = (time.monotonic() - t0) * 1000
            return r

        # REACT: reflection loop
        for attempt in range(1, MAX_REACT_RETRIES + 1):
            r["reflection_history"].append({
                "attempt": attempt,
                "prev_args": raw_args,
                "error": error_msg[:100],
            })

            t_reflect = time.monotonic()
            decision = react_reflect(adapter, question, tools, error_msg,
                                     tool_name, raw_args, attempt)
            reflect_ms = (time.monotonic() - t_reflect) * 1000
            r["llm_reflect_ms"] += reflect_ms
            r["llm_calls"] += 1
            r["total_input_tokens_est"] += 350  # error + schema + reflection prompt
            r["total_output_tokens_est"] += 60   # new tool call

            if "tool" not in decision:
                r["error"] = f"Reflection {attempt}: LLM gave up"
                continue

            tool_name = decision["tool"]
            raw_args = decision.get("arguments", {}) or {}
            r["attempts"] = attempt + 1

            resp = _tool_call(transport, tool_name, raw_args)

            if "error" not in resp:
                r["success"] = True
                r["final_args"] = raw_args
                r["total_ms"] = (time.monotonic() - t0) * 1000
                return r

            error_msg = resp["error"].get("message", "unknown error")

        # Exhausted retries
        r["error"] = f"ReAct exhausted {MAX_REACT_RETRIES} retries: {error_msg[:80]}"

    except Exception as e:
        r["error"] = str(e)[:200]

    r["total_ms"] = (time.monotonic() - t0) * 1000
    return r


# ═══════════════════════════════════════════════════════════════
# BENCHMARK RUNNER
# ═══════════════════════════════════════════════════════════════

def run_benchmark(providers=None):
    if providers is None:
        providers = ["ollama", "groq"]

    model_configs = []
    for p in providers:
        info = adapters.available_providers().get(p, {})
        if info.get("ready") and info.get("default_model"):
            model_configs.append((p, info["default_model"]))

    log.info("Models: %s", [f"{p}/{m}" for p, m in model_configs])
    log.info("Tests: %d  |  Strategies: baseline, react, sbsa", len(TEST_SUITE))

    all_results = []

    for provider, model in model_configs:
        model_key = f"{provider}/{model}"
        adapter = adapters.get_adapter(provider, model)

        for drift_on in [False, True]:
            strategies = ["baseline", "react", "sbsa"] if drift_on else ["baseline", "sbsa"]

            for strategy in strategies:
                log.info("\n=== %s | drift=%s | strategy=%s ===",
                         model_key, "ON" if drift_on else "OFF", strategy.upper())

                # SBSA goes through interceptor, others go direct
                if strategy == "sbsa":
                    try:
                        transport = SocketTransport()
                    except (ConnectionRefusedError, OSError):
                        log.error("Interceptor not running — skip SBSA")
                        continue
                else:
                    transport = PipeTransport()

                try:
                    tools = _handshake(transport)
                    for question, expected_tool, domain in TEST_SUITE:
                        r = run_test(adapter, question, expected_tool, tools,
                                     transport, drift_on, strategy)
                        r["model"] = model_key
                        r["domain"] = domain

                        status = "✅" if r["success"] else "❌"
                        extra = f" ({r['attempts']} attempts, {r['llm_calls']} LLM calls)" if r["attempts"] > 1 else ""
                        log.info("  %s %s → %s%s", status, question[:35], r["tool_called"], extra)

                        all_results.append(r)
                        time.sleep(0.3)
                finally:
                    transport.close()

    return all_results


# ═══════════════════════════════════════════════════════════════
# METRICS
# ═══════════════════════════════════════════════════════════════

def compute_metrics(results):
    models = sorted(set(r["model"] for r in results))
    metrics = {}

    for model in models:
        mr = [r for r in results if r["model"] == model]
        pricing = MODEL_PRICING.get(model, {"input": 0, "output": 0})

        row = {}
        for strategy in ["baseline", "react", "sbsa"]:
            sr = [r for r in mr if r["strategy"] == strategy and r["drift_on"]]
            if not sr:
                continue
            prefix = strategy

            successes = sum(1 for r in sr if r["success"])
            total = len(sr)
            row[f"{prefix}_success_rate"] = round(successes / max(total, 1) * 100, 1)
            row[f"{prefix}_avg_total_ms"] = round(sum(r["total_ms"] for r in sr) / max(total, 1), 1)
            row[f"{prefix}_avg_llm_calls"] = round(sum(r["llm_calls"] for r in sr) / max(total, 1), 2)
            row[f"{prefix}_avg_attempts"] = round(sum(r["attempts"] for r in sr) / max(total, 1), 2)

            total_in = sum(r["total_input_tokens_est"] for r in sr)
            total_out = sum(r["total_output_tokens_est"] for r in sr)
            row[f"{prefix}_total_tokens"] = total_in + total_out
            row[f"{prefix}_total_cost"] = round(
                (total_in * pricing["input"] + total_out * pricing["output"]) / 1_000_000, 6)

            # Avg reflection time (react only)
            if strategy == "react":
                reflect_times = [r["llm_reflect_ms"] for r in sr if r["llm_reflect_ms"] > 0]
                row["react_avg_reflect_ms"] = round(
                    sum(reflect_times) / max(len(reflect_times), 1), 1) if reflect_times else 0

        # Sanity: no-drift baseline
        nodrift = [r for r in mr if r["strategy"] == "baseline" and not r["drift_on"]]
        row["nodrift_baseline_success"] = round(
            sum(1 for r in nodrift if r["success"]) / max(len(nodrift), 1) * 100, 1)

        # Deltas
        base_sr = row.get("baseline_success_rate", 0)
        react_sr = row.get("react_success_rate", 0)
        sbsa_sr = row.get("sbsa_success_rate", 0)
        row["react_vs_baseline"] = round(react_sr - base_sr, 1)
        row["sbsa_vs_baseline"] = round(sbsa_sr - base_sr, 1)
        row["sbsa_vs_react"] = round(sbsa_sr - react_sr, 1)

        # Token savings: SBSA vs ReAct
        react_tok = row.get("react_total_tokens", 0)
        sbsa_tok = row.get("sbsa_total_tokens", 0)
        row["token_savings_vs_react"] = react_tok - sbsa_tok
        row["token_savings_pct"] = round(
            (react_tok - sbsa_tok) / max(react_tok, 1) * 100, 1) if react_tok else 0

        # Latency savings
        react_ms = row.get("react_avg_total_ms", 0)
        sbsa_ms = row.get("sbsa_avg_total_ms", 0)
        row["latency_savings_ms"] = round(react_ms - sbsa_ms, 1)
        row["speedup_factor"] = round(react_ms / max(sbsa_ms, 1), 1) if sbsa_ms else 0

        metrics[model] = row

    return metrics


def print_report(metrics):
    print("\n" + "═" * 95)
    print("  SBSA vs ReAct — Comparative Benchmark Report")
    print("═" * 95)

    # Table 1: Recovery Rate
    print("\n┌─ Recovery Rate under Schema Drift (%) ──────────────────────────────────────────┐")
    print(f"│ {'Model':<32s} │ {'No Drift':<9s} │ {'Baseline':<9s} │ {'ReAct':<9s} │ {'SBSA':<9s} │ {'SBSA vs ReAct':<14s} │")
    print("├" + "─"*34 + "┼" + "─"*11 + "┼" + "─"*11 + "┼" + "─"*11 + "┼" + "─"*11 + "┼" + "─"*16 + "┤")
    for model, m in metrics.items():
        nd = m.get('nodrift_baseline_success', 0)
        bl = m.get('baseline_success_rate', 0)
        re = m.get('react_success_rate', 0)
        sb = m.get('sbsa_success_rate', 0)
        delta = m.get('sbsa_vs_react', 0)
        print(f"│ {model:<32s} │ {nd:>7.1f}% │ {bl:>7.1f}% │ {re:>7.1f}% │ {sb:>7.1f}% │ {delta:>+12.1f}% │")
    print("└" + "─"*34 + "┴" + "─"*11 + "┴" + "─"*11 + "┴" + "─"*11 + "┴" + "─"*11 + "┴" + "─"*16 + "┘")

    # Table 2: Latency
    print("\n┌─ Avg Latency per Query (ms) ────────────────────────────────────────────────────┐")
    print(f"│ {'Model':<32s} │ {'Baseline':<10s} │ {'ReAct':<10s} │ {'SBSA':<10s} │ {'Speedup':<10s} │ {'Saved (ms)':<11s} │")
    print("├" + "─"*34 + "┼" + "─"*12 + "┼" + "─"*12 + "┼" + "─"*12 + "┼" + "─"*12 + "┼" + "─"*13 + "┤")
    for model, m in metrics.items():
        bl = m.get('baseline_avg_total_ms', 0)
        re = m.get('react_avg_total_ms', 0)
        sb = m.get('sbsa_avg_total_ms', 0)
        sp = m.get('speedup_factor', 0)
        sv = m.get('latency_savings_ms', 0)
        print(f"│ {model:<32s} │ {bl:>8.0f}ms │ {re:>8.0f}ms │ {sb:>8.0f}ms │ {sp:>8.1f}x  │ {sv:>+9.0f}ms │")
    print("└" + "─"*34 + "┴" + "─"*12 + "┴" + "─"*12 + "┴" + "─"*12 + "┴" + "─"*12 + "┴" + "─"*13 + "┘")

    # Table 3: Token Economics
    print("\n┌─ Token & Cost Economics ────────────────────────────────────────────────────────┐")
    print(f"│ {'Model':<32s} │ {'ReAct Tok':<10s} │ {'SBSA Tok':<10s} │ {'Saved':<8s} │ {'Saved %':<8s} │ {'ReAct $':<9s} │ {'SBSA $':<9s} │")
    print("├" + "─"*34 + "┼" + "─"*12 + "┼" + "─"*12 + "┼" + "─"*10 + "┼" + "─"*10 + "┼" + "─"*11 + "┼" + "─"*11 + "┤")
    for model, m in metrics.items():
        rt = m.get('react_total_tokens', 0)
        st = m.get('sbsa_total_tokens', 0)
        sv = m.get('token_savings_vs_react', 0)
        sp = m.get('token_savings_pct', 0)
        rc = m.get('react_total_cost', 0)
        sc = m.get('sbsa_total_cost', 0)
        print(f"│ {model:<32s} │ {rt:>10d} │ {st:>10d} │ {sv:>8d} │ {sp:>6.1f}% │ ${rc:>8.5f} │ ${sc:>8.5f} │")
    print("└" + "─"*34 + "┴" + "─"*12 + "┴" + "─"*12 + "┴" + "─"*10 + "┴" + "─"*10 + "┴" + "─"*11 + "┴" + "─"*11 + "┘")

    # Table 4: Step Efficiency
    print("\n┌─ Step Efficiency (Avg per Query) ──────────────────────────────────────────────┐")
    print(f"│ {'Model':<32s} │ {'ReAct LLM Calls':<16s} │ {'SBSA LLM Calls':<15s} │ {'ReAct Attempts':<15s} │")
    print("├" + "─"*34 + "┼" + "─"*18 + "┼" + "─"*17 + "┼" + "─"*17 + "┤")
    for model, m in metrics.items():
        rl = m.get('react_avg_llm_calls', 0)
        sl = m.get('sbsa_avg_llm_calls', 0)
        ra = m.get('react_avg_attempts', 0)
        print(f"│ {model:<32s} │ {rl:>14.1f}   │ {sl:>13.1f}   │ {ra:>13.1f}   │")
    print("└" + "─"*34 + "┴" + "─"*18 + "┴" + "─"*17 + "┴" + "─"*17 + "┘")

    print("\n═" * 95)


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", nargs="+", default=["ollama", "groq"])
    args = parser.parse_args()

    results = run_benchmark(args.models)

    ts = time.strftime('%Y%m%d_%H%M%S')
    os.makedirs("benchmark_logs", exist_ok=True)

    with open(f"benchmark_logs/bench_{ts}.json", "w") as f:
        json.dump(results, f, indent=2, default=str)

    metrics = compute_metrics(results)
    print_report(metrics)

    with open(f"benchmark_logs/bench_{ts}_metrics.json", "w") as f:
        json.dump(metrics, f, indent=2)

    # Also save as CSV
    os.makedirs("benchmark_reports", exist_ok=True)
    import csv

    with open(f"benchmark_reports/sbsa_vs_react_{ts}.csv", "w", newline="") as f:
        if metrics:
            cols = ["model"] + list(next(iter(metrics.values())).keys())
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for model, m in metrics.items():
                w.writerow({"model": model, **m})

    with open(f"benchmark_reports/raw_results_{ts}.csv", "w", newline="") as f:
        cols = ["model","question","expected_tool","tool_called","strategy","drift_on",
                "success","attempts","llm_calls","total_input_tokens_est","total_output_tokens_est",
                "llm_route_ms","llm_reflect_ms","total_ms","raw_args","final_args","error"]
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in results:
            row = dict(r)
            row["raw_args"] = json.dumps(r.get("raw_args", {}))
            row["final_args"] = json.dumps(r.get("final_args", {}))
            w.writerow(row)

    log.info("\nResults saved to benchmark_logs/ and benchmark_reports/")
