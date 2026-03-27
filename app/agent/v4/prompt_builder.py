"""
Prompt Builder -- 已迁移至 agent_core/prompt_builder.py

此文件保留为向后兼容的 re-export shim，供 app 层现有 import 语句使用。
实际逻辑在 agent_core/prompt_builder.py。
"""
from agent_core.prompt_builder import PromptBuilder  # noqa: F401

__all__ = ["PromptBuilder"]
