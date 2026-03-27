# shim: 统一从 agent_core 导出，保持所有现有调用者不变
from agent_core.observability.langfuse_client import LangfuseManager, langfuse, _NoOpSpan

__all__ = ["LangfuseManager", "langfuse", "_NoOpSpan"]
