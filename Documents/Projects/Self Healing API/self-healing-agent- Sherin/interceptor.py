"""
Self-Healing MCP Interceptor — Independent Proxy
=================================================

Architecture:
    mcp_client.py  ──►  interceptor.py (TCP :6010)  ──►  mcp_server.py (subprocess)

Pipeline for every tools/call
──────────────────────────────
  0. QUERY ROUTING       — LLM agent selects appropriate tool for natural language queries.
  1. TYPE COERCION       — wrong-type args (int/list instead of string) silently cast.
  2. SCHEMA HEALING      — SBSA maps client args to exact server schema (field renames +
                           value normalisation). Runs BEFORE extra-field strip so that
                           misnamed keys like "location" are mapped to "city" rather
                           than being dropped, leaving the healer with nothing to work from.
  3. EXTRA FIELD STRIP   — unknown keys removed AFTER healing.
  4. FORWARD             — repaired request sent to mcp_server.py.
  5. DRIFT RECOVERY      — field-name drift detected; args re-healed and retried.
  6. RATE-LIMIT (429)    — Retry-After header respected; deterministic sleep + retry.
  7. API TIMEOUT/5xx     — upstream HTTP timeouts AND 5xx errors retried with back-off;
                           LLM decides retry | fallback stub | fail. Per-attempt timing
                           logged to terminal. Separate retry budget (API_TIMEOUT_RETRIES)
                           from network errors (MAX_RETRIES).
  8. PARTIAL RESULT      — missing expected fields detected; warning annotated.
  9. STALE RESULT        — out-of-range numeric values flagged with a warning.
 10. CASCADING TOOLS     — result feeds a second tool call automatically when useful.
 11. RESULT REASSESSMENT — final LLM sanity check; suspicious data annotated.
"""
import os
import re
import socket
import subprocess
import sys
import threading
import time
import logging
import json
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
# TOOL REGISTRY — loaded from api_registry.py
# ═══════════════════════════════════════════════════════════════

from api_registry import API_REGISTRY
import schema_discovery

TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {}
for _name, _info in API_REGISTRY.items():
    _schema = _info["schema"]
    TOOL_REGISTRY[_name] = {
        "description":     _info["description"],
        "category":        _info["domain"],
        "required":        [k for k, v in _schema.items() if v.get("required")],
        "defaults":        {},
        "fields":          list(_schema.keys()),
        "schema":          {k: v.get("description", "") for k, v in _schema.items()},
        "expected_result": [],
        "result_ranges":   {},
        "cascade":         _info.get("cascade"),
        "docs_url":        _info.get("docs_url"),
        "base_url":        _info.get("base_url"),
    }

log.info("Interceptor registry: %d tools from api_registry", len(TOOL_REGISTRY))

# Schema tracking
_server_schema_version: Dict[str, str] = {"version": "current"}


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
# QUERY ANALYSIS AGENT
# ═══════════════════════════════════════════════════════════════

class QueryAnalysisAgent:
    """
    Agent for analyzing natural language queries and mapping them to appropriate tools/APIs.
    Handles query understanding, tool selection, and argument extraction.
    """

    def __init__(self):
        self.analysis_history = []
        self.last_selected_tool = None
        self.last_extracted_args = {}

    def analyze_and_map(self, query: str) -> Tuple[Optional[str], Dict[str, Any], Dict[str, Any]]:
        """
        Analyze a user query and map it to the most appropriate tool.
        Returns: (tool_name, extracted_arguments, analysis_details)
        """
        log.info("►► QueryAnalysisAgent | analyzing: %s", query)

        analysis = {
            "query": query,
            "timestamp": time.time(),
            "status": "pending",
        }

        # Step 1: Build tool catalog with descriptions
        tool_catalog = self._build_tool_catalog()
        analysis["tools_available"] = len(tool_catalog)

        # Step 2: Query analysis via LLM
        tool_name, confidence, reasoning = self._select_tool_with_reasoning(query, tool_catalog)
        analysis["selected_tool"] = tool_name
        analysis["confidence"] = confidence
        analysis["reasoning"] = reasoning

        if not tool_name:
            analysis["status"] = "no_match"
            log.warning("QueryAnalysisAgent | no matching tool found")
            self.analysis_history.append(analysis)
            return None, {}, analysis

        # Step 3: Extract arguments from query
        args = self._extract_arguments(query, tool_name)
        analysis["extracted_arguments"] = args

        # Step 4: Validate against tool schema
        validation = self._validate_arguments(tool_name, args)
        analysis["validation"] = validation

        if not validation.get("is_valid", False):
            log.warning("QueryAnalysisAgent | validation failed: %s", validation.get("issues"))

        analysis["status"] = "success"
        self.last_selected_tool = tool_name
        self.last_extracted_args = args

        log.info("QueryAnalysisAgent ✓ | tool=%s  args=%s  confidence=%.2f",
                 tool_name, args, confidence)

        self.analysis_history.append(analysis)
        return tool_name, args, analysis

    def _build_tool_catalog(self) -> str:
        """Build formatted catalog of available tools for LLM prompt."""
        tools_desc = []
        for name, info in TOOL_REGISTRY.items():
            desc = info.get("description", "")
            category = info.get("category", "")
            required = info.get("required", [])
            fields = info.get("fields", [])

            tool_desc = f"• {name} ({category})\n"
            tool_desc += f"  Description: {desc}\n"
            tool_desc += f"  Fields: {', '.join(fields) if fields else 'none'}\n"
            tool_desc += f"  Required: {', '.join(required) if required else 'none'}"

            tools_desc.append(tool_desc)

        return "\n".join(tools_desc)

    def _select_tool_with_reasoning(
        self, query: str, tool_catalog: str
    ) -> Tuple[Optional[str], float, str]:
        """Use LLM to select best tool with reasoning and confidence score."""
        prompt = f"""You are an intelligent API router agent. Given a user query, select the most appropriate tool.

Available Tools:
{tool_catalog}

User Query: "{query}"

Respond with a JSON object containing:
- "tool": the name of the best matching tool (or null if no match)
- "confidence": a float between 0 and 1 indicating how confident you are (1.0 = perfect match)
- "reasoning": a brief explanation of why you selected this tool

Example:
{{
  "tool": "get_weather",
  "confidence": 0.95,
  "reasoning": "The query asks about weather in a specific city"
}}
"""
        decision = _llm(prompt, "tool_selection")
        if not decision:
            return None, 0.0, "LLM failed to respond"

        tool = decision.get("tool")
        confidence = float(decision.get("confidence", 0.0))
        reasoning = decision.get("reasoning", "")

        return tool, confidence, reasoning

    def _extract_arguments(self, query: str, tool_name: str) -> Dict[str, Any]:
        """Extract tool arguments from the query."""
        tool_info = TOOL_REGISTRY.get(tool_name, {})
        fields = tool_info.get("fields", [])
        field_descs = tool_info.get("schema", {})

        prompt = f"""Extract arguments from this query for the '{tool_name}' tool.

Available fields:
{chr(10).join(f"- {f}: {field_descs.get(f, '')}" for f in fields)}

Query: "{query}"

Return a JSON object with extracted values. Only include fields that you can confidently extract.
If a field cannot be extracted, omit it. Return {{}}} if no arguments can be extracted.
"""
        result = _llm(prompt, f"extract_args_{tool_name}")
        if not result:
            return {}

        # Ensure result is a dict
        return result if isinstance(result, dict) else {}

    def _validate_arguments(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Validate extracted arguments against tool schema."""
        tool_info = TOOL_REGISTRY.get(tool_name, {})
        required_fields = tool_info.get("required", [])
        available_fields = tool_info.get("fields", [])

        validation = {
            "is_valid": True,
            "issues": [],
            "missing_required": [],
            "invalid_fields": [],
        }

        # Check required fields
        missing = [f for f in required_fields if f not in args or not str(args.get(f, "")).strip()]
        if missing:
            validation["is_valid"] = False
            validation["missing_required"] = missing

        # Check for invalid fields
        invalid = [f for f in args.keys() if f not in available_fields]
        if invalid:
            validation["invalid_fields"] = invalid

        if validation["missing_required"]:
            validation["issues"].append(f"Missing required: {', '.join(validation['missing_required'])}")
        if validation["invalid_fields"]:
            validation["issues"].append(f"Unknown fields: {', '.join(validation['invalid_fields'])}")

        return validation

    def get_last_analysis(self) -> Dict[str, Any]:
        """Get the most recent analysis result."""
        return self.analysis_history[-1] if self.analysis_history else {}

    def get_analysis_history(self, limit: int = 10) -> list:
        """Get analysis history (most recent first)."""
        return self.analysis_history[-limit:][::-1]


# Global agent instance
_query_agent = QueryAnalysisAgent()


def _route_query_to_tool(query: str) -> Tuple[Optional[str], Dict[str, Any]]:
    """
    Delegate to QueryAnalysisAgent for intelligent query routing.
    Returns tool name and extracted arguments.
    """
    tool, args, _ = _query_agent.analyze_and_map(query)
    return tool, args if args else {}


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
    server_fields = list(API_REGISTRY.get(tool, {}).get(f"{version}_schema", {}).keys())
    return bool(server_fields) and all(str(args.get(f, "")).strip() for f in server_fields)


class SchemaHealer:
    """
    SBSA-powered deterministic field-name repair.

    Uses sentence-transformers + Hungarian Algorithm instead of LLM inference.
    Runs in <100ms. No probabilistic guessing.

    SHORT-CIRCUIT: if all required fields already match, args pass through unchanged.
    """

    # Store last alignment report for analytics
    last_report: Optional[Dict[str, Any]] = None

    @classmethod
    def heal(cls, tool: str, raw_args: Dict[str, Any], force_version: str = None) -> Dict[str, Any]:
        info = TOOL_REGISTRY.get(tool)
        if info is None:
            log.warning("  SchemaHealer | unknown tool '%s' — passing through", tool)
            return raw_args

        # Tools with no required args need no healing
        if not info["required"]:
            return {}

        version       = force_version or _server_schema_version["version"]
        api_tool      = API_REGISTRY.get(tool, {})
        schema        = api_tool.get(f"{version}_schema", {})
        server_fields = list(schema.keys())

        # Short-circuit: args already match the current server schema
        if server_fields and all(str(raw_args.get(f, "")).strip() for f in server_fields):
            log.info(
                "  SchemaHealer | args already valid for %s — pass-through  %s",
                version, raw_args
            )
            cls.last_report = None
            return raw_args

        # ──────────────────────────────────────────────
        # SBSA HEALING
        # ──────────────────────────────────────────────

        log.info(
            "  SchemaHealer | SBSA healing '%s'  schema=%s  raw=%s",
            tool, version, raw_args,
        )

        # Run deterministic SBSA alignment
        agent_keys = list(raw_args.keys())

        # Get schema descriptions for semantic enrichment
        agent_desc = api_tool.get("schema", {})
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

        log.info("  SchemaHealer | SBSA healed -> %s  (%.1fms)",
                 healed, cls.last_report["elapsed_ms"])
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
    if tool not in API_REGISTRY:
        return args
    schema_keys = set(API_REGISTRY.get(tool, {}).get(f"{version}_schema", {}).keys())
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
    version = _server_schema_version["version"]
    healed_args = SchemaHealer.heal(next_tool, next_args, force_version=version)
    healed_args = strip_extra_fields(next_tool, healed_args, version)

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

            if method == "query":
                self._query_pipeline(req)
                return

            # Intercept set_drift to track drift state
            if method == "set_drift":
                drift_active = req.get("params", {}).get("active", False)
                _server_schema_version["version"] = "current"
                log.info("DRIFT TOGGLED → %s",
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
            SchemaHealer.last_report = None
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
            log.warning("  DRIFT DETECTED — schema mismatch; re-healing ...")
            healed_alt = SchemaHealer.heal(tool_name, raw_args, force_version="alternate")
            healed_alt = strip_extra_fields(tool_name, healed_alt, "alternate")
            req["params"]["arguments"] = healed_alt
            log.info("  re-healed = %s", healed_alt)
            resp = self._forward_with_retry(req, tool_name, healed_alt)

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
            sbsa_report=SchemaHealer.last_report,
        )
        if SchemaHealer.last_report:
            rpt = SchemaHealer.last_report
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
                schema_version=version,
            )

        log.info(
            "PIPELINE END  tool=%s  outcome=%s  total=%.2fs",
            tool_name, outcome, time.monotonic() - pipeline_start,
        )
        log.info("=" * 60)

    def _query_pipeline(self, req: dict):
        """
        Handle natural language queries by using an LLM agent to select the appropriate tool
        and extract arguments, then delegate to the tool call pipeline.
        """
        params = req.get("params", {})
        query = params.get("query", "")
        if not query:
            self._send_client({
                "jsonrpc": "2.0", "id": req.get("id"),
                "error": {"code": -32602, "message": "Missing 'query' parameter", "error_type": "schema"},
            })
            return

        log.info("QUERY PIPELINE START  query=%s", query)

        # Build prompt with available tools
        tools_info = []
        for name, info in TOOL_REGISTRY.items():
            desc = info.get("description", "")
            category = info.get("category", "")
            tools_info.append(f"- {name} ({category}): {desc}")

        tools_list = "\n".join(tools_info)
        prompt = f"""
You are an intelligent agent that routes user queries to the appropriate MCP tool.

Available tools:
{tools_list}

User query: {query}

Analyze the query and select the most appropriate tool. Extract any relevant arguments from the query.

Respond with a JSON object in this exact format:
{{
  "tool": "tool_name",
  "arguments": {{
    "arg1": "value1",
    "arg2": "value2"
  }}
}}

If no tool matches, respond with {{"tool": null, "arguments": {{}}}}
"""

        # Call LLM to decide tool and arguments
        decision = _llm(prompt, "tool_selection")
        if not decision:
            self._send_client({
                "jsonrpc": "2.0", "id": req.get("id"),
                "error": {"code": -32001, "message": "Failed to select tool via LLM", "error_type": "unknown"},
            })
            return

        selected_tool = decision.get("tool")
        selected_args = decision.get("arguments", {})
        if not isinstance(selected_args, dict):
            selected_args = {}

        if not selected_tool or selected_tool not in TOOL_REGISTRY:
            self._send_client({
                "jsonrpc": "2.0", "id": req.get("id"),
                "error": {"code": -32001, "message": f"No suitable tool found for query: {query}", "error_type": "schema"},
            })
            return

        log.info("QUERY PIPELINE | selected tool=%s  args=%s", selected_tool, selected_args)

        # Convert to tool call and delegate to existing pipeline
        tool_req = {
            "jsonrpc": "2.0",
            "id": req.get("id"),
            "method": "tools/call",
            "params": {
                "name": selected_tool,
                "arguments": selected_args
            }
        }

        self._tool_call_pipeline(tool_req)

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