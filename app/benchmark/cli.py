"""
GAIA Benchmark CLI 入口

用法:
    python -m app.benchmark.cli                          # 运行全部
    python -m app.benchmark.cli --level 1 --limit 10     # Level 1 前 10 题
    python -m app.benchmark.cli --concurrency 5          # 5 并发
    python -m app.benchmark.cli --task-ids abc123,def456  # 指定题目
    python -m app.benchmark.cli --dry-run                # 预览（不执行）
    python -m app.benchmark.cli --agent-url http://10.0.0.5:8000  # 指定 Agent 地址
"""
import argparse
import asyncio
import sys

from loguru import logger

from app.benchmark.models import BenchmarkRunConfig
from app.benchmark.runner import GaiaBenchmarkRunner


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GAIA Benchmark Runner")
    parser.add_argument(
        "--level",
        type=int,
        nargs="+",
        default=[1, 2, 3],
        help="GAIA difficulty levels to evaluate (1, 2, 3). Default: all.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of questions to evaluate.",
    )
    parser.add_argument(
        "--task-ids",
        type=str,
        default=None,
        help="Comma-separated list of specific task IDs to evaluate.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=3,
        help="Maximum concurrent requests. Default: 3.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Timeout per question in seconds. Default: 300.",
    )
    parser.add_argument(
        "--agent-url",
        type=str,
        default="http://localhost:8000",
        help="Agent service base URL. Default: http://localhost:8000",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="app/config/benchmark/gaia.json",
        help="Path to benchmark config file.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview questions without executing.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    task_ids = []
    if args.task_ids:
        task_ids = [tid.strip() for tid in args.task_ids.split(",") if tid.strip()]

    config = BenchmarkRunConfig(
        levels=args.level,
        limit=args.limit,
        task_ids=task_ids,
        max_concurrency=args.concurrency,
        timeout_per_question=args.timeout,
        agent_base_url=args.agent_url,
        dry_run=args.dry_run,
    )

    logger.info(f"[GAIA CLI] Config: levels={config.levels}, limit={config.limit}, "
                f"concurrency={config.max_concurrency}, dry_run={config.dry_run}")

    runner = GaiaBenchmarkRunner(config_path=args.config)
    report = await runner.run(config)

    # 输出结果摘要
    print("\n" + "=" * 60)
    print(f"GAIA Benchmark — {report.status}")
    print("=" * 60)
    print(f"Run ID:     {report.run_id}")
    print(f"Questions:  {report.total_questions}")
    print(f"Correct:    {report.total_correct}")
    print(f"Accuracy:   {report.overall_accuracy}%")
    print(f"Duration:   {report.duration_seconds}s")
    print(f"Report:     {report.report_path}")
    print(f"Results:    {report.results_path}")

    for level, stats in sorted(report.per_level.items()):
        print(f"  Level {level}: {stats['correct']}/{stats['total']} ({stats['accuracy']}%)")

    if report.errors:
        print(f"\nErrors: {len(report.errors)}")
        for e in report.errors[:5]:
            print(f"  - {e['task_id']}: {e['error'][:80]}")

    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
