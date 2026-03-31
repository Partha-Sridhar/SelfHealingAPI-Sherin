# Auto-Discovery Files (ReAct + Web Search)

**Files to change:**
1. `mcp_server.py` - add `web_search` tool (Tavily API)
2. `web_ui.py` - adapters ReAct (search → discover → call)
3. `interceptor.py` - "discover_api" handler (URL → schema → TOOL_REGISTRY)
4. `dynamic_discovery.py` - revive (OpenAPI parser)

**Test:** "pets info" → web_search → petstore → works!

**Backup:** git commit now.

**Proceed?** Reply **GO** (backup + 4 edits + test)
