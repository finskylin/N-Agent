from .base import BuiltinTool, BuiltinToolRegistry
from .bash_tool import BashTool
from .read_file_tool import ReadFileTool
from .grep_tool import GrepTool
from .write_file_tool import WriteFileTool
from .edit_file_tool import EditFileTool
from .glob_tool import GlobTool
from .agent_tools import SpawnAgentTool, QuerySubagentTool


def get_default_tools():
    """返回默认内置工具列表（不含 agent_tools，需依赖注入）"""
    return [
        BashTool(),
        ReadFileTool(),
        GrepTool(),
        WriteFileTool(),
        EditFileTool(),
        GlobTool(),
    ]


__all__ = [
    "BuiltinTool", "BuiltinToolRegistry",
    "BashTool", "ReadFileTool", "GrepTool",
    "WriteFileTool", "EditFileTool", "GlobTool",
    "SpawnAgentTool", "QuerySubagentTool",
    "get_default_tools",
]
