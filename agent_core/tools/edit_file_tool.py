"""
EditFileTool — 精确替换文件中字符串的内置工具（old_str → new_str）
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict

from .base import BuiltinTool
from ..agentloop.message_types import ToolResult


class EditFileTool(BuiltinTool):
    name: str = "edit_file"
    description: str = "精确替换文件中的字符串（old_str → new_str）"
    readonly: bool = False
    parameters_schema: dict = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "文件路径",
            },
            "old_str": {
                "type": "string",
                "description": "要被替换的原始字符串（必须在文件中唯一，除非 replace_all=true）",
            },
            "new_str": {
                "type": "string",
                "description": "替换后的新字符串",
            },
            "replace_all": {
                "type": "boolean",
                "description": "是否替换所有匹配项，默认 false（false 时要求 old_str 唯一）",
                "default": False,
            },
        },
        "required": ["file_path", "old_str", "new_str"],
    }

    async def execute(
        self,
        arguments: Dict[str, Any],
        tool_call_id: str = "",
        sandbox=None,
        **kwargs,
    ) -> ToolResult:
        file_path: str = arguments.get("file_path", "")
        old_str: str = arguments.get("old_str", "")
        new_str: str = arguments.get("new_str", "")
        replace_all: bool = arguments.get("replace_all", False)
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

            original = p.read_text(encoding="utf-8")

            if old_str not in original:
                return ToolResult(
                    tool_call_id=tool_call_id,
                    name=self.name,
                    content=json.dumps({"error": f"old_str not found in file: {file_path}"}),
                    is_error=True,
                    duration_ms=time.monotonic() * 1000 - start_ms,
                )

            if not replace_all:
                count = original.count(old_str)
                if count > 1:
                    return ToolResult(
                        tool_call_id=tool_call_id,
                        name=self.name,
                        content=json.dumps({
                            "error": (
                                f"old_str is not unique in file: found {count} occurrences. "
                                "Use replace_all=true to replace all, or provide more context to make it unique."
                            )
                        }),
                        is_error=True,
                        duration_ms=time.monotonic() * 1000 - start_ms,
                    )
                updated = original.replace(old_str, new_str, 1)
            else:
                updated = original.replace(old_str, new_str)

            p.write_text(updated, encoding="utf-8")

            replacements = original.count(old_str) if replace_all else 1
            return ToolResult(
                tool_call_id=tool_call_id,
                name=self.name,
                content=json.dumps({
                    "success": True,
                    "file_path": file_path,
                    "replacements": replacements,
                }),
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
