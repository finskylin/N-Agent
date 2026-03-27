"""
V4 智能规划链 — 解析 LLM Phase 1 输出的 JSON 执行计划

与旧版完全不同:
- 旧版: 规则 + 关键词匹配生成思考过程
- 新版: 解析 LLM 输出的结构化 JSON 计划，转为用户可读的 Markdown

输出示例:
    > **意图分析**: 用户希望全面分析贵州茅台的投资价值
    > **任务类型**: 综合分析
    > **执行计划**:
    > 1. 股票代码解析 — 解析"贵州茅台"对应的股票代码
    > 2. 实时行情 — 获取最新价格和涨跌幅
    > 3. 财务报表 — 查看关键财务指标
    > ...

    ```mermaid
    graph LR
        A[用户请求] --> B[股票代码解析] --> C[实时行情] --> D[生成报告]
    ```
    ---
"""
import json
import re
from typing import Optional, Dict, List
from loguru import logger

from agent_core.skill_metadata_provider import get_skill_metadata_provider


def tool_cn(name: str) -> str:
    """获取工具的中文名（从 SkillMetadataProvider 查询）"""
    provider = get_skill_metadata_provider()
    return provider.get_display_name(name)


class V4PlanningChain:
    """V4 智能规划链 — 解析 LLM Phase 1 输出的 JSON 计划"""

    def parse_plan(self, llm_output: str) -> Optional[dict]:
        """
        从 LLM 输出中提取 JSON 格式的执行计划

        Args:
            llm_output: LLM Phase 1 输出的原始文本

        Returns:
            解析后的 dict 计划，解析失败返回 None
        """
        if not llm_output or not llm_output.strip():
            return None

        text = llm_output.strip()

        # 尝试从 ```json ... ``` 代码块中提取
        json_match = re.search(r"```json\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
        if json_match:
            text = json_match.group(1).strip()

        # 尝试从 { ... } 中提取（最外层大括号）
        if not text.startswith("{"):
            brace_match = re.search(r"\{.*\}", text, re.DOTALL)
            if brace_match:
                text = brace_match.group(0)

        try:
            plan = json.loads(text)
        except json.JSONDecodeError as e:
            logger.warning(f"[V4PlanningChain] Failed to parse plan JSON: {e}")
            logger.debug(f"[V4PlanningChain] Raw output: {llm_output[:500]}")
            return None

        # 基本结构校验
        if not isinstance(plan, dict):
            logger.warning("[V4PlanningChain] Plan is not a dict")
            return None

        if "intent_analysis" not in plan:
            logger.warning("[V4PlanningChain] Missing intent_analysis in plan")
            return None

        logger.info(
            f"[V4PlanningChain] Plan parsed: "
            f"intent={plan.get('intent_analysis', {}).get('user_need', '?')}, "
            f"steps={len(plan.get('steps', []))}"
        )

        return plan

    def format_plan_markdown(self, plan: dict) -> str:
        """
        将结构化计划转为用户可读的 Markdown

        Args:
            plan: parse_plan() 返回的 dict

        Returns:
            Markdown 格式的规划展示文本
        """
        lines = []

        intent = plan.get("intent_analysis", {})
        steps = plan.get("steps", [])

        # 意图分析
        user_need = intent.get("user_need", "分析用户请求")
        entities = intent.get("entities", [])
        task_type = intent.get("task_type", "综合分析")

        lines.append(f"> **意图分析**: {user_need}")
        if entities:
            lines.append(f"> **关键实体**: {'、'.join(str(e) for e in entities)}")
        lines.append(f"> **任务类型**: {task_type}")

        # 执行计划
        if steps:
            lines.append("> **执行计划**:")
            for step in steps:
                step_id = step.get("step_id", "?")
                tool_name = step.get("tool_name", "unknown")
                purpose = step.get("purpose", "")
                cn_name = tool_cn(tool_name)
                lines.append(f"> {step_id}. **{cn_name}** — {purpose}")

        # Mermaid 流程图
        mermaid = self.build_mermaid_from_plan(plan)
        if mermaid:
            lines.append("")  # 结束 blockquote
            lines.append("```mermaid")
            lines.append(mermaid)
            lines.append("```")

        lines.append("")
        lines.append("---")
        lines.append("")

        return "\n".join(lines)

    def build_mermaid_from_plan(self, plan: dict) -> str:
        """
        从计划中提取/生成 Mermaid 流程图

        优先使用 LLM 生成的 mermaid_code，若不存在则根据 steps 自动生成。
        """
        # 优先使用 LLM 输出的 mermaid
        mermaid_code = plan.get("mermaid_code", "")
        if mermaid_code and ("graph" in mermaid_code or "flowchart" in mermaid_code):
            # 标准化: graph -> flowchart (兼容 mermaid v10+)
            mermaid_code = re.sub(
                r"^graph\s+(LR|RL|TD|TB|BT)",
                r"flowchart \1",
                mermaid_code.strip(),
            )
            return mermaid_code

        # 自动生成
        steps = plan.get("steps", [])
        if not steps:
            return ""

        lines = ["flowchart LR"]
        prev_id = "A"
        prev_label = "用户请求"

        for i, step in enumerate(steps):
            tool_name = step.get("tool_name", f"step{i+1}")
            cn_name = tool_cn(tool_name)
            node_id = chr(66 + i)  # B, C, D, ...
            if 66 + i > 90:
                break
            lines.append(f"    {prev_id}[{prev_label}] --> {node_id}[{cn_name}]")
            prev_id = node_id
            prev_label = cn_name

        end_id = chr(66 + min(len(steps), 25))
        lines.append(f"    {prev_id}[{prev_label}] --> {end_id}[生成报告]")

        return "\n".join(lines)
