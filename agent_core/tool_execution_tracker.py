"""
Tool Execution Tracker — 工具执行事实记录

仅记录客观事实（工具名、耗时、数据量），不做质量判断。
质量判断交给 LLM 在反思环节完成。
"""
import time
from dataclasses import dataclass, field
from typing import Dict, Any, List
from loguru import logger


@dataclass
class ToolExecution:
    """单次工具执行记录"""
    tool_name: str
    start_time: float
    end_time: float = 0.0
    result_chars: int = 0
    has_error_in_result: bool = False
    input_summary: str = ""


class ToolExecutionTracker:
    """
    记录工具执行事实，为 LLM 反思提供客观数据

    设计要点:
    - 只记录事实，不判断好坏（判断交给 LLM）
    - 不引用任何 Skill 模块（保持 Skill 独立性）
    - 通过 config 控制摘要截断长度
    """

    def __init__(self, config: Dict[str, Any] = None):
        self._config = config or {}
        self._executions: List[ToolExecution] = []
        self._summary_max_chars = self._config.get("summary_max_chars", 2000)

    def on_tool_start(self, tool_name: str, tool_input: Dict[str, Any] = None):
        """记录工具开始执行"""
        input_summary = ""
        if tool_input and isinstance(tool_input, dict):
            # 只记录参数 key，不记录值（避免泄露数据）
            input_summary = ", ".join(list(tool_input.keys())[:5])

        execution = ToolExecution(
            tool_name=tool_name,
            start_time=time.monotonic(),
            input_summary=input_summary,
        )
        self._executions.append(execution)

    def on_tool_end(self, tool_name: str, result: Any = None):
        """记录工具执行完成"""
        # 查找最近一次同名工具执行
        for exe in reversed(self._executions):
            if exe.tool_name == tool_name and exe.end_time == 0.0:
                exe.end_time = time.monotonic()
                if result is not None:
                    result_str = str(result)
                    exe.result_chars = len(result_str)
                break

    def get_summary(self) -> str:
        """
        生成工具执行摘要（纯事实描述）

        格式:
        - tool_name: 耗时 Xs, 返回 N 字符, 参数: [key1, key2]
        - tool_name: 耗时 Xs, 返回 0 字符 (可能无数据)
        """
        if not self._executions:
            return "本轮未调用任何工具。"

        lines = []
        for exe in self._executions:
            duration = exe.end_time - exe.start_time if exe.end_time > 0 else 0
            parts = [f"- {exe.tool_name}: 耗时 {duration:.1f}s"]
            parts.append(f"返回 {exe.result_chars} 字符")
            if exe.input_summary:
                parts.append(f"参数: [{exe.input_summary}]")
            if exe.result_chars == 0:
                parts.append("(可能无数据)")
            lines.append(", ".join(parts))

        summary = "\n".join(lines)
        # 截断到配置的最大长度
        if len(summary) > self._summary_max_chars:
            summary = summary[:self._summary_max_chars] + "\n... (摘要已截断)"

        return summary

    def get_stats(self) -> Dict[str, Any]:
        """获取统计数据"""
        total = len(self._executions)
        empty = sum(1 for e in self._executions if e.result_chars == 0 and e.end_time > 0)
        return {
            "total_tools": total,
            "empty_results": empty,
            "tools_called": [e.tool_name for e in self._executions],
        }

    def reset(self):
        """重置（新轮次前调用）"""
        self._executions.clear()
