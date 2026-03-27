"""
StreamAdapter — SSE 事件格式化

将 AgentLoop 内部事件转换为与现有 native_agent.py 输出格式完全一致的 SSE 事件。

现有 SSE 事件类型（保持向后兼容）:
- text_delta: 文本流式输出
- tool_call: 工具开始调用
- tool_done: 工具执行完成
- thinking: 思考过程
- component_for_render: UI 组件渲染
- confidence: 置信度评估结果
- report_ready: 报告生成完成
- report_lang: 报告语种
- status: 状态信息
- error: 错误
- done: 完成
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

from .message_types import LLMResponse, ToolCallRequest, ToolResult


def make_text_delta(delta: str) -> Dict[str, Any]:
    """文本流式输出事件"""
    return {"event": "text_delta", "data": {"delta": delta}}


def make_thinking(thinking: str) -> Dict[str, Any]:
    """思考过程事件"""
    return {"event": "thinking", "data": {"thinking": thinking}}


def make_tool_call(tool_name: str, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    """工具开始调用事件"""
    return {
        "event": "tool_call",
        "data": {
            "name": tool_name,
            "input": tool_input,
        },
    }


def make_tool_done(
    tool_name: str,
    tool_result: ToolResult,
    render_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """工具执行完成事件"""
    data: Dict[str, Any] = {
        "name": tool_name,
        "success": not tool_result.is_error,
        "duration_ms": int(tool_result.duration_ms),
    }
    if render_data:
        data.update(render_data)
    return {"event": "tool_done", "data": data}


def make_component_for_render(
    component_type: str,
    component_data: Dict[str, Any],
) -> Dict[str, Any]:
    """UI 组件渲染事件（与 EventBridge 输出格式一致）"""
    return {
        "event": "component_for_render",
        "data": {
            "type": component_type,
            **component_data,
        },
    }


def make_status(status: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """状态信息事件"""
    data: Dict[str, Any] = {"status": status}
    if metadata:
        data.update(metadata)
    return {"event": "status", "data": data}


def make_error(error: str, recoverable: bool = False) -> Dict[str, Any]:
    """错误事件"""
    return {
        "event": "error",
        "data": {
            "error": error,
            "recoverable": recoverable,
        },
    }


def make_done(
    final_text: str = "",
    tools_used: Optional[list] = None,
    total_iterations: int = 0,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """完成事件"""
    data: Dict[str, Any] = {
        "final_text": final_text,
        "tools_used": tools_used or [],
        "total_iterations": total_iterations,
        "timestamp": int(time.time()),
    }
    if metadata:
        data.update(metadata)
    return {"event": "done", "data": data}


def make_report_lang(lang: str = "zh") -> Dict[str, Any]:
    """报告语种事件"""
    return {"event": "report_lang", "data": {"lang": lang}}


def make_report_ready(report_data: Dict[str, Any]) -> Dict[str, Any]:
    """报告生成完成事件"""
    return {"event": "report_ready", "data": report_data}


def format_llm_response_events(response: LLMResponse):
    """
    将 LLMResponse 转换为 SSE 事件序列

    Yields SSE event dicts.
    """
    if response.thinking_content:
        yield make_thinking(response.thinking_content)
    if response.content:
        yield make_text_delta(response.content)
