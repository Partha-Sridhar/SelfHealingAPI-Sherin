# SBSA — Semantic Bipartite Schema Alignment Middleware

Deterministic self-healing middleware for LLM agents. Resolves schema drift and environmental failures without re-triggering LLM inference.

## Architecture

```
User Question
    ↓
LLM Agent (Ollama / Groq / Gemini)    ← native tool-calling
    ↓
adapters.py (BaseAgentAdapter)         ← normalizes tool call format
    ↓
interceptor.py (TCP :6010)             ← 11-stage healing pipeline
    ↓  ┌─ sbsa_engine.py              ← sentence-transformers + Hungarian Algorithm
    ↓  ├─ type coercion, field strip
    ↓  ├─ drift recovery (V1→V2)
    ↓  ├─ retry with backoff (429/5xx)
    ↓  └─ result validation
    ↓
mcp_server.py                          ← MCP tool server (JSON-RPC 2.0)
    ↓
Real APIs / Mock responses
```

## Project Structure

```
├── sbsa_engine.py          # Core: embeddings + Hungarian Algorithm
├── interceptor.py          # Core: 11-stage healing proxy
├── mcp_server.py           # MCP tool server with drift simulation
├── mcp_client.py           # CLI client (auto-detects interceptor)
├── adapters.py             # LLM adapters: Ollama, Groq, Gemini
├── toolbench_loader.py     # Auto-parses ToolBench schemas (54 tools, 5 domains)
├── analytics_logger.py     # JSONL benchmark recorder
├── benchmark.py            # 3-way comparison: Baseline vs ReAct vs SBSA
├── web_ui.py               # Flask web interface
├── start.sh                # One-command startup
├── .env                    # API keys (GROQ_API_KEY, GEMINI_API_KEY)
├── templates/
│   └── index.html          # Web UI frontend
├── benchmark_logs/         # Raw benchmark data (JSON/JSONL)
└── benchmark_reports/      # Excel + CSV reports
```

## Quick Start

```bash
# 1. Set API keys
echo "GROQ_API_KEY=gsk_..." >> .env
echo "GEMINI_API_KEY=..." >> .env

# 2. Start (interceptor + web UI)
bash start.sh

# 3. Open http://localhost:5002

# 4. Run benchmark
export $(grep -v '^#' .env | xargs)
python3 benchmark.py
```

## Key Components

### SBSA Engine (`sbsa_engine.py`)
- `all-MiniLM-L6-v2` sentence-transformer (384-dim embeddings)
- Cosine similarity matrix → cost matrix
- Hungarian Algorithm (`scipy.optimize.linear_sum_assignment`) for optimal 1-to-1 mapping
- Threshold filtering (sim < 0.30 → reject)
- Healing in <1ms (after model warm-up)

### ToolBench Integration (`toolbench_loader.py`)
- Auto-parses 54 tools across 5 domains from ToolBench corpus
- Generates V1→V2 drift variants via synonym transformation
- 168 total parameters for cross-domain evaluation

### Benchmark (`benchmark.py`)
3-way comparison under schema drift:
- **Baseline**: no recovery → raw failure
- **ReAct**: LLM reflection retry (3 attempts) → expensive, unreliable
- **SBSA**: deterministic healing → fast, reliable, zero extra tokens

## Research Metrics

| Metric | Source | What it proves |
|--------|--------|---------------|
| Recovery Rate | ToolBench/StableToolBench | SBSA > ReAct under drift |
| Healing Latency | DeepEval | <1ms (SBSA) vs 2-5s (ReAct) |
| Token Savings | ToolEval | 80-85% fewer tokens than ReAct |
| Step Efficiency | DeepEval Agentic | 1 LLM call (SBSA) vs 3-4 (ReAct) |

## Models Tested

| Provider | Model | Type |
|----------|-------|------|
| Ollama | llama3 (8B) | Local, free |
| Groq | llama-3.3-70b-versatile | Cloud, free tier |
| Groq | llama-3.1-8b-instant | Cloud, free tier |
| Groq | gemma2-9b-it | Cloud, free tier |
