"""
sbsa/discovery.py - Schema discovery for MCP server

Adapted from ToolBench version to work with MCP's TOOL_REGISTRY.
"""

from typing import List, Optional

# Import from parent interceptor.py
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class MCPDiscovery:
    """
    Fetches live schema from MCP TOOL_REGISTRY.
    
    Unlike ToolBench which reads JSON files, MCP schemas are
    defined in interceptor.py's TOOL_REGISTRY dictionary.
    """
    
    def __init__(self, tool_registry: dict):
        """
        Args:
            tool_registry: Reference to TOOL_REGISTRY from interceptor.py
        """
        self.registry = tool_registry
    
    def get_schema(self, tool_name: str, version: str = "v2") -> Optional[List[str]]:
        """
        Get required field names for a tool at a specific schema version.
        
        Args:
            tool_name: Tool name (e.g., "get_weather")
            version: Schema version ("v1" or "v2")
        
        Returns:
            List of required field names, or None if tool not found
        
        Example:
            get_schema("get_weather", "v1") → ["city"]
            get_schema("get_weather", "v2") → ["location_name"]
        """
        info = self.registry.get(tool_name)
        if not info:
            return None
        
        # Try explicit fields list first
        fields = info.get(f"{version}_fields")
        if fields:
            return fields
        
        # Fall back to schema keys
        schema = info.get(f"{version}_schema")
        if schema:
            return list(schema.keys())
        
        return None
    
    def get_current_version(self, server_state: dict) -> str:
        """
        Get the current schema version the server is using.
        
        Args:
            server_state: Reference to _server_schema_version dict
        
        Returns:
            "v1" or "v2"
        """
        return server_state.get("version", "v1")
