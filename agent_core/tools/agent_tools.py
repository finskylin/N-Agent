"""
AgentTools — spawn_agent 和 query_subagent 内置工具

依赖注入模式：由 skill_invoker 或 native_agent 在启用子代理功能时条件注册。
execute() 委托给已有的 subagent_executor / subagent_store，不重复实现逻辑。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict

from .base import BuiltinTool
from ..agentloop.message_types import ToolResult


class SpawnAgentTool(BuiltinTool):
    name: str = "spawn_agent"
    description: str = (
        "创建子代理执行独立子任务。子代理拥有完整工具集和独立上下文，"
        "可进行多轮 LLM 推理和工具调用，执行完毕返回文本结果。"
        "适用于：需要隔离执行的子任务、任务边界清晰且可并行的场景。"
        "子代理过程事件会实时透传给当前对话流。"
        "重要：软件开发、代码编写、系统部署、续接开发（'继续开发'/'不要停'/'直到完成'）"
        "等预计超过15分钟的任务，必须传 background=true，否则会阻塞主流程超时。"
    )
    readonly: bool = False
    parameters_schema: dict = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "子任务的完整描述，必须包含执行所需的所有上下文信息。"
                    "子代理无法访问父代理的对话历史，请将关键信息直接写入 task。"
                ),
            },
            "role": {
                "type": "string",
                "description": (
                    "子代理角色：stock_expert（股票专家）/ military_expert（军事专家）"
                    "/ political_expert（政治专家）/ general_expert（通用专家）"
                    "/ software_expert（高级软件专家）。不填默认 general_expert。"
                ),
                "enum": [
                    "stock_expert",
                    "military_expert",
                    "political_expert",
                    "general_expert",
                    "software_expert",
                ],
            },
            "background": {
                "type": "boolean",
                "description": (
                    "true=后台异步执行（立即返回 task_id，子代理在后台独立运行最长5小时，完成后自动通知用户）。"
                    "false=同步等待（父代理阻塞直到子代理完成，仅适合3轮以内的快速任务）。"
                    "以下场景必须设为 true：软件开发、代码编写、系统部署、"
                    "续接开发（'继续开发'/'不要停'/'直到完成'/'接着做'）、"
                    "预计超过15分钟的任务。"
                ),
            },
        },
        "required": ["task"],
    }

    def __init__(self, subagent_executor):
        self._subagent_executor = subagent_executor

    async def execute(
        self,
        arguments: Dict[str, Any],
        tool_call_id: str = "",
        sandbox=None,
        **kwargs,
    ) -> ToolResult:
        start_ms = time.monotonic() * 1000

        if not self._subagent_executor:
            return ToolResult(
                tool_call_id=tool_call_id,
                name=self.name,
                content=json.dumps({"error": "SubAgent not enabled. Set subagent_enabled=True in config."}),
                is_error=True,
                duration_ms=time.monotonic() * 1000 - start_ms,
            )

        try:
            task = arguments.get("task", "")
            parent_agent_id = arguments.get("_parent_agent_id", "root")
            current_depth = arguments.get("_depth", 0)
            parent_session_id = arguments.get("_parent_session_id", "")
            req_user_id = int(arguments.get("_user_id", 0))

            result_text = await self._subagent_executor.execute(
                task=task,
                parent_agent_id=parent_agent_id,
                current_depth=current_depth,
                parent_session_id=parent_session_id,
                user_id=req_user_id,
                role=arguments.get("role", "general_expert"),
                background=arguments.get("background", False),
            )
            return ToolResult(
                tool_call_id=tool_call_id,
                name=self.name,
                content=result_text,
                raw_data={
                    "task": task,
                    "result": result_text,
                    "parent_agent_id": parent_agent_id,
                    "parent_session_id": parent_session_id,
                },
                duration_ms=time.monotonic() * 1000 - start_ms,
            )
        except Exception as e:
            return ToolResult(
                tool_call_id=tool_call_id,
                name=self.name,
                content=json.dumps({"error": str(e), "skill": self.name}),
                is_error=True,
                duration_ms=time.monotonic() * 1000 - start_ms,
            )


class QuerySubagentTool(BuiltinTool):
    name: str = "query_subagent"
    description: str = (
        "查询已执行完毕的子代理上下文，包括状态、工具调用列表、最终结果。"
        "用于多轮对话中回溯子代理的执行细节。"
    )
    readonly: bool = True
    parameters_schema: dict = {
        "type": "object",
        "properties": {
            "sub_agent_id": {
                "type": "string",
                "description": "子代理 ID（spawn_agent 返回的 raw_data.sub_agent_id）",
            },
            "parent_session_id": {
                "type": "string",
                "description": "父 session_id，查询该 session 下的所有子代理记录",
            },
        },
    }

    def __init__(self, subagent_store):
        self._subagent_store = subagent_store

    async def execute(
        self,
        arguments: Dict[str, Any],
        tool_call_id: str = "",
        sandbox=None,
        **kwargs,
    ) -> ToolResult:
        start_ms = time.monotonic() * 1000

        if not self._subagent_store:
            return ToolResult(
                tool_call_id=tool_call_id,
                name=self.name,
                content=json.dumps({"error": "SubAgentStore not configured"}),
                is_error=True,
                duration_ms=time.monotonic() * 1000 - start_ms,
            )

        try:
            sub_agent_id = arguments.get("sub_agent_id", "")
            parent_session_id = arguments.get("parent_session_id", "")

            if sub_agent_id:
                record = await self._subagent_store.get_by_id(sub_agent_id)
                data = record or {"error": f"sub_agent_id={sub_agent_id!r} not found"}
            elif parent_session_id:
                records = await self._subagent_store.list_by_parent_session(parent_session_id)
                data = {"records": records, "count": len(records)}
            else:
                data = {"error": "请提供 sub_agent_id 或 parent_session_id"}

            return ToolResult(
                tool_call_id=tool_call_id,
                name=self.name,
                content=json.dumps(data, ensure_ascii=False, default=str),
                raw_data=data,
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
