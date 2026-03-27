"""
GAIA Benchmark Runner — 编排器

加载数据集 → 注入提示词 → 分发请求 → 提取答案 → 评分 → 生成报告
"""
import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

from loguru import logger

from app.benchmark.models import (
    BenchmarkRunConfig,
    BenchmarkRunReport,
    GaiaQuestion,
    GaiaResult,
)
from app.benchmark.gaia_dataset import GaiaDatasetLoader
from app.benchmark.dispatcher import GaiaDispatcher
from app.benchmark.answer_extractor import extract_final_answer
from app.benchmark.scorer import question_scorer
from app.benchmark.report import GaiaBenchmarkReport


class GaiaBenchmarkRunner:
    """GAIA 评测编排器"""

    def __init__(self, config_path: str = "app/config/benchmark/gaia.json"):
        self.config_path = Path(config_path)
        self._config = self._load_config()

    def _load_config(self) -> dict:
        """加载评测配置"""
        if not self.config_path.exists():
            logger.warning(f"[GAIA Runner] Config not found: {self.config_path}, using defaults")
            return {}
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def _load_system_instruction(self) -> str:
        """加载 GAIA 系统提示词"""
        from app.prompts.loader import load_prompt
        prompt_name = self._config.get("prompt", {}).get(
            "system_instruction_template", "gaia_system_instruction"
        )
        instruction = load_prompt(prompt_name)
        if not instruction:
            logger.warning("[GAIA Runner] System instruction prompt is empty, using default")
            instruction = (
                "You are a helpful assistant. Answer the question and finish with:\n"
                "FINAL ANSWER: [YOUR ANSWER]"
            )
        return instruction

    def _format_message(self, instruction: str, question: GaiaQuestion) -> str:
        """构建发送给 Agent 的消息"""
        template = self._config.get("prompt", {}).get(
            "message_template", "{system_instruction}\n\nQuestion: {question}"
        )
        return template.format(
            system_instruction=instruction,
            question=question.question,
        )

    def _get_timeout_for_level(self, level: int, default_timeout: int) -> int:
        """获取指定 level 的超时时间"""
        overrides = self._config.get("execution", {}).get("timeout_level_overrides", {})
        return overrides.get(str(level), default_timeout)

    async def run(self, run_config: BenchmarkRunConfig) -> BenchmarkRunReport:
        """
        执行 GAIA 评测。

        Args:
            run_config: 运行配置

        Returns:
            BenchmarkRunReport
        """
        run_id = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        logger.info(f"[GAIA Runner] Starting run {run_id}")

        # 1. 加载数据集
        ds_config = self._config.get("dataset", {})
        loader = GaiaDatasetLoader(
            base_path=ds_config.get("base_path", "app/data/benchmark/gaia"),
            split=ds_config.get("split", "validation"),
            metadata_file=ds_config.get("metadata_file", "metadata.jsonl"),
        )
        questions = loader.load(
            levels=run_config.levels,
            limit=run_config.limit,
            task_ids=run_config.task_ids or None,
        )

        if not questions:
            logger.warning("[GAIA Runner] No questions matched the filter criteria")
            report_gen = self._make_report_generator()
            return report_gen.generate(run_id, [], 0.0, status="completed")

        # 2. Dry run — 只预览，不执行
        if run_config.dry_run:
            logger.info(f"[GAIA Runner] Dry run: {len(questions)} questions would be evaluated")
            report_gen = self._make_report_generator()
            dry_results = [
                GaiaResult(
                    task_id=q.task_id,
                    level=q.level,
                    question=q.question,
                    ground_truth=q.final_answer,
                    correct=False,
                )
                for q in questions
            ]
            return report_gen.generate(run_id, dry_results, 0.0, status="dry_run")

        # 3. 加载提示词
        instruction = self._load_system_instruction()

        # 4. 初始化分发器
        exec_config = self._config.get("execution", {})
        dispatcher = GaiaDispatcher(
            base_url=run_config.agent_base_url or exec_config.get(
                "agent_base_url", "http://localhost:8000"
            ),
            chat_endpoint=exec_config.get("chat_endpoint", "/api/v1/chat/v4/send"),
            max_concurrency=run_config.max_concurrency,
            timeout=run_config.timeout_per_question,
            retry_count=exec_config.get("retry_count", 1),
            retry_delay=exec_config.get("retry_delay_seconds", 5.0),
            benchmark_user_id=self._config.get("benchmark_user_id", 99999),
        )

        # 5. 分发并评测
        scoring_config = self._config.get("scoring", {})
        marker = scoring_config.get("final_answer_marker", "FINAL ANSWER:")
        case_insensitive = scoring_config.get("case_insensitive_marker_search", True)

        start_time = time.monotonic()
        results = await self._evaluate_all(
            questions, instruction, dispatcher, marker, case_insensitive,
        )
        duration = time.monotonic() - start_time

        # 6. 生成报告
        report_gen = self._make_report_generator()
        report = report_gen.generate(run_id, results, duration, status="completed")

        logger.info(
            f"[GAIA Runner] Run {run_id} completed: "
            f"{report.total_correct}/{report.total_questions} correct "
            f"({report.overall_accuracy}%)"
        )
        return report

    async def _evaluate_all(
        self,
        questions: list[GaiaQuestion],
        instruction: str,
        dispatcher: GaiaDispatcher,
        marker: str,
        case_insensitive: bool,
    ) -> list[GaiaResult]:
        """并发评测所有题目"""
        tasks = [
            self._evaluate_single(q, instruction, dispatcher, marker, case_insensitive)
            for q in questions
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        final_results = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                q = questions[i]
                final_results.append(GaiaResult(
                    task_id=q.task_id,
                    level=q.level,
                    question=q.question,
                    ground_truth=q.final_answer,
                    correct=False,
                    error=str(r),
                ))
            else:
                final_results.append(r)
        return final_results

    async def _evaluate_single(
        self,
        question: GaiaQuestion,
        instruction: str,
        dispatcher: GaiaDispatcher,
        marker: str,
        case_insensitive: bool,
    ) -> GaiaResult:
        """评测单道题"""
        formatted_message = self._format_message(instruction, question)

        logger.info(
            f"[GAIA Runner] Dispatching task {question.task_id} (L{question.level}): "
            f"{question.question[:60]}..."
        )

        # 发送请求
        resp = await dispatcher.dispatch(question, formatted_message)

        # 提取答案
        extracted = None
        if resp["text"]:
            extracted = extract_final_answer(
                resp["text"], marker=marker, case_insensitive=case_insensitive,
            )

        # 评分
        correct = question_scorer(extracted, question.final_answer)

        result = GaiaResult(
            task_id=question.task_id,
            level=question.level,
            question=question.question,
            ground_truth=question.final_answer,
            model_answer=resp["text"],
            extracted_answer=extracted,
            correct=correct,
            latency_ms=resp["latency_ms"],
            error=resp["error"],
        )

        status = "CORRECT" if correct else "WRONG"
        logger.info(
            f"[GAIA Runner] {status} task {question.task_id}: "
            f"extracted='{extracted}' truth='{question.final_answer}'"
        )
        return result

    def _make_report_generator(self) -> GaiaBenchmarkReport:
        """创建报告生成器"""
        output_config = self._config.get("output", {})
        return GaiaBenchmarkReport(
            results_dir=output_config.get("results_dir", "app/data/benchmark/results"),
            leaderboard_path="app/config/benchmark/gaia_leaderboard.json",
            report_filename_pattern=output_config.get(
                "report_filename_pattern", "gaia_report_{run_id}.md"
            ),
            results_filename_pattern=output_config.get(
                "results_filename_pattern", "gaia_results_{run_id}.json"
            ),
        )
