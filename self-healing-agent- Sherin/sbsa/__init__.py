"""
SBSA (Semantic Bipartite Schema Alignment) Package

Mathematical schema drift resolution using semantic embeddings
and Hungarian algorithm for optimal parameter mapping.
"""

from .sbsa_engine import SBSAEngine
from .discovery import MCPDiscovery

__all__ = ["SBSAEngine", "MCPDiscovery"]
