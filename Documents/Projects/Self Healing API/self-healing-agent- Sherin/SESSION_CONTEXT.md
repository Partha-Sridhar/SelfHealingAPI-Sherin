# SBSA Project — Full Context & Session History

## Project Overview

**Name:** SBSA (Semantic Bipartite Schema Alignment) Middleware
**Author:** Sherin (bshivn)
**Type:** Final Year Project — Research
**Location:** `/Users/bshivn/Final Year Project/SelfHealingAPI-Sherin/Documents/Projects/Self Healing API/self-healing-agent- Sherin/`

### Core Thesis

LLMs produce wrong parameter names when making tool/API calls — even when given the correct schema. OpenAI's own data shows GPT-4 follows schemas correctly less than 40% of the time. SBSA fixes this deterministically using sentence-transformers + Hungarian Algorithm in <1ms, without any additional LLM inference.

### Research Contribution

SBSA is NOT competing with Structured Outputs (OpenAI/Anthropic). Those fix "LLM doesn't follow the schema it was given." SBSA fixes two different problems:
1. **Natural LLM mismatch** — LLM has correct schema but produces wrong field names (probabilistic behavior)
2. **Schema drift** — API upgrades field names after the LLM's tool definitions were configured

Key differentiator: SBSA operates at the **middleware layer**, is **model-agnostic**, costs **zero tokens**, and runs in **<1ms**.

---

## Architecture

```
User Question
    ↓
LLM Agent (Ollama / Groq / Mistral / Anthropic)  ← native tool-calling
    ↓
adapters.py (BaseAgentAdapter)                     ← normalizes tool call format
    ↓
interceptor.py (TCP :6010)                         ← 11-stage healing pipeline
    ↓  ┌─ sbsa_engine.py                           ← sentence-transformers + Hungarian Algorithm
    ↓  ├─ type coercion, field strip
    ↓  ├─ drift recovery (V1→V2)
    ↓  ├─ retry with backoff (429/5xx)
    ↓  └─ result validation
    ↓
mcp_server.py                                      ← MCP tool server (JSON-RPC 2.0)
    ↓
18 Real APIs across 17 domains
```

---

## File Structure

```
├── sbsa_engine.py          # Core: all-MiniLM-L6-v2 embeddings + Hungarian Algorithm
├── interceptor.py          # 11-stage healing proxy (TCP :6010)
├── mcp_server.py           # MCP tool server — 18 real APIs + drift simulation
├── mcp_client.py           # CLI agent client (auto-detects interceptor)
├── adapters.py             # LLM adapters: Ollama, Groq, Gemini, Mistral, Anthropic
├── api_registry.py         # 18 APIs across 17 domains with V1/V2 schemas
├── schema_discovery.py     # Runtime schema fetching: OpenAPI → LLM doc scraping → cache
├── toolbench_loader.py     # Parses ToolBench api.py files (50 domains, 10K+ APIs)
├── analytics_logger.py     # JSONL benchmark recorder
├── benchmark.py            # 3-way comparison: Baseline vs ReAct vs SBSA
├── web_ui.py               # Flask web UI with SSE streaming pipeline status
├── start.sh                # One-command startup (loads .env, starts interceptor + web UI)
├── .env                    # API keys (Groq, Gemini, Mistral, Anthropic, RapidAPI)
├── templates/index.html    # Web frontend with real-time pipeline steps + analytics panel
├── benchmark_logs/         # Raw benchmark data (JSON/JSONL)
├── benchmark_reports/      # Excel + CSV reports
├── README.md               # Project documentation
├── schema_cache.json       # Cached schema discovery results
├── working_toolbench_apis.json  # ToolBench API accessibility scan results
└── data/                   # Full ToolBench dataset (50 domains, 10,094 APIs)
```

---

## LLM Providers Configured

| Provider | Model | Status | Key Location |
|----------|-------|--------|-------------|
| Ollama | llama3 (8B) | ✅ Working (local) | No key needed |
| Groq | llama-3.3-70b-versatile | ✅ Working | .env GROQ_API_KEY |
| Mistral | mistral-small-latest | ✅ Working | .env MISTRAL_API_KEY |
| Anthropic | claude-sonnet-4-20250514 | ✅ Working | .env ANTHROPIC_API_KEY |
| Gemini | gemini-2.0-flash-lite | ❌ Quota=0, never activated | .env GEMINI_API_KEY |

---

## 18 Real APIs (api_registry.py)

| Domain | Tool Name | Provider | V1→V2 Drift |
|--------|-----------|----------|-------------|
| Weather | get_weather | Open-Meteo | city → location_name |
| Geography | get_country_info | REST Countries | country → country_name |
| Crypto | get_crypto_price | CoinGecko | coin/currency → crypto_id/vs_currency |
| Finance | get_exchange_rate | fawazahmed0 | base/target → from_currency/to_currency |
| Books | search_books | Open Library | query/limit → search_term/max_results |
| Entertainment | get_joke | JokeAPI | category → joke_type |
| Education | define_word | Free Dictionary | word → term |
| Education | search_universities | Hipolabs | name/country → university_name/country_name |
| Food & Drink | search_cocktail | TheCocktailDB | name → drink_name |
| Trivia | get_trivia | Open Trivia DB | category/difficulty → topic_id/level |
| Gaming | get_pokemon | PokeAPI | name → pokemon_name |
| Space | get_space_photo | NASA APOD | date → photo_date |
| Geolocation | geolocate_ip | ip-api | ip → ip_address |
| Sports | search_team | TheSportsDB | team → team_name |
| Music | search_song | Genius (ToolBench/RapidAPI) | q → search_query |
| Animals | get_dog_image | Dog CEO | breed → dog_breed |
| Lifestyle | get_activity | Bored API | type → activity_type |
| Data | predict_age | Agify | name → first_name |

---

## ToolBench Integration

- Full dataset downloaded to `data/toolenv/tools/` — 50 domains, 10,094 APIs, 39,087 functions
- `toolbench_loader.py` parses api.py files via AST, extracts schemas, generates V1/V2 drift variants
- Most ToolBench APIs are dead (RapidAPI subscriptions expired) — only Genius Song Lyrics works with our key
- RapidAPI key: `97d16b7b82msh11e4e27d5d98ac8p14faa6jsn99dabf4a5bec` (in .env as RAPIDAPI_KEY)
- Scan results saved in `all_working_toolbench_apis.json` — 29/50 domains respond, but most return HTML not JSON

---

## Schema Discovery (schema_discovery.py)

Novel feature: when V2 schema is not available via OpenAPI/MCP, the middleware scrapes the API's documentation page and uses an LLM to extract parameter schemas.

**Tested and working on:**
- NewsAPI (12/12 params extracted perfectly)
- CoinGecko, Stripe, Twilio, GitHub, Spotify, OpenWeatherMap, PokeAPI, CocktailDB, JokeAPI, Open Library, ip-api, OMDb
- 11/12 APIs successfully extracted — only NASA failed (JS-rendered page)

**Priority order:**
1. OpenAPI/Swagger endpoint (instant, free)
2. LLM doc scraping via Groq (1-2s, cached after first call)
3. Fallback to Ollama if no Groq key

---

## Benchmark System (benchmark.py)

3-way comparison under schema drift:
- **Baseline** — no recovery, raw failure
- **ReAct** — LLM reflection retry (up to 3 attempts, standard agent pattern)
- **SBSA** — deterministic Hungarian Algorithm healing

### Key Results from Last Benchmark Run

**Groq/Llama-3.3-70b:**
- Baseline (drift): 8.3% success
- ReAct (drift): 8.3% success (reflection didn't help!)
- SBSA (drift): 100% success
- Speedup: 5.6x faster than ReAct
- Token savings: 84.9% vs ReAct

**Ollama/Llama3-8B:**
- Baseline (drift): 33.3% success
- ReAct (drift): 25.0% success (worse than baseline!)
- SBSA (drift): 100% success
- Speedup: 2.8x faster than ReAct
- Token savings: 82.2% vs ReAct

Results saved in:
- `benchmark_logs/bench_20260329_123143.json` (raw)
- `benchmark_logs/bench_20260329_123143_metrics.json` (metrics)
- `benchmark_reports/SBSA_vs_ReAct_Benchmark.xlsx` (Excel with 6 sheets)

---

## Web UI (web_ui.py)

- Flask app on port 5002
- SSE streaming for real-time pipeline status (connect → LLM routing → tool call → summarize)
- Provider/Model dropdowns (Ollama, Groq, Mistral, Anthropic)
- Two toggles:
  - **SBSA Middleware** (ON/OFF) — enables/disables the interceptor
  - **Simulate API Upgrade** (ON/OFF) — switches server between V1 and V2 schemas
- 17 domain tabs with clickable suggestion queries
- Right panel: per-model analytics (success rate, latency, token economics vs ReAct)

---

## Key Research Citations

1. **OpenAI (Aug 2024)** — "Introducing Structured Outputs": GPT-4 scores <40% on schema following without enforcement. https://openai.com/index/introducing-structured-outputs-in-the-api/
2. **Anthropic (2025)** — Structured Outputs blog: built to "eliminate schema-related parsing errors and failed tool calls." https://claude.com/blog/structured-outputs-on-the-claude-developer-platform
3. **ToolBench/ToolLLM — ICLR 2024 Spotlight** (Qin et al.): 16,464 APIs, tool-use evaluation framework. https://proceedings.iclr.cc/paper_files/paper/2024/hash/28e50ee5b72e90b50e7196fde8ea260e-Abstract-Conference.html
4. **Berkeley BFCL — ICML 2025** (Patil et al.): Function-calling leaderboard with AST evaluation. https://proceedings.mlr.press/v267/patil25a.html
5. **HammerBench (Dec 2024)**: "Errors in parameter naming constitute the primary factor behind conversation failures." https://arxiv.org/abs/2412.16516
6. **StableToolBench (Mar 2024)**: Documents ToolBench API instability. https://arxiv.org/abs/2403.07714

---

## Novelty Argument (for reviewers)

**"Doesn't Structured Outputs already solve this?"**

No. Structured Outputs (OpenAI/Anthropic) constrains token generation to follow a given schema. SBSA solves what happens when:
1. The schema the LLM was given is outdated (API upgraded)
2. The LLM is open-source and doesn't have Structured Outputs (Llama, Mistral)
3. You need model-agnostic healing across different providers

SBSA is complementary to Structured Outputs, not competing.

**"How does SBSA get the V2 schema if the API doesn't expose it?"**

Priority: MCP tools/list → OpenAPI/Swagger → LLM doc scraping (novel contribution). The doc scraping was tested on 12 major APIs (Stripe, Spotify, GitHub, etc.) with 92% success rate.

**"SBSA can't fix missing parameters"**

Correct. SBSA heals wrong names, not missing values. The UI now clearly distinguishes: "LLM forgot parameter X — this is an LLM limitation, not a middleware failure."

---

## Known Issues / TODO

1. **Gemini adapter** — API key quota stuck at 0. Need new key from different Google project.
2. **Ollama/llama3** — doesn't support native tool-calling, falls back to JSON mode (less reliable).
3. **First SBSA call** — takes ~6s (model loading). Subsequent calls <1ms. Could pre-warm on startup.
4. **Cascade tool calls** — when drift is on, cascade calls (e.g., country→weather) need healing too. Partially fixed but not fully tested.
5. **Benchmark needs re-run** — with all 4 providers (Ollama, Groq, Mistral, Anthropic) and all 18 APIs.
6. **Schema discovery** — built but not yet integrated into the interceptor's drift recovery flow. Currently the interceptor uses the pre-defined V2 schemas from api_registry.py.

---

## How to Run

```bash
cd "/Users/bshivn/Final Year Project/SelfHealingAPI-Sherin/Documents/Projects/Self Healing API/self-healing-agent- Sherin"
bash start.sh
# Open http://localhost:5002

# Run benchmark:
export $(grep -v '^#' .env | xargs)
python3 benchmark.py
```

---

## Session Timeline (2026-03-29)

1. Started with existing 5 hardcoded APIs (weather, country, exchange, stock, bitcoin)
2. Fixed critical bugs: sleep(20) in weather, broken exchange rate API, Yahoo Finance 429, SBSA threshold too high
3. Added adapter system (Ollama, Groq, Gemini) with native tool-calling
4. Built benchmark.py with 3-way comparison (Baseline vs ReAct vs SBSA)
5. Generated Excel reports with 6 sheets
6. Attempted ToolBench integration — discovered most APIs are dead (RapidAPI subscriptions expired)
7. Downloaded full ToolBench dataset (50 domains, 10K APIs) — scanned all, only ~2 return real JSON
8. Pivoted to 18 free public APIs across 17 domains
9. Built schema discovery module (OpenAPI → LLM doc scraping) — tested on 12 major APIs
10. Rewrote mcp_server.py to use api_registry.py as single source of truth
11. Added Mistral and Anthropic adapters (both working)
12. Updated UI: renamed toggles, added SSE streaming pipeline status, fixed analytics panel
13. Discussed research framing, novelty argument, and key citations for the paper
