"""
Schema Discovery — Runtime V2 Schema Fetcher
=============================================

When the interceptor detects a schema mismatch and V2 is not cached:
  1. Try OpenAPI/Swagger endpoint (instant, free)
  2. Fallback: LLM scrapes the API's docs page and extracts params
  3. Cache the result — never fetch twice for the same API

This is the novel contribution: automatic schema discovery from
documentation when machine-readable specs aren't available.
"""

import json
import logging
import os
import re
import time
from typing import Dict, List, Optional

import requests

log = logging.getLogger("schema_discovery")

# Cache: tool_name → {"params": [...], "fetched_at": timestamp}
_schema_cache: Dict[str, dict] = {}
CACHE_FILE = os.path.join(os.path.dirname(__file__), "schema_cache.json")


def _load_cache():
    global _schema_cache
    if os.path.isfile(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                _schema_cache = json.load(f)
        except:
            pass

def _save_cache():
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(_schema_cache, f, indent=2)
    except:
        pass

_load_cache()


# ═══════════════════════════════════════════════════════════════
# METHOD 1: OpenAPI / Swagger auto-discovery
# ═══════════════════════════════════════════════════════════════

OPENAPI_PATHS = ["/openapi.json", "/swagger.json", "/api-docs", "/.well-known/openapi.yaml"]

def try_openapi(base_url: str) -> Optional[List[dict]]:
    """Try to fetch OpenAPI spec from standard paths."""
    from urllib.parse import urlparse
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    for path in OPENAPI_PATHS:
        try:
            r = requests.get(f"{origin}{path}", timeout=3)
            if r.status_code == 200 and r.text.strip().startswith("{"):
                spec = r.json()
                if "paths" in spec or "openapi" in spec:
                    return _extract_from_openapi(spec)
        except:
            continue
    return None


def _extract_from_openapi(spec: dict) -> List[dict]:
    """Extract parameter info from an OpenAPI spec."""
    results = []
    for path, methods in spec.get("paths", {}).items():
        for method, details in methods.items():
            if method in ("get", "post", "put", "patch", "delete"):
                params = []
                for p in details.get("parameters", []):
                    params.append({
                        "name": p["name"],
                        "required": p.get("required", False),
                        "type": p.get("schema", {}).get("type", "string"),
                    })
                # Also check requestBody
                body = details.get("requestBody", {}).get("content", {})
                for ct, schema_info in body.items():
                    props = schema_info.get("schema", {}).get("properties", {})
                    req = schema_info.get("schema", {}).get("required", [])
                    for pname, pinfo in props.items():
                        params.append({
                            "name": pname,
                            "required": pname in req,
                            "type": pinfo.get("type", "string"),
                        })
                if params:
                    results.append({"endpoint": path, "method": method.upper(), "params": params})
    return results


# ═══════════════════════════════════════════════════════════════
# METHOD 2: LLM-powered doc scraping
# ═══════════════════════════════════════════════════════════════

def _strip_html(html: str) -> str:
    text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.S)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.S)
    text = re.sub(r'<[^>]+>', '\n', text)
    return re.sub(r'\s+', ' ', text).strip()


def try_llm_scrape(docs_url: str, tool_name: str = "") -> Optional[List[dict]]:
    """Fetch API docs page and use LLM to extract parameter schema."""
    try:
        r = requests.get(docs_url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return None
    except:
        return None

    text = _strip_html(r.text)[:5000]
    if len(text) < 50:
        return None

    # Try Groq first (fast, free), fallback to Ollama
    prompt = f"""Extract ALL API endpoints and their query/path parameters from this documentation.
Return ONLY a valid JSON array. No markdown. No explanation.
[{{"endpoint": "...", "params": [{{"name": "...", "required": true, "type": "string"}}]}}]

Documentation:
{text}"""

    try:
        groq_key = os.environ.get("GROQ_API_KEY")
        if groq_key:
            from groq import Groq
            client = Groq(api_key=groq_key)
            resp = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
            )
            result = resp.choices[0].message.content.strip()
        else:
            import ollama
            resp = ollama.chat(
                model="llama3", format="json",
                messages=[{"role": "user", "content": prompt}],
            )
            result = resp["message"]["content"].strip()

        # Clean markdown fences
        result = re.sub(r'^```\w*\n?', '', result)
        result = re.sub(r'\n?```$', '', result)
        return json.loads(result)

    except Exception as e:
        log.warning("LLM schema extraction failed for %s: %s", docs_url, e)
        return None


# ═══════════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════════

def discover_schema(tool_name: str, base_url: str, docs_url: str = None,
                    openapi_url: str = None) -> Optional[List[str]]:
    """
    Discover the current API schema for a tool.

    Returns list of parameter names, or None if discovery fails.
    Results are cached permanently.

    Priority:
      1. Cache hit
      2. OpenAPI/Swagger endpoint
      3. LLM doc scraping
    """
    # Check cache
    if tool_name in _schema_cache:
        log.info("Schema cache hit for %s", tool_name)
        return _schema_cache[tool_name].get("params")

    log.info("Discovering schema for %s ...", tool_name)
    t0 = time.monotonic()

    # Method 1: OpenAPI
    if openapi_url:
        endpoints = try_openapi(openapi_url)
    else:
        endpoints = try_openapi(base_url)

    method_used = "openapi"

    # Method 2: LLM doc scraping
    if not endpoints and docs_url:
        method_used = "llm_scrape"
        endpoints = try_llm_scrape(docs_url, tool_name)

    if not endpoints:
        log.warning("Schema discovery failed for %s", tool_name)
        return None

    # Extract all unique param names across endpoints
    all_params = []
    seen = set()
    for ep in endpoints:
        for p in ep.get("params", []):
            pname = p.get("name", "")
            if pname and pname not in seen:
                all_params.append(pname)
                seen.add(pname)

    elapsed = (time.monotonic() - t0) * 1000
    log.info("Schema discovered for %s via %s: %s (%.0fms)",
             tool_name, method_used, all_params, elapsed)

    # Cache
    _schema_cache[tool_name] = {
        "params": all_params,
        "endpoints": endpoints,
        "method": method_used,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_ms": round(elapsed, 1),
    }
    _save_cache()

    return all_params
