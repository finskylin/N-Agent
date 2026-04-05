"""
BashTool — 执行 bash 命令的内置工具

迁移自 agent_core/agentloop/skill_invoker.py 的 invoke_bash 方法。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict

from .base import BuiltinTool
from ..agentloop.message_types import ToolResult


class BashTool(BuiltinTool):
    name: str = "bash"
    description: str = "执行 bash 命令（python3、curl、shell 脚本等）"
    readonly: bool = False
    parameters_schema: dict = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 bash 命令",
            },
            "timeout": {
                "type": "integer",
                "description": "超时时间（秒），默认 120",
                "default": 120,
            },
        },
        "required": ["command"],
    }

    async def execute(
        self,
        arguments: Dict[str, Any],
        tool_call_id: str = "",
        sandbox=None,
        **kwargs,
    ) -> ToolResult:
        command = arguments.get("command", "")
        timeout = arguments.get("timeout", 120)
        start_ms = time.monotonic() * 1000

        try:
            if sandbox is not None:
                result = await sandbox.execute(command=command, timeout=timeout)
                output = result.stdout or ""
                if result.stderr:
                    output += f"\n[stderr]\n{result.stderr}"
                is_error = result.exit_code != 0
                raw_data = {
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "exit_code": result.exit_code,
                }
            else:
                # 无 sandbox 时使用 asyncio 子进程
                import asyncio
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=timeout
                    )
                except asyncio.TimeoutError:
                    proc.kill()
                    return ToolResult(
                        tool_call_id=tool_call_id,
                        name=self.name,
                        content=json.dumps({"error": f"Command timed out after {timeout}s"}),
                        is_error=True,
                        duration_ms=time.monotonic() * 1000 - start_ms,
                    )
                output = stdout.decode("utf-8", errors="replace")
                err_text = stderr.decode("utf-8", errors="replace")
                if err_text:
                    output += f"\n[stderr]\n{err_text}"
                is_error = proc.returncode != 0
                raw_data = {
                    "stdout": stdout.decode("utf-8", errors="replace"),
                    "stderr": err_text,
                    "exit_code": proc.returncode,
                }

            return ToolResult(
                tool_call_id=tool_call_id,
                name=self.name,
                content=output,
                raw_data=raw_data,
                is_error=is_error,
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
