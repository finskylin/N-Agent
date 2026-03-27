"""
V4 Agent Module -- 基于 Claude Agent SDK 原生架构（完全独立于 V3）

架构特点:
- Agent Loop 由 ClaudeSDKClient 原生管理
- Tool Execution 通过 MCP Server 协议驱动
- 自定义逻辑通过 Hooks (PreToolUse/PostToolUse/Stop) 注入
- 技能发现基于 SKILL.md YAML 元数据（SkillDiscovery）
- 技能执行使用独立实例（V4SkillExecutor）
- MCP 工具构建使用 MCPToolBuilder
- UI 组件选择使用 V4UISelector
- 支持 render_mode 控制 UI 组件渲染
- 支持知识库目录配置
- 支持会话上下文管理和经验积累
"""

from .native_agent import V4NativeAgent, V4AgentRequest

__all__ = ["V4NativeAgent", "V4AgentRequest"]
