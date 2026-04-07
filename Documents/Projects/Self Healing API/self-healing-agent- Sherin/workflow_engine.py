"""
Multi-Step Workflow Engine — Compound Reliability Measurement
=============================================================

Chains multiple tool calls where each step's output feeds the next step's input.
Measures per-step and compound success rates with/without SBSA middleware.
Validates Lusser's Law: P(task) = P(step_1) × P(step_2) × ... × P(step_n)

Usage:
    # Start interceptor first: python interceptor.py
    python workflow_engine.py
"""

import json
import logging
import os
import socket
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

logging.basicConfig(level=logging.INFO, format="[workflow] %(levelname)s  %(message)s")
log = logging.getLogger("workflow")

# Load .env
from pathlib import Path
_env = Path(os.path.join(os.path.dirname(__file__), ".env"))
if _env.exists():
    for line in _env.read_text().splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

INTERCEPTOR_HOST = "127.0.0.1"
INTERCEPTOR_PORT = 6010

# ═══════════════════════════════════════════════════════════════
# WORKFLOW DEFINITIONS
# ═══════════════════════════════════════════════════════════════
# Each step: tool, args (with {var} placeholders from prior steps), extract (what to pull from result)

WORKFLOWS = {
    # ── 3-STEP WORKFLOWS ──

    "3_step_person_origin": {
        "trigger": "Where is Sherin from and what's the weather there?",
        "description": "Predict origin from name → get country details → check weather in capital",
        "input": {"name": "Sherin"},
        "steps": [
            {"tool": "predict_nationality", "args": {"name": "{name}"},      "extract": {"country": "country"}},
            {"tool": "get_country_info",    "args": {"country": "{country}"}, "extract": {"capital": "capital"}},
            {"tool": "get_weather",         "args": {"city": "{capital}"},    "extract": {"temp": "temperature_c"}},
        ],
    },
    "3_step_book_author": {
        "trigger": "Find a book about Python and tell me a joke about programming",
        "description": "Search book → get joke in same category → suggest an activity",
        "input": {"query": "Python programming"},
        "steps": [
            {"tool": "search_books",  "args": {"query": "{query}"},          "extract": {"title": "title", "author": "author"}},
            {"tool": "get_joke",      "args": {"category": "Programming"},   "extract": {"joke": "setup"}},
            {"tool": "get_activity",  "args": {"type": "education"},         "extract": {"activity": "activity"}},
        ],
    },

    # ── 5-STEP WORKFLOWS ──

    "5_step_trip_planner": {
        "trigger": "Plan a trip to wherever Sherin is from — weather, costs, timezone, things to do",
        "description": "Name → nationality → country info → weather in capital → exchange rate → local time",
        "input": {"name": "Sherin"},
        "steps": [
            {"tool": "predict_nationality", "args": {"name": "{name}"},               "extract": {"country": "country"}},
            {"tool": "get_country_info",    "args": {"country": "{country}"},          "extract": {"capital": "capital"}},
            {"tool": "get_weather",         "args": {"city": "{capital}"},             "extract": {"temp": "temperature_c"}},
            {"tool": "get_exchange_rate",   "args": {"base": "USD", "target": "INR"}, "extract": {"rate": "rate"}},
            {"tool": "get_activity",        "args": {"type": "recreational"},          "extract": {"activity": "activity"}},
        ],
    },
    "5_step_crypto_investor": {
        "trigger": "I want to buy Bitcoin — what's the price, convert to INR, and tell me about India's economy",
        "description": "Crypto price → exchange rate → country info → weather in financial capital → age prediction for fun",
        "input": {"coin": "bitcoin", "currency": "usd"},
        "steps": [
            {"tool": "get_crypto_price",  "args": {"coin": "{coin}", "currency": "{currency}"}, "extract": {"price": "price"}},
            {"tool": "get_exchange_rate", "args": {"base": "USD", "target": "INR"},              "extract": {"rate": "rate"}},
            {"tool": "get_country_info",  "args": {"country": "India"},                          "extract": {"capital": "capital", "population": "population"}},
            {"tool": "get_weather",       "args": {"city": "{capital}"},                          "extract": {"temp": "temperature_c"}},
            {"tool": "predict_age",       "args": {"name": "Satoshi"},                           "extract": {"age": "predicted_age"}},
        ],
    },

    # ── 7-STEP WORKFLOW ──

    "7_step_person_dossier": {
        "trigger": "Tell me everything about someone named Sherin — origin, age, gender, their country, weather, currency, and a fun fact",
        "description": "Full person profile: gender → age → nationality → country → weather → exchange rate → random word",
        "input": {"name": "Sherin"},
        "steps": [
            {"tool": "predict_gender",      "args": {"name": "{name}"},               "extract": {"gender": "gender"}},
            {"tool": "predict_age",         "args": {"name": "{name}"},               "extract": {"age": "predicted_age"}},
            {"tool": "predict_nationality", "args": {"name": "{name}"},               "extract": {"country": "country"}},
            {"tool": "get_country_info",    "args": {"country": "{country}"},          "extract": {"capital": "capital"}},
            {"tool": "get_weather",         "args": {"city": "{capital}"},             "extract": {"temp": "temperature_c"}},
            {"tool": "get_exchange_rate",   "args": {"base": "USD", "target": "INR"}, "extract": {"rate": "rate"}},
            {"tool": "define_word",         "args": {"word": "serendipity"},           "extract": {"meaning": "word"}},
        ],
    },

    # ── 10-STEP WORKFLOW ──

    "10_step_world_explorer": {
        "trigger": "I'm curious about someone named Sherin — find out everything: who they might be, where they're from, what it's like there, costs, entertainment, and fun facts",
        "description": "Complete world exploration: person profiling → country deep-dive → entertainment → knowledge",
        "input": {"name": "Sherin", "second_country": "France"},
        "steps": [
            {"tool": "predict_gender",      "args": {"name": "{name}"},                "extract": {"gender": "gender"}},
            {"tool": "predict_age",         "args": {"name": "{name}"},                "extract": {"age": "predicted_age"}},
            {"tool": "predict_nationality", "args": {"name": "{name}"},                "extract": {"country": "country"}},
            {"tool": "get_country_info",    "args": {"country": "{country}"},           "extract": {"capital": "capital"}},
            {"tool": "get_weather",         "args": {"city": "{capital}"},              "extract": {"temp": "temperature_c"}},
            {"tool": "get_exchange_rate",   "args": {"base": "USD", "target": "INR"},  "extract": {"rate": "rate"}},
            {"tool": "get_crypto_price",    "args": {"coin": "bitcoin", "currency": "usd"}, "extract": {"btc_price": "price"}},
            {"tool": "search_books",        "args": {"query": "{country} travel guide"},     "extract": {"book": "title"}},
            {"tool": "get_joke",            "args": {"category": "Programming"},       "extract": {"joke": "setup"}},
            {"tool": "get_activity",        "args": {"type": "social"},                "extract": {"activity": "activity"}},
        ],
    },
}


# ═══════════════════════════════════════════════════════════════
# TRANSPORT
# ═══════════════════════════════════════════════════════════════

class Transport:
    """Connects to interceptor (SBSA ON) or directly to server (SBSA OFF)."""

    def __init__(self, use_interceptor: bool):
        self.use_interceptor = use_interceptor
        if use_interceptor:
            self._sock = socket.create_connection((INTERCEPTOR_HOST, INTERCEPTOR_PORT), timeout=5)
            self._sock.settimeout(60)
            self._buf = ""
        else:
            self._proc = subprocess.Popen(
                [sys.executable, "mcp_server.py"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
            )

    def send(self, payload: dict) -> dict:
        if self.use_interceptor:
            self._sock.sendall((json.dumps(payload) + "\n").encode())
            while True:
                if "\n" in self._buf:
                    line, self._buf = self._buf.split("\n", 1)
                    if line.strip().startswith("{"):
                        return json.loads(line.strip())
                    continue
                chunk = self._sock.recv(4096)
                if not chunk:
                    raise RuntimeError("Connection closed")
                self._buf += chunk.decode(errors="replace")
        else:
            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()
            while True:
                line = self._proc.stdout.readline()
                if not line:
                    raise RuntimeError("Server exited")
                if line.strip().startswith("{"):
                    return json.loads(line.strip())

    def notify(self, payload: dict):
        if self.use_interceptor:
            self._sock.sendall((json.dumps(payload) + "\n").encode())
        else:
            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()

    def close(self):
        try:
            if self.use_interceptor:
                self._sock.close()
            else:
                self._proc.terminate()
        except Exception:
            pass


def _handshake(transport: Transport):
    transport.send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                               "clientInfo": {"name": "workflow-engine", "version": "1.0"}}})
    transport.notify({"jsonrpc": "2.0", "method": "initialized"})


def _set_drift(transport: Transport, active: bool):
    transport.send({"jsonrpc": "2.0", "id": 2, "method": "set_drift", "params": {"active": active}})


# ═══════════════════════════════════════════════════════════════
# WORKFLOW EXECUTOR
# ═══════════════════════════════════════════════════════════════

def _resolve_args(args_template: dict, context: dict) -> dict:
    """Replace {var} placeholders in args with values from context."""
    resolved = {}
    for k, v in args_template.items():
        if isinstance(v, str) and v.startswith("{") and v.endswith("}"):
            var = v[1:-1]
            resolved[k] = str(context.get(var, v))
        else:
            resolved[k] = v
    return resolved


def run_workflow(
    workflow_name: str,
    transport: Transport,
    drift: bool = False,
) -> Dict[str, Any]:
    """
    Execute a multi-step workflow. Returns per-step results and compound metrics.
    """
    wf = WORKFLOWS[workflow_name]
    steps = wf["steps"]
    context = dict(wf["input"])
    step_results = []
    all_success = True

    log.info("━" * 60)
    log.info("WORKFLOW: %s (%d steps) | drift=%s", workflow_name, len(steps), drift)
    log.info("━" * 60)

    _set_drift(transport, drift)
    t_start = time.monotonic()

    for i, step in enumerate(steps):
        args = _resolve_args(step["args"], context)
        t0 = time.monotonic()

        log.info("  Step %d/%d: %s(%s)", i + 1, len(steps), step["tool"], args)

        try:
            resp = transport.send({
                "jsonrpc": "2.0", "id": 100 + i, "method": "tools/call",
                "params": {"name": step["tool"], "arguments": args},
            })
            elapsed = time.monotonic() - t0

            if "error" in resp:
                log.warning("  Step %d FAILED: %s", i + 1, resp["error"].get("message", "")[:80])
                step_results.append({
                    "step": i + 1, "tool": step["tool"], "args": args,
                    "success": False, "error": resp["error"].get("message", ""),
                    "elapsed_s": round(elapsed, 2),
                })
                all_success = False
                # Chain breaks — remaining steps can't get input
                for j in range(i + 1, len(steps)):
                    step_results.append({
                        "step": j + 1, "tool": steps[j]["tool"],
                        "success": False, "error": "skipped (prior step failed)",
                        "elapsed_s": 0,
                    })
                break

            data = resp["result"].get("structuredContent", resp["result"])
            log.info("  Step %d OK: %s (%.2fs)", i + 1, {k: str(v)[:40] for k, v in data.items() if not k.startswith("_")}, elapsed)

            # Extract values into context for next step
            for ctx_key, data_key in step["extract"].items():
                if data_key in data:
                    context[ctx_key] = data[data_key]

            step_results.append({
                "step": i + 1, "tool": step["tool"], "args": args,
                "success": True, "result": {k: v for k, v in data.items() if not k.startswith("_")},
                "elapsed_s": round(elapsed, 2),
            })

        except Exception as e:
            elapsed = time.monotonic() - t0
            log.error("  Step %d EXCEPTION: %s", i + 1, e)
            step_results.append({
                "step": i + 1, "tool": step["tool"], "args": args,
                "success": False, "error": str(e), "elapsed_s": round(elapsed, 2),
            })
            all_success = False
            for j in range(i + 1, len(steps)):
                step_results.append({
                    "step": j + 1, "tool": steps[j]["tool"],
                    "success": False, "error": "skipped (prior step failed)",
                    "elapsed_s": 0,
                })
            break

    total_elapsed = time.monotonic() - t_start
    steps_succeeded = sum(1 for s in step_results if s["success"])
    per_step_rate = steps_succeeded / len(steps)

    result = {
        "workflow": workflow_name,
        "num_steps": len(steps),
        "drift": drift,
        "task_success": all_success,
        "steps_succeeded": steps_succeeded,
        "per_step_accuracy": round(per_step_rate, 4),
        "total_elapsed_s": round(total_elapsed, 2),
        "step_results": step_results,
        "context": {k: str(v)[:50] for k, v in context.items()},
    }

    status = "✅ SUCCESS" if all_success else f"❌ FAILED at step {steps_succeeded + 1}"
    log.info("  %s | %d/%d steps | %.2fs", status, steps_succeeded, len(steps), total_elapsed)
    return result


# ═══════════════════════════════════════════════════════════════
# COMPOUND RELIABILITY MEASUREMENT
# ═══════════════════════════════════════════════════════════════

def measure_compound_reliability(
    runs_per_workflow: int = 3,
    workflows: List[str] = None,
) -> Dict[str, Any]:
    """
    Run all workflows with SBSA ON, SBSA OFF, and SBSA+drift.
    Compute per-step accuracy and compound success rates.
    Compare against Lusser's Law prediction.
    """
    workflows = workflows or list(WORKFLOWS.keys())
    results = {"sbsa_on": {}, "sbsa_off": {}, "sbsa_drift": {}}

    for mode, use_interceptor, drift in [
        ("sbsa_on", True, False),
        ("sbsa_off", False, False),
        ("sbsa_drift", True, True),
    ]:
        log.info("\n" + "=" * 60)
        log.info("MODE: %s (interceptor=%s, drift=%s)", mode, use_interceptor, drift)
        log.info("=" * 60)

        try:
            transport = Transport(use_interceptor)
            _handshake(transport)
        except Exception as e:
            log.error("Cannot connect for mode %s: %s", mode, e)
            if mode == "sbsa_on" or mode == "sbsa_drift":
                log.error("Start interceptor: python interceptor.py")
            continue

        for wf_name in workflows:
            wf_results = []
            for run in range(runs_per_workflow):
                log.info("\n--- %s | %s | run %d/%d ---", mode, wf_name, run + 1, runs_per_workflow)
                r = run_workflow(wf_name, transport, drift=drift)
                wf_results.append(r)

            # Aggregate
            num_steps = WORKFLOWS[wf_name]["steps"].__len__()
            task_successes = sum(1 for r in wf_results if r["task_success"])
            all_step_successes = sum(r["steps_succeeded"] for r in wf_results)
            total_steps = num_steps * runs_per_workflow

            per_step_acc = all_step_successes / total_steps if total_steps else 0
            compound_measured = task_successes / runs_per_workflow
            lusser_prediction = per_step_acc ** num_steps

            results[mode][wf_name] = {
                "num_steps": num_steps,
                "runs": runs_per_workflow,
                "task_success_rate": round(compound_measured, 4),
                "per_step_accuracy": round(per_step_acc, 4),
                "lusser_prediction": round(lusser_prediction, 4),
                "lusser_delta": round(compound_measured - lusser_prediction, 4),
                "avg_elapsed_s": round(sum(r["total_elapsed_s"] for r in wf_results) / runs_per_workflow, 2),
            }

        transport.close()

    return results


# ═══════════════════════════════════════════════════════════════
# LLM-DRIVEN WORKFLOW EXECUTOR
# ═══════════════════════════════════════════════════════════════
# The LLM plans and executes — decides which tools to call and in what order.
# SBSA only heals the tool calls, doesn't influence tool selection.

def run_llm_driven(
    trigger: str,
    transport: Transport,
    adapter,
    tools: list,
    max_steps: int = 10,
    drift: bool = False,
) -> Dict[str, Any]:
    """
    Give the LLM a trigger question + tool list. Let it plan and execute.
    Measure per-step success and compound task completion.
    """
    _set_drift(transport, drift)
    log.info("━" * 60)
    log.info("LLM-DRIVEN: %s (max %d steps, drift=%s)", trigger[:60], max_steps, drift)
    log.info("━" * 60)

    step_results = []
    conversation = [
        {"role": "system", "content": (
            "You are a tool-calling agent. Answer the user's question by calling tools one at a time. "
            "After each tool result, decide if you need another tool call or can give a final answer. "
            "When you have enough information, respond with a comprehensive final answer (no tool call)."
        )},
        {"role": "user", "content": trigger},
    ]
    t_start = time.monotonic()
    final_answer = None

    for step_num in range(1, max_steps + 1):
        t0 = time.monotonic()

        # Ask LLM what to do next
        try:
            decision = adapter.route(trigger if step_num == 1 else conversation[-1]["content"], tools)
        except Exception as e:
            log.error("  Step %d LLM error: %s", step_num, e)
            step_results.append({"step": step_num, "success": False, "error": f"LLM error: {e}"})
            break

        # LLM decided to give final answer — done
        if "final" in decision:
            final_answer = decision["final"]
            log.info("  Step %d: LLM gave final answer (%.1fs)", step_num, time.monotonic() - t0)
            break

        tool_name = decision.get("tool", "")
        raw_args = decision.get("arguments", {})
        log.info("  Step %d: LLM chose %s(%s)", step_num, tool_name, raw_args)

        # Execute tool call through transport (interceptor or direct)
        try:
            resp = transport.send({
                "jsonrpc": "2.0", "id": 200 + step_num, "method": "tools/call",
                "params": {"name": tool_name, "arguments": raw_args},
            })
            elapsed = time.monotonic() - t0

            if "error" in resp:
                err_msg = resp["error"].get("message", "")[:100]
                log.warning("  Step %d FAILED: %s (%.2fs)", step_num, err_msg, elapsed)
                step_results.append({
                    "step": step_num, "tool": tool_name, "args": raw_args,
                    "success": False, "error": err_msg, "elapsed_s": round(elapsed, 2),
                })
                # Feed error back to LLM so it can adapt
                conversation.append({"role": "assistant", "content": f"Tool {tool_name} failed: {err_msg}"})
                conversation.append({"role": "user", "content": "That tool failed. Try a different approach or give your best answer with what you have."})
                continue

            data = resp["result"].get("structuredContent", resp["result"])
            clean = {k: v for k, v in data.items() if not k.startswith("_")} if isinstance(data, dict) else data
            log.info("  Step %d OK: %s (%.2fs)", step_num, str(clean)[:80], elapsed)

            step_results.append({
                "step": step_num, "tool": tool_name, "args": raw_args,
                "success": True, "result": clean, "elapsed_s": round(elapsed, 2),
            })

            # Feed result back to LLM for next decision
            result_summary = json.dumps(clean)[:500]
            conversation.append({"role": "assistant", "content": f"Called {tool_name}: {result_summary}"})
            conversation.append({"role": "user", "content": f"Result from {tool_name}: {result_summary}\n\nDo you need to call another tool to fully answer the original question, or can you give a final answer now?"})

        except Exception as e:
            elapsed = time.monotonic() - t0
            log.error("  Step %d EXCEPTION: %s", step_num, e)
            step_results.append({
                "step": step_num, "tool": tool_name, "args": raw_args,
                "success": False, "error": str(e), "elapsed_s": round(elapsed, 2),
            })
            break

    total_elapsed = time.monotonic() - t_start
    steps_taken = len(step_results)
    steps_succeeded = sum(1 for s in step_results if s["success"])
    per_step_acc = steps_succeeded / steps_taken if steps_taken else 0
    task_success = steps_succeeded == steps_taken and final_answer is not None

    log.info("  %s | %d/%d steps succeeded | %.2fs",
             "✅" if task_success else "❌", steps_succeeded, steps_taken, total_elapsed)

    return {
        "trigger": trigger,
        "steps_taken": steps_taken,
        "steps_succeeded": steps_succeeded,
        "per_step_accuracy": round(per_step_acc, 4),
        "task_success": task_success,
        "final_answer": final_answer,
        "total_elapsed_s": round(total_elapsed, 2),
        "step_results": step_results,
        "drift": drift,
    }


# ═══════════════════════════════════════════════════════════════
# LLM-DRIVEN BENCHMARK
# ═══════════════════════════════════════════════════════════════

LLM_TRIGGERS = [
    "Where is Sherin from and what's the weather like there?",
    "I want to buy Bitcoin — what's the price in USD and INR?",
    "Plan a trip to Japan — weather, currency exchange, and things to do",
    "Tell me everything about someone named Sherin — origin, age, gender, their country's weather",
    "Find a book about Python, tell me a programming joke, and suggest something fun to do",
    "What's the weather in the capital of France, and how much is 1 EUR in USD?",
    "Get me a random cocktail recipe and a joke to go with it",
    "What Pokemon is #25, and what's the weather in Tokyo?",
]


def run_llm_driven_benchmark(
    provider: str = "groq",
    model: str = "llama-3.3-70b-versatile",
    runs_per_trigger: int = 1,
) -> Dict[str, Any]:
    """Run LLM-driven workflows with and without SBSA."""
    import adapters
    adapter = adapters.get_adapter(provider, model)
    results = {"sbsa_on": [], "sbsa_off": []}

    for mode, use_interceptor, drift in [
        ("sbsa_off", False, False),
        ("sbsa_on", True, False),
    ]:
        log.info("\n" + "=" * 60)
        log.info("LLM-DRIVEN MODE: %s", mode)
        log.info("=" * 60)

        try:
            transport = Transport(use_interceptor)
            _handshake(transport)
            tools_resp = transport.send({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
            tools = tools_resp.get("result", {}).get("tools", [])
        except Exception as e:
            log.error("Cannot connect for %s: %s", mode, e)
            continue

        for trigger in LLM_TRIGGERS:
            for run in range(runs_per_trigger):
                r = run_llm_driven(trigger, transport, adapter, tools, drift=drift)
                results[mode].append(r)

        transport.close()

    # Print comparison
    print("\n" + "=" * 80)
    print("  LLM-DRIVEN WORKFLOW COMPARISON")
    print("=" * 80)
    for mode in ["sbsa_off", "sbsa_on"]:
        runs = results[mode]
        if not runs:
            continue
        label = "Baseline" if mode == "sbsa_off" else "SBSA"
        total = len(runs)
        task_ok = sum(1 for r in runs if r["task_success"])
        avg_steps = sum(r["steps_taken"] for r in runs) / total if total else 0
        avg_step_acc = sum(r["per_step_accuracy"] for r in runs) / total if total else 0
        avg_time = sum(r["total_elapsed_s"] for r in runs) / total if total else 0
        print(f"  {label:<10} | Tasks: {task_ok}/{total} ({task_ok/total*100:.0f}%) | "
              f"Avg steps: {avg_steps:.1f} | Step acc: {avg_step_acc:.0%} | Avg time: {avg_time:.1f}s")
    print("=" * 80)

    return results


# ═══════════════════════════════════════════════════════════════
# REPORT
# ═══════════════════════════════════════════════════════════════

def print_report(results: Dict[str, Any]):
    print("\n" + "=" * 90)
    print("  COMPOUND RELIABILITY REPORT — Lusser's Law Validation")
    print("=" * 90)

    # Header
    print(f"\n{'Workflow':<22} {'Steps':>5} │ {'Mode':<12} {'Task✓':>6} {'Step✓':>7} {'Lusser':>7} {'Δ':>7} {'Time':>6}")
    print("─" * 90)

    for wf_name in WORKFLOWS:
        first = True
        for mode in ["sbsa_off", "sbsa_on", "sbsa_drift"]:
            if wf_name not in results.get(mode, {}):
                continue
            r = results[mode][wf_name]
            label = {"sbsa_off": "Baseline", "sbsa_on": "SBSA", "sbsa_drift": "SBSA+Drift"}[mode]
            name_col = f"{wf_name}" if first else ""
            steps_col = f"{r['num_steps']}" if first else ""
            print(f"{name_col:<22} {steps_col:>5} │ {label:<12} {r['task_success_rate']:>5.0%} {r['per_step_accuracy']:>6.0%} {r['lusser_prediction']:>6.0%} {r['lusser_delta']:>+6.0%} {r['avg_elapsed_s']:>5.1f}s")
            first = False
        print("─" * 90)

    # Summary
    for mode, label in [("sbsa_off", "Baseline"), ("sbsa_on", "SBSA"), ("sbsa_drift", "SBSA+Drift")]:
        if mode not in results or not results[mode]:
            continue
        vals = results[mode].values()
        avg_step = sum(v["per_step_accuracy"] for v in vals) / len(list(vals))
        avg_task = sum(v["task_success_rate"] for v in vals) / len(list(vals))
        print(f"  {label:<12} avg per-step: {avg_step:.0%}  avg task: {avg_task:.0%}")

    print("=" * 90)


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["controlled", "llm", "both"], default="both",
                        help="controlled=hardcoded chains, llm=LLM-driven, both=run both")
    parser.add_argument("--provider", default="groq")
    parser.add_argument("--model", default="llama-3.3-70b-versatile")
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()

    all_results = {}

    if args.mode in ("controlled", "both"):
        log.info("\n\n▶ CONTROLLED WORKFLOWS (hardcoded chains)")
        controlled = measure_compound_reliability(runs_per_workflow=args.runs)
        all_results["controlled"] = controlled
        print_report(controlled)

    if args.mode in ("llm", "both"):
        log.info("\n\n▶ LLM-DRIVEN WORKFLOWS (LLM plans + executes)")
        llm_results = run_llm_driven_benchmark(
            provider=args.provider, model=args.model, runs_per_trigger=1,
        )
        all_results["llm_driven"] = llm_results

    out_path = os.path.join(os.path.dirname(__file__), "workflow_results.json")
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    log.info("Results saved to %s", out_path)
