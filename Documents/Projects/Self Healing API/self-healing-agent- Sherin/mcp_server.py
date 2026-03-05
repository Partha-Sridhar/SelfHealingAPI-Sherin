"""
MCP Tool Server — with Schema Drift Simulator
=============================================
Communicates over stdin/stdout (JSON-RPC 2.0).

SCHEMA DRIFT
────────────
Schema drift happens when a server is "upgraded" but clients were built
against the old field names. This server simulates that by supporting
two schema versions that can be switched live.

  V1 (original) — field names the LLM/client was trained on
  V2 (drifted)  — new field names after a server "upgrade"

V1 → V2 field renames (what breaks without the interceptor)
────────────────────────────────────────────────────────────
  get_weather      : "city"    → "location_name"
  get_country_info : "country" → "country_name"
  get_exchange_rate: "base"    → "from_currency",  "target" → "to_currency"
  get_stock_price  : "symbol"  → "ticker"

Toggle drift live via a special JSON-RPC method:
  {"jsonrpc":"2.0","id":99,"method":"set_drift","params":{"active":true}}
  {"jsonrpc":"2.0","id":99,"method":"set_drift","params":{"active":false}}

Or pre-enable at startup:
  SCHEMA_DRIFT=1 python mcp_server.py

WITHOUT interceptor: V2 calls with V1 args fail immediately (schema error).
WITH    interceptor: the LLM reads the intent and maps args correctly every time.

Error taxonomy (error_type field on every error response)
─────────────────────────────────────────────────────────
  schema  — required arg missing or wrong field name
  drift   — drift is active; caller used old (V1) field names
  timeout — upstream API timed out
  network — connection error
  api     — upstream returned non-200
  parse   — unexpected response shape
  unknown — anything else
"""

import json
import logging
import os
import sys
import time

import requests
from requests.exceptions import ConnectionError as ReqConnectionError, ReadTimeout, Timeout

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════

HTTP_TIMEOUT_S = 8

logging.basicConfig(
    level=logging.INFO,
    format="[server] %(levelname)s  %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("server")


# ═══════════════════════════════════════════════════════════════
# DRIFT STATE  (mutable — toggled live via set_drift method)
# ═══════════════════════════════════════════════════════════════

_state = {"drift": bool(os.environ.get("SCHEMA_DRIFT", ""))}


def drift_active() -> bool:
    return _state["drift"]


# ═══════════════════════════════════════════════════════════════
# SCHEMA VERSIONS
# ═══════════════════════════════════════════════════════════════

# V1 — original field names (what the LLM was trained on)
_SCHEMAS_V1 = {
    "get_bitcoin_price": {
        "description": "Get the current Bitcoin price in USD. No arguments needed.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    "get_weather": {
        "description": "Get current weather for a city.",
        "inputSchema": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "City name"}},
            "required": ["city"],
        },
    },
    "get_country_info": {
        "description": "Get facts about a country.",
        "inputSchema": {
            "type": "object",
            "properties": {"country": {"type": "string", "description": "Country name"}},
            "required": ["country"],
        },
    },
    "get_exchange_rate": {
        "description": "Get the exchange rate between two currencies.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "base":   {"type": "string", "description": "Base currency, e.g. USD"},
                "target": {"type": "string", "description": "Target currency, e.g. EUR"},
            },
            "required": ["base", "target"],
        },
    },
    "get_stock_price": {
        "description": "Get the current price for a stock.",
        "inputSchema": {
            "type": "object",
            "properties": {"symbol": {"type": "string", "description": "Ticker symbol, e.g. AAPL"}},
            "required": ["symbol"],
        },
    },
}

# V2 — drifted field names (simulates a server "upgrade")
_SCHEMAS_V2 = {
    "get_bitcoin_price": _SCHEMAS_V1["get_bitcoin_price"],   # unchanged
    "get_weather": {
        "description": "Get current weather for a location.",
        "inputSchema": {
            "type": "object",
            "properties": {"location_name": {"type": "string", "description": "Name of the city or location"}},
            "required": ["location_name"],
        },
    },
    "get_country_info": {
        "description": "Get facts about a country.",
        "inputSchema": {
            "type": "object",
            "properties": {"country_name": {"type": "string", "description": "Full country name"}},
            "required": ["country_name"],
        },
    },
    "get_exchange_rate": {
        "description": "Get the exchange rate between two currencies.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from_currency": {"type": "string", "description": "Source currency code, e.g. USD"},
                "to_currency":   {"type": "string", "description": "Target currency code, e.g. EUR"},
            },
            "required": ["from_currency", "to_currency"],
        },
    },
    "get_stock_price": {
        "description": "Get the current price for a stock.",
        "inputSchema": {
            "type": "object",
            "properties": {"ticker": {"type": "string", "description": "Stock ticker symbol, e.g. AAPL"}},
            "required": ["ticker"],
        },
    },
}

# Field mapping: V1 name → V2 name (used for drift detection hints in errors)
_DRIFT_MAP = {
    "get_weather":       {"city":   "location_name"},
    "get_country_info":  {"country": "country_name"},
    "get_exchange_rate": {"base":   "from_currency", "target": "to_currency"},
    "get_stock_price":   {"symbol": "ticker"},
}


def active_schemas() -> dict:
    return _SCHEMAS_V2 if drift_active() else _SCHEMAS_V1


# ═══════════════════════════════════════════════════════════════
# STRUCTURED ERROR
# ═══════════════════════════════════════════════════════════════

class ToolError(Exception):
    def __init__(self, message: str, error_type: str = "unknown"):
        super().__init__(message)
        self.error_type = error_type


# ═══════════════════════════════════════════════════════════════
# HTTP HELPER
# ═══════════════════════════════════════════════════════════════

def _http_get(url: str, params: dict = None, label: str = "") -> dict:
    log.info("  %s → GET %s  params=%s", label, url, params)
    try:
        r = requests.get(url, params=params, timeout=HTTP_TIMEOUT_S)
        if r.status_code != 200:
            raise ToolError(f"API returned HTTP {r.status_code} from {url}", error_type="api")
        return r.json()
    except (Timeout, ReadTimeout) as exc:
        raise ToolError(f"timeout calling {url}: {exc}", error_type="timeout") from exc
    except ReqConnectionError as exc:
        raise ToolError(f"network error calling {url}: {exc}", error_type="network") from exc
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(str(exc), error_type="unknown") from exc


# ═══════════════════════════════════════════════════════════════
# ARGUMENT EXTRACTION — version-aware
# ═══════════════════════════════════════════════════════════════

def _extract(arguments: dict, tool: str, v1_field: str, v2_field: str) -> str:
    """
    Pull a value from arguments regardless of whether the caller sent
    the V1 or V2 field name.

    If drift is active and the caller used the OLD (V1) name, raise a
    drift error — this is exactly what breaks without the interceptor.
    """
    if drift_active():
        # Server now only accepts V2 names
        val = arguments.get(v2_field, "").strip()
        if not val:
            # Check if they sent the old V1 name — give a specific drift error
            old_val = arguments.get(v1_field, "").strip()
            if old_val:
                raise ToolError(
                    f"[{tool}] Schema drift detected: field '{v1_field}' is no longer accepted. "
                    f"The server was upgraded and now expects '{v2_field}' instead. "
                    f"You sent: {arguments}",
                    error_type="drift",
                )
            raise ToolError(
                f"[{tool}] Missing required field '{v2_field}'. "
                f"Got: {arguments}",
                error_type="schema",
            )
        return val
    else:
        # V1 mode — only accept original names
        val = arguments.get(v1_field, "").strip()
        if not val:
            raise ToolError(
                f"[{tool}] Missing required field '{v1_field}'. Got: {arguments}",
                error_type="schema",
            )
        return val


# ═══════════════════════════════════════════════════════════════
# TOOL IMPLEMENTATIONS
# ═══════════════════════════════════════════════════════════════

_CITIES = {
    "delhi":         (28.6139,  77.2090),
    "london":        (51.5074,  -0.1278),
    "new york":      (40.7128, -74.0060),
    "new york city": (40.7128, -74.0060),
    "nyc":           (40.7128, -74.0060),
    "tokyo":         (35.6762, 139.6503),
    "mumbai":        (19.0760,  72.8777),
    "paris":         (48.8566,   2.3522),
    "berlin":        (52.5200,  13.4050),
    "sydney":       (-33.8688, 151.2093),
    "dubai":         (25.2048,  55.2708),
    "singapore":     (1.3521,  103.8198),
    "bangalore":    (12.9716,   77.5946),
    "bengaluru":    (12.9716,   77.5946),
}


def get_bitcoin_price() -> dict:
    data = _http_get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": "bitcoin", "vs_currencies": "usd"},
        label="get_bitcoin_price",
    )
    try:
        return {"bitcoin_usd": data["bitcoin"]["usd"]}
    except KeyError as exc:
        raise ToolError(f"Unexpected response shape: {exc}", error_type="parse") from exc


def get_weather(arguments: dict) -> dict:
    city = _extract(arguments, "get_weather", v1_field="city", v2_field="location_name")
    lat, lon = _CITIES.get(city.lower(), (28.6139, 77.2090))
    time.sleep(20)
    data = _http_get(
        "https://api.open-meteo.com/v1/forecast",
        params={"latitude": lat, "longitude": lon, "current_weather": True},
        label=f"get_weather({city})",
    )
    try:
        cw = data["current_weather"]
        return {
            "city":          city,
            "temperature_c": cw["temperature"],
            "windspeed_kmh": cw["windspeed"],
            "weathercode":   cw.get("weathercode"),
        }
    except KeyError as exc:
        raise ToolError(f"Unexpected weather response: {exc}", error_type="parse") from exc


def get_country_info(arguments: dict) -> dict:
    country = _extract(arguments, "get_country_info", v1_field="country", v2_field="country_name")
    data = _http_get(
        f"https://restcountries.com/v3.1/name/{country}",
        label=f"get_country_info({country})",
    )
    try:
        d = data[0]
        return {
            "country":    d["name"]["common"],
            "capital":    d["capital"][0],
            "population": d["population"],
            "region":     d["region"],
            "subregion":  d.get("subregion", ""),
            "currency": d.get("currency", "")
        }
    except (KeyError, IndexError) as exc:
        raise ToolError(f"Unexpected country response: {exc}", error_type="parse") from exc


def get_exchange_rate(arguments: dict) -> dict:
    base   = _extract(arguments, "get_exchange_rate", v1_field="base",   v2_field="from_currency")
    target = _extract(arguments, "get_exchange_rate", v1_field="target", v2_field="to_currency")
    data = _http_get(
        "https://api.exchangerate.host/convert",
        params={"from": base.upper(), "to": target.upper()},
        label=f"get_exchange_rate({base}/{target})",
    )
    try:
        return {"base": base.upper(), "target": target.upper(), "rate": data["result"]}
    except KeyError as exc:
        raise ToolError(f"Unexpected exchange-rate response: {exc}", error_type="parse") from exc


def get_stock_price(arguments: dict) -> dict:
    symbol = _extract(arguments, "get_stock_price", v1_field="symbol", v2_field="ticker")
    data = _http_get(
        "https://query1.finance.yahoo.com/v7/finance/quote",
        params={"symbols": symbol.upper()},
        label=f"get_stock_price({symbol})",
    )
    try:
        r = data["quoteResponse"]["result"][0]
        return {
            "symbol":       r["symbol"],
            "price":        r["regularMarketPrice"],
            "currency":     r["currency"],
            "exchange":     r.get("fullExchangeName", ""),
            "market_state": r.get("marketState", ""),
        }
    except (KeyError, IndexError) as exc:
        raise ToolError(f"Unexpected stock response: {exc}", error_type="parse") from exc


# ═══════════════════════════════════════════════════════════════
# TOOL ROUTER
# ═══════════════════════════════════════════════════════════════

def call_tool(name: str, arguments: dict) -> dict:
    if name == "get_bitcoin_price":  return get_bitcoin_price()
    if name == "get_weather":        return get_weather(arguments)
    if name == "get_country_info":   return get_country_info(arguments)
    if name == "get_exchange_rate":  return get_exchange_rate(arguments)
    if name == "get_stock_price":    return get_stock_price(arguments)
    raise ToolError(f"Unknown tool: '{name}'", error_type="unknown")


# ═══════════════════════════════════════════════════════════════
# JSON-RPC TRANSPORT
# ═══════════════════════════════════════════════════════════════

def _send(payload: dict):
    print(json.dumps(payload), flush=True)


def _ok(id_, result: dict):
    _send({"jsonrpc": "2.0", "id": id_, "result": result})


def _err(id_, message: str, error_type: str = "unknown", code: int = -32000):
    _send({
        "jsonrpc": "2.0",
        "id": id_,
        "error": {"code": code, "message": str(message), "error_type": error_type},
    })


# ═══════════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════════

def main():
    log.info("mcp_server ready  drift=%s", drift_active())
    log.info("  V1 fields: city | country | base/target | symbol")
    log.info("  V2 fields: location_name | country_name | from_currency/to_currency | ticker")

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        req = {}
        try:
            req    = json.loads(raw)
            method = req.get("method")
            id_    = req.get("id")

            # ── Standard MCP methods ──────────────────────────
            if method == "initialize":
                _ok(id_, {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {
                        "name":    "mcp-tool-server",
                        "version": "2.0",
                        "schema_version": "V2 (drifted)" if drift_active() else "V1 (original)",
                    },
                    "capabilities": {"tools": {}},
                })

            elif method == "tools/list":
                schemas = active_schemas()
                _ok(id_, {"tools": [{"name": n, **s} for n, s in schemas.items()]})
                log.info(
                    "tools/list served  schema_version=%s",
                    "V2 (drifted)" if drift_active() else "V1 (original)",
                )

            elif method == "tools/call":
                params    = req.get("params", {})
                name      = params.get("name")
                arguments = params.get("arguments", {})
                schema_v  = "V2" if drift_active() else "V1"
                log.info("tools/call  tool=%-22s  schema=%s  args=%s", name, schema_v, arguments)
                result = call_tool(name, arguments)
                log.info("  ✓ success")
                _ok(id_, {"structuredContent": result, "isError": False})

            elif method in ("initialized", "notifications/initialized"):
                pass  # fire-and-forget

            # ── Drift control (for demo/testing) ──────────────
            elif method == "set_drift":
                active = req.get("params", {}).get("active", False)
                _state["drift"] = bool(active)
                status = "V2 DRIFTED 🔴" if _state["drift"] else "V1 ORIGINAL 🟢"
                log.info("DRIFT TOGGLED → %s", status)
                _ok(id_, {
                    "drift_active":    _state["drift"],
                    "schema_version":  "V2 (drifted)" if _state["drift"] else "V1 (original)",
                    "v1_fields": {"get_weather": "city", "get_country_info": "country",
                                  "get_exchange_rate": "base/target", "get_stock_price": "symbol"},
                    "v2_fields": {"get_weather": "location_name", "get_country_info": "country_name",
                                  "get_exchange_rate": "from_currency/to_currency", "get_stock_price": "ticker"},
                })

            elif method == "get_drift_status":
                _ok(id_, {
                    "drift_active":   _state["drift"],
                    "schema_version": "V2 (drifted)" if _state["drift"] else "V1 (original)",
                    "drift_map":      _DRIFT_MAP,
                })

            else:
                _err(id_, f"Unknown method: '{method}'", error_type="unknown", code=-32601)

        except ToolError as te:
            log.warning("ToolError [%s]: %s", te.error_type, te)
            _err(req.get("id"), str(te), error_type=te.error_type)
        except json.JSONDecodeError as je:
            log.error("JSON parse error: %s", je)
            _err(None, f"Invalid JSON: {je}", error_type="parse", code=-32700)
        except Exception as exc:
            log.error("Unhandled error: %s", exc)
            _err(req.get("id"), str(exc), error_type="unknown")


if __name__ == "__main__":
    main()