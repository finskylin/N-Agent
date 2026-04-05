"""
Agent 能力评测框架 — 主评测编排器

流程：
1. 从 question_sets/ 加载题目
2. 并发（最多 2 题）调用 Agent /api/v1/chat/v4/send
3. 从 v4_skill_outputs 数据库查询工具调用轨迹
4. TraceEvaluator + JudgeEvaluator 两路评分
5. 汇总 EvalReport，保存 JSON + Markdown
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import aiosqlite
import httpx
from loguru import logger

from .judge_evaluator import JudgeEvaluator
from .models import (
    EvalQuestion,
    EvalReport,
    EvalResult,
    EvalRunConfig,
    StepwiseResult,
    ToolCallRecord,
    TraceScore,
    JudgeScore,
)
from .report import EvalReportGenerator
from .trace_evaluator import TraceEvaluator

_QUESTION_SETS_DIR = Path(__file__).parent / "question_sets"
_CONCURRENCY = 3       # 最多同时跑 3 题
_AGENT_TIMEOUT = 420   # 每题 Agent 调用超时（秒）


class EvalRunner:
    """主评测编排器"""

    def __init__(
        self,
        agent_base_url: str,
        results_dir: str,
        llm_provider=None,
    ):
        self._agent_base_url = agent_base_url.rstrip("/")
        self._results_dir = Path(results_dir)
        self._results_dir.mkdir(parents=True, exist_ok=True)
        self._trace_evaluator = TraceEvaluator()
        self._judge_evaluator = JudgeEvaluator(llm_provider)
        self._report_generator = EvalReportGenerator()

        # 数据库路径（从环境变量 DATABASE_URL 解析）
        self._db_path = self._resolve_db_path()

    def _resolve_db_path(self) -> Optional[Path]:
        """从 DATABASE_URL 解析 SQLite 数据库文件路径"""
        db_url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./agent.db")
        # 去掉 scheme 前缀，取文件路径部分
        if "sqlite" in db_url:
            # sqlite+aiosqlite:///./agent.db  →  ./agent.db
            # sqlite+aiosqlite:////abs/path/agent.db  →  /abs/path/agent.db
            parts = db_url.split("///", 1)
            if len(parts) == 2:
                raw_path = parts[1]
                if raw_path.startswith("/"):
                    return Path(raw_path)
                else:
                    # 相对路径，基于工作目录
                    return Path(raw_path)
        return None

    async def run(self, config: EvalRunConfig) -> EvalReport:
        """执行完整评测流程"""
        run_id = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        logger.info(f"[EvalRunner] Starting run {run_id}, set={config.question_set}")

        questions = self._load_questions(config.question_set, config.question_ids)
        if not questions:
            raise ValueError(f"No questions found in set '{config.question_set}'")

        logger.info(f"[EvalRunner] Loaded {len(questions)} question(s)")

        # 并发控制：最多 _CONCURRENCY 题同时执行
        semaphore = asyncio.Semaphore(_CONCURRENCY)
        tasks = [
            self._run_one_question(q, config.user_id, semaphore)
            for q in questions
        ]
        results: List[EvalResult] = await asyncio.gather(*tasks)

        # 过滤 None（理论上不会有，但 gather 时容错）
        valid_results = [r for r in results if r is not None]

        report = self._build_report(run_id, config, questions, valid_results)

        # 对比模式
        if config.compare_mode and config.baseline_run_id:
            report.compare = self._compare_with_baseline(
                valid_results, config.baseline_run_id
            )

        # 保存报告
        report = self._report_generator.save(report, self._results_dir)

        logger.info(
            f"[EvalRunner] Run {run_id} done: "
            f"avg_total={report.avg_total_score:.2f}, "
            f"report={report.report_path}"
        )
        return report

    async def _run_one_question(
        self,
        question: EvalQuestion,
        user_id: int,
        semaphore: asyncio.Semaphore,
    ) -> EvalResult:
        """单题执行（含容错）"""
        async with semaphore:
            start_time = time.time()
            logger.info(f"[EvalRunner] Running question {question.id}: {question.question[:60]}...")

            answer = ""
            session_id = str(uuid.uuid4())
            trace: List[ToolCallRecord] = []

            try:
                session_id, answer = await self._call_agent(
                    question.question, user_id, session_id
                )
            except Exception as e:
                logger.warning(f"[EvalRunner] Agent call failed for {question.id}: {e}")
                duration = time.time() - start_time
                return EvalResult(
                    question_id=question.id,
                    question=question.question,
                    answer="",
                    trace=[],
                    trace_score=TraceScore(
                        required_coverage=0.0,
                        expected_coverage=0.0,
                        data_citation=0.0,
                        hallucination_free=1.0,
                        total=0.0,
                    ),
                    judge_score=JudgeScore(
                        data_source=0.0,
                        logic_coherence=0.0,
                        conclusion_support=0.0,
                        total=0.0,
                        overall_comment="Agent 调用失败",
                    ),
                    total_score=0.0,
                    duration_seconds=round(duration, 2),
                    error=str(e),
                )

            # 查询轨迹（等待异步写入完成，最多重试 3 次）
            for _wait in [3, 3, 4]:
                await asyncio.sleep(_wait)
                try:
                    trace = await self._get_trace(session_id, user_id, start_time)
                except Exception as e:
                    logger.warning(f"[EvalRunner] Trace query failed for {question.id}: {e}")
                    break
                if trace:
                    break
                logger.debug(f"[EvalRunner] Trace empty, retrying...")

            # 两路评分
            trace_score = self._trace_evaluator.evaluate(question, trace, answer)

            try:
                judge_score = await self._judge_evaluator.evaluate(question, trace, answer)
            except Exception as e:
                logger.warning(f"[EvalRunner] Judge eval failed for {question.id}: {e}")
                judge_score = self._judge_evaluator._default_score()

            total_score = round(trace_score.total * 0.5 + judge_score.total * 0.5, 2)
            duration = time.time() - start_time

            logger.info(
                f"[EvalRunner] {question.id} done: "
                f"trace={trace_score.total:.1f}, judge={judge_score.total:.1f}, "
                f"total={total_score:.1f}, t={duration:.1f}s"
            )

            return EvalResult(
                question_id=question.id,
                question=question.question,
                answer=answer,
                trace=trace,
                trace_score=trace_score,
                judge_score=judge_score,
                total_score=total_score,
                duration_seconds=round(duration, 2),
            )

    async def _call_agent(
        self,
        question: str,
        user_id: int,
        session_id: str,
    ) -> Tuple[str, str]:
        """
        调用 Agent /api/v1/chat/v4/send

        Returns:
            (session_id, answer)
        """
        url = f"{self._agent_base_url}/api/v1/chat/v4/send"
        payload = {
            "message": question,
            "user_id": user_id,
            "render_mode": "auto",
            "skip_memory": True,     # eval 模式：跳过记忆/蒸馏/反思写入，避免污染知识库
        }

        async with httpx.AsyncClient(timeout=_AGENT_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()

        answer = data.get("text", data.get("response", ""))
        return session_id, answer

    async def _get_trace(
        self,
        session_id: str,
        user_id: int,
        started_at: float,
    ) -> List[ToolCallRecord]:
        """
        从 v4_skill_outputs 表查询本次会话的工具调用记录。

        使用 aiosqlite 直接查询（不 import app 层代码）。
        如果表不存在或数据库不可达，返回空列表。
        """
        if not self._db_path:
            logger.debug("[EvalRunner] No database path configured, skipping trace query")
            return []

        db_file = self._db_path
        if not db_file.is_absolute():
            # 相对路径相对于项目根目录
            project_root = Path(__file__).parent.parent.parent
            db_file = project_root / db_file

        if not db_file.exists():
            logger.debug(f"[EvalRunner] Database file not found: {db_file}")
            return []

        try:
            async with aiosqlite.connect(str(db_file)) as db:
                # 检查表是否存在
                cursor = await db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='v4_skill_outputs'"
                )
                if not await cursor.fetchone():
                    logger.debug("[EvalRunner] v4_skill_outputs table not found")
                    return []

                # 将 float timestamp 转为本地时间字符串（DB 存储本地时间）
                started_dt = datetime.fromtimestamp(started_at).strftime("%Y-%m-%d %H:%M:%S")

                # 按时间窗口查询（eval 串行执行，窗口内写入即为本次请求的工具调用）
                # 不过滤 user_id：skill_outputs 写入时统一用系统 user_id=1
                cursor = await db.execute(
                    """
                    SELECT skill_name, query, raw_data_json, executed_at
                    FROM v4_skill_outputs
                    WHERE executed_at > ?
                      AND skill_name != '__session_metadata__'
                    ORDER BY executed_at
                    """,
                    (started_dt,),
                )
                rows = await cursor.fetchall()

        except Exception as e:
            logger.warning(f"[EvalRunner] Trace DB query error: {e}")
            return []

        records: List[ToolCallRecord] = []
        for skill_name, query, raw_data_json, executed_at in rows:
            # skill_output = raw_data_json（原始工具输出，已是字符串）
            skill_output = raw_data_json or ""

            # skill_input: 用 query 字段重建
            skill_input: dict = {}
            if query:
                skill_input = {"query": query}

            # executed_at 转为 float（便于排序，但 DB 里是 datetime 字符串）
            try:
                ts = datetime.strptime(str(executed_at), "%Y-%m-%d %H:%M:%S.%f").timestamp()
            except ValueError:
                try:
                    ts = datetime.strptime(str(executed_at), "%Y-%m-%d %H:%M:%S").timestamp()
                except Exception:
                    ts = 0.0

            records.append(
                ToolCallRecord(
                    skill_name=str(skill_name),
                    skill_input=skill_input,
                    skill_output=skill_output,
                    created_at=ts,
                )
            )

        logger.debug(f"[EvalRunner] Found {len(records)} tool call(s) for session {session_id}")
        return records

    def _load_questions(
        self,
        question_set: str,
        question_ids: Optional[List[str]],
    ) -> List[EvalQuestion]:
        """加载问题集"""
        # 先查内置集，再查 custom 目录
        candidates = [
            _QUESTION_SETS_DIR / f"{question_set}.json",
            _QUESTION_SETS_DIR / "custom" / f"{question_set}.json",
        ]
        json_path: Optional[Path] = None
        for p in candidates:
            if p.exists():
                json_path = p
                break

        if not json_path:
            raise FileNotFoundError(
                f"Question set '{question_set}' not found. "
                f"Searched: {[str(c) for c in candidates]}"
            )

        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)

        questions = []
        for q in data.get("questions", []):
            eq = EvalQuestion(
                id=q["id"],
                category=q.get("category", ""),
                difficulty=q.get("difficulty", "medium"),
                question=q["question"],
                expected_tools=q.get("expected_tools", []),
                required_tools=q.get("required_tools", []),
                judge_focus=q.get("judge_focus", []),
                hallucination_check=q.get("hallucination_check", {"enabled": True}),
            )
            if question_ids is None or eq.id in question_ids:
                questions.append(eq)

        return questions

    def _build_report(
        self,
        run_id: str,
        config: EvalRunConfig,
        questions: List[EvalQuestion],
        results: List[EvalResult],
    ) -> EvalReport:
        """汇总评测报告"""
        valid = [r for r in results if r.error is None]

        avg_total = round(
            sum(r.total_score for r in valid) / len(valid), 2
        ) if valid else 0.0

        avg_trace = round(
            sum(r.trace_score.total for r in valid) / len(valid), 2
        ) if valid else 0.0

        avg_judge = round(
            sum(r.judge_score.total for r in valid) / len(valid), 2
        ) if valid else 0.0

        # 按分类汇总
        by_category: dict = {}
        for r in valid:
            q = next((q for q in questions if q.id == r.question_id), None)
            cat = q.category if q else "unknown"
            by_category.setdefault(cat, {"scores": [], "count": 0})
            by_category[cat]["scores"].append(r.total_score)
            by_category[cat]["count"] += 1
        for cat, v in by_category.items():
            v["avg"] = round(sum(v["scores"]) / v["count"], 2)
            del v["scores"]

        # 按难度汇总
        by_difficulty: dict = {}
        for r in valid:
            q = next((q for q in questions if q.id == r.question_id), None)
            diff = q.difficulty if q else "unknown"
            by_difficulty.setdefault(diff, {"scores": [], "count": 0})
            by_difficulty[diff]["scores"].append(r.total_score)
            by_difficulty[diff]["count"] += 1
        for diff, v in by_difficulty.items():
            v["avg"] = round(sum(v["scores"]) / v["count"], 2)
            del v["scores"]

        return EvalReport(
            run_id=run_id,
            question_set=config.question_set,
            total_questions=len(results),
            avg_total_score=avg_total,
            avg_trace_score=avg_trace,
            avg_judge_score=avg_judge,
            by_category=by_category,
            by_difficulty=by_difficulty,
            results=results,
            triggered_by=getattr(config, "triggered_by", "manual"),
        )

    def _compare_with_baseline(
        self,
        current_results: List[EvalResult],
        baseline_run_id: str,
    ) -> dict:
        """与历史基准对比"""
        baseline_path = self._results_dir / f"eval_{baseline_run_id}.json"
        if not baseline_path.exists():
            logger.warning(f"[EvalRunner] Baseline not found: {baseline_path}")
            return {"error": f"Baseline run {baseline_run_id} not found"}

        try:
            with open(baseline_path, encoding="utf-8") as f:
                baseline_data = json.load(f)
        except Exception as e:
            return {"error": f"Failed to load baseline: {e}"}

        baseline_scores: dict = {}
        for r in baseline_data.get("results", []):
            baseline_scores[r["question_id"]] = r.get("total_score", 0.0)

        current_scores = {r.question_id: r.total_score for r in current_results}

        improved = 0
        declined = 0
        unchanged = 0
        total_delta = 0.0
        compared_count = 0

        for qid, cur_score in current_scores.items():
            if qid not in baseline_scores:
                continue
            base_score = baseline_scores[qid]
            delta = cur_score - base_score
            total_delta += delta
            compared_count += 1
            if delta > 0.1:
                improved += 1
            elif delta < -0.1:
                declined += 1
            else:
                unchanged += 1

        avg_delta = round(total_delta / compared_count, 2) if compared_count else 0.0
        baseline_avg = baseline_data.get("avg_total_score", 0.0)
        current_avg = (
            sum(r.total_score for r in current_results) / len(current_results)
            if current_results else 0.0
        )

        return {
            "baseline_run_id": baseline_run_id,
            "baseline_avg_score": baseline_avg,
            "current_avg_score": round(current_avg, 2),
            "score_delta": avg_delta,
            "improved": improved,
            "declined": declined,
            "unchanged": unchanged,
            "compared_questions": compared_count,
        }

    # ── 按难度的回退阈值（可通过 env 覆盖） ──
    _ROLLBACK_THRESHOLDS = {
        "easy":   float(os.getenv("EVAL_ROLLBACK_THRESHOLD_EASY",   "-0.5")),
        "medium": float(os.getenv("EVAL_ROLLBACK_THRESHOLD_MEDIUM", "-0.8")),
        "hard":   float(os.getenv("EVAL_ROLLBACK_THRESHOLD_HARD",   "-1.0")),
        "avg":    float(os.getenv("EVAL_ROLLBACK_THRESHOLD",        "-0.5")),
    }

    def _check_rollback(
        self,
        baseline_results: "List[EvalResult]",
        current_results: "List[EvalResult]",
        questions: "List[EvalQuestion]",
    ) -> tuple:
        """
        按难度分组计算 delta，判断是否需要回退。

        规则（AND 逻辑：同时满足才触发）：
          - 任一难度组 avg_delta < 该组阈值
          - 且全局 avg_delta < avg 阈值

        返回: (should_rollback: bool, delta_detail: dict)
        """
        base_by_id = {r.question_id: r.total_score for r in baseline_results}
        curr_by_id = {r.question_id: r.total_score for r in current_results}
        q_by_id = {q.id: q for q in questions}

        # 按难度分组计算 delta
        diff_deltas: dict = {}
        all_deltas = []
        for qid, cur_score in curr_by_id.items():
            if qid not in base_by_id:
                continue
            delta = cur_score - base_by_id[qid]
            all_deltas.append(delta)
            diff = q_by_id.get(qid, None)
            diff_label = diff.difficulty if diff else "unknown"
            diff_deltas.setdefault(diff_label, []).append(delta)

        avg_delta = round(sum(all_deltas) / len(all_deltas), 3) if all_deltas else 0.0

        diff_avg: dict = {}
        triggered_by: list = []

        for diff_label, deltas in diff_deltas.items():
            group_avg = round(sum(deltas) / len(deltas), 3)
            diff_avg[diff_label] = group_avg
            threshold = self._ROLLBACK_THRESHOLDS.get(diff_label, -1.0)
            if group_avg < threshold:
                triggered_by.append(
                    f"{diff_label}: delta={group_avg:.3f} < threshold={threshold}"
                )

        # 全局 avg 也必须低于 avg 阈值才触发（防止单难度随机波动误触发）
        avg_threshold = self._ROLLBACK_THRESHOLDS["avg"]
        should_rollback = bool(triggered_by) and avg_delta < avg_threshold

        detail = {
            "avg_delta": avg_delta,
            "by_difficulty": diff_avg,
            "triggered_rules": triggered_by,
            "avg_threshold": avg_threshold,
        }
        return should_rollback, detail

    async def run_stepwise(
        self,
        user_id: int,
        question_set: str = "general",
        knowledge_store=None,
        instance_id: str = "",
        rollback_threshold: float = -0.5,  # 保留参数兼容，实际用 _ROLLBACK_THRESHOLDS
    ) -> EvalReport:
        """
        分步评测流程：
          Step 0 — Baseline 评测
          Step 1 — Post-Knowledge 评测（标记今日 distill/reflect 批次 + 评测）
          回退判定（按难度分组）：
            - 任一难度组 avg_delta 低于该组阈值（easy:-0.5 / medium:-0.8 / hard:-1.0）
            - 且全局 avg_delta < -0.5
            → 触发 soft-delete 回退该批次知识

        返回最终 EvalReport（含 stepwise_results + rollbacks）。
        """
        import time as _time
        from datetime import datetime as _dt

        stepwise: list[StepwiseResult] = []
        rollbacks: list[dict] = []

        # ── Step 0: Baseline ──
        baseline_config = EvalRunConfig(
            question_set=question_set,
            user_id=user_id,
            triggered_by="nightly_baseline",
        )
        logger.info("[EvalRunner/Stepwise] Phase 0: Baseline eval")
        baseline_report = await self.run(baseline_config)
        baseline_score = baseline_report.avg_total_score
        stepwise.append(StepwiseResult(
            step="baseline",
            score=baseline_score,
            run_id=baseline_report.run_id,
            timestamp=_time.time(),
            action="keep",
        ))
        logger.info(f"[EvalRunner/Stepwise] Baseline score={baseline_score:.2f}")

        # ── Step 1: Post-Knowledge 评测 ──
        today_str = _dt.now().strftime("%Y-%m-%d")
        batch_id = f"distill_{today_str}"
        prev_24h = _time.time() - 86400

        # 标记今天产出的 distill/reflect 知识
        if knowledge_store:
            try:
                async with knowledge_store._db._connect() as _db:
                    await knowledge_store._db._setup_conn(_db)
                    for _src_type in ("distill", "reflect"):
                        await _db.execute(
                            "UPDATE knowledge_units "
                            "SET source_batch_id = ? "
                            "WHERE source_type = ? "
                            "AND created_at > ? "
                            "AND source_batch_id IS NULL",
                            (batch_id, _src_type, prev_24h),
                        )
                    await _db.commit()
                logger.info(f"[EvalRunner/Stepwise] Labeled batch_id={batch_id}")
            except Exception as _e:
                logger.warning(f"[EvalRunner/Stepwise] Label batch failed: {_e}")

        logger.info("[EvalRunner/Stepwise] Phase 1: Post-Knowledge eval")
        pk_config = EvalRunConfig(
            question_set=question_set,
            user_id=user_id,
            triggered_by="nightly_knowledge",
        )
        # 加载题目（用于难度分组）
        try:
            _questions = self._load_questions(question_set, None)
        except Exception:
            _questions = []

        pk_report = await self.run(pk_config)
        pk_score = pk_report.avg_total_score

        # 按难度分组判断是否回退
        should_rollback, rollback_detail = self._check_rollback(
            baseline_report.results, pk_report.results, _questions,
        )
        avg_delta = rollback_detail.get("avg_delta", 0.0)
        action = "rollback" if should_rollback else "keep"

        stepwise.append(StepwiseResult(
            step="after_knowledge",
            score=pk_score,
            run_id=pk_report.run_id,
            timestamp=_time.time(),
            delta=avg_delta,
            action=action,
            detail={
                "knowledge_batch": batch_id,
                **rollback_detail,
            },
        ))
        logger.info(
            f"[EvalRunner/Stepwise] Post-knowledge score={pk_score:.2f}, "
            f"avg_delta={avg_delta:.3f}, action={action}, "
            f"detail={rollback_detail}"
        )

        # ── 回退执行 ──
        if should_rollback and knowledge_store:
            try:
                rolled_back = await knowledge_store.rollback_knowledge_batch(batch_id)
                rollbacks.append({
                    "type": "knowledge",
                    "batch_id": batch_id,
                    "units_rolled_back": rolled_back,
                    "reason": f"avg_delta={avg_delta:.3f}, triggered={rollback_detail.get('triggered_rules')}",
                })
                logger.warning(
                    f"[EvalRunner/Stepwise] ROLLBACK batch={batch_id}, units={rolled_back}, "
                    f"triggered={rollback_detail.get('triggered_rules')}"
                )
            except Exception as _re:
                logger.warning(f"[EvalRunner/Stepwise] Rollback failed: {_re}")

        # ── 汇总 ──
        pk_report.stepwise_results = stepwise
        pk_report.rollbacks = rollbacks if rollbacks else None
        pk_report.triggered_by = "nightly_stepwise"
        saved = self._report_generator.save(pk_report, self._results_dir)

        logger.info(
            f"[EvalRunner/Stepwise] Done: final_score={pk_score:.2f}, "
            f"rollbacks={len(rollbacks)}"
        )
        return saved
