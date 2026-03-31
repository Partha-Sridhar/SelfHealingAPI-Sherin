"""
REMOVED - Dynamic discovery reverted per user request
"""

import json
import logging
import requests
from typing import Dict, Any, Optional
from urllib.parse import urljoin, urlparse

import ollama

log = logging.getLogger("dynamic_discovery")

# Global cache - shared with TOOL_REGISTRY
DISCOVERED_APIS: Dict[str, Dict[str, Any]] = {}

def discover_api(source: str) -> Optional[Dict[str, Any]]:
    """
    Input: OpenAPI URL OR API docs URL OR plain docs text
    
    Output: TOOL_REGISTRY format (v1_schema, v2_schema, description...)
    """
    if source in DISCOVERED_APIS:
        log.info("  DynamicDiscovery | cache HIT %s", source)
        return DISCOVERED_APIS[source]
    
    log.info("  DynamicDiscovery | discovering %s...", source)
    
    # Timeout - never block main path
    try:
        if source.startswith('http'):
            return _discover_openapi(source) or _discover_docs(source)
        else:
            return _llm_discover(source)
    except Exception as e:
        log.warning("  DynamicDiscovery | failed %s: %s", source, e)
        return None

def _discover_openapi(url: str) -> Optional[Dict[str, Any]]:
    """Parse OpenAPI v2/v3 → TOOL_REGISTRY format"""
    resp = requests.get(url, timeout=5)
    spec = resp.json()
    
    # Extract first operation as example tool
    paths = spec.get('paths', {})
    if not paths:
        return None
        
    path, operations = next(iter(paths.items()))
    op = next(iter(operations.items()))
    
    tool_name = urlparse(url).netloc.replace('.', '_') + '_api'
    schema = {
        "description": spec.get("info", {}).get("description", "Dynamic API"),
        "domain": "Dynamic",
        "v1_schema": _extract_params(op),
        "v2_schema": _extract_params(op),  # Same for now
        "docs_url": url,
        "base_url": url,
        "cascade": None,  # Manual for now
    }
    
    DISCOVERED_APIS[url] = schema
    log.info("  DynamicDiscovery | OpenAPI OK: %s → %d params", url, len(schema["v1_schema"]))
    return schema

def _extract_params(op: Dict) -> Dict[str, Dict]:
    """Extract parameters → schema dict"""
    params = {}
    for param in op.get('parameters', []):
        if param.get('in') == 'query':
            name = param['name']
            params[name] = {
                "type": param.get('type', 'string'),
                "description": param.get('description', ''),
                "required": param.get('required', False)
            }
    return params

def _discover_docs(url: str) -> Optional[Dict[str, Any]]:
    """LLM extracts schema from docs page"""
    resp = requests.get(url, timeout=5)
    text = resp.text[:4000]  # Truncate
    
    prompt = f"""API docs: {{text[:2000]}}...

Extract 1 example endpoint. Return JSON:
{{
  "name": "api_tool_name",
  "description": "...",
  "v1_schema": {{"param1": {{"type": "string", "description": "..."}}}} 
}}
"""

    
    resp = ollama.chat(model="llama3", messages=[{"role": "user", "content": prompt}])
    try:
        schema = json.loads(resp['message']['content'])
        DISCOVERED_APIS[url] = schema
        return schema
    except:
        return None

def _llm_discover(docs_text: str) -> Optional[Dict[str, Any]]:
    """Pure LLM inference from docs text"""
    prompt = f"""From docs: {docs_text[:1000]}

Extract example tool schema JSON:
{"name": "tool_name", "description": "...", "v1_schema": {{"param": {{"type": "string"}}}}}"""
    
    resp = ollama.chat(model="llama3", messages=[{"role": "user", "content": prompt}])
    try:
        schema = json.loads(resp['message']['content'])
        DISCOVERED_APIS["llm:" + docs_text[:50]] = schema
        return schema
    except:
        return None

if __name__ == "__main__":
    # Test
    api = discover_api("https://petstore.swagger.io/v2/swagger.json")
    print(json.dumps(api, indent=2))

