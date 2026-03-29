"""
SBSA Engine — Semantic Bipartite Schema Alignment
==================================================

Deterministic field-name repair using Vector Math + Combinatorial Optimization.
No LLM calls. Runs in <100ms.

Pipeline:
  1. Encode stale (agent) keys and current (API) keys into 384-dim embeddings
     using sentence-transformers (all-MiniLM-L6-v2).
  2. Build a cost matrix: C[i][j] = 1 - cosine_similarity(agent_key_i, api_key_j).
  3. Solve the assignment problem with the Hungarian Algorithm
     (scipy.optimize.linear_sum_assignment) for global optimal 1-to-1 mapping.
  4. Threshold check: reject any match with similarity < SIMILARITY_THRESHOLD
     to avoid hallucinated mappings.

Why Hungarian over Top-K:
  Top-K can cause collisions — two agent keys mapping to the same API key.
  Hungarian guarantees a globally optimal bijection in O(n^3).
"""

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from scipy.optimize import linear_sum_assignment
from sentence_transformers import SentenceTransformer

log = logging.getLogger("sbsa")

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════

MODEL_NAME = "all-MiniLM-L6-v2"
SIMILARITY_THRESHOLD = 0.30   # below this → hard failure, no mapping

# ═══════════════════════════════════════════════════════════════
# SINGLETON MODEL LOADER
# ═══════════════════════════════════════════════════════════════

_model: Optional[SentenceTransformer] = None


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        log.info("Loading sentence-transformer model: %s", MODEL_NAME)
        _model = SentenceTransformer(MODEL_NAME)
        log.info("Model loaded (dim=%d)", _model.get_sentence_embedding_dimension())
    return _model


# ═══════════════════════════════════════════════════════════════
# EMBEDDING CACHE — avoid re-encoding known keys
# ═══════════════════════════════════════════════════════════════

_embedding_cache: Dict[str, np.ndarray] = {}


def _encode_keys(keys: List[str], descriptions: Dict[str, str] = None) -> np.ndarray:
    """
    Encode keys using cache. When descriptions are available, encode
    'key_name: description' for richer semantic signal.
    E.g. 'base' alone is ambiguous, but 'base: ISO 4217 source currency code'
    gives the transformer enough context.
    """
    model = _get_model()
    descriptions = descriptions or {}
    cache_keys = []
    for k in keys:
        # Cache key includes description so same field name with different
        # descriptions gets a distinct embedding
        desc = descriptions.get(k, "")
        cache_keys.append(f"{k}||{desc}" if desc else k)

    uncached = [(k, ck) for k, ck in zip(keys, cache_keys) if ck not in _embedding_cache]
    if uncached:
        texts = []
        for k, ck in uncached:
            desc = descriptions.get(k, "")
            if desc:
                # Strip type prefix like "string — " for cleaner semantics
                clean_desc = desc.split("—")[-1].strip() if "—" in desc else desc
                texts.append(f"{k.replace('_', ' ')}: {clean_desc}")
            else:
                texts.append(k.replace("_", " ").replace("-", " "))
        embeddings = model.encode(texts, normalize_embeddings=True)
        for (_, ck), emb in zip(uncached, embeddings):
            _embedding_cache[ck] = emb

    return np.array([_embedding_cache[ck] for ck in cache_keys])


# ═══════════════════════════════════════════════════════════════
# CORE: SBSA ALIGNMENT
# ═══════════════════════════════════════════════════════════════

def align_keys(
    agent_keys: List[str],
    api_keys: List[str],
    agent_descriptions: Dict[str, str] = None,
    api_descriptions: Dict[str, str] = None,
) -> Tuple[Dict[str, str], np.ndarray, float]:
    """
    Find the optimal 1-to-1 mapping from agent_keys → api_keys.

    Returns:
        mapping:    dict {agent_key: api_key} for accepted matches
        sim_matrix: full cosine similarity matrix (for logging/analytics)
        elapsed_ms: time taken in milliseconds
    """
    t0 = time.monotonic()

    if not agent_keys or not api_keys:
        return {}, np.array([]), (time.monotonic() - t0) * 1000

    # Step 1: Encode (with descriptions for semantic enrichment)
    agent_emb = _encode_keys(agent_keys, agent_descriptions)  # shape (m, 384)
    api_emb = _encode_keys(api_keys, api_descriptions)        # shape (n, 384)

    # Step 2: Cosine similarity matrix (embeddings are already L2-normalized)
    sim_matrix = agent_emb @ api_emb.T     # shape (m, n)

    # Step 3: Cost matrix = 1 - similarity
    cost_matrix = 1.0 - sim_matrix

    # Step 4: Hungarian algorithm — global minimum cost assignment
    row_idx, col_idx = linear_sum_assignment(cost_matrix)

    # Step 5: Build mapping with threshold check
    mapping = {}
    for r, c in zip(row_idx, col_idx):
        sim = sim_matrix[r, c]
        if sim >= SIMILARITY_THRESHOLD:
            mapping[agent_keys[r]] = api_keys[c]
            log.info(
                "  SBSA | %s → %s  (sim=%.3f ✓)",
                agent_keys[r], api_keys[c], sim,
            )
        else:
            log.warning(
                "  SBSA | %s → %s  (sim=%.3f ✗ below threshold %.2f)",
                agent_keys[r], api_keys[c], sim, SIMILARITY_THRESHOLD,
            )

    elapsed_ms = (time.monotonic() - t0) * 1000
    log.info("  SBSA | alignment done in %.1fms", elapsed_ms)
    return mapping, sim_matrix, elapsed_ms


# ═══════════════════════════════════════════════════════════════
# HIGH-LEVEL HEAL FUNCTION
# ═══════════════════════════════════════════════════════════════

def heal(
    agent_args: Dict[str, Any],
    target_fields: List[str],
    defaults: Dict[str, Any] = None,
    agent_descriptions: Dict[str, str] = None,
    api_descriptions: Dict[str, str] = None,
) -> Dict[str, Any]:
    """
    Remap agent_args keys to match target_fields using SBSA alignment.

    Args:
        agent_args:          the args the agent sent (possibly stale key names)
        target_fields:       the field names the API currently expects
        defaults:            fallback values for fields that can't be mapped
        agent_descriptions:  schema descriptions for agent-side keys (v1)
        api_descriptions:    schema descriptions for api-side keys (v2)

    Returns:
        dict with keys from target_fields, values from agent_args (remapped)
    """
    defaults = defaults or {}
    agent_keys = list(agent_args.keys())

    # Exact match short-circuit — no alignment needed
    if set(agent_keys) == set(target_fields):
        return {k: agent_args[k] for k in target_fields if k in agent_args}

    # Run SBSA alignment
    mapping, sim_matrix, elapsed_ms = align_keys(
        agent_keys, target_fields, agent_descriptions, api_descriptions,
    )

    # Build healed args
    healed = {}
    reverse = {v: k for k, v in mapping.items()}

    for field in target_fields:
        if field in reverse:
            healed[field] = agent_args[reverse[field]]
        elif field in agent_args:
            healed[field] = agent_args[field]
        elif field in defaults:
            healed[field] = defaults[field]
            log.warning("  SBSA | using default for '%s': %s", field, defaults[field])

    return healed


# ═══════════════════════════════════════════════════════════════
# ANALYTICS EXPORT — for the benchmark logger
# ═══════════════════════════════════════════════════════════════

def get_alignment_report(
    agent_keys: List[str],
    api_keys: List[str],
    agent_descriptions: Dict[str, str] = None,
    api_descriptions: Dict[str, str] = None,
) -> Dict[str, Any]:
    """
    Full diagnostic report for a single alignment operation.
    Used by the analytics logger to record per-call metrics.
    """
    mapping, sim_matrix, elapsed_ms = align_keys(
        agent_keys, api_keys, agent_descriptions, api_descriptions,
    )

    similarities = {}
    for agent_k, api_k in mapping.items():
        i = agent_keys.index(agent_k)
        j = api_keys.index(api_k)
        similarities[f"{agent_k} → {api_k}"] = float(sim_matrix[i, j])

    return {
        "mapping": mapping,
        "similarity_scores": similarities,
        "cost_matrix": (1.0 - sim_matrix).tolist() if sim_matrix.size else [],
        "elapsed_ms": elapsed_ms,
        "threshold": SIMILARITY_THRESHOLD,
        "agent_keys": agent_keys,
        "api_keys": api_keys,
    }
