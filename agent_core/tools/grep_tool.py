"""
GrepTool — 在文件或目录中搜索内容的内置工具

迁移自 agent_core/agentloop/skill_invoker.py 的 invoke_grep 方法，
并扩展支持 recursive 和 include 参数。
"""
from __future__ import annotations

import json
import subprocess
import time
from typing import Any, Dict, Optional

from .base import BuiltinTool
from ..agentloop.message_types import ToolResult


class GrepTool(BuiltinTool):
    name: str = "grep"
    description: str = "在文件或目录中搜索内容（支持正则）"
    readonly: bool = True
    parameters_schema: dict = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "搜索模式（正则表达式）",
            },
            "path": {
                "type": "string",
                "description": "搜索路径，默认为当前目录 \".\"",
                "default": ".",
            },
            "recursive": {
                "type": "boolean",
                "description": "是否递归搜索子目录，默认 true",
                "default": True,
            },
            "include": {
                "type": "string",
                "description": "文件 glob 过滤，例如 \"*.py\"，可选",
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
        path: str = arguments.get("path", ".")
        recursive: bool = arguments.get("recursive", True)
        include: Optional[str] = arguments.get("include")
        start_ms = time.monotonic() * 1000

        try:
            cmd = ["grep", "-n", pattern]
            if recursive:
                cmd.append("-r")
            if include:
                cmd.extend(["--include", include])
            cmd.append(path)

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = result.stdout or "(no matches)"
            return ToolResult(
                tool_call_id=tool_call_id,
                name=self.name,
                content=output,
                raw_data={"matches": result.stdout, "pattern": pattern, "path": path},
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
