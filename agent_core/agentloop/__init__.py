"""
agent_core.agentloop — 自建 AgentLoop，替换 Claude Agent SDK

公开 API:
    AgentLoop           核心 Agent 循环
    LiteLLMProvider     LLM 调用提供者（多模型 failover）
    SkillInvoker        统一 Skill 执行器
    HookEngine          开放式 Hook/插件引擎
    HookPlugin          插件基类
    HookPoint           Hook 挂载点枚举
    LegacyHookPlugin    桥接已有 HookManager 的插件
    ContextBuilder      LLM 消息数组组装
    SessionEngine       Session/记忆/学习集成引擎
    StreamAdapter       SSE 事件格式化工具函数

Phase 2 扩展:
    ParallelToolExecutor  并行工具执行器
    _NullParallelExecutor 空实现（默认行为）

Phase 3 扩展:
    ContextCompactor      Mid-Session 上下文压缩

Phase 4 扩展:
    PermissionGuard       工具权限管控
    PermissionMode        权限模式枚举
    PermissionDecision    权限决策结果

Phase 5 扩展:
    OutputValidator       结构化输出校验

Phase 6 扩展:
    SubAgentExecutor      子代理执行器

数据类:
    LLMResponse     LLM 单次调用响应
    ToolCallRequest 工具调用请求
    ToolResult      工具执行结果（含 raw_data！）
    SessionContext  会话上下文
"""

from .loop import AgentLoop
from .llm_provider import (
    LiteLLMProvider,
    LLMEndpointProvider,
    LLMEndpoint,
    call_anthropic_api,
    stream_anthropic_api,
    get_small_fast_llm_call,
    get_anthropic_client_config,
    shutdown_http_pools,
)
from .skill_invoker import SkillInvoker
from .hook_engine import HookEngine, HookPlugin, HookPoint, LegacyHookPlugin, HookRegistration
from .context_builder import ContextBuilder
from .session_engine import SessionEngine
from .message_types import LLMResponse, ToolCallRequest, ToolResult, SessionContext
from . import stream_adapter

# Phase 2: 并行工具执行
from .parallel_executor import ParallelToolExecutor, _NullParallelExecutor

# Phase 3: 上下文压缩
from .context_compactor import ContextCompactor

# Phase 4: 权限管控
from .permission_guard import PermissionGuard, PermissionMode, PermissionDecision

# Phase 5: 结构化输出校验
from .output_validator import OutputValidator

# Phase 6: 子代理
from .subagent import SubAgentExecutor

# Ring 3: 能力盲区检测
from .capability_gap_counter import CapabilityGapCounter

__all__ = [
    # 核心组件
    "AgentLoop",
    "LiteLLMProvider",
    "LLMEndpointProvider",
    "LLMEndpoint",
    "call_anthropic_api",
    "stream_anthropic_api",
    "get_small_fast_llm_call",
    "get_anthropic_client_config",
    "shutdown_http_pools",
    "SkillInvoker",
    "HookEngine",
    "HookPlugin",
    "HookPoint",
    "HookRegistration",
    "LegacyHookPlugin",
    "ContextBuilder",
    "SessionEngine",
    # 数据类
    "LLMResponse",
    "ToolCallRequest",
    "ToolResult",
    "SessionContext",
    # 工具模块
    "stream_adapter",
    # Phase 2
    "ParallelToolExecutor",
    "_NullParallelExecutor",
    # Phase 3
    "ContextCompactor",
    # Phase 4
    "PermissionGuard",
    "PermissionMode",
    "PermissionDecision",
    # Phase 5
    "OutputValidator",
    # Phase 6
    "SubAgentExecutor",
    # Ring 3
    "CapabilityGapCounter",
]
