"""
WriteFileTool — 写入文件内容的内置工具（自动创建父目录）
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict

from .base import BuiltinTool
from ..agentloop.message_types import ToolResult


class WriteFileTool(BuiltinTool):
    name: str = "write_file"
    description: str = "写入文件内容（自动创建父目录）"
    readonly: bool = False
    parameters_schema: dict = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "文件路径",
            },
            "content": {
                "type": "string",
                "description": "要写入的文件内容",
            },
            "create_directories": {
                "type": "boolean",
                "description": "是否自动创建父目录，默认 true",
                "default": True,
            },
        },
        "required": ["file_path", "content"],
    }

    async def execute(
        self,
        arguments: Dict[str, Any],
        tool_call_id: str = "",
        sandbox=None,
        **kwargs,
    ) -> ToolResult:
        file_path: str = arguments.get("file_path", "")
        content: str = arguments.get("content", "")
        create_directories: bool = arguments.get("create_directories", True)
        start_ms = time.monotonic() * 1000

        try:
            p = Path(file_path)
            if create_directories:
                p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return ToolResult(
                tool_call_id=tool_call_id,
                name=self.name,
                content=json.dumps({"success": True, "file_path": file_path, "bytes_written": len(content.encode("utf-8"))}),
                raw_data={"file_path": file_path},
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
