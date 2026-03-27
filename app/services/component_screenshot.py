"""
组件截图服务 - 便捷导入

从 .claude/skills/component_screenshot 导入 ComponentScreenshot 类，
方便 app 层模块直接使用。
"""

from pathlib import Path
import sys

# 确保 skills 路径可导入
_skills_dir = Path(__file__).resolve().parents[2] / ".claude" / "skills" / "component_screenshot" / "scripts"
if str(_skills_dir) not in sys.path:
    sys.path.insert(0, str(_skills_dir))

from component_screenshot import ComponentScreenshot  # noqa: E402

__all__ = ["ComponentScreenshot"]
