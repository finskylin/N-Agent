"""
Agent 能力评测框架 — 报告生成器

输出格式：
- JSON: results_dir/eval_{run_id}.json
- Markdown: results_dir/eval_{run_id}.md
"""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import EvalReport, EvalResult


def _to_dict(obj: Any) -> Any:
    """递归将 dataclass 转为 dict（便于 JSON 序列化）"""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _to_dict(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, list):
        return [_to_dict(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _to_dict(v) for k, v in obj.items()}
    return obj


class EvalReportGenerator:
    """评测报告生成器"""

    def save(self, report: EvalReport, results_dir: Path) -> EvalReport:
        """保存 JSON 和 Markdown 报告，返回更新了路径的 EvalReport"""
        results_dir.mkdir(parents=True, exist_ok=True)

        json_path = results_dir / f"eval_{report.run_id}.json"
        md_path = results_dir / f"eval_{report.run_id}.md"

        # 保存 JSON
        report_dict = _to_dict(report)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(report_dict, f, ensure_ascii=False, indent=2)

        # 保存 Markdown
        md_content = self._to_markdown(report)
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_content)

        report.results_path = str(json_path)
        report.report_path = str(md_path)
        return report

    def _to_markdown(self, report: EvalReport) -> str:
        """生成 Markdown 报告"""
        lines = []

        # 标题
        lines.append("# Agent 能力评测报告\n")
        lines.append(f"**Run ID：** `{report.run_id}`")
        lines.append(f"**时间：** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"**问题集：** {report.question_set}")
        lines.append(f"**题目数：** {report.total_questions}")
        lines.append("")

        # 总体得分
        lines.append("## 总体得分\n")
        lines.append("| 维度 | 得分 |")
        lines.append("|------|------|")
        lines.append(f"| 综合得分 | **{report.avg_total_score:.2f} / 10** |")
        lines.append(f"| 轨迹得分（客观） | {report.avg_trace_score:.2f} / 10 |")
        lines.append(f"| 裁判得分（主观） | {report.avg_judge_score:.2f} / 10 |")
        lines.append("")

        # 对比信息
        if report.compare:
            cmp = report.compare
            if "error" in cmp:
                lines.append(f"## 对比基准\n\n> {cmp['error']}\n")
            else:
                delta = cmp.get("score_delta", 0)
                delta_str = f"+{delta:.2f}" if delta >= 0 else f"{delta:.2f}"
                baseline_avg = cmp.get("baseline_avg_score", 0)
                lines.append(f"## 对比基准（vs `{cmp.get('baseline_run_id', '')}`）\n")
                lines.append(f"- 基准得分：{baseline_avg:.2f}")
                lines.append(f"- 当前得分：{cmp.get('current_avg_score', 0):.2f}")
                lines.append(f"- 综合变化：**{delta_str}**")
                lines.append(f"- 提升：{cmp.get('improved', 0)} 题 / 下降：{cmp.get('declined', 0)} 题 / 持平：{cmp.get('unchanged', 0)} 题")
                lines.append("")

        # 分类得分
        if report.by_category:
            lines.append("## 按分类得分\n")
            lines.append("| 分类 | 平均分 | 题目数 |")
            lines.append("|------|--------|--------|")
            for cat, v in sorted(report.by_category.items()):
                lines.append(f"| {cat} | {v['avg']:.2f} | {v['count']} |")
            lines.append("")

        # 按难度得分
        if report.by_difficulty:
            lines.append("## 按难度得分\n")
            lines.append("| 难度 | 平均分 | 题目数 |")
            lines.append("|------|--------|--------|")
            for diff, v in sorted(report.by_difficulty.items()):
                lines.append(f"| {diff} | {v['avg']:.2f} | {v['count']} |")
            lines.append("")

        # 逐题结果
        lines.append("## 逐题结果\n")
        for r in report.results:
            lines.append(f"### [{r.question_id}] {r.question}")
            lines.append("")

            if r.error:
                lines.append(f"> **执行失败：** {r.error}")
                lines.append("")
                continue

            lines.append("**得分**")
            lines.append("")
            lines.append("| 维度 | 得分 |")
            lines.append("|------|------|")
            lines.append(f"| 综合 | **{r.total_score:.2f}** |")
            lines.append(f"| 轨迹（客观） | {r.trace_score.total:.2f} |")
            lines.append(f"| 裁判（主观） | {r.judge_score.total:.2f} |")
            lines.append("")

            # 轨迹评分详情
            ts = r.trace_score
            lines.append("**轨迹评分明细**")
            lines.append("")
            lines.append("| 指标 | 分值 |")
            lines.append("|------|------|")
            lines.append(f"| 必选工具覆盖率 | {ts.required_coverage:.2%} |")
            lines.append(f"| 期望工具覆盖率 | {ts.expected_coverage:.2%} |")
            lines.append(f"| 数据引用率 | {ts.data_citation:.2%} |")
            lines.append(f"| 幻觉洁净度 | {ts.hallucination_free:.2%} |")

            if ts.detail.get("called_tools"):
                lines.append(f"\n调用工具：`{'`, `'.join(ts.detail['called_tools'])}`")
            lines.append("")

            # 裁判评分理由
            js = r.judge_score
            lines.append("**裁判评分理由**")
            lines.append("")
            lines.append("| 维度 | 得分 | 理由 |")
            lines.append("|------|------|------|")
            reasons = js.reasons or {}
            lines.append(f"| 数据来源 | {js.data_source:.1f} | {reasons.get('data_source', '')} |")
            lines.append(f"| 逻辑自洽 | {js.logic_coherence:.1f} | {reasons.get('logic_coherence', '')} |")
            lines.append(f"| 结论支撑 | {js.conclusion_support:.1f} | {reasons.get('conclusion_support', '')} |")

            if js.overall_comment:
                lines.append(f"\n**总评：** {js.overall_comment}")
            lines.append("")

            # 回答摘要（前 300 字）
            answer_preview = (r.answer or "")[:300].replace("\n", " ")
            if answer_preview:
                lines.append(f"**回答摘要：** {answer_preview}{'...' if len(r.answer) > 300 else ''}")
            lines.append("")
            lines.append(f"*耗时：{r.duration_seconds:.1f}s*")
            lines.append("")
            lines.append("---")
            lines.append("")

        return "\n".join(lines)
