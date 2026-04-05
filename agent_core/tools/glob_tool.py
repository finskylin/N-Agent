"""
GlobTool — 按 glob 模式匹配文件路径的内置工具
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .base import BuiltinTool
from ..agentloop.message_types import ToolResult


class GlobTool(BuiltinTool):
    name: str = "glob"
    description: str = "按 glob 模式匹配文件路径"
    readonly: bool = True
    parameters_schema: dict = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "glob 模式，例如 \"**/*.py\"",
            },
            "root": {
                "type": "string",
                "description": "搜索根目录，默认为当前目录 \".\"",
                "default": ".",
            },
            "limit": {
                "type": "integer",
                "description": "最多返回的路径数量，默认 200",
                "default": 200,
            },
        },
        "required": ["pattern"],
    }

    async def execute(
        self,
        arguments: Dict[str, Any],
        tool_call_id: str = "",
        sandbox=None,
        **kwargs,
    ) -> ToolResult:
        pattern: str = arguments.get("pattern", "")
        root: str = arguments.get("root", ".")
        limit: int = arguments.get("limit", 200)
        start_ms = time.monotonic() * 1000

        try:
            root_path = Path(root)
            if not root_path.exists():
                return ToolResult(
                    tool_call_id=tool_call_id,
                    name=self.name,
                    content=json.dumps({"error": f"Root directory not found: {root}"}),
                    is_error=True,
                    duration_ms=time.monotonic() * 1000 - start_ms,
                )

            matched = sorted(str(p) for p in root_path.glob(pattern))[:limit]
            output = "\n".join(matched) if matched else "(no matches)"

            return ToolResult(
                tool_call_id=tool_call_id,
                name=self.name,
                content=output,
                raw_data={"pattern": pattern, "root": root, "matches": matched, "count": len(matched)},
                duration_ms=time.monotonic() * 1000 - start_ms,
            )
        except Exception as e:
            return ToolResult(
                tool_call_id=tool_call_id,
                name=self.name,
                content=json.dumps({"error": str(e)}),
                is_error=True,
                duration_ms=time.monotonic() * 1000 - start_ms,
            )
