"""
Analytics Logger — SBSA Benchmark Recorder
===========================================

Records per-call metrics for comparing:
  - SBSA deterministic healing vs LLM-reflection retry
  - Performance across different LLM models (Llama3, GPT, Claude)
  - Healing accuracy across API domains (Finance, Music, Commerce, etc.)

Logs to JSON-lines file for easy analysis with pandas/matplotlib.
"""

import json
import logging
import os
import time
from typing import Any, Dict, Optional

log = logging.getLogger("analytics")

LOG_DIR = os.path.join(os.path.dirname(__file__), "benchmark_logs")
os.makedirs(LOG_DIR, exist_ok=True)

_log_path = os.path.join(LOG_DIR, f"sbsa_benchmark_{time.strftime('%Y%m%d_%H%M%S')}.jsonl")
_log_file = None


def _writer():
    global _log_file
    if _log_file is None:
        _log_file = open(_log_path, "a")
        log.info("Analytics logging to: %s", _log_path)
    return _log_file


def record_healing_event(
    tool: str,
    model: str,
    domain: str,
    agent_keys: list,
    api_keys: list,
    mapping: Dict[str, str],
    similarity_scores: Dict[str, float],
    cost_matrix: list,
    sbsa_elapsed_ms: float,
    llm_elapsed_ms: Optional[float] = None,
    healed_successfully: bool = True,
    threshold: float = 0.4,
    schema_version: str = "v1",
):
    """Record a single healing event to the benchmark log."""
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "tool": tool,
        "model": model,
        "domain": domain,
        "schema_version": schema_version,
        "agent_keys": agent_keys,
        "api_keys": api_keys,
        "mapping": mapping,
        "similarity_scores": similarity_scores,
        "cost_matrix": cost_matrix,
        "sbsa_elapsed_ms": round(sbsa_elapsed_ms, 2),
        "llm_elapsed_ms": round(llm_elapsed_ms, 2) if llm_elapsed_ms else None,
        "speedup_factor": round(llm_elapsed_ms / max(sbsa_elapsed_ms, 0.01), 1) if llm_elapsed_ms else None,
        "healed_successfully": healed_successfully,
        "threshold": threshold,
        "num_params": len(agent_keys),
    }
    f = _writer()
    f.write(json.dumps(entry) + "\n")
    f.flush()


def record_pipeline_event(
    tool: str,
    model: str,
    outcome: str,
    total_elapsed_s: float,
    retries: int = 0,
    error_type: Optional[str] = None,
    sbsa_report: Optional[Dict[str, Any]] = None,
):
    """Record a full pipeline execution (including retries, errors)."""
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "event": "pipeline",
        "tool": tool,
        "model": model,
        "outcome": outcome,
        "total_elapsed_s": round(total_elapsed_s, 3),
        "retries": retries,
        "error_type": error_type,
    }
    if sbsa_report:
        entry["sbsa_elapsed_ms"] = round(sbsa_report.get("elapsed_ms", 0), 2)
        entry["sbsa_mapping"] = sbsa_report.get("mapping", {})
        entry["sbsa_similarities"] = sbsa_report.get("similarity_scores", {})
    f = _writer()
    f.write(json.dumps(entry) + "\n")
    f.flush()


def close():
    global _log_file
    if _log_file:
        _log_file.close()
        _log_file = None
