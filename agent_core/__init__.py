"""
AgentCore -- 独立的 Agent 核心包

核心职责:
1. 问答单实例流程（V4NativeAgent）
2. 会话/记忆管理（session/）
3. Skill 发现与执行

依赖铁律:
- 只允许: anthropic/claude SDK, aiosqlite, loguru, python-dotenv, pyyaml, 标准库
- 禁止: fastapi, redis, sqlalchemy, minio, dingtalk_stream, langfuse
"""

from .config import V4Config, AgentCoreConfig
from .skill_discovery import SkillMetadata, SkillDiscovery
from .skill_metadata_provider import SkillMetadataProvider, get_skill_metadata_provider, init_skill_metadata_provider
from .agent import V4AgentRequest, DataCollector
from .tool_execution_tracker import ToolExecutionTracker, ToolExecution
from .sandbox import (
    ContainerSandbox,
    SandboxExecutionResult,
    SandboxProvider,
    SrtSandbox,
    SubprocessSandbox,
    create_sandbox_provider,
)

__all__ = [
    "V4Config",
    "AgentCoreConfig",
    "SkillMetadata",
    "SkillDiscovery",
    "SkillMetadataProvider",
    "get_skill_metadata_provider",
    "init_skill_metadata_provider",
    "V4AgentRequest",
    "DataCollector",
    "ToolExecutionTracker",
    "ToolExecution",
    "ContainerSandbox",
    "SandboxProvider",
    "SandboxExecutionResult",
    "SrtSandbox",
    "SubprocessSandbox",
    "create_sandbox_provider",
]
