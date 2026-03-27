"""
AgentLoop Message Types — 核心数据类

替换 Claude Agent SDK 的 Message 对象，用纯 Python dataclass 表达 LLM 对话语义。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolCallRequest:
    """LLM 发出的工具调用请求"""
    id: str                          # tool_use_id
    name: str                        # 工具名称
    arguments: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    """LLM 单次调用的完整响应"""
    content: Optional[str] = None                          # 文本内容（stop 时有值）
    tool_calls: List[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"                            # stop | tool_use | max_tokens | error
    usage: Dict[str, int] = field(default_factory=dict)    # input_tokens, output_tokens
    thinking_content: Optional[str] = None                 # extended thinking 内容
    thinking_signature: Optional[str] = None               # dashscope/anthropic thinking 签名（回写多轮必须携带）
    model: str = ""

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0

    @property
    def has_text(self) -> bool:
        return bool(self.content)


@dataclass
class ToolResult:
    """工具执行结果"""
    tool_call_id: str               # 对应 ToolCallRequest.id
    name: str                       # 工具名称
    content: str                    # 序列化后供 LLM 看到的 JSON 字符串
    raw_data: Any = None            # 完整原始数据（供 hooks 看，核心修复！）
    is_error: bool = False
    duration_ms: float = 0.0


@dataclass
class SessionContext:
    """会话上下文（由 SessionEngine.prepare_session 返回）"""
    session_id: str
    user_id: int
    history_messages: List[Dict[str, Any]] = field(default_factory=list)  # OpenAI 格式消息数组
    summary: Optional[str] = None
    experience: Dict[str, Any] = field(default_factory=dict)
    knowledge: str = ""             # 知识引擎注入的文本
    token_budget: Optional[Any] = None   # ContextWindowBudget
