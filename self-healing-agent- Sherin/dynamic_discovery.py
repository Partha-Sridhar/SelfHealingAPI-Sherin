"""
Dynamic API Discovery — Runtime Tool Registration
==================================================

When no registered tool matches a user's question, this module:
  1. Asks the LLM to suggest a free, no-auth public API that can answer it
  2. Validates the API is reachable
  3. Discovers its schema (via schema_discovery.py)
  4. Registers it as a dynamic tool so SBSA can heal calls to it

Keeps a persistent cache of discovered APIs to avoid repeated LLM calls.
"""

import json
import logging
import os
import time
from typing import Any, Dict, Optional

import requests
import schema_discovery

log = logging.getLogger("dynamic_discovery")

CACHE_FILE = os.path.join(os.path.dirname(__file__), "dynamic_api_cache.json")
_cache: Dict[str, dict] = {}

# Dynamic tools registered at runtime — mcp_server.py reads this
_dynamic_tools: Dict[str, dict] = {}


def _load_cache():
    global _cache
    if os.path.isfile(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                _cache = json.load(f)
        except Exception:
            pass

def _save_cache():
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(_cache, f, indent=2)
    except Exception:
        pass

_load_cache()


# ═══════════════════════════════════════════════════════════════
# STEP 1: Ask LLM to find a suitable public API
# ═══════════════════════════════════════════════════════════════

_DISCOVERY_PROMPT = """You are an API discovery system. Given a user question, suggest ONE free public REST API that can answer it.

Requirements:
- Must be free, no API key or authentication required
- Must return JSON
- Must be a real, working, well-known API (prefer popular ones like open-notify.org, restcountries.com, api.agify.io, zenquotes.io, catfact.ninja, dog.ceo, api.open-meteo.com, etc.)
- Prefer simple GET endpoints with minimal parameters
- For NEWS queries: use https://api.rss2json.com/v1/api.json?rss_url=RSS_FEED_URL — it converts any RSS feed to JSON for free. Pick the most relevant feed:
  - World news: BBC (https://feeds.bbci.co.uk/news/world/rss.xml), CNN (http://rss.cnn.com/rss/edition_world.rss), Reuters (https://feeds.reuters.com/reuters/worldNews)
  - India news: Times of India (https://timesofindia.indiatimes.com/rssfeedstopstories.cms), NDTV (https://feeds.feedburner.com/ndtvnews-top-stories), The Hindu (https://www.thehindu.com/news/national/feeder/default.rss), Indian Express (https://indianexpress.com/feed/), Hindustan Times (https://www.hindustantimes.com/feeds/rss/india-news/rssfeed.xml)
  - Tech news: TechCrunch (https://techcrunch.com/feed/), Ars Technica (https://feeds.arstechnica.com/arstechnica/index)
  - Sports: ESPN (https://www.espn.com/espn/rss/news)
  Set rss_url as the param. The response has "items" array with title, link, description, pubDate.
- NEVER suggest newsapi.org, newsdata.io, gnews.io, mediastack.com, currentsapi — they all require paid API keys

Respond ONLY with valid JSON, no markdown:
{{
  "api_name": "short_snake_case_name",
  "description": "what it does",
  "base_url": "https://exact.url/endpoint",
  "method": "GET",
  "params": {{
    "param_name": {{
      "description": "what this param is",
      "required": true,
      "example": "example_value"
    }}
  }},
  "extract_from_question": {{
    "param_name": "value extracted from the question"
  }}
}}

If no suitable free API exists, respond: {{"api_name": null}}

User question: {question}"""


def _ask_llm_for_api(question: str, exclude_urls: list = None) -> Optional[dict]:
    """Use available LLM to find a suitable API."""
    prompt = _DISCOVERY_PROMPT.format(question=question)
    if exclude_urls:
        prompt += f"\n\nDO NOT suggest these URLs (they are down/broken): {', '.join(exclude_urls)}"

    # Try Groq first (fast), then Ollama
    groq_key = os.environ.get("GROQ_API_KEY")
    if groq_key:
        try:
            from groq import Groq
            client = Groq(api_key=groq_key)
            resp = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
            )
            result = json.loads(resp.choices[0].message.content.strip())
            if result.get("api_name"):
                return result
        except Exception as e:
            log.warning("Groq discovery failed: %s", e)

    try:
        import ollama
        resp = ollama.chat(
            model="llama3", format="json",
            messages=[{"role": "user", "content": prompt}],
        )
        result = json.loads(resp["message"]["content"].strip())
        if result.get("api_name"):
            return result
    except Exception as e:
        log.warning("Ollama discovery failed: %s", e)

    return None


# ═══════════════════════════════════════════════════════════════
# STEP 2: Validate the API is reachable
# ═══════════════════════════════════════════════════════════════

def _validate_api(api_info: dict) -> bool:
    """Quick check that the API responds with JSON."""
    try:
        url = api_info["base_url"]
        params = dict(api_info.get("extract_from_question", {}))
        # For URL-templated APIs, substitute params
        for k, v in list(params.items()):
            if f"{{{k}}}" in url:
                url = url.replace(f"{{{k}}}", str(v))
                del params[k]

        r = requests.get(url, params=params or None, timeout=8,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            r.json()  # verify it's JSON
            return True
        log.warning("API validation failed: HTTP %d from %s", r.status_code, url)
    except Exception as e:
        log.warning("API validation failed for %s: %s", api_info.get("base_url"), e)
    return False


# ═══════════════════════════════════════════════════════════════
# STEP 3: Register as dynamic tool
# ═══════════════════════════════════════════════════════════════

def _register_dynamic_tool(api_info: dict) -> str:
    """Register the discovered API as a callable dynamic tool.
    Uses schema_discovery.py for real schema (OpenAPI → doc scraping → LLM guess fallback).
    Also registers into interceptor's TOOL_REGISTRY for full SBSA pipeline."""
    name = f"dynamic_{api_info['api_name']}"
    base_url = api_info["base_url"]
    docs_url = api_info.get("docs_url")

    # Step 1: Try real schema discovery (OpenAPI / doc scraping)
    llm_params = api_info.get("params", {})
    real_params = schema_discovery.discover_schema(
        tool_name=name, base_url=base_url, docs_url=docs_url,
    )

    # Step 2: Build schema — prefer real discovery, fallback to LLM guess
    schema_props = {}
    required = []
    if real_params:
        log.info("Schema discovery found real params for %s: %s", name, real_params)
        for pname in real_params:
            llm_info = llm_params.get(pname, {})
            schema_props[pname] = {
                "type": "string",
                "description": llm_info.get("description", pname.replace("_", " ")),
            }
            if llm_info.get("required", False) or pname in [p for p, i in llm_params.items() if i.get("required")]:
                required.append(pname)
        # Also include LLM-suggested params not in real schema (they might be valid)
        for pname, pinfo in llm_params.items():
            if pname not in schema_props:
                schema_props[pname] = {"type": "string", "description": pinfo.get("description", "")}
                if pinfo.get("required", False):
                    required.append(pname)
    else:
        log.info("No real schema found for %s, using LLM-suggested params", name)
        for pname, pinfo in llm_params.items():
            schema_props[pname] = {"type": "string", "description": pinfo.get("description", "")}
            if pinfo.get("required", False):
                required.append(pname)

    _dynamic_tools[name] = {
        "description": api_info.get("description", ""),
        "base_url": base_url,
        "method": api_info.get("method", "GET"),
        "inputSchema": {"type": "object", "properties": schema_props, "required": required},
        "params": llm_params,
        "schema_source": "openapi/docs" if real_params else "llm_guess",
        "registered_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    # Step 3: Register into interceptor's TOOL_REGISTRY for SBSA healing
    try:
        from interceptor import TOOL_REGISTRY
        TOOL_REGISTRY[name] = {
            "description": api_info.get("description", ""),
            "category": "dynamic",
            "required": required,
            "defaults": {},
            "schema": {k: v.get("description", "") for k, v in schema_props.items()},
            "drift_schema": {},
            "drift_aliases": {},
            "expected_result": [],
            "result_ranges": {},
            "cascade": None,
            "docs_url": docs_url,
            "base_url": base_url,
        }
        log.info("Registered %s into interceptor TOOL_REGISTRY (SBSA-enabled)", name)
    except Exception as e:
        log.warning("Could not register %s in TOOL_REGISTRY: %s", name, e)

    log.info("Registered dynamic tool: %s → %s (schema: %s)", name, base_url,
             "real" if real_params else "llm_guess")
    return name


# ═══════════════════════════════════════════════════════════════
# STEP 4: Call a dynamic tool
# ═══════════════════════════════════════════════════════════════

def call_dynamic_tool(name: str, arguments: dict) -> dict:
    """Execute a dynamically discovered API call."""
    tool = _dynamic_tools.get(name)
    if not tool:
        raise ValueError(f"Unknown dynamic tool: {name}")

    url = tool["base_url"]
    params = dict(arguments)

    # Handle URL-templated params (e.g., /api/{id})
    for k, v in list(params.items()):
        if f"{{{k}}}" in url:
            url = url.replace(f"{{{k}}}", str(v))
            del params[k]

    try:
        r = requests.get(url, params=params or None, timeout=8,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}", "url": url}
        data = r.json()
        # Flatten if the response is a list
        if isinstance(data, list):
            data = data[0] if len(data) == 1 else {"results": data[:5]}
        # Truncate large responses
        return _truncate(data)
    except Exception as e:
        return {"error": str(e), "url": url}


def _truncate(data: Any, max_keys: int = 15) -> Any:
    """Keep responses manageable for LLM summarization."""
    if isinstance(data, dict):
        items = list(data.items())[:max_keys]
        return {k: _truncate(v) for k, v in items}
    if isinstance(data, list):
        return [_truncate(x) for x in data[:5]]
    if isinstance(data, str) and len(data) > 300:
        return data[:300] + "..."
    return data


# ═══════════════════════════════════════════════════════════════
# PUBLIC API — called from web_ui.py
# ═══════════════════════════════════════════════════════════════

def discover_and_call(question: str) -> Optional[dict]:
    """
    Full pipeline: discover API → validate → register → call → return result.

    Returns:
        {"tool_name": str, "result": dict, "api_info": dict, "elapsed_ms": float}
        or None if discovery fails.
    """
    t0 = time.monotonic()

    # Check cache first
    cache_key = question.strip().lower()[:100]
    if cache_key in _cache:
        cached = _cache[cache_key]
        name = cached["tool_name"]
        if name not in _dynamic_tools:
            _register_dynamic_tool(cached["api_info"])
        args = cached.get("args", {})
        result = call_dynamic_tool(name, args)
        elapsed = (time.monotonic() - t0) * 1000
        return {"tool_name": name, "result": result, "api_info": cached["api_info"],
                "elapsed_ms": elapsed, "cached": True}

    # Step 1: Ask LLM for an API (up to 2 attempts)
    log.info("Discovering API for: %s", question[:80])
    api_info = None
    failed_urls = []
    for attempt in range(2):
        candidate = _ask_llm_for_api(question, exclude_urls=failed_urls or None)
        if not candidate:
            break
        if _validate_api(candidate):
            api_info = candidate
            break
        failed_urls.append(candidate.get("base_url", ""))
        log.warning("Attempt %d: API %s failed validation, retrying...", attempt + 1, candidate.get("base_url"))

    if not api_info:
        log.warning("No working API found for question: %s", question[:80])
        return None

    # Step 3: Register
    tool_name = _register_dynamic_tool(api_info)

    # Step 4: Call with extracted args
    args = api_info.get("extract_from_question", {})
    result = call_dynamic_tool(tool_name, args)

    elapsed = (time.monotonic() - t0) * 1000
    log.info("Dynamic discovery complete: %s (%.0fms)", tool_name, elapsed)

    # Cache for future use
    _cache[cache_key] = {
        "tool_name": tool_name,
        "api_info": api_info,
        "args": args,
        "discovered_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    _save_cache()

    return {"tool_name": tool_name, "result": result, "api_info": api_info,
            "elapsed_ms": elapsed, "cached": False}


def get_dynamic_tools_for_mcp() -> list:
    """Return dynamic tools in MCP tools/list format."""
    return [{"name": name, "description": info["description"],
             "inputSchema": info["inputSchema"]}
            for name, info in _dynamic_tools.items()]
