"""
Self-Healing MCP Interceptor — Independent Proxy
=================================================

Architecture:
    mcp_client.py  ──►  interceptor.py (TCP :6010)  ──►  mcp_server.py (subprocess)

Pipeline for every tools/call
──────────────────────────────
  1. TYPE COERCION       — wrong-type args (int/list instead of string) silently cast.
  2. SCHEMA HEALING      — LLM maps client args to exact server schema (field renames +
                           value normalisation). Runs BEFORE extra-field strip so that
                           misnamed keys like "location" are mapped to "city" rather
                           than being dropped, leaving the healer with nothing to work from.
  3. EXTRA FIELD STRIP   — unknown keys removed AFTER healing.
  4. FORWARD             — repaired request sent to mcp_server.py.
  5. DRIFT RECOVERY      — SBSA latches drift from the server; args re-healed to drift_schema and retried.
  6. RATE-LIMIT (429)    — Retry-After header respected; deterministic sleep + retry.
  7. API TIMEOUT/5xx     — upstream HTTP timeouts AND 5xx errors retried with back-off;
                           LLM decides retry | fallback stub | fail. Per-attempt timing
                           logged to terminal. Separate retry budget (API_TIMEOUT_RETRIES)
                           from network errors (MAX_RETRIES).
  8. PARTIAL RESULT      — missing expected fields detected; warning annotated.
  9. STALE RESULT        — out-of-range numeric values flagged with a warning.
 10. CASCADING TOOLS     — result feeds a second tool call automatically when useful.
 11. RESULT REASSESSMENT — final LLM sanity check; suspicious data annotated.

Notifications (no "id") are forwarded fire-and-forget — they never block.
Pass-through requests (initialize, tools/list) are forwarded transparently.

Run:  python interceptor.py
"""

import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Optional, Tuple

from api_registry import API_REGISTRY as TOOL_REGISTRY
import ollama
import sbsa_engine
import analytics_logger


# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════

PROXY_HOST          = "127.0.0.1"
PROXY_PORT          = 6010
LLM_MODEL           = "llama3"
MAX_RETRIES         = 3
BACKOFF_BASE_S      = 1.5
MAX_RATE_LIMIT_WAIT = 60
MAX_CASCADE_DEPTH   = 2
API_TIMEOUT_RETRIES = 3      # max retries for upstream API timeouts / 5xx (error_type="timeout"/"api")
API_BACKOFF_BASE_S  = 2.0    # back-off seconds for API-level retries

logging.basicConfig(
    level=logging.INFO,
    format="[interceptor] %(levelname)s  %(message)s",
)
log = logging.getLogger("interceptor")

_NOTIFICATIONS = {"initialized", "notifications/initialized", "notifications/cancelled"}


# ═══════════════════════════════════════════════════════════════
# SERVER MANAGER
# ═══════════════════════════════════════════════════════════════

def _start_server() -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "mcp_server.py"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    log.info("mcp_server.py spawned  pid=%d", proc.pid)
    return proc


class ServerManager:
    """
    Owns the single mcp_server.py subprocess and a global lock that
    serialises all stdin/stdout access across threads.
    Auto-restarts the subprocess if it dies.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._proc = _start_server()

    def send(self, payload: dict) -> dict:
        with self._lock:
            if self._proc.poll() is not None:
                log.warning("mcp_server.py exited (rc=%d) — restarting ...", self._proc.returncode)
                self._proc = _start_server()
            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()
            while True:
                line = self._proc.stdout.readline()
                if not line:
                    log.error("mcp_server.py closed stdout — restarting ...")
                    self._proc = _start_server()
                    raise RuntimeError("mcp_server.py closed stdout unexpectedly; restarted")
                line = line.strip()
                if line.startswith("{"):
                    return json.loads(line)

    def notify(self, payload: dict):
        with self._lock:
            if self._proc.poll() is not None:
                self._proc = _start_server()
            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()

    def terminate(self):
        with self._lock:
            self._proc.terminate()


# ═══════════════════════════════════════════════════════════════
# TOOL REGISTRY — loaded from api_registry.py  (SBSA: baseline schema + drift shape + aliases)
# ═══════════════════════════════════════════════════════════════

from api_registry import API_REGISTRY
import schema_discovery

TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {}
for _name, _info in API_REGISTRY.items():
    _v1 = _info["v1_schema"]
    _v2 = _info["v2_schema"]
    TOOL_REGISTRY[_name] = {
        "description":     _info["description"],
        "category":        _info["domain"],
        "required":        [k for k, v in _v1.items() if v.get("required")],
        "defaults":        {},
        "schema":          {k: v.get("description", "") for k, v in _v1.items()},
        "drift_schema":    {k: v.get("description", "") for k, v in _v2.items()},
        "drift_aliases":   {k1: k2 for k1, k2 in zip(_v1.keys(), _v2.keys()) if k1 != k2},
        "expected_result": [],
        "result_ranges":   {},
        "cascade":         _info.get("cascade"),
        "docs_url":        _info.get("docs_url"),
        "base_url":        _info.get("base_url"),
    }

log.info("Interceptor registry: %d tools from api_registry", len(TOOL_REGISTRY))

# Set True after the server returns error_type=drift; further heals use drift_schema.
_sbsa_drift_active: bool = False


# ═══════════════════════════════════════════════════════════════
# LLM HELPER
# ═══════════════════════════════════════════════════════════════

def _llm(prompt: str, label: str) -> Optional[dict]:
    try:
        resp   = ollama.chat(
            model=LLM_MODEL,
            format="json",
            messages=[{"role": "user", "content": prompt}],
        )
        parsed = json.loads(resp["message"]["content"])
        log.debug("  LLM[%s] -> %s", label, parsed)
        return parsed
    except json.JSONDecodeError as exc:
        log.warning("  LLM[%s] returned non-JSON: %s", label, exc)
        return None
    except Exception as exc:
        log.warning("  LLM[%s] call failed: %s", label, exc)
        return None


# ═══════════════════════════════════════════════════════════════
# STAGE 0 — FUZZY TOOL-NAME MATCHING (tool hallucination recovery)
# ═══════════════════════════════════════════════════════════════

def fuzzy_match_tool(tool_name: str) -> str:
    """If tool_name isn't registered, find the closest match via embeddings."""
    if tool_name in TOOL_REGISTRY:
        return tool_name
    known = list(TOOL_REGISTRY.keys())
    if not known:
        return tool_name
    try:
        from sbsa_engine import _encode_keys
        query_emb = _encode_keys([tool_name.replace("_", " ")])
        known_emb = _encode_keys([k.replace("_", " ") for k in known])
        sims = (query_emb @ known_emb.T)[0]
        best_idx = int(sims.argmax())
        best_sim = float(sims[best_idx])
        if best_sim >= 0.5:
            log.warning(
                "  FuzzyTool | '%s' not found → matched '%s' (sim=%.3f)",
                tool_name, known[best_idx], best_sim,
            )
            return known[best_idx]
        log.warning("  FuzzyTool | '%s' not found, best match '%s' too weak (sim=%.3f)", tool_name, known[best_idx], best_sim)
    except Exception as e:
        log.warning("  FuzzyTool | matching failed: %s", e)
    return tool_name


# ═══════════════════════════════════════════════════════════════
# STAGE 1 — TYPE COERCION
# ═══════════════════════════════════════════════════════════════

def coerce_types(tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Cast non-string arg values to string. Unwrap single-element lists."""
    coerced = {}
    changed = []
    for k, v in args.items():
        if isinstance(v, list):
            new_v = v[0] if len(v) == 1 else ", ".join(str(x) for x in v)
            coerced[k] = str(new_v)
            changed.append(f"{k}: {v!r} -> {coerced[k]!r}")
        elif not isinstance(v, str):
            coerced[k] = str(v)
            changed.append(f"{k}: {v!r} -> {coerced[k]!r}")
        else:
            coerced[k] = v
    if changed:
        log.info("  TypeCoercion | fixed: %s", ", ".join(changed))
    return coerced


# ═══════════════════════════════════════════════════════════════
# STAGE 2 — SBSA SCHEMA HEALING + VALUE NORMALISATION
# ═══════════════════════════════════════════════════════════════
#
# IMPORTANT: healing runs BEFORE strip_extra_fields.
#
# If we strip first, a misnamed field like {"location": "London"} becomes {}
# because "location" is not a known schema key. The healer then has nothing
# to work from and falls back to the default city ("Delhi").
#
# With healing first, the LLM sees {"location": "London"} and correctly
# maps it to {"city": "London"}. The strip then passes through cleanly.

class SBSA:
    """
    Schema-Based Self-Healing Adapter: maps client args onto the live server field names
    (baseline schema, or drift_schema after the server signals a rename).

    Uses sentence-transformers + Hungarian Algorithm instead of LLM inference.
    Runs in <100ms. No probabilistic guessing.

    SHORT-CIRCUIT: if all required fields already match, args pass through unchanged.
    """

    # Store last alignment report for analytics
    last_report: Optional[Dict[str, Any]] = None

    @staticmethod
    def target_schema(info: Dict[str, Any]) -> Dict[str, Any]:
        if _sbsa_drift_active and info.get("drift_schema"):
            return info["drift_schema"]
        return info.get("schema") or {}

    @staticmethod
    def target_field_keys(tool: str) -> list:
        info = TOOL_REGISTRY.get(tool)
        if not info:
            return []
        return list(SBSA.target_schema(info).keys())

    @staticmethod
    def args_satisfy(tool: str, args: Dict[str, Any]) -> bool:
        info = TOOL_REGISTRY.get(tool)
        if not info or not info["required"]:
            return True
        keys = SBSA.target_field_keys(tool)
        if not keys:
            return True
        return all(str(args.get(f, "")).strip() for f in keys)

    @staticmethod
    def mapped_defaults(info: Dict[str, Any]) -> Dict[str, Any]:
        base = dict(info.get("defaults") or {})
        if not _sbsa_drift_active:
            return base
        aliases = info.get("drift_aliases") or {}
        if not aliases:
            return base
        return {aliases.get(k, k): v for k, v in base.items()}

    @classmethod
    def heal(cls, tool: str, raw_args: Dict[str, Any]) -> Dict[str, Any]:
        info = TOOL_REGISTRY.get(tool)
        if info is None:
            log.warning("  SBSA | unknown tool '%s' — passing through", tool)
            return raw_args

        # Tools with no required args need no healing
        if not info["required"]:
            return {}

        schema        = SBSA.target_schema(info)
        server_fields = list(schema.keys())
        using_drift   = bool(_sbsa_drift_active and info.get("drift_schema"))
        mode          = "drift" if using_drift else "baseline"

        # Short-circuit: args already match the current server schema
        if server_fields and all(str(raw_args.get(f, "")).strip() for f in server_fields):
            log.info("  SBSA | args already valid (%s) — pass-through  %s", mode, raw_args)
            cls.last_report = None
            return raw_args

        # ──────────────────────────────────────────────
        # SBSA HEALING
        # ──────────────────────────────────────────────

        log.info(
            "  SBSA | healing '%s'  mode=%s  raw=%s",
            tool, mode, raw_args,
        )

        # Run deterministic SBSA alignment
        agent_keys = list(raw_args.keys())

        # Get descriptions from both schema versions for semantic enrichment
        baseline_schema = info.get("schema", {})
        drift_schema_desc = info.get("drift_schema", {})
        # Agent descriptions: try the opposite version (agent is likely using the old one)
        agent_desc = baseline_schema if using_drift else drift_schema_desc
        api_desc = schema  # current version's schema has the descriptions

        cls.last_report = sbsa_engine.get_alignment_report(
            agent_keys, server_fields, agent_desc, api_desc,
        )

        healed = sbsa_engine.heal(
            agent_args=raw_args,
            target_fields=server_fields,
            defaults=info.get("defaults", {}),
            agent_descriptions=agent_desc,
            api_descriptions=api_desc,
        )

        log.info("  SBSA | healed -> %s  (%.1fms)",
                 healed, cls.last_report["elapsed_ms"])
        return healed


# ═══════════════════════════════════════════════════════════════
# STAGE 2.5 — DETERMINISTIC VALUE NORMALISATION
# ═══════════════════════════════════════════════════════════════

_CURRENCY_ALIASES = {
    "dollar": "USD", "dollars": "USD", "usd": "USD", "us dollar": "USD",
    "euro": "EUR", "euros": "EUR", "eur": "EUR",
    "pound": "GBP", "pounds": "GBP", "gbp": "GBP", "sterling": "GBP",
    "yen": "JPY", "jpy": "JPY", "japanese yen": "JPY",
    "rupee": "INR", "rupees": "INR", "inr": "INR", "indian rupee": "INR",
    "yuan": "CNY", "cny": "CNY", "rmb": "CNY", "renminbi": "CNY",
    "won": "KRW", "krw": "KRW", "franc": "CHF", "chf": "CHF",
    "real": "BRL", "brl": "BRL", "ruble": "RUB", "rub": "RUB",
    "bitcoin": "BTC", "btc": "BTC", "ethereum": "ETH", "eth": "ETH",
}

_TICKER_ALIASES = {
    "apple": "AAPL", "google": "GOOGL", "alphabet": "GOOGL",
    "microsoft": "MSFT", "amazon": "AMZN", "tesla": "TSLA",
    "meta": "META", "facebook": "META", "netflix": "NFLX",
    "nvidia": "NVDA", "amd": "AMD", "intel": "INTC",
    "ibm": "IBM", "oracle": "ORCL", "spotify": "SPOT",
    "uber": "UBER", "airbnb": "ABNB", "disney": "DIS",
    "coca cola": "KO", "pepsi": "PEP", "nike": "NKE",
    "walmart": "WMT", "boeing": "BA", "jpmorgan": "JPM",
}

_COUNTRY_ALIASES = {
    "usa": "United States", "us": "United States", "america": "United States",
    "uk": "United Kingdom", "britain": "United Kingdom", "england": "United Kingdom",
    "uae": "United Arab Emirates", "south korea": "South Korea",
}

# Fields that should receive currency normalisation
_CURRENCY_FIELDS = {"base", "target", "currency", "from_currency", "to_currency", "vs_currency"}
_TICKER_FIELDS = {"symbol", "ticker", "stock"}
_COUNTRY_FIELDS = {"country", "country_name", "nation"}


def normalise_values(tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic value cleanup using lookup tables. Zero tokens, <0.1ms."""
    changed = []
    out = {}
    for k, v in args.items():
        if not isinstance(v, str):
            out[k] = v
            continue
        low = v.strip().lower()
        if k in _CURRENCY_FIELDS and low in _CURRENCY_ALIASES:
            out[k] = _CURRENCY_ALIASES[low]
            changed.append(f"{k}: '{v}' → '{out[k]}'")
        elif k in _TICKER_FIELDS and low in _TICKER_ALIASES:
            out[k] = _TICKER_ALIASES[low]
            changed.append(f"{k}: '{v}' → '{out[k]}'")
        elif k in _COUNTRY_FIELDS and low in _COUNTRY_ALIASES:
            out[k] = _COUNTRY_ALIASES[low]
            changed.append(f"{k}: '{v}' → '{out[k]}'")
        else:
            out[k] = v.strip()
    if changed:
        log.info("  ValueNorm | %s", ", ".join(changed))
    return out


# ═══════════════════════════════════════════════════════════════
# STAGE 3 — EXTRA FIELD STRIP  (runs AFTER healing)
# ═══════════════════════════════════════════════════════════════

def strip_extra_fields(tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Remove keys not declared on the active SBSA target schema."""
    info = TOOL_REGISTRY.get(tool)
    if info is None:
        return args
    schema_keys = set(SBSA.target_schema(info).keys())
    if not schema_keys:
        return args
    stripped = {k: v for k, v in args.items() if k in schema_keys}
    removed  = set(args.keys()) - schema_keys
    if removed:
        log.info("  ExtraFieldStrip | removed unknown keys: %s", removed)
    return stripped


def sbsa_apply_drift_aliases(tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Map canonical keys to drift field names for direct server calls (e.g. cascade)."""
    if not _sbsa_drift_active:
        return args
    info = TOOL_REGISTRY.get(tool)
    if not info:
        return args
    aliases = info.get("drift_aliases") or {}
    if not aliases:
        return args
    return {aliases.get(k, k): v for k, v in args.items()}


# ═══════════════════════════════════════════════════════════════
# STAGE 6 — RATE-LIMIT RECOVERY (HTTP 429)
# ═══════════════════════════════════════════════════════════════

def _extract_retry_after(error_message: str) -> Optional[int]:
    m = re.search(r"retry[-_]after[:\s]+(\d+)", error_message, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"429[^\d]*(\d+)", error_message)
    if m:
        return int(m.group(1))
    return None


def handle_rate_limit(error_message: str, attempt: int) -> bool:
    if "429" not in error_message and "rate limit" not in error_message.lower():
        return False
    wait = _extract_retry_after(error_message) or int(BACKOFF_BASE_S ** attempt)
    wait = min(wait, MAX_RATE_LIMIT_WAIT)
    log.warning("  RateLimitRecovery | 429 — sleeping %ds  attempt=%d", wait, attempt)
    time.sleep(wait)
    return True


# ═══════════════════════════════════════════════════════════════
# STAGE 7 — TIMEOUT / NETWORK RECOVERY
# ═══════════════════════════════════════════════════════════════

_TIMEOUT_KEYWORDS = (
    "timeout", "timed out", "connection refused", "connection error",
    "unreachable", "network", "read error", "httperror", "nodename",
    "remote end closed", "broken pipe", "errno",
)

def _is_retryable_error(resp: dict) -> bool:
    """
    True when the response carries an error worth retrying:
      timeout — upstream API exceeded HTTP timeout inside mcp_server.py
      network — TCP/DNS failure reaching the upstream API
      api     — upstream returned HTTP 5xx (transient server-side failure)
    schema/parse/drift/unknown are NOT retried — they need human intervention.
    """
    if "error" not in resp:
        return False
    msg        = str(resp["error"].get("message", "")).lower()
    error_type = resp["error"].get("error_type", "")
    return (
        error_type in ("timeout", "network", "api")
        or any(kw in msg for kw in _TIMEOUT_KEYWORDS)
    )


def _classify_error(resp: dict) -> str:
    """Return a short human-readable label for an error response."""
    if "error" not in resp:
        return "ok"
    et = resp["error"].get("error_type", "unknown")
    return {
        "timeout": "API timeout",
        "network": "Network error",
        "api":     "Upstream API error (5xx)",
        "drift":   "Schema drift",
        "schema":  "Schema / argument error",
        "parse":   "Parse error",
    }.get(et, f"Error ({et})")


class TimeoutRecovery:
    _YLW = "\033[33m"; _RED = "\033[31m"; _CYN = "\033[36m"
    _RST = "\033[0m";  _BLD = "\033[1m"

    @classmethod
    def _say(cls, colour: str, symbol: str, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        print(f"{colour}{cls._BLD}[{ts}] {symbol}  {msg}{cls._RST}",
              file=sys.stderr, flush=True)

    @classmethod
    def handle(
        cls, tool: str, arguments: Dict[str, Any],
        error_message: str, error_type: str,
        attempt: int, original_req: dict,
    ) -> Tuple[bool, dict]:
        cls._say(cls._YLW, "⏱ ", f"[{tool}]  {error_type.upper()} attempt {attempt}/{MAX_RETRIES}")

        # Deterministic retry policy — no LLM call
        if attempt < MAX_RETRIES:
            cls._say(cls._YLW, "↻ ", f"[{tool}]  RETRY — attempt {attempt}/{MAX_RETRIES}, {error_type} is retryable")
            cls._wait(attempt, tool)
            return True, {}

        cls._say(cls._RED, "✖ ", f"[{tool}]  FAIL — exhausted {MAX_RETRIES} retries for {error_type}")
        return False, cls._make_error(original_req, f"{error_type}: {error_message}", error_type)

    @staticmethod
    def _wait(attempt: int, tool: str = "") -> None:
        secs = int(BACKOFF_BASE_S ** attempt)
        for remaining in range(secs, 0, -1):
            print(f"\r\033[33m\033[1m   ⏳  [{tool}]  Retrying in {remaining}s …  \033[0m",
                  end="", file=sys.stderr, flush=True)
            time.sleep(1)
        print("\r" + " " * 55 + "\r", end="", file=sys.stderr, flush=True)

    @staticmethod
    def _make_error(req: dict, message: str, error_type: str = "unknown") -> dict:
        return {
            "jsonrpc": "2.0", "id": req.get("id"),
            "error":   {"code": -32001, "message": message, "error_type": error_type},
        }


# ═══════════════════════════════════════════════════════════════
# STAGE 8 — PARTIAL RESULT CHECK
# ═══════════════════════════════════════════════════════════════

def check_partial_result(
    tool: str,
    arguments: Dict[str, Any],
    result: Any,
    server_mgr: ServerManager,
    original_req: dict,
) -> Any:
    """
    Check whether the result is missing fields declared in expected_result.
    Annotates _interceptor_warning if any are absent — does not retry,
    since a partial live-API response is still a valid (if incomplete) result.

    Example: get_weather returns {"city": "London"} with no temperature_c
    -> _interceptor_warning: "Partial result: missing fields: temperature_c, windspeed_kmh"
    """
    info = TOOL_REGISTRY.get(tool)
    if not info or not isinstance(result, dict):
        return result

    expected = info.get("expected_result", [])
    missing  = [f for f in expected if f not in result]

    if missing:
        log.warning("  PartialResult | tool=%s missing: %s", tool, missing)
        result = dict(result)
        existing     = result.get("_interceptor_warning", "")
        partial_msg  = f"Partial result: missing fields: {', '.join(missing)}"
        result["_interceptor_warning"] = f"{existing} | {partial_msg}" if existing else partial_msg
    else:
        log.info("  PartialResult | ✓ all expected fields present")

    return result


# ═══════════════════════════════════════════════════════════════
# STAGE 9 — STALE RESULT DETECTION
# ═══════════════════════════════════════════════════════════════

def check_stale_result(tool: str, result: Any) -> Any:
    """
    Flag numeric values outside their plausible real-world range.
    Ranges defined in TOOL_REGISTRY["result_ranges"].
    """
    info = TOOL_REGISTRY.get(tool)
    if not info or not isinstance(result, dict):
        return result

    ranges   = info.get("result_ranges", {})
    warnings = []

    for field, (lo, hi) in ranges.items():
        val = result.get(field)
        if val is None:
            continue
        try:
            num = float(val)
        except (TypeError, ValueError):
            continue
        if not (lo <= num <= hi):
            warnings.append(f"{field}={val} outside expected range [{lo}, {hi}]")
            log.warning("  StaleResult | suspicious: %s=%s (expected %s-%s)", field, val, lo, hi)

    if warnings:
        result   = dict(result)
        existing = result.get("_interceptor_warning", "")
        stale    = "Possibly stale/invalid: " + "; ".join(warnings)
        result["_interceptor_warning"] = f"{existing} | {stale}" if existing else stale

    return result


# ═══════════════════════════════════════════════════════════════
# STAGE 10 — CASCADING TOOL CALLS
# ═══════════════════════════════════════════════════════════════
def flatten_dict(d, parent_key='', sep='_'):
    items = {}
    for k, v in d.items():
        new_key = k.replace("_cascade_", "")  # clean keys
        if isinstance(v, dict):
            items.update(flatten_dict(v, new_key, sep=sep))
        else:
            items[new_key] = v
    return items

def maybe_cascade(
    tool: str, arguments: Dict[str, Any], result: Any,
    server_mgr: ServerManager, original_req: dict, depth: int = 0,
) -> Any:

    # DEBUG START
    log.info("DEBUG Cascade START | tool=%s | depth=%d | result=%s", tool, depth, result)

    if depth >= MAX_CASCADE_DEPTH:
        log.warning("DEBUG Cascade STOP | max depth reached (%d)", depth)
        return result

    info = TOOL_REGISTRY.get(tool)

    # 🔥 NEW DEBUG (MOST IMPORTANT)
    log.info("DEBUG TOOL ENTRY FULL = %s", info)
    log.info("DEBUG AVAILABLE TOOLS = %s", list(TOOL_REGISTRY.keys()))

    cascade = info.get("cascade") if info else None

    if not cascade:
        log.warning("DEBUG Cascade SKIPPED | tool=%s has no cascade config", tool)
        return result

    if not isinstance(result, dict):
        log.warning("DEBUG Cascade SKIPPED | result is not dict: %s", result)
        return result

    # CHECK trigger field
    trigger_field = cascade["trigger_field"]
    trigger_value = result.get(trigger_field)

    if not trigger_value:
        log.warning(
            "DEBUG Cascade SKIPPED | tool=%s | missing trigger_field='%s' in result=%s",
            tool, trigger_field, result
        )
        return result

    next_tool = cascade["next_tool"]

    # Build next args
    next_args = {
        next_arg: result[src_field]
        for next_arg, src_field in cascade["arg_map"].items()
        if result.get(src_field)
    }

    if not next_args:
        log.warning(
            "DEBUG Cascade SKIPPED | tool=%s | empty next_args after mapping | result=%s",
            tool, result
        )
        return result

    log.info(
        "Cascade | %s -> %s | trigger=%s | args=%s | depth=%d",
        tool, next_tool, trigger_value, next_args, depth
    )

    # SBSA HEALING
    healed_args = SBSA.heal(next_tool, next_args)
    healed_args = strip_extra_fields(next_tool, healed_args)
    healed_args = sbsa_apply_drift_aliases(next_tool, healed_args)

    if healed_args != next_args:
        log.info("DEBUG Cascade HEALED | %s -> %s", next_args, healed_args)

    # DEBUG CALL
    log.info(
        "DEBUG Cascade CALL | %s -> %s | trigger=%s | healed_args=%s",
        tool, next_tool, trigger_value, healed_args
    )

    try:
        next_resp = server_mgr.send({
            "jsonrpc": "2.0",
            "id": original_req.get("id"),
            "method": "tools/call",
            "params": {
                "name": next_tool,
                "arguments": healed_args
            },
        })

        # DEBUG RESPONSE
        log.info("DEBUG Cascade RESPONSE | tool=%s | resp=%s", next_tool, next_resp)

        if "result" in next_resp:
            next_data = next_resp["result"].get("structuredContent", next_resp["result"])

            # RECURSIVE CASCADE
            next_data = maybe_cascade(
                next_tool,
                healed_args,
                next_data,
                server_mgr,
                original_req,
                depth + 1
            )

            cascade_key = f"_cascade_{next_tool.replace('get_', '')}"
            result = dict(result)
            # 🔥 FLATTEN instead of nesting
            for k, v in next_data.items():
                if k not in result:
                    result[k] = v

            log.info("DEBUG Cascade ATTACHED | key=%s", cascade_key)

        else:
            log.warning(
                "DEBUG Cascade ERROR | tool=%s returned error=%s",
                next_tool, next_resp.get("error")
            )

    except Exception as exc:
        log.error("DEBUG Cascade EXCEPTION | tool=%s | error=%s", next_tool, exc)

    return result


# ═══════════════════════════════════════════════════════════════
# STAGE 11 — RESULT REASSESSMENT
# ═══════════════════════════════════════════════════════════════

def reassess_result(tool: str, arguments: Dict[str, Any], result: Any) -> Any:
    """
    Deterministic structural validation using TOOL_REGISTRY metadata.
    Checks expected fields and value ranges — no LLM call.
    """
    if isinstance(result, dict) and "_interceptor_warning" in result:
        log.info("  reassess_result | skipping — already annotated")
        return result

    info = TOOL_REGISTRY.get(tool)
    if not info:
        return result

    issues = []

    # Check expected fields
    expected = info.get("expected_result", [])
    if expected and isinstance(result, dict):
        missing = [f for f in expected if f not in result]
        if missing:
            issues.append(f"missing expected fields: {missing}")

    # Check value ranges
    ranges = info.get("result_ranges", {})
    if isinstance(result, dict):
        for field, (lo, hi) in ranges.items():
            val = result.get(field)
            if val is not None:
                try:
                    num = float(val)
                    if not (lo <= num <= hi):
                        issues.append(f"{field}={num} outside expected range [{lo}, {hi}]")
                except (ValueError, TypeError):
                    pass

    if issues:
        warning = "; ".join(issues)
        log.warning("  reassess_result | ⚠  %s", warning)
        if isinstance(result, dict):
            result = dict(result)
            result["_interceptor_warning"] = warning
    else:
        log.info("  reassess_result | ✓ result looks OK")

    return result


# ═══════════════════════════════════════════════════════════════
# STAGE 12 — RESULT INTEGRITY CHECK (gaslighting detection)
# ═══════════════════════════════════════════════════════════════

def check_result_integrity(tool_data: dict, llm_summary: str) -> Optional[str]:
    """
    Compare key numeric/factual values in tool output against the LLM summary.
    If the LLM's summary contradicts the tool's actual data, flag it.
    Returns a warning string, or None if integrity holds.

    This catches "gaslighting" — where the LLM ignores tool output and
    substitutes values from its training data.
    """
    if not isinstance(tool_data, dict) or not llm_summary:
        return None

    mismatches = []
    for key, val in tool_data.items():
        if key.startswith("_"):
            continue
        # Check numeric values
        try:
            num = float(val)
            # Look for this number (or close to it) in the summary
            import re
            # Extract all numbers from summary
            summary_nums = [float(x) for x in re.findall(r'[\d,]+\.?\d*', llm_summary.replace(",", ""))]
            if summary_nums and abs(num) > 1:
                # Check if any summary number is within 10% of the tool value
                close = any(abs(s - num) / max(abs(num), 1) < 0.1 for s in summary_nums)
                if not close and abs(num) > 10:
                    mismatches.append(f"{key}={val} not reflected in summary")
        except (ValueError, TypeError):
            continue

    if mismatches:
        warning = "Integrity check: " + "; ".join(mismatches[:3])
        log.warning("  IntegrityCheck | ⚠ %s", warning)
        return warning
    return None


# ═══════════════════════════════════════════════════════════════
# PER-CLIENT PROXY SESSION
# ═══════════════════════════════════════════════════════════════

class ProxySession:
    """Handles one connected client socket in its own thread."""

    def __init__(self, client_sock: socket.socket, server_mgr: ServerManager):
        self.client     = client_sock
        self.server_mgr = server_mgr

    def _forward_to_server(self, payload: dict) -> dict:
        return self.server_mgr.send(payload)

    def _notify_server(self, payload: dict):
        self.server_mgr.notify(payload)

    def _send_client(self, payload: dict):
        self.client.sendall((json.dumps(payload) + "\n").encode())

    def run(self):
        addr = self.client.getpeername()
        log.info("client connected  addr=%s", addr)
        buf = ""
        try:
            self.client.settimeout(None)
            while True:
                chunk = self.client.recv(4096)
                if not chunk:
                    break
                buf += chunk.decode(errors="replace")
                while "\n" in buf:
                    raw, buf = buf.split("\n", 1)
                    raw = raw.strip()
                    if raw:
                        self._dispatch(raw)
        except Exception as exc:
            log.error("session error addr=%s  %s: %s", addr, type(exc).__name__, exc)
        finally:
            self.client.close()
            log.info("client disconnected  addr=%s", addr)

    def _dispatch(self, raw: str):
        req = {}
        try:
            req    = json.loads(raw)
            method = req.get("method", "")
            has_id = req.get("id") is not None

            if method in _NOTIFICATIONS or not has_id:
                log.info("notification  method=%s", method)
                self._notify_server(req)
                return

            if method == "tools/call":
                self._tool_call_pipeline(req)
                return

            # Intercept set_drift to reset our schema version tracker
            if method == "set_drift":
                global _sbsa_drift_active
                _sbsa_drift_active = False
                drift_active = req.get("params", {}).get("active", False)
                log.info("DRIFT TOGGLED → %s  (interceptor SBSA reset to baseline)",
                         "ON 🔴" if drift_active else "OFF 🟢")

            log.info("pass-through  method=%s  id=%s", method, req.get("id"))
            self._send_client(self._forward_to_server(req))

        except json.JSONDecodeError as exc:
            log.error("bad JSON from client: %s", exc)
            self._send_client({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}", "error_type": "parse"},
            })
        except Exception as exc:
            log.error("_dispatch error  method=%s  %s: %s",
                      req.get("method", "?"), type(exc).__name__, exc)
            self._send_client({
                "jsonrpc": "2.0", "id": req.get("id"),
                "error":   {"code": -32000, "message": str(exc), "error_type": "unknown"},
            })

    def _tool_call_pipeline(self, req: dict):
        global _sbsa_drift_active
        params    = req.get("params", {})
        tool_name = params.get("name", "")
        raw_args  = params.get("arguments", {})
        drift_tag = "drift" if _sbsa_drift_active else "baseline"

        pipeline_start = time.monotonic()
        log.info("=" * 60)
        log.info("PIPELINE START  tool=%-22s  SBSA=%s", tool_name, drift_tag)
        log.info("  raw_args = %s", raw_args)

        # Stage 0: Fuzzy tool-name matching (tool hallucination recovery)
        resolved_name = fuzzy_match_tool(tool_name)
        if resolved_name != tool_name:
            tool_name = resolved_name
            params["name"] = tool_name

        # Stage 1: Type coercion
        args = coerce_types(tool_name, raw_args)

        # Stage 2: SBSA schema healing + value normalisation (BEFORE strip)
        if SBSA.args_satisfy(tool_name, args):
            healed = args
            SBSA.last_report = None
            log.info("  SBSA | args already valid — pass-through  %s", healed)
        else:
            healed = SBSA.heal(tool_name, args)

        # Stage 2.5: Deterministic value normalisation
        healed = normalise_values(tool_name, healed)

        # Stage 3: Strip extra/unknown fields (AFTER healing)
        healed = strip_extra_fields(tool_name, healed)

        req["params"]["arguments"] = healed
        log.info("  healed = %s", healed)

        # Stage 4: Forward — API timeout, network, and 5xx errors all retried
        log.info(
            "  forwarding  tool=%s  healed_args=%s  budget=%d attempts",
            tool_name, healed, max(MAX_RETRIES, API_TIMEOUT_RETRIES) + 1,
        )
        resp = self._forward_with_retry(req, tool_name, healed)
        log.info(
            "  forward done  outcome=%s",
            "success" if "result" in resp else _classify_error(resp),
        )

        # Stage 5: Drift recovery (latch drift_schema, re-heal, retry)
        if resp.get("error", {}).get("error_type") == "drift":
            _sbsa_drift_active = True
            log.warning("  SBSA | DRIFT DETECTED — latching drift_schema; re-healing ...")
            args_retry   = coerce_types(tool_name, raw_args)
            healed_drift = SBSA.heal(tool_name, args_retry)
            healed_drift = strip_extra_fields(tool_name, healed_drift)
            req["params"]["arguments"] = healed_drift
            log.info("  re-healed (drift) = %s", healed_drift)
            resp = self._forward_with_retry(req, tool_name, healed_drift)

            if "result" in resp:
                healed = healed_drift
                sc = resp["result"].get("structuredContent", {})
                if isinstance(sc, dict):
                    sc["_sbsa_drift_note"] = f"Schema drift auto-corrected: {raw_args} -> {healed_drift}"
                resp["result"]["structuredContent"] = sc

        # Stages 8-11: Post-processing
        if "result" in resp:
            data = resp["result"].get("structuredContent", resp["result"])
            data = check_partial_result(tool_name, healed, data, self.server_mgr, req)
            data = check_stale_result(tool_name, data)
            data = maybe_cascade(tool_name, healed, data, self.server_mgr, req)
            data = reassess_result(tool_name, healed, data)
            data = flatten_dict(data)
            log.info("FINAL FLATTENED DATA = %s", data)

            resp["result"]["structuredContent"] = data

        self._send_client(resp)
        outcome = "success" if "result" in resp else _classify_error(resp)

        # Analytics: record pipeline event with SBSA report
        analytics_logger.record_pipeline_event(
            tool=tool_name,
            model=LLM_MODEL,
            outcome=outcome,
            total_elapsed_s=time.monotonic() - pipeline_start,
            sbsa_report=SBSA.last_report,
        )
        if SBSA.last_report:
            rpt = SBSA.last_report
            info = TOOL_REGISTRY.get(tool_name, {})
            analytics_logger.record_healing_event(
                tool=tool_name,
                model=LLM_MODEL,
                domain=info.get("category", "unknown"),
                agent_keys=rpt.get("agent_keys", []),
                api_keys=rpt.get("api_keys", []),
                mapping=rpt.get("mapping", {}),
                similarity_scores=rpt.get("similarity_scores", {}),
                cost_matrix=rpt.get("cost_matrix", []),
                sbsa_elapsed_ms=rpt.get("elapsed_ms", 0),
                healed_successfully=outcome == "success",
                threshold=rpt.get("threshold", 0.4),
                schema_version=drift_tag,
            )

        log.info(
            "PIPELINE END  tool=%s  outcome=%s  total=%.2fs",
            tool_name, outcome, time.monotonic() - pipeline_start,
        )
        log.info("=" * 60)

    def _forward_with_retry(self, req: dict, tool_name: str, arguments: Dict[str, Any]) -> dict:
        """
        Forward a tool call to mcp_server.py with full retry / recovery handling.

        Retries three distinct recoverable error categories:
          timeout — upstream HTTP call exceeded the server-side timeout
          network — TCP/DNS failure reaching the upstream REST API
          api     — upstream returned HTTP 5xx (transient server-side failure)

        Non-retryable categories (schema, parse, drift, unknown) are returned
        immediately so the pipeline can handle them without wasting retry budget.

        Every attempt is timed and logged so the terminal shows a clear audit trail.
        """
        total_budget = max(MAX_RETRIES, API_TIMEOUT_RETRIES) + 1
        t_pipeline   = time.monotonic()

        for attempt in range(1, total_budget + 1):
            t0 = time.monotonic()
            log.info(
                "  ┌─ attempt %d/%d  tool=%s",
                attempt, total_budget, tool_name,
            )

            # ── Send to server ────────────────────────────────────────────────
            try:
                resp = self._forward_to_server(req)
            except Exception as exc:
                elapsed = time.monotonic() - t0
                log.error(
                    "  └─ attempt %d | transport exception  elapsed=%.2fs  exc=%s",
                    attempt, elapsed, exc,
                )
                resp = {
                    "jsonrpc": "2.0", "id": req.get("id"),
                    "error":   {"code": -32001, "message": str(exc), "error_type": "network"},
                }

            elapsed    = time.monotonic() - t0
            error_type = resp.get("error", {}).get("error_type", "")
            label      = _classify_error(resp)

            # ── Drift: not a retry concern — hand off immediately ─────────────
            if error_type == "drift":
                log.info(
                    "  └─ attempt %d | drift detected  elapsed=%.2fs — handing to drift handler",
                    attempt, elapsed,
                )
                return resp

            # ── Success ───────────────────────────────────────────────────────
            if "error" not in resp:
                log.info(
                    "  └─ attempt %d | ✓ success  elapsed=%.2fs  total=%.2fs",
                    attempt, elapsed, time.monotonic() - t_pipeline,
                )
                return resp

            # ── Error: log detail before deciding what to do ──────────────────
            error_msg = resp["error"].get("message", "")
            log.warning(
                "  └─ attempt %d | ✗ %s  elapsed=%.2fs  detail=%s",
                attempt, label, elapsed, error_msg[:120],
            )

            # ── Rate-limit (429): sleep Retry-After, then continue loop ───────
            if handle_rate_limit(error_msg, attempt) and attempt < total_budget:
                log.info(
                    "  rate-limit sleep done — continuing to attempt %d", attempt + 1
                )
                continue

            # ── Non-retryable: schema / parse / unknown — return immediately ──
            if not _is_retryable_error(resp):
                log.info(
                    "  error_type=%s is not retryable — returning immediately", error_type
                )
                return resp

            # ── Retryable: timeout / network / api 5xx ────────────────────────
            budget = API_TIMEOUT_RETRIES if error_type in ("timeout", "api") else MAX_RETRIES
            log.warning(
                "  retryable error  type=%s  budget=%d retries  attempt=%d/%d",
                error_type, budget, attempt, total_budget,
            )

            should_retry, recovery = TimeoutRecovery.handle(
                tool=tool_name, arguments=arguments,
                error_message=error_msg, error_type=error_type,
                attempt=attempt, original_req=req,
            )
            if not should_retry:
                log.warning(
                    "  recovery decision: give up  tool=%s  total_elapsed=%.2fs",
                    tool_name, time.monotonic() - t_pipeline,
                )
                return recovery

            log.info(
                "  recovery decision: retry  next_attempt=%d  total_elapsed=%.2fs",
                attempt + 1, time.monotonic() - t_pipeline,
            )

        total_elapsed = time.monotonic() - t_pipeline
        log.error(
            "  all %d attempts exhausted  tool=%s  total_elapsed=%.2fs",
            total_budget, tool_name, total_elapsed,
        )
        return TimeoutRecovery._make_error(
            req,
            f"All {total_budget} retry attempts exhausted for '{tool_name}' ({total_elapsed:.1f}s)",
            "timeout",
        )


# ═══════════════════════════════════════════════════════════════
# TCP SERVER
# ═══════════════════════════════════════════════════════════════

def serve():
    server_mgr = ServerManager()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((PROXY_HOST, PROXY_PORT))
    sock.listen(5)
    log.info("interceptor ready  addr=%s:%d  model=%s", PROXY_HOST, PROXY_PORT, LLM_MODEL)
    log.info("Ctrl-C to stop\n")
    try:
        while True:
            client_sock, addr = sock.accept()
            log.info("accepted connection from %s", addr)
            session = ProxySession(client_sock, server_mgr)
            threading.Thread(target=session.run, daemon=True).start()
    except KeyboardInterrupt:
        log.info("shutting down ...")
    finally:
        sock.close()
        server_mgr.terminate()


if __name__ == "__main__":
    serve()