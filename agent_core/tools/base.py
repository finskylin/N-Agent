"""
BuiltinTool 抽象基类 + BuiltinToolRegistry
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class BuiltinTool(ABC):
    name: str = ""
    description: str = ""
    parameters_schema: dict = {"type": "object", "properties": {}}
    readonly: bool = False

    @abstractmethod
    async def execute(
        self,
        arguments: Dict[str, Any],
        tool_call_id: str = "",
        sandbox=None,
        **kwargs,
    ):  # -> ToolResult
        ...

    def to_api_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }


class BuiltinToolRegistry:
    def __init__(self):
        self._tools: Dict[str, BuiltinTool] = {}

    def register(self, tool: BuiltinTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[BuiltinTool]:
        return self._tools.get(name)

    def all_tools(self) -> List[BuiltinTool]:
        return list(self._tools.values())

    def to_api_schemas(self) -> List[dict]:
        return [t.to_api_schema() for t in self._tools.values()]
