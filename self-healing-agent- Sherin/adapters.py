"""
Agent Adapters — Model-Agnostic Tool-Calling Interface
======================================================

Abstract BaseAgentAdapter with concrete implementations for:
  - Ollama   (local, free)
  - Groq     (cloud, free tier — Llama3-70B, Mixtral)
  - Gemini   (cloud, free tier — Gemini 2.0 Flash)

Each adapter uses the provider's NATIVE tool/function-calling API,
so the LLM decides the tool name and arguments on its own.
The middleware then intercepts whatever the LLM produced.

Normalized output format:
  {"tool": "tool_name", "arguments": {"key": "value"}}
  {"final": "direct text answer"}
"""

import json
import logging
import os
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

log = logging.getLogger("adapters")


# ═══════════════════════════════════════════════════════════════
# TOOL SCHEMA BUILDER — converts MCP tools to each provider's format
# ═══════════════════════════════════════════════════════════════

def _mcp_to_openai_tools(tools: List[dict]) -> List[dict]:
    """Convert MCP tool list to OpenAI/Groq function-calling format."""
    out = []
    for t in tools:
        schema = t.get("inputSchema", {"type": "object", "properties": {}})
        out.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": schema,
            },
        })
    return out


def _mcp_to_gemini_tools(tools: List[dict]) -> List[dict]:
    """Convert MCP tool list to Gemini function-calling format."""
    declarations = []
    for t in tools:
        schema = t.get("inputSchema", {"type": "object", "properties": {}})
        # Gemini doesn't allow empty required arrays or empty properties well
        params = dict(schema)
        if not params.get("properties"):
            params["properties"] = {"_dummy": {"type": "string", "description": "unused"}}
        declarations.append({
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": params,
        })
    return [{"function_declarations": declarations}]


# ═══════════════════════════════════════════════════════════════
# BASE ADAPTER
# ═══════════════════════════════════════════════════════════════

class BaseAgentAdapter(ABC):
    """
    Abstract adapter that normalizes LLM tool-calling into a common format.
    Each provider's native function-calling API is used — no manual JSON
    parsing or prompt-based tool routing.
    """

    name: str = "base"

    @abstractmethod
    def route(self, question: str, tools: List[dict]) -> dict:
        """
        Send question + tool definitions to the LLM using native tool-calling.

        Returns:
            {"tool": "name", "arguments": {...}, "latency_ms": float}
            or {"final": "text answer", "latency_ms": float}
        """
        ...

    @abstractmethod
    def summarize(self, question: str, tool_result: Any) -> str:
        """Generate a human-friendly answer from the tool result."""
        ...

    @abstractmethod
    def list_models(self) -> List[str]:
        """Return available model names for this provider."""
        ...


# ═══════════════════════════════════════════════════════════════
# OLLAMA ADAPTER (local)
# ═══════════════════════════════════════════════════════════════

class OllamaAdapter(BaseAgentAdapter):
    name = "ollama"

    def __init__(self, model: str = "llama3"):
        self.model = model

    def route(self, question: str, tools: List[dict]) -> dict:
        import ollama
        t0 = time.monotonic()

        # Ollama supports native tool calling since 0.4+
        try:
            resp = ollama.chat(
                model=self.model,
                messages=[{"role": "user", "content": question}],
                tools=_mcp_to_openai_tools(tools),
            )
            latency = (time.monotonic() - t0) * 1000

            msg = resp["message"]
            tool_calls = msg.get("tool_calls", [])

            if tool_calls:
                tc = tool_calls[0]
                fn = tc.get("function", tc)
                return {
                    "tool": fn["name"],
                    "arguments": fn.get("arguments", {}),
                    "latency_ms": latency,
                }

            # No tool call — LLM answered directly
            return {"final": msg.get("content", ""), "latency_ms": latency}

        except Exception as e:
            log.warning("Ollama native tool-call failed (%s), falling back to JSON mode", e)
            return self._json_fallback(question, tools, t0)

    def _json_fallback(self, question: str, tools: List[dict], t0: float) -> dict:
        """Fallback for older Ollama versions without native tool support."""
        import ollama
        tool_desc = "\n".join(f"  - {t['name']}: {t.get('description', '')}" for t in tools)
        system = (
            "You are a tool-routing agent. Pick the right tool.\n"
            "Respond ONLY with JSON: {\"tool\": \"name\", \"arguments\": {...}}\n"
            f"Available tools:\n{tool_desc}"
        )
        resp = ollama.chat(
            model=self.model, format="json",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": question},
            ],
        )
        latency = (time.monotonic() - t0) * 1000
        decision = json.loads(resp["message"]["content"])
        decision["latency_ms"] = latency
        return decision

    def summarize(self, question: str, tool_result: Any) -> str:
        import ollama
        resp = ollama.chat(
            model=self.model,
            messages=[
                {"role": "system", "content": "Answer using ONLY the tool result. Be concise."},
                {"role": "user", "content": f"Question: {question}\nResult: {json.dumps(tool_result)}"},
            ],
        )
        return resp["message"]["content"]

    def list_models(self) -> List[str]:
        import ollama
        try:
            return [m.model for m in ollama.list().models]
        except Exception:
            return ["llama3"]


# ═══════════════════════════════════════════════════════════════
# GROQ ADAPTER (cloud, free tier)
# ═══════════════════════════════════════════════════════════════

class GroqAdapter(BaseAgentAdapter):
    name = "groq"

    MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "gemma2-9b-it"]

    def __init__(self, model: str = "llama-3.3-70b-versatile"):
        self.model = model
        self.api_key = os.environ.get("GROQ_API_KEY", "")
        if not self.api_key:
            log.warning("GROQ_API_KEY not set — Groq adapter will fail")

    def _client(self):
        from groq import Groq
        return Groq(api_key=self.api_key)

    def route(self, question: str, tools: List[dict]) -> dict:
        import re
        t0 = time.monotonic()
        client = self._client()

        try:
            resp = client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": question}],
                tools=_mcp_to_openai_tools(tools),
                tool_choice="auto",
            )
            latency = (time.monotonic() - t0) * 1000

            msg = resp.choices[0].message
            if msg.tool_calls:
                tc = msg.tool_calls[0]
                return {
                    "tool": tc.function.name,
                    "arguments": json.loads(tc.function.arguments),
                    "latency_ms": latency,
                }
            return {"final": msg.content or "", "latency_ms": latency}

        except Exception as e:
            latency = (time.monotonic() - t0) * 1000
            err_str = str(e)

            # Groq returns tool_use_failed when the LLM generates field names
            # that don't match the declared schema — this is EXACTLY the kind
            # of natural schema mismatch SBSA is designed to heal.
            # Extract the tool call from failed_generation and pass it through.
            m = re.search(r'<function=(\w+)\s*(\{.*?\})\s*</function>', err_str)
            if m:
                tool_name = m.group(1)
                try:
                    args = json.loads(m.group(2))
                except json.JSONDecodeError:
                    args = {}
                log.info("Groq tool_use_failed — extracted: %s(%s) — SBSA will heal", tool_name, args)
                return {
                    "tool": tool_name,
                    "arguments": args,
                    "latency_ms": latency,
                    "_groq_native_mismatch": True,
                }
            raise

    def summarize(self, question: str, tool_result: Any) -> str:
        client = self._client()
        resp = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "Answer using ONLY the tool result. Be concise."},
                {"role": "user", "content": f"Question: {question}\nResult: {json.dumps(tool_result)}"},
            ],
        )
        return resp.choices[0].message.content

    def list_models(self) -> List[str]:
        return self.MODELS


# ═══════════════════════════════════════════════════════════════
# GEMINI ADAPTER (cloud, free tier)
# ═══════════════════════════════════════════════════════════════

class GeminiAdapter(BaseAgentAdapter):
    name = "gemini"

    MODELS = ["gemini-2.0-flash-lite", "gemini-2.0-flash", "gemini-2.5-flash-preview-05-20"]

    def __init__(self, model: str = "gemini-2.0-flash-lite"):
        self.model = model
        self.api_key = os.environ.get("GEMINI_API_KEY", "")
        if not self.api_key:
            log.warning("GEMINI_API_KEY not set — Gemini adapter will fail")

    def route(self, question: str, tools: List[dict]) -> dict:
        from google import genai
        from google.genai import types

        t0 = time.monotonic()
        client = genai.Client(api_key=self.api_key)

        # Build tool declarations
        tool_declarations = []
        for t in tools:
            schema = t.get("inputSchema", {"type": "object", "properties": {}})
            props = schema.get("properties", {})
            # Convert to Gemini Schema format
            gem_props = {}
            for k, v in props.items():
                gem_props[k] = types.Schema(
                    type=types.Type.STRING,
                    description=v.get("description", ""),
                )
            tool_declarations.append(types.FunctionDeclaration(
                name=t["name"],
                description=t.get("description", ""),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties=gem_props,
                    required=schema.get("required", []),
                ) if gem_props else None,
            ))

        gem_tools = types.Tool(function_declarations=tool_declarations)

        resp = client.models.generate_content(
            model=self.model,
            contents=question,
            config=types.GenerateContentConfig(
                tools=[gem_tools],
            ),
        )
        latency = (time.monotonic() - t0) * 1000

        # Check for function calls in response
        for part in resp.candidates[0].content.parts:
            if part.function_call:
                fc = part.function_call
                return {
                    "tool": fc.name,
                    "arguments": dict(fc.args) if fc.args else {},
                    "latency_ms": latency,
                }

        # No function call — direct answer
        text = resp.text if resp.text else ""
        return {"final": text, "latency_ms": latency}

    def summarize(self, question: str, tool_result: Any) -> str:
        from google import genai

        client = genai.Client(api_key=self.api_key)
        resp = client.models.generate_content(
            model=self.model,
            contents=f"Answer using ONLY this tool result. Be concise.\n\n"
                     f"Question: {question}\nResult: {json.dumps(tool_result)}",
        )
        return resp.text

    def list_models(self) -> List[str]:
        return self.MODELS


# ═══════════════════════════════════════════════════════════════
# MISTRAL ADAPTER (cloud, free tier)
# ═══════════════════════════════════════════════════════════════

class MistralAdapter(BaseAgentAdapter):
    name = "mistral"
    MODELS = ["mistral-small-latest", "mistral-medium-latest", "mistral-large-latest"]

    def __init__(self, model: str = "mistral-small-latest"):
        self.model = model
        self.api_key = os.environ.get("MISTRAL_API_KEY", "")

    def route(self, question: str, tools: List[dict]) -> dict:
        import requests as req
        t0 = time.monotonic()
        r = req.post("https://api.mistral.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={"model": self.model,
                  "messages": [{"role": "user", "content": question}],
                  "tools": _mcp_to_openai_tools(tools), "tool_choice": "auto"}, timeout=30)
        latency = (time.monotonic() - t0) * 1000
        data = r.json()
        if r.status_code != 200:
            raise RuntimeError(f"Mistral {r.status_code}: {data}")
        msg = data["choices"][0]["message"]
        if msg.get("tool_calls"):
            tc = msg["tool_calls"][0]["function"]
            return {"tool": tc["name"], "arguments": json.loads(tc["arguments"]) if isinstance(tc["arguments"], str) else tc["arguments"], "latency_ms": latency}
        return {"final": msg.get("content", ""), "latency_ms": latency}

    def summarize(self, question: str, tool_result: Any) -> str:
        import requests as req
        r = req.post("https://api.mistral.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={"model": self.model,
                  "messages": [{"role": "system", "content": "Answer using ONLY the tool result. Be concise."},
                               {"role": "user", "content": f"Question: {question}\nResult: {json.dumps(tool_result)}"}]}, timeout=30)
        return r.json()["choices"][0]["message"]["content"]

    def list_models(self) -> List[str]:
        return self.MODELS


# ═══════════════════════════════════════════════════════════════
# ANTHROPIC ADAPTER (cloud, $5 free credit)
# ═══════════════════════════════════════════════════════════════

class AnthropicAdapter(BaseAgentAdapter):
    name = "anthropic"
    MODELS = ["claude-sonnet-4-20250514", "claude-haiku-4-20250514"]

    def __init__(self, model: str = "claude-sonnet-4-20250514"):
        self.model = model
        self.api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    def _client(self):
        import anthropic
        return anthropic.Anthropic(api_key=self.api_key)

    def route(self, question: str, tools: List[dict]) -> dict:
        t0 = time.monotonic()
        client = self._client()
        # Convert to Anthropic tool format
        anth_tools = []
        for t in tools:
            schema = t.get("inputSchema", {"type": "object", "properties": {}})
            anth_tools.append({"name": t["name"], "description": t.get("description", ""),
                               "input_schema": schema})
        resp = client.messages.create(model=self.model, max_tokens=300,
            messages=[{"role": "user", "content": question}], tools=anth_tools)
        latency = (time.monotonic() - t0) * 1000
        for block in resp.content:
            if block.type == "tool_use":
                return {"tool": block.name, "arguments": block.input, "latency_ms": latency}
        text = next((b.text for b in resp.content if hasattr(b, "text")), "")
        return {"final": text, "latency_ms": latency}

    def summarize(self, question: str, tool_result: Any) -> str:
        client = self._client()
        resp = client.messages.create(model=self.model, max_tokens=300,
            messages=[{"role": "user", "content": f"Answer using ONLY this tool result. Be concise.\n\nQuestion: {question}\nResult: {json.dumps(tool_result)}"}])
        return resp.content[0].text

    def list_models(self) -> List[str]:
        return self.MODELS


# ═══════════════════════════════════════════════════════════════
# ADAPTER REGISTRY
# ═══════════════════════════════════════════════════════════════

_ADAPTERS: Dict[str, type] = {
    "ollama": OllamaAdapter,
    "groq": GroqAdapter,
    "gemini": GeminiAdapter,
    "mistral": MistralAdapter,
    "anthropic": AnthropicAdapter,
}


def get_adapter(provider: str, model: str = None) -> BaseAgentAdapter:
    """Factory: get an adapter instance by provider name."""
    cls = _ADAPTERS.get(provider)
    if not cls:
        raise ValueError(f"Unknown provider: {provider}. Available: {list(_ADAPTERS.keys())}")
    if model:
        return cls(model=model)
    return cls()


def available_providers() -> Dict[str, Dict]:
    """Return all providers with their available models and status."""
    result = {}
    for name, cls in _ADAPTERS.items():
        adapter = cls()
        ready = True
        if name == "groq":
            ready = bool(os.environ.get("GROQ_API_KEY"))
        elif name == "gemini":
            ready = bool(os.environ.get("GEMINI_API_KEY"))
        elif name == "mistral":
            ready = bool(os.environ.get("MISTRAL_API_KEY"))
        elif name == "anthropic":
            ready = bool(os.environ.get("ANTHROPIC_API_KEY"))

        result[name] = {
            "models": adapter.list_models(),
            "ready": ready,
            "default_model": adapter.model if hasattr(adapter, "model") else None,
        }
    return result
