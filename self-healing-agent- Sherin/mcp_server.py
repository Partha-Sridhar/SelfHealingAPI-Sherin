"""
MCP Tool Server — 18 Real APIs + Schema Drift Simulator (SBSA-aligned)
======================================================================
Communicates over stdin/stdout (JSON-RPC 2.0).

Serves 18 real-world APIs across 17 domains from api_registry.py.
Supports live baseline↔drift schema toggling for benchmarking.

This server exposes two shapes for the same logical tools — **baseline** and **drift** —
matching the interceptor's TOOL_REGISTRY (`schema` vs `drift_schema` / `drift_aliases`).

BASELINE SCHEMA
───────────────
Field names stable clients / LLMs typically emit.

DRIFT SCHEMA
────────────
After a simulated "upgrade", the server only accepts renamed parameters.

Toggle drift live:
  {"jsonrpc":"2.0","id":99,"method":"set_drift","params":{"active":true}}

Or at startup:
  SCHEMA_DRIFT=1 python mcp_server.py

WITHOUT interceptor: drift mode + baseline argument names → immediate drift error.
WITH    interceptor (SBSA): arguments are healed to the live shape and retried.

Error taxonomy (error_type on every error response)
───────────────────────────────────────────────────
  schema  — required arg missing or wrong field name
  drift   — drift active; caller used baseline field names instead of drift names
  timeout / network / api / parse / unknown — as documented inline
"""

import hashlib
import json
import logging
import os
import sys
import time

import requests
from requests.exceptions import ConnectionError as ReqConnectionError, ReadTimeout, Timeout

from api_registry import API_REGISTRY

import dynamic_discovery

HTTP_TIMEOUT_S = 8

logging.basicConfig(level=logging.INFO, format="[server] %(levelname)s  %(message)s", stream=sys.stderr)
log = logging.getLogger("server")

# ═══════════════════════════════════════════════════════════════
# DRIFT STATE
# ═══════════════════════════════════════════════════════════════

_state = {"drift": bool(os.environ.get("SCHEMA_DRIFT", ""))}
def drift_active(): return _state["drift"]

# ═══════════════════════════════════════════════════════════════
# BUILD SCHEMAS FROM REGISTRY — baseline (stable) vs drift (renamed parameters)
# ═══════════════════════════════════════════════════════════════

def _build_schemas(version):
    schemas = {}
    for name, info in API_REGISTRY.items():
        s = info[f"{version}_schema"]
        props = {}
        required = []
        for pname, pinfo in s.items():
            props[pname] = {"type": pinfo.get("type", "string"), "description": pinfo.get("description", "")}
            if pinfo.get("required", False):
                required.append(pname)
        schemas[name] = {
            "description": info["description"],
            "inputSchema": {"type": "object", "properties": props, "required": required},
        }
    return schemas

_SCHEMAS_BASELINE = _build_schemas("v1")
_SCHEMAS_DRIFT = _build_schemas("v2")

def active_schemas():
    return _SCHEMAS_DRIFT if drift_active() else _SCHEMAS_BASELINE

def _schema_mode_label() -> str:
    return "drift" if drift_active() else "baseline"

log.info("Loaded %d tools from api_registry", len(API_REGISTRY))

# ═══════════════════════════════════════════════════════════════
# ERRORS
# ═══════════════════════════════════════════════════════════════

class ToolError(Exception):
    def __init__(self, message, error_type="unknown"):
        super().__init__(message)
        self.error_type = error_type

def _http_get(url, params=None, headers=None, label=""):
    log.info("  %s → GET %s", label, url)
    try:
        r = requests.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT_S)
        if r.status_code != 200:
            raise ToolError(f"HTTP {r.status_code} from {url}", error_type="api")
        return r.json()
    except (Timeout, ReadTimeout) as e:
        raise ToolError(f"timeout: {e}", error_type="timeout") from e
    except ReqConnectionError as e:
        raise ToolError(f"network error: {e}", error_type="network") from e
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(str(e), error_type="unknown") from e

# ═══════════════════════════════════════════════════════════════
# ARGUMENT EXTRACTION — baseline vs drift field names
# ═══════════════════════════════════════════════════════════════

def _extract(arguments, tool, baseline_field, drift_field):
    """
    When drift is off, accept only baseline_field.
    When drift is on, accept only drift_field; if the client still sends baseline_field,
    raise error_type=drift (SBSA / interceptor heals and retries with drift_field).
    """
    if drift_active():
        val = arguments.get(drift_field, "")
        if isinstance(val, str): val = val.strip()
        if not val:
            baseline_val = arguments.get(baseline_field, "")
            if isinstance(baseline_val, str): baseline_val = baseline_val.strip()
            if baseline_val:
                raise ToolError(
                    f"[{tool}] Schema drift detected: field '{baseline_field}' is no longer accepted. "
                    f"The server now expects '{drift_field}' instead. "
                    f"You sent: {arguments}",
                    error_type="drift",
                )
            schema = active_schemas().get(tool, {}).get("inputSchema", {})
            required = schema.get("required", [])
            raise ToolError(
                f"[{tool}] Missing required field '{drift_field}'. "
                f"Got: {arguments}",
                error_type="schema",
            )
        return val
    else:
        val = arguments.get(baseline_field, "")
        if isinstance(val, str): val = val.strip()
        if not val:
            raise ToolError(f"[{tool}] Missing required field '{baseline_field}'. Got: {arguments}", error_type="schema")
        return val

def _extract_optional(arguments, baseline_field, drift_field, default=None):
    if drift_active():
        return arguments.get(drift_field, default)
    return arguments.get(baseline_field, default)

# ═══════════════════════════════════════════════════════════════
# TOOL IMPLEMENTATIONS — all 18 real APIs
# ═══════════════════════════════════════════════════════════════

RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "97d16b7b82msh11e4e27d5d98ac8p14faa6jsn99dabf4a5bec")

_CITIES = {
    "delhi": (28.6139, 77.2090), "london": (51.5074, -0.1278),
    "new york": (40.7128, -74.0060), "tokyo": (35.6762, 139.6503),
    "mumbai": (19.0760, 72.8777), "paris": (48.8566, 2.3522),
    "berlin": (52.5200, 13.4050), "sydney": (-33.8688, 151.2093),
    "dubai": (25.2048, 55.2708), "singapore": (1.3521, 103.8198),
}

def _call_get_weather(args):
    city = _extract(args, "get_weather", "city", "location_name")
    lat, lon = _CITIES.get(city.lower(), (28.6139, 77.2090))
    data = _http_get("https://api.open-meteo.com/v1/forecast",
                     params={"latitude": lat, "longitude": lon, "current_weather": True}, label="weather")
    cw = data["current_weather"]
    return {"city": city, "temperature_c": cw["temperature"], "windspeed_kmh": cw["windspeed"]}

def _call_get_country_info(args):
    country = _extract(args, "get_country_info", "country", "country_name")
    data = _http_get(f"https://restcountries.com/v3.1/name/{country}", params={"fullText": "true"}, label="country")
    d = data[0]
    return {"country": d["name"]["common"], "capital": d["capital"][0], "population": d["population"], "region": d["region"]}

def _call_get_crypto_price(args):
    coin = _extract(args, "get_crypto_price", "coin", "crypto_id")
    currency = _extract(args, "get_crypto_price", "currency", "vs_currency")
    data = _http_get("https://api.coingecko.com/api/v3/simple/price",
                     params={"ids": coin.lower(), "vs_currencies": currency.lower()}, label="crypto")
    return {"coin": coin, "currency": currency, "price": data[coin.lower()][currency.lower()]}

def _call_get_exchange_rate(args):
    base = _extract(args, "get_exchange_rate", "base", "from_currency")
    target = _extract(args, "get_exchange_rate", "target", "to_currency")
    data = _http_get(f"https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/{base.lower()}.json", label="exchange")
    return {"base": base.upper(), "target": target.upper(), "rate": data[base.lower()][target.lower()]}

def _call_search_books(args):
    query = _extract(args, "search_books", "query", "search_term")
    limit = _extract_optional(args, "limit", "max_results", 3)
    data = _http_get("https://openlibrary.org/search.json", params={"q": query, "limit": limit}, label="books")
    books = [{"title": b.get("title"), "author": b.get("author_name", ["Unknown"])[0], "year": b.get("first_publish_year")}
             for b in data.get("docs", [])[:3]]
    return {"query": query, "results": books, "total": data.get("numFound", 0)}

def _call_get_joke(args):
    category = _extract(args, "get_joke", "category", "joke_type")
    data = _http_get(f"https://v2.jokeapi.dev/joke/{category}", label="joke")
    if data.get("type") == "twopart":
        return {"category": data["category"], "setup": data["setup"], "delivery": data["delivery"]}
    return {"category": data.get("category"), "joke": data.get("joke")}

def _call_define_word(args):
    word = _extract(args, "define_word", "word", "term")
    data = _http_get(f"https://api.dictionaryapi.dev/api/v2/entries/en/{word}", label="dictionary")
    entry = data[0]
    meanings = [{"part_of_speech": m["partOfSpeech"], "definition": m["definitions"][0]["definition"]}
                for m in entry.get("meanings", [])[:2]]
    return {"word": entry["word"], "phonetic": entry.get("phonetic", ""), "meanings": meanings}

def _call_search_universities(args):
    name = _extract(args, "search_universities", "name", "university_name")
    country = _extract_optional(args, "country", "country_name")
    params = {"name": name}
    if country: params["country"] = country
    data = _http_get("http://universities.hipolabs.com/search", params=params, label="universities")
    return {"results": [{"name": u["name"], "country": u["country"], "website": u.get("web_pages", [""])[0]}
                        for u in data[:5]], "total": len(data)}

def _call_search_cocktail(args):
    name = _extract(args, "search_cocktail", "name", "drink_name")
    data = _http_get("https://www.thecocktaildb.com/api/json/v1/1/search.php", params={"s": name}, label="cocktail")
    drinks = data.get("drinks") or []
    return {"results": [{"name": d["strDrink"], "category": d.get("strCategory"), "instructions": d.get("strInstructions", "")[:100]}
                        for d in drinks[:3]]}

def _call_get_trivia(args):
    category = _extract(args, "get_trivia", "category", "topic_id")
    difficulty = _extract_optional(args, "difficulty", "level")
    params = {"amount": 1, "category": category, "type": "multiple"}
    if difficulty: params["difficulty"] = difficulty
    data = _http_get("https://opentdb.com/api.php", params=params, label="trivia")
    if data.get("results"):
        q = data["results"][0]
        return {"question": q["question"], "correct_answer": q["correct_answer"],
                "difficulty": q["difficulty"], "category": q["category"]}
    return {"error": "No questions found"}

def _call_get_pokemon(args):
    name = _extract(args, "get_pokemon", "name", "pokemon_name")
    data = _http_get(f"https://pokeapi.co/api/v2/pokemon/{name.lower()}", label="pokemon")
    return {"name": data["name"], "id": data["id"],
            "types": [t["type"]["name"] for t in data["types"]],
            "height": data["height"], "weight": data["weight"]}

def _call_get_space_photo(args):
    date = _extract_optional(args, "date", "photo_date")
    params = {"api_key": os.environ.get("NASA_API_KEY", "DEMO_KEY")}
    if date: params["date"] = date
    data = _http_get("https://api.nasa.gov/planetary/apod", params=params, label="nasa")
    return {"title": data["title"], "date": data["date"], "explanation": data["explanation"][:200], "url": data.get("url")}

def _call_geolocate_ip(args):
    ip = _extract(args, "geolocate_ip", "ip", "ip_address")
    data = _http_get(f"http://ip-api.com/json/{ip}", label="geoip")
    return {"ip": ip, "country": data["country"], "city": data.get("city"), "lat": data.get("lat"), "lon": data.get("lon"), "isp": data.get("isp")}

def _call_search_team(args):
    team = _extract(args, "search_team", "team", "team_name")
    data = _http_get("https://www.thesportsdb.com/api/v1/json/3/searchteams.php", params={"t": team}, label="sports")
    teams = data.get("teams") or []
    if teams:
        t = teams[0]
        return {"team": t["strTeam"], "sport": t.get("strSport"), "league": t.get("strLeague"),
                "country": t.get("strCountry"), "description": (t.get("strDescriptionEN") or "")[:200]}
    return {"error": f"Team '{team}' not found"}

def _call_search_song(args):
    q = _extract(args, "search_song", "q", "search_query")
    data = _http_get("https://genius-song-lyrics1.p.rapidapi.com/search/", params={"q": q},
                     headers={"X-RapidAPI-Key": RAPIDAPI_KEY, "X-RapidAPI-Host": "genius-song-lyrics1.p.rapidapi.com"}, label="genius")
    hits = data.get("hits", [])[:3]
    return {"query": q, "results": [{"title": h["result"]["title"], "artist": h["result"]["primary_artist"]["name"]}
                                     for h in hits if "result" in h]}

def _call_get_dog_image(args):
    breed = _extract(args, "get_dog_image", "breed", "dog_breed")
    data = _http_get(f"https://dog.ceo/api/breed/{breed.lower()}/images/random", label="dog")
    return {"breed": breed, "image_url": data.get("message")}

def _call_get_activity(args):
    atype = _extract(args, "get_activity", "type", "activity_type")
    data = _http_get("https://bored-api.appbrewery.com/filter", params={"type": atype}, label="bored")
    if isinstance(data, list) and data:
        a = data[0]
        return {"activity": a.get("activity"), "type": a.get("type"), "participants": a.get("participants")}
    return {"error": f"No activities found for type '{atype}'"}

def _call_predict_age(args):
    name = _extract(args, "predict_age", "name", "first_name")
    data = _http_get("https://api.agify.io", params={"name": name}, label="agify")
    return {"name": data["name"], "predicted_age": data["age"], "count": data["count"]}

# ───────── NEW APIs ─────────

def _call_get_timezone_time(args):
    zone = _extract(args, "get_timezone_time", "zone", "timezone")
    data = _http_get("https://timeapi.io/api/time/current/zone", params={"timeZone": zone}, label="timezone")
    return {
        "timezone": data.get("timeZone", zone),
        "datetime": data.get("dateTime"),
        "utc_offset": f"UTC{'+' if not str(data.get('time','')).startswith('-') else ''}{data.get('time', '')}",
    }


def _call_predict_gender(args):
    name = _extract(args, "predict_gender", "name", "first_name")
    data = _http_get("https://api.genderize.io", params={"name": name}, label="gender")
    return {
        "name": name,
        "gender": data.get("gender"),
        "probability": data.get("probability"),
    }


def _call_predict_nationality(args):
    name = _extract(args, "predict_nationality", "name", "person_name")
    data = _http_get("https://api.nationalize.io", params={"name": name}, label="nationality")
    countries = data.get("country", [])
    country_map = {
    "IN": "India",
    "US": "United States",
    "FR": "France",
    "GB": "United Kingdom",
    "BD": "Bangladesh",
    "AE": "United Arab Emirates",
    "PK": "Pakistan",
    }

    top_country = None

    if countries and "country_id" in countries[0]:
        code = countries[0]["country_id"]
        top_country = country_map.get(code)

    result = {"name": name}

    if top_country:
        result["country"] = top_country   # REQUIRED FOR CASCADE

    return result


def _call_reverse_geocode(args):
    lat = _extract(args, "reverse_geocode", "lat", "latitude")
    lon = _extract(args, "reverse_geocode", "lon", "longitude")

    data = _http_get(
        "https://api.bigdatacloud.net/data/reverse-geocode-client",
        params={"latitude": lat, "longitude": lon},
        label="reverse_geo"
    )

    return {
        "city": data.get("city"),
        "country": data.get("countryName"),
    }


def _call_get_bank_details(args):
    ifsc = _extract(args, "get_bank_details", "ifsc", "ifsc_code")
    data = _http_get(f"https://ifsc.razorpay.com/{ifsc}", label="bank")

    return {
        "bank": data.get("BANK"),
        "branch": data.get("BRANCH"),
        "city": data.get("CITY"),
    }


def _call_get_ip_details(args):
    ip = _extract(args, "get_ip_details", "ip", "ip_address")
    data = _http_get(f"https://ipapi.co/{ip}/json/", label="ip")

    return {
        "ip": ip,
        "city": data.get("city"),
        "country": data.get("country_name"),
    }


def _call_get_currency_info(args):
    country = _extract(args, "get_currency_info", "country", "country_name")
    data = _http_get(f"https://restcountries.com/v3.1/name/{country}", label="currency")

    d = data[0]
    currencies = d.get("currencies", {})
    return {
        "country": country,
        "currencies": list(currencies.keys()),
    }

# ═══════════════════════════════════════════════════════════════
# TOOL ROUTER
# ═══════════════════════════════════════════════════════════════

_TOOL_MAP = {
    "get_weather": _call_get_weather,
    "get_country_info": _call_get_country_info,
    "get_crypto_price": _call_get_crypto_price,
    "get_exchange_rate": _call_get_exchange_rate,
    "search_books": _call_search_books,
    "get_joke": _call_get_joke,
    "define_word": _call_define_word,
    "search_universities": _call_search_universities,
    "search_cocktail": _call_search_cocktail,
    "get_trivia": _call_get_trivia,
    "get_pokemon": _call_get_pokemon,
    "get_space_photo": _call_get_space_photo,
    "geolocate_ip": _call_geolocate_ip,
    "search_team": _call_search_team,
    "search_song": _call_search_song,
    "get_dog_image": _call_get_dog_image,
    "get_activity": _call_get_activity,
    "predict_age": _call_predict_age,
    "get_timezone_time": _call_get_timezone_time,
    "predict_gender": _call_predict_gender,
    "predict_nationality": _call_predict_nationality,
    "reverse_geocode": _call_reverse_geocode,
    "get_bank_details": _call_get_bank_details,
    "get_ip_details": _call_get_ip_details,
    "get_currency_info": _call_get_currency_info,
}

def call_tool(name, arguments):
    fn = _TOOL_MAP.get(name)
    if not fn:
        raise ToolError(f"Unknown tool: '{name}'", error_type="unknown")
    return fn(arguments)

# ═══════════════════════════════════════════════════════════════
# JSON-RPC TRANSPORT
# ═══════════════════════════════════════════════════════════════

def _send(p): print(json.dumps(p), flush=True)
def _ok(id_, result): _send({"jsonrpc": "2.0", "id": id_, "result": result})
def _err(id_, msg, error_type="unknown", code=-32000):
    _send({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": str(msg), "error_type": error_type}})

def main():
    log.info("mcp_server ready  schema_mode=%s  tools=%d", _schema_mode_label(), len(_TOOL_MAP))

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw: continue
        req = {}
        try:
            req = json.loads(raw)
            method = req.get("method")
            id_ = req.get("id")

            if method == "initialize":
                _ok(id_, {"protocolVersion": "2024-11-05",
                          "serverInfo": {"name": "sbsa-tool-server", "version": "3.0",
                                         "schema_mode": _schema_mode_label(),
                                         "tools_count": len(_TOOL_MAP)},
                          "capabilities": {"tools": {}}})

            elif method == "tools/list":
                schemas = active_schemas()
                static_tools = [{"name": n, **s} for n, s in schemas.items()]
                dynamic_tools = dynamic_discovery.get_dynamic_tools_for_mcp()
                _ok(id_, {"tools": static_tools + dynamic_tools})

            elif method == "tools/call":
                params = req.get("params", {})
                name = params.get("name")
                arguments = params.get("arguments", {})
                log.info("tools/call  tool=%s  schema_mode=%s  args=%s", name, _schema_mode_label(), arguments)
                if name and name.startswith("dynamic_"):
                    result = dynamic_discovery.call_dynamic_tool(name, arguments)
                else:
                    result = call_tool(name, arguments)
                _ok(id_, {"structuredContent": result, "isError": False})

            elif method in ("initialized", "notifications/initialized"):
                pass

            elif method == "set_drift":
                active = req.get("params", {}).get("active", False)
                _state["drift"] = bool(active)
                log.info("DRIFT → %s", "ON 🔴" if _state["drift"] else "OFF 🟢")
                _ok(id_, {"drift_active": _state["drift"], "schema_mode": _schema_mode_label()})

            elif method == "register_dynamic_tool":
                p = req.get("params", {})
                tname = p.get("name", "")
                dynamic_discovery._dynamic_tools[tname] = {
                    "description": p.get("description", ""),
                    "base_url": p.get("base_url", ""),
                    "method": p.get("method", "GET"),
                    "inputSchema": p.get("inputSchema", {}),
                    "params": p.get("params", {}),
                }
                log.info("Registered dynamic tool in server: %s → %s", tname, p.get("base_url"))
                _ok(id_, {"registered": tname})

            elif method == "get_drift_status":
                _ok(id_, {"drift_active": _state["drift"], "schema_mode": _schema_mode_label()})

            else:
                _err(id_, f"Unknown method: '{method}'", code=-32601)

        except ToolError as te:
            log.warning("ToolError [%s]: %s", te.error_type, te)
            _err(req.get("id"), str(te), error_type=te.error_type)
        except json.JSONDecodeError as je:
            _err(None, f"Invalid JSON: {je}", error_type="parse", code=-32700)
        except Exception as exc:
            log.error("Unhandled: %s", exc, exc_info=True)
            _err(req.get("id"), str(exc))

if __name__ == "__main__":
    main()
