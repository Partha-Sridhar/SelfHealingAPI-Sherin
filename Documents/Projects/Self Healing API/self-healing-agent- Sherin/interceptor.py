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
  5. DRIFT RECOVERY      — V1->V2 field-name drift detected; args re-healed and retried.
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
import re
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Optional, Tuple

import ollama
from sbsa import SBSAEngine, MCPDiscovery

# Initialize SBSA engine (singleton - only once at startup)
_sbsa_engine = SBSAEngine(threshold=0.35)
_sbsa_discovery = None  # Will be initialized after TOOL_REGISTRY is defined

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
# TOOL REGISTRY
# ═══════════════════════════════════════════════════════════════

TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {
    "get_bitcoin_price": {
        "description":     "Returns the current Bitcoin price in USD. Takes NO arguments.",
        "required":        [],
        "defaults":        {},
        "v1_fields":       [],
        "v2_fields":       [],
        "v1_schema":       {},
        "v2_schema":       {},
        "expected_result": ["bitcoin_usd"],
        "result_ranges":   {"bitcoin_usd": (1_000, 200_000)},
        "cascade":         None,
    },
    # V1: city          V2: location_name
    "get_weather": {
        "description":     "Returns current weather for a city or location.",
        "required":        ["city"],
        "defaults":        {"city": "Delhi"},
        "v1_fields":       ["city"],
        "v2_fields":       ["location_name"],
        "v1_schema":       {"city":          "string — name of the city"},
        "v2_schema":       {"location_name": "string — name of the city or location"},
        "expected_result": ["city", "temperature_c", "windspeed_kmh"],
        "result_ranges":   {"temperature_c": (-80, 60), "windspeed_kmh": (0, 400)},
        "cascade":         None,
    },
    # V1: country       V2: country_name
    "get_country_info": {
        "description":     "Returns facts about a country (capital, population, region).",
        "required":        ["country"],
        "defaults":        {"country": "India"},
        "v1_fields":       ["country"],
        "v2_fields":       ["country_name"],
        "v1_schema":       {"country":      "string — name of the country"},
        "v2_schema":       {"country_name": "string — full name of the country"},
        "expected_result": ["country", "capital", "population", "region"],
        "result_ranges":   {"population": (100, 2_000_000_000)},
        "cascade": {
            "trigger_field": "capital",
            "next_tool":     "get_weather",
            "arg_map":       {"city": "capital"},
        },
    },
    # V1: base/target   V2: from_currency/to_currency
    "get_exchange_rate": {
        "description":     "Returns the exchange rate between two ISO 4217 currency codes.",
        "required":        ["base", "target"],
        "defaults":        {"base": "USD", "target": "EUR"},
        "v1_fields":       ["base", "target"],
        "v2_fields":       ["from_currency", "to_currency"],
        "v1_schema":       {
            "base":          "string — ISO 4217 source currency code, e.g. USD",
            "target":        "string — ISO 4217 target currency code, e.g. EUR",
        },
        "v2_schema":       {
            "from_currency": "string — ISO 4217 source currency code, e.g. USD",
            "to_currency":   "string — ISO 4217 target currency code, e.g. EUR",
        },
        "expected_result": ["base", "target", "rate"],
        "result_ranges":   {"rate": (0.000001, 100_000)},
        "cascade":         None,
    },
    # V1: symbol        V2: ticker
    "get_stock_price": {
        "description":     "Returns the current market price for a stock.",
        "required":        ["symbol"],
        "defaults":        {"symbol": "AAPL"},
        "v1_fields":       ["symbol"],
        "v2_fields":       ["ticker"],
        "v1_schema":       {"symbol": "string — stock ticker symbol in UPPERCASE, e.g. AAPL"},
        "v2_schema":       {"ticker": "string — stock ticker symbol in UPPERCASE, e.g. AAPL"},
        "expected_result": ["symbol", "price", "currency"],
        "result_ranges":   {"price": (0.001, 1_000_000)},
        "cascade":         None,
    },
}

# Tracks which schema version the server is currently using.
# Bumped to "v2" the first time a drift error is received.
_server_schema_version: Dict[str, str] = {"version": "v1"}


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
# STAGE 2 — SCHEMA HEALING + VALUE NORMALISATION
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

def _args_satisfy_schema(tool: str, args: Dict[str, Any], version: str) -> bool:
    """True if all required field names for the current schema version are present and non-empty."""
    info = TOOL_REGISTRY.get(tool)
    if not info or not info["required"]:
        return True
    server_fields = info.get(f"{version}_fields") or list(info.get(f"{version}_schema", {}).keys())
    return bool(server_fields) and all(str(args.get(f, "")).strip() for f in server_fields)


class SchemaHealer:
    """
    SBSA-powered field-name repair (replaces LLM-based healing).

    Uses semantic embeddings + Hungarian algorithm for deterministic,
    fast (~30–50ms), zero-token parameter mapping.
    """

    @classmethod
    def _initialize_discovery(cls):
        """
        Lazy initialization of MCPDiscovery after TOOL_REGISTRY exists.
        This avoids circular initialization issues during module load.
        """
        global _sbsa_discovery
        if _sbsa_discovery is None:
            _sbsa_discovery = MCPDiscovery(TOOL_REGISTRY)

    @classmethod
    def heal(
        cls,
        tool: str,
        raw_args: Dict[str, Any],
        force_version: str = None
    ) -> Dict[str, Any]:
        """
        Heal schema mismatches using SBSA algorithm.

        Args:
            tool: Tool name (e.g., "get_weather")
            raw_args: Arguments sent by the client
            force_version: Optional override schema version ("v1" or "v2")

        Returns:
            Dict with corrected argument field names.
        """

        # Ensure discovery is initialized
        cls._initialize_discovery()

        info = TOOL_REGISTRY.get(tool)
        if info is None:
            log.warning("  SchemaHealer | unknown tool '%s' — passing through", tool)
            return raw_args

        # Tools with no required args need no healing
        if not info["required"]:
            return {}

        # Determine schema version
        version = force_version or _server_schema_version["version"]

        # Get required schema fields
        required_fields = _sbsa_discovery.get_schema(tool, version)
        if not required_fields:
            log.warning(
                "  SchemaHealer | no schema found for '%s' version=%s",
                tool, version
            )
            return raw_args

        # Filter internal metadata fields
        agent_fields = [k for k in raw_args.keys() if not k.startswith("_")]

        # Skip healing if schema requirements are already satisfied
        if all(str(raw_args.get(f, "")).strip() for f in required_fields):
            log.info(
                "  SchemaHealer | args already valid for %s — pass-through  %s",
                version, raw_args
            )
            return raw_args

        # ──────────────────────────────────────────────
        # SBSA HEALING
        # ──────────────────────────────────────────────

        log.info(
            "  SchemaHealer[SBSA] | healing tool='%s' schema=%s raw=%s",
            tool, version, raw_args
        )

        start_time = time.time()

        mapping = _sbsa_engine.find_mapping(agent_fields, required_fields)

        latency_ms = (time.time() - start_time) * 1000

        # If SBSA fails to find a confident mapping
        if not mapping:
            log.warning(
                "  SchemaHealer[SBSA] | no valid mapping found "
                "(threshold=%.2f)",
                _sbsa_engine.threshold
            )

            fallback = dict(info["defaults"])
            fallback.update(raw_args)
            return fallback

        # Apply mapping
        healed = {}

        for agent_key, value in raw_args.items():
            api_key = mapping.get(agent_key, agent_key)
            healed[api_key] = value

        log.info(
            "  SchemaHealer[SBSA] | healed in %.1fms mapping=%s result=%s",
            latency_ms,
            mapping,
            healed
        )

        # ──────────────────────────────────────────────
        # Validate required fields
        # ──────────────────────────────────────────────

        missing = [
            field for field in required_fields
            if not str(healed.get(field, "")).strip()
        ]

        if missing:
            log.warning(
                "  SchemaHealer[SBSA] | missing required fields after healing: %s",
                missing
            )

            for field in missing:
                if field in info["defaults"]:
                    healed[field] = info["defaults"][field]

                    log.info(
                        "  SchemaHealer[SBSA] | filled %s with default: %s",
                        field,
                        healed[field]
                    )

        return healed


# ═══════════════════════════════════════════════════════════════
# STAGE 3 — EXTRA FIELD STRIP  (runs AFTER healing)
# ═══════════════════════════════════════════════════════════════

def strip_extra_fields(tool: str, args: Dict[str, Any], version: str) -> Dict[str, Any]:
    """
    Remove keys the server schema does not declare.
    Must run after SchemaHealer so that misnamed fields are first mapped
    to their correct names before any unknown keys are dropped.
    """
    info = TOOL_REGISTRY.get(tool)
    if info is None:
        return args
    schema_keys = set(info.get(f"{version}_schema", {}).keys())
    if not schema_keys:
        return args
    stripped = {k: v for k, v in args.items() if k in schema_keys}
    removed  = set(args.keys()) - schema_keys
    if removed:
        log.info("  ExtraFieldStrip | removed unknown keys: %s", removed)
    return stripped


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
        cls._say(cls._CYN, "   ", "Consulting LLM — retry / fallback / fail …")

        prompt = f"""You are a fault-recovery agent for an API proxy.
Tool: {tool}  |  Attempt: {attempt}/{MAX_RETRIES}
Error type: {error_type}
Error: {error_message}

Choose ONE action and respond ONLY with valid JSON:
  {{"action": "retry",    "reason": "<why>"}}
  {{"action": "fallback", "reason": "<why>", "result": {{...plausible stub...}}}}
  {{"action": "fail",     "reason": "<why>"}}
"""
        decision = _llm(prompt, label=f"recovery/{tool}")

        if decision is None:
            if attempt < MAX_RETRIES:
                cls._say(cls._YLW, "⚠ ", "LLM unavailable — defaulting to retry")
                cls._wait(attempt, tool)
                return True, {}
            return False, cls._make_error(original_req, error_message, error_type)

        action = decision.get("action", "fail")
        reason = decision.get("reason", "")

        if action == "retry" and attempt < MAX_RETRIES:
            cls._say(cls._YLW, "↻ ", f"[{tool}]  RETRY — {reason}")
            cls._wait(attempt, tool)
            return True, {}

        if action == "fallback":
            stub = dict(decision.get("result") or {})
            stub["_interceptor_warning"] = f"Fallback — {error_type}: {reason}"
            log.warning("  TimeoutRecovery | fallback stub: %s", stub)
            return False, {
                "jsonrpc": "2.0", "id": original_req.get("id"),
                "result":  {"structuredContent": stub, "isError": False},
            }

        cls._say(cls._RED, "✖ ", f"[{tool}]  FAIL — {reason or error_message}")
        return False, cls._make_error(original_req, f"{error_type}: {reason or error_message}", error_type)

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

def maybe_cascade(
    tool: str, arguments: Dict[str, Any], result: Any,
    server_mgr: ServerManager, original_req: dict, depth: int = 0,
) -> Any:
    """
    Automatically chain a second tool call when the first result contains
    a value useful as input to another tool. Depth-limited to MAX_CASCADE_DEPTH.

    Example: get_country_info(country="France")
      -> result contains capital="Paris"
      -> automatically calls get_weather(city="Paris")
      -> result["_cascade_weather"] = {...}
    """
    if depth >= MAX_CASCADE_DEPTH:
        return result

    info    = TOOL_REGISTRY.get(tool)
    cascade = info.get("cascade") if info else None
    if not cascade or not isinstance(result, dict):
        return result

    trigger_value = result.get(cascade["trigger_field"])
    if not trigger_value:
        return result

    next_tool = cascade["next_tool"]
    next_args = {
        next_arg: result[src_field]
        for next_arg, src_field in cascade["arg_map"].items()
        if result.get(src_field)
    }
    if not next_args:
        return result

    log.info("  Cascade | %s -> %s  args=%s  depth=%d", tool, next_tool, next_args, depth)

    try:
        next_resp = server_mgr.send({
            "jsonrpc": "2.0", "id": original_req.get("id"),
            "method":  "tools/call",
            "params":  {"name": next_tool, "arguments": next_args},
        })
        if "result" in next_resp:
            next_data   = next_resp["result"].get("structuredContent", next_resp["result"])
            next_data   = maybe_cascade(next_tool, next_args, next_data,
                                        server_mgr, original_req, depth + 1)
            cascade_key = f"_cascade_{next_tool.replace('get_', '')}"
            result      = dict(result)
            result[cascade_key] = next_data
            log.info("  Cascade | attached %s", cascade_key)
        else:
            log.warning("  Cascade | %s returned error: %s", next_tool, next_resp.get("error"))
    except Exception as exc:
        log.warning("  Cascade | %s failed: %s", next_tool, exc)

    return result


# ═══════════════════════════════════════════════════════════════
# STAGE 11 — RESULT REASSESSMENT
# ═══════════════════════════════════════════════════════════════

def reassess_result(tool: str, arguments: Dict[str, Any], result: Any) -> Any:
    """
    Final LLM sanity check. Skipped if a prior stage already annotated a warning
    to avoid double-flagging results that were partially repaired.
    """
    if isinstance(result, dict) and "_interceptor_warning" in result:
        log.info("  reassess_result | skipping — already annotated")
        return result

    prompt = f"""You are a quality-assurance agent for API tool results.
Tool: {tool}  |  Args: {json.dumps(arguments)}  |  Result: {json.dumps(result)}

Is this result correct and complete? Check: expected fields present? Values plausible
(non-zero prices, real city names, valid ISO currency codes, reasonable temperatures)?

Respond ONLY with valid JSON:
  {{"ok": true}}
  {{"ok": false, "issue": "<concise description>"}}
"""
    assessment = _llm(prompt, label=f"reassess/{tool}")
    if assessment is None:
        return result

    if not assessment.get("ok", True):
        issue = assessment.get("issue", "interceptor flagged a potential issue")
        log.warning("  reassess_result | ⚠  %s", issue)
        if isinstance(result, dict):
            result = dict(result)
            result["_interceptor_warning"] = issue
        else:
            result = {"_interceptor_warning": issue, "_raw": result}
    else:
        log.info("  reassess_result | ✓ result looks OK")

    return result


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
        params    = req.get("params", {})
        tool_name = params.get("name", "")
        raw_args  = params.get("arguments", {})
        version   = _server_schema_version["version"]

        pipeline_start = time.monotonic()
        log.info("=" * 60)
        log.info("PIPELINE START  tool=%-22s  schema=%s", tool_name, version)
        log.info("  raw_args = %s", raw_args)

        # Stage 1: Type coercion
        args = coerce_types(tool_name, raw_args)

        # Stage 2: Schema healing + value normalisation (BEFORE strip)
        if _args_satisfy_schema(tool_name, args, version):
            healed = args
            log.info("  SchemaHealer | args already valid — pass-through  %s", healed)
        else:
            healed = SchemaHealer.heal(tool_name, args)

        # Stage 3: Strip extra/unknown fields (AFTER healing)
        healed = strip_extra_fields(tool_name, healed, version)

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

        # Stage 5: Drift recovery
        if resp.get("error", {}).get("error_type") == "drift":
            old_v = _server_schema_version["version"]
            _server_schema_version["version"] = "v2"
            log.warning("  DRIFT DETECTED — schema %s -> v2; re-healing ...", old_v)
            healed_v2 = SchemaHealer.heal(tool_name, raw_args, force_version="v2")
            healed_v2 = strip_extra_fields(tool_name, healed_v2, "v2")
            req["params"]["arguments"] = healed_v2
            log.info("  re-healed (v2) = %s", healed_v2)
            resp = self._forward_with_retry(req, tool_name, healed_v2)

            if "result" in resp:
                sc = resp["result"].get("structuredContent", {})
                if isinstance(sc, dict):
                    sc["_drift_healed"] = f"Schema drift auto-corrected: {raw_args} -> {healed_v2}"
                resp["result"]["structuredContent"] = sc

        # Stages 8-11: Post-processing
        if "result" in resp:
            data = resp["result"].get("structuredContent", resp["result"])
            data = check_partial_result(tool_name, healed, data, self.server_mgr, req)
            data = check_stale_result(tool_name, data)
            data = maybe_cascade(tool_name, healed, data, self.server_mgr, req)
            data = reassess_result(tool_name, healed, data)
            resp["result"]["structuredContent"] = data

        self._send_client(resp)
        outcome = "success" if "result" in resp else _classify_error(resp)
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