"""
Agentic Reflection — 模型驱动的反思评估

核心原则: 不写规则判断，充分性评估完全交给 LLM。
应用层只负责:
1. 组装反思 prompt（模板从 app/prompts/ 加载）
2. 调用 LLM
3. 解析 LLM 的 JSON 回答
4. 将结果转化为 Stop hook 的 continue_/systemMessage

所有阈值通过 agent_core/agentloop/agentic_loop.json 管理。
"""
import json
import re
from typing import Dict, Any
from loguru import logger

from agent_core.prompts.loader import load_prompt as _load_prompt


class AgenticReflection:
    """
    LLM 驱动的反思评估器

    工作流:
    1. 从 ToolExecutionTracker 获取工具执行事实摘要
    2. 将摘要 + 用户查询 + 当前输出组装为反思 prompt
    3. 调用 LLM 评估 → JSON 结果
    4. 返回 Stop hook 所需的 continue_/systemMessage
    """

    def __init__(self, config: Dict[str, Any] = None):
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)
        self._max_rounds = self._config.get("max_reflection_rounds", 2)
        self._max_total_tool_calls = self._config.get("max_total_tool_calls", 30)
        self._max_total_elapsed = self._config.get("max_total_elapsed_seconds", 120)
        self._llm_config = self._config.get("llm_eval", {})
        self._current_round = 0

    async def evaluate(
        self,
        user_query: str,
        tool_summary: str,
        current_output: str,
        total_tool_calls: int = 0,
        elapsed_seconds: float = 0.0,
        quality_gaps: str = "",
    ) -> Dict[str, Any]:
        """
        执行一次反思评估

        Args:
            user_query: 用户原始查询
            tool_summary: 工具执行事实摘要
            current_output: 当前已生成的回答
            total_tool_calls: 累计工具调用次数（熔断用）
            elapsed_seconds: 请求开始至今的总耗时（熔断用）
            quality_gaps: 质量缺口描述（PostToolUse Hook 检测到的缺口）

        Returns:
            {
                "sufficient": bool,
                "continue_": bool,        # Stop hook 的 continue_ 值
                "system_message": str,    # 注入模型的系统消息
                "reason": str,            # 评估理由
                "round": int,             # 当前轮次
            }
        """
        self._current_round += 1

        # 关闭状态 → 始终充分
        if not self._enabled:
            return self._make_result(sufficient=True, reason="反思评估已关闭")

        # === 三层熔断（防止死循环） ===

        # 熔断 1: 轮次上限
        if self._current_round > self._max_rounds:
            logger.info(
                f"[Reflection] FUSE: max rounds ({self._max_rounds}) reached"
            )
            return self._make_result(
                sufficient=True,
                reason=f"已达最大反思轮次 {self._max_rounds}",
            )

        # 熔断 2: 工具调用总次数上限
        if total_tool_calls >= self._max_total_tool_calls:
            logger.info(
                f"[Reflection] FUSE: total tool calls ({total_tool_calls}) "
                f">= limit ({self._max_total_tool_calls})"
            )
            return self._make_result(
                sufficient=True,
                reason=f"工具调用总次数 {total_tool_calls} 达上限",
            )

        # 熔断 3: 总耗时上限
        if elapsed_seconds >= self._max_total_elapsed:
            logger.info(
                f"[Reflection] FUSE: elapsed {elapsed_seconds:.0f}s "
                f">= limit ({self._max_total_elapsed}s)"
            )
            return self._make_result(
                sufficient=True,
                reason=f"总耗时 {elapsed_seconds:.0f}s 达上限",
            )

        # LLM 评估
        if not self._llm_config.get("enabled", True):
            return self._make_result(sufficient=True, reason="LLM 评估已关闭")

        try:
            return await self._llm_evaluate(
                user_query, tool_summary, current_output,
                total_tool_calls, elapsed_seconds,
                quality_gaps=quality_gaps,
            )
        except Exception as e:
            logger.warning(f"[Reflection] LLM eval failed: {e}, defaulting to sufficient")
            return self._make_result(sufficient=True, reason=f"LLM 评估调用失败: {e}")

    async def _llm_evaluate(
        self,
        user_query: str,
        tool_summary: str,
        current_output: str,
        total_tool_calls: int = 0,
        elapsed_seconds: float = 0.0,
        quality_gaps: str = "",
    ) -> Dict[str, Any]:
        """调用 LLM 评估充分性"""
        # 加载外置的反思 prompt 模板（含轮次和熔断信息，让 LLM 感知边界）
        prompt = _load_prompt(
            "v4_reflection",
            user_query=user_query,
            tool_execution_summary=tool_summary,
            current_output=current_output[:3000],
            current_round=str(self._current_round),
            max_rounds=str(self._max_rounds),
            total_tool_calls=str(total_tool_calls),
            quality_gaps=quality_gaps or "无",
        )

        if not prompt:
            logger.warning("[Reflection] Prompt template v4_reflection.md not found")
            return self._make_result(sufficient=True, reason="反思 prompt 模板缺失")

        # 调用 LLM
        llm_response = await self._call_llm(prompt)
        if not llm_response:
            return self._make_result(sufficient=True, reason="LLM 无响应")

        # 解析 JSON
        return self._parse_response(llm_response)

    async def _call_llm(self, prompt: str) -> str:
        """调用 LLM 进行反思评估"""
        max_tokens = self._llm_config.get("max_tokens", 1024)
        timeout = self._llm_config.get("timeout_seconds", 15)
        try:
            from agent_core.agentloop.llm_provider import call_llm
            return await call_llm(
                prompt,
                use_small_fast=True,
                max_tokens=max_tokens,
                timeout=float(timeout),
            )
        except Exception as e:
            logger.warning(f"[Reflection] LLM call error: {e}")
            return ""

    def _parse_response(self, response: str) -> Dict[str, Any]:
        """解析 LLM 返回的 JSON（支持嵌套 dimensions 结构）"""
        try:
            text = response.strip()
            # 去除 markdown 代码块包裹
            if text.startswith("```"):
                lines = text.split("\n")
                json_lines = []
                in_block = False
                for line in lines:
                    if line.strip().startswith("```") and not in_block:
                        in_block = True
                        continue
                    elif line.strip() == "```" and in_block:
                        break
                    elif in_block:
                        json_lines.append(line)
                text = "\n".join(json_lines)

            # 找到最外层 JSON 对象（贪婪匹配，支持嵌套）
            start = text.find("{")
            if start >= 0:
                # 从 start 开始，匹配花括号平衡的完整 JSON
                depth = 0
                end = start
                for i in range(start, len(text)):
                    if text[i] == "{":
                        depth += 1
                    elif text[i] == "}":
                        depth -= 1
                        if depth == 0:
                            end = i + 1
                            break
                data = json.loads(text[start:end])
            else:
                return self._make_result(sufficient=True, reason="未找到 JSON 块，降级为充分")

            sufficient = data.get("sufficient", True)
            system_message = data.get("system_message", "")
            reason = data.get("reason", "LLM 评估")
            dimensions = data.get("dimensions", {})

            # 日志记录维度评估结果
            if dimensions:
                dim_summary = ", ".join(f"{k}={v}" for k, v in dimensions.items())
                logger.info(
                    f"[Reflection] Round {self._current_round}: "
                    f"sufficient={sufficient}, dims=[{dim_summary}], reason={reason}"
                )
            else:
                logger.info(
                    f"[Reflection] Round {self._current_round}: "
                    f"sufficient={sufficient}, reason={reason}"
                )

            return self._make_result(
                sufficient=sufficient,
                reason=reason,
                system_message=system_message if not sufficient else "",
            )
        except (json.JSONDecodeError, AttributeError, ValueError) as e:
            logger.warning(f"[Reflection] JSON parse failed: {e}, response: {response[:200]}")

        return self._make_result(sufficient=True, reason="JSON 解析失败，降级为充分")

    def _make_result(
        self,
        sufficient: bool,
        reason: str = "",
        system_message: str = "",
    ) -> Dict[str, Any]:
        """构造统一的结果格式"""
        return {
            "sufficient": sufficient,
            "continue_": not sufficient,  # sufficient=false → continue_=true
            "system_message": system_message,
            "reason": reason,
            "round": self._current_round,
        }

    @property
    def current_round(self) -> int:
        return self._current_round

    def reset(self):
        """重置轮次计数"""
        self._current_round = 0
