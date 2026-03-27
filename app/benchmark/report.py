"""
GAIA 评测报告生成器

生成 JSON + Markdown 格式的评测报告，含排行榜对比和历史趋势。
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger

from app.benchmark.models import GaiaResult, BenchmarkRunReport


class GaiaBenchmarkReport:
    """生成评测报告"""

    def __init__(
        self,
        results_dir: str = "app/data/benchmark/results",
        leaderboard_path: str = "app/config/benchmark/gaia_leaderboard.json",
        report_filename_pattern: str = "gaia_report_{run_id}.md",
        results_filename_pattern: str = "gaia_results_{run_id}.json",
    ):
        self.results_dir = Path(results_dir)
        self.leaderboard_path = Path(leaderboard_path)
        self.report_filename_pattern = report_filename_pattern
        self.results_filename_pattern = results_filename_pattern

    def generate(
        self,
        run_id: str,
        results: list[GaiaResult],
        duration_seconds: float,
        status: str = "completed",
    ) -> BenchmarkRunReport:
        """
        生成评测报告。

        Args:
            run_id: 运行 ID
            results: 所有题目的评测结果
            duration_seconds: 总耗时（秒）
            status: 运行状态

        Returns:
            BenchmarkRunReport
        """
        self.results_dir.mkdir(parents=True, exist_ok=True)

        # 统计
        total = len(results)
        correct = sum(1 for r in results if r.correct)
        accuracy = correct / total if total > 0 else 0.0

        # 按 level 统计
        per_level: dict[int, dict] = {}
        for level in sorted({r.level for r in results}):
            level_results = [r for r in results if r.level == level]
            level_correct = sum(1 for r in level_results if r.correct)
            level_total = len(level_results)
            per_level[level] = {
                "total": level_total,
                "correct": level_correct,
                "accuracy": level_correct / level_total if level_total > 0 else 0.0,
            }

        # 收集错误
        errors = []
        for r in results:
            if r.error:
                errors.append({"task_id": r.task_id, "error": r.error})

        # 文件路径
        report_filename = self.report_filename_pattern.format(run_id=run_id)
        results_filename = self.results_filename_pattern.format(run_id=run_id)
        report_path = str(self.results_dir / report_filename)
        results_path = str(self.results_dir / results_filename)

        # 保存 JSON 结果
        self._save_json_results(results_path, run_id, results, per_level, duration_seconds, status)

        # 生成 Markdown 报告
        self._save_markdown_report(
            report_path, run_id, results, per_level,
            total, correct, accuracy, duration_seconds, errors, status,
        )

        report = BenchmarkRunReport(
            run_id=run_id,
            status=status,
            total_questions=total,
            total_correct=correct,
            overall_accuracy=round(accuracy * 100, 2),
            per_level={
                k: {**v, "accuracy": round(v["accuracy"] * 100, 2)}
                for k, v in per_level.items()
            },
            duration_seconds=round(duration_seconds, 2),
            report_path=report_path,
            results_path=results_path,
            errors=errors,
        )

        logger.info(
            f"[GAIA Report] Generated: accuracy={report.overall_accuracy}%, "
            f"report={report_path}"
        )
        return report

    def _save_json_results(
        self,
        path: str,
        run_id: str,
        results: list[GaiaResult],
        per_level: dict,
        duration_seconds: float,
        status: str,
    ) -> None:
        """保存 JSON 格式的详细结果"""
        data = {
            "run_id": run_id,
            "status": status,
            "timestamp": datetime.now().isoformat(),
            "duration_seconds": round(duration_seconds, 2),
            "summary": {
                "total": len(results),
                "correct": sum(1 for r in results if r.correct),
                "accuracy": round(
                    sum(1 for r in results if r.correct) / len(results) * 100, 2
                ) if results else 0.0,
            },
            "per_level": {
                str(k): {**v, "accuracy": round(v["accuracy"] * 100, 2)}
                for k, v in per_level.items()
            },
            "results": [r.model_dump() for r in results],
        }
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _save_markdown_report(
        self,
        path: str,
        run_id: str,
        results: list[GaiaResult],
        per_level: dict,
        total: int,
        correct: int,
        accuracy: float,
        duration_seconds: float,
        errors: list[dict],
        status: str,
    ) -> None:
        """生成 Markdown 格式报告"""
        duration_str = self._format_duration(duration_seconds)
        lines: list[str] = []

        # Header
        lines.append("# GAIA Benchmark Report\n")
        lines.append(f"**Run ID**: {run_id}  |  **Duration**: {duration_str}  |  **Status**: {status}\n")

        # Summary table
        lines.append("## Summary\n")
        lines.append("| Level | Total | Correct | Accuracy |")
        lines.append("|-------|-------|---------|----------|")
        for level in sorted(per_level.keys()):
            lv = per_level[level]
            lines.append(
                f"| Level {level} | {lv['total']} | {lv['correct']} | "
                f"**{lv['accuracy'] * 100:.1f}%** |"
            )
        lines.append(
            f"| **Overall** | **{total}** | **{correct}** | **{accuracy * 100:.1f}%** |"
        )
        lines.append("")

        # Leaderboard ranking
        leaderboard_section = self._generate_leaderboard_section(per_level, accuracy)
        if leaderboard_section:
            lines.append(leaderboard_section)

        # Detailed results
        lines.append("## Detailed Results\n")
        lines.append("| # | Task ID | Level | Correct | Latency | Question (truncated) |")
        lines.append("|---|---------|-------|---------|---------|---------------------|")
        for i, r in enumerate(results, 1):
            status_mark = "Y" if r.correct else "N"
            q_short = r.question[:60].replace("|", "\\|") + ("..." if len(r.question) > 60 else "")
            lines.append(
                f"| {i} | {r.task_id[:12]}... | {r.level} | {status_mark} | "
                f"{r.latency_ms:.0f}ms | {q_short} |"
            )
        lines.append("")

        # Errors section
        if errors:
            lines.append("## Errors\n")
            for e in errors:
                lines.append(f"- **{e['task_id']}**: {e['error']}")
            lines.append("")

        # Historical trend
        trend_section = self._generate_trend_section(run_id)
        if trend_section:
            lines.append(trend_section)

        Path(path).write_text("\n".join(lines), encoding="utf-8")

    def _generate_leaderboard_section(
        self,
        per_level: dict,
        overall_accuracy: float,
    ) -> Optional[str]:
        """生成排行榜对比"""
        if not self.leaderboard_path.exists():
            return None

        try:
            lb_data = json.loads(self.leaderboard_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[GAIA Report] Failed to load leaderboard: {e}")
            return None

        entries = lb_data.get("entries", [])
        if not entries:
            return None

        # 构建本次结果条目
        our_entry = {
            "agent": "STHG Agent (this run)",
            "level1": per_level.get(1, {}).get("accuracy", 0) * 100,
            "level2": per_level.get(2, {}).get("accuracy", 0) * 100,
            "level3": per_level.get(3, {}).get("accuracy", 0) * 100,
            "average": overall_accuracy * 100,
        }

        # 合并排序
        all_entries = entries + [our_entry]
        all_entries.sort(key=lambda x: x["average"], reverse=True)

        lines = [
            "## Leaderboard Ranking\n",
            "| Rank | Agent | Level 1 | Level 2 | Level 3 | Average |",
            "|------|-------|---------|---------|---------|---------|",
        ]

        for i, entry in enumerate(all_entries, 1):
            is_ours = entry["agent"] == "STHG Agent (this run)"
            marker = "**->**" if is_ours else str(i)
            agent_name = f"**{entry['agent']}**" if is_ours else entry["agent"]
            lines.append(
                f"| {marker} | {agent_name} | {entry['level1']:.1f} | "
                f"{entry['level2']:.1f} | {entry['level3']:.1f} | "
                f"**{entry['average']:.1f}** |"
            )

        lines.append("")
        lines.append(f"> Ranking based on GAIA Leaderboard ({lb_data.get('source', 'N/A')})\n")
        return "\n".join(lines)

    def _generate_trend_section(self, current_run_id: str) -> Optional[str]:
        """生成历史趋势对比"""
        if not self.results_dir.exists():
            return None

        # 收集历史 JSON 结果
        history = []
        for f in sorted(self.results_dir.glob("gaia_results_*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                history.append(data)
            except Exception:
                continue

        if len(history) < 2:
            return None

        lines = [
            "## Historical Trend\n",
            "| Run ID | Date | Overall | L1 | L2 | L3 | Delta |",
            "|--------|------|---------|----|----|----|----- |",
        ]

        prev_accuracy = None
        for entry in history:
            rid = entry.get("run_id", "?")
            ts = entry.get("timestamp", "")[:10]
            overall = entry.get("summary", {}).get("accuracy", 0)
            pl = entry.get("per_level", {})
            l1 = pl.get("1", {}).get("accuracy", 0)
            l2 = pl.get("2", {}).get("accuracy", 0)
            l3 = pl.get("3", {}).get("accuracy", 0)

            delta = ""
            if prev_accuracy is not None:
                diff = overall - prev_accuracy
                delta = f"+{diff:.1f}%" if diff >= 0 else f"{diff:.1f}%"
            prev_accuracy = overall

            lines.append(
                f"| {rid[:20]} | {ts} | {overall:.1f}% | {l1:.1f} | {l2:.1f} | {l3:.1f} | {delta} |"
            )

        lines.append("")
        return "\n".join(lines)

    def list_results(self) -> list[dict]:
        """列出所有历史评测结果"""
        if not self.results_dir.exists():
            return []

        results = []
        for f in sorted(self.results_dir.glob("gaia_results_*.json"), reverse=True):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                results.append({
                    "run_id": data.get("run_id"),
                    "timestamp": data.get("timestamp"),
                    "status": data.get("status"),
                    "summary": data.get("summary"),
                })
            except Exception:
                continue
        return results

    def get_result(self, run_id: str) -> Optional[dict]:
        """获取特定评测的完整结果"""
        filename = self.results_filename_pattern.format(run_id=run_id)
        path = self.results_dir / filename
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """格式化时长"""
        if seconds < 60:
            return f"{seconds:.1f}s"
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        if minutes < 60:
            return f"{minutes}m {secs}s"
        hours = minutes // 60
        mins = minutes % 60
        return f"{hours}h {mins}m {secs}s"
