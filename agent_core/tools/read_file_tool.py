"""
ReadFileTool — 读取文件内容的内置工具

迁移自 agent_core/agentloop/skill_invoker.py 的 invoke_read 方法，
并扩展支持 offset/limit 行截取。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .base import BuiltinTool
from ..agentloop.message_types import ToolResult


class ReadFileTool(BuiltinTool):
    name: str = "read_file"
    description: str = "读取文件内容"
    readonly: bool = True
    parameters_schema: dict = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "文件路径",
            },
            "offset": {
                "type": "integer",
                "description": "起始行号（从 1 开始），可选",
            },
            "limit": {
                "type": "integer",
                "description": "最多读取的行数，可选",
            },
        },
        "required": ["file_path"],
    }

    async def execute(
        self,
        arguments: Dict[str, Any],
        tool_call_id: str = "",
        sandbox=None,
        **kwargs,
    ) -> ToolResult:
        file_path: str = arguments.get("file_path", "")
        offset: Optional[int] = arguments.get("offset")
        limit: Optional[int] = arguments.get("limit")
        start_ms = time.monotonic() * 1000

        try:
            p = Path(file_path)
            if not p.exists():
                return ToolResult(
                    tool_call_id=tool_call_id,
                    name=self.name,
                    content=json.dumps({"error": f"File not found: {file_path}"}),
                    is_error=True,
                    duration_ms=time.monotonic() * 1000 - start_ms,
                )

            content = p.read_text(encoding="utf-8", errors="replace")

            # 支持 offset/limit 行截取
            if offset is not None or limit is not None:
                lines = content.splitlines(keepends=True)
                start = (offset - 1) if offset and offset > 0 else 0
                if limit is not None:
                    lines = lines[start: start + limit]
                else:
                    lines = lines[start:]
                content = "".join(lines)

            return ToolResult(
                tool_call_id=tool_call_id,
                name=self.name,
                content=content,
                raw_data={"file_path": file_path, "content": content},
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
