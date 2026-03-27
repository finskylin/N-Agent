"""
Prompt Loader - 统一提示词加载器

从 app/prompts/ 目录加载所有系统提示词，支持：
- .md 文件格式
- 占位符替换（{variable} 格式）
- 缓存加载结果
"""

from pathlib import Path
from typing import Dict, Optional
from loguru import logger

_PROMPTS_DIR = Path(__file__).parent
_cache: Dict[str, str] = {}


def load_prompt(name: str, **kwargs) -> str:
    """
    加载指定名称的提示词文件

    Args:
        name: 提示词文件名（不含扩展名），如 "sdk_agent_system"
        **kwargs: 占位符替换变量

    Returns:
        提示词内容字符串
    """
    # 检查缓存（只缓存原始模板，不缓存替换后的结果）
    if name not in _cache:
        file_path = _PROMPTS_DIR / f"{name}.md"
        if not file_path.exists():
            logger.warning(f"[PromptLoader] Prompt file not found: {file_path}")
            return ""

        try:
            _cache[name] = file_path.read_text(encoding="utf-8")
            logger.info(f"[PromptLoader] Loaded prompt: {name} ({len(_cache[name])} chars)")
        except Exception as e:
            logger.error(f"[PromptLoader] Failed to load prompt {name}: {e}")
            return ""

    template = _cache[name]

    # 如果有占位符变量，进行替换
    if kwargs:
        try:
            return template.format(**kwargs)
        except KeyError as e:
            logger.warning(f"[PromptLoader] Missing placeholder {e} in prompt {name}, returning raw template")
            return template

    return template


def reload_prompt(name: str) -> None:
    """强制重新加载指定提示词（清除缓存）"""
    if name in _cache:
        del _cache[name]
        logger.info(f"[PromptLoader] Cache cleared for prompt: {name}")


def reload_all() -> None:
    """清除所有缓存，强制重新加载"""
    _cache.clear()
    logger.info("[PromptLoader] All prompt caches cleared")


def list_prompts() -> list:
    """列出所有可用的提示词文件"""
    return [f.stem for f in _PROMPTS_DIR.glob("*.md")]
