"""
AgentCore Prompt Loader

从 agent_core/prompts/ 目录加载提示词模板。
不依赖 app 层任何模块，完全自包含。
"""
from pathlib import Path
from typing import Optional
from loguru import logger

_PROMPTS_DIR = Path(__file__).parent


def load_prompt(name: str, **kwargs) -> str:
    """
    加载提示词模板并渲染变量。

    Args:
        name: 提示词文件名（不含 .md 后缀）
        **kwargs: 模板变量，替换 {var} 占位符

    Returns:
        渲染后的提示词字符串，失败返回空字符串
    """
    path = _PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        logger.warning(f"[AgentCore] Prompt not found: {name}")
        return ""
    try:
        text = path.read_text(encoding="utf-8")
        if kwargs:
            text = text.format(**kwargs)
        return text
    except Exception as e:
        logger.warning(f"[AgentCore] Failed to load prompt '{name}': {e}")
        return ""
