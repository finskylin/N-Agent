"""
PredictionScheduler — 预测验证 + 策略学习的定时调度器

在 app 启动时由 native_agent 注册到 CronService。
所有任务均按 (user_id, instance_id) 独立执行，不跨用户合并。
register_jobs() 是幂等的，重启后重复调用不会创建重复任务。
"""
from __future__ import annotations

import os
import time
from typing import Dict, Optional

from loguru import logger


class PredictionScheduler:
    """预测验证 + 策略学习定时调度器"""

    def __init__(
        self,
        prediction_verifier,
        strategy_learner,
        cron_service,
        config: dict = None,
        learn_evaluator=None,
        evolution_manager=None,
    ):
        """
        Args:
            prediction_verifier: PredictionVerifier 实例
            strategy_learner: StrategyLearner 实例
            cron_service: app/services/cron_service.py CronService 实例
            config: 配置字典（来自 V4Config 或 .env）
            learn_evaluator: LearnEvaluator 实例（可选，用于准确率快照对比）
            evolution_manager: EvolutionTaskManager 实例（可选，准确率下降时触发进化）
        """
        self._verifier = prediction_verifier
        self._learner = strategy_learner
        self._cron = cron_service
        self._config = config or {}
        self._evaluator = learn_evaluator
        self._evolution_manager = evolution_manager

    def register_jobs(self, user_id: int, instance_id: str) -> None:
        """
        为指定用户注册两个定时任务。
        幂等：若任务已存在则跳过。

        任务 ID：
          pred_verify_{user_id}_{instance_id}
          strategy_learn_{user_id}_{instance_id}
        """
        verify_job_id = f"pred_verify_{user_id}_{instance_id}"
        learn_job_id = f"strategy_learn_{user_id}_{instance_id}"

        verify_cron = os.getenv("PREDICTION_VERIFY_CRON", "*/30 * * * *")
        learn_cron = os.getenv("STRATEGY_LEARN_CRON", "0 2 * * 1")

        # 注册验证任务（默认每 30 分钟，可通过 PREDICTION_VERIFY_CRON 覆盖）
        self._register_once(
            job_id=verify_job_id,
            cron_expr=verify_cron,
            description=f"预测验证 user={user_id}",
            callback=lambda: self.run_verify_cycle(user_id, instance_id),
        )

        # 注册学习任务（每周一 02:00）
        self._register_once(
            job_id=learn_job_id,
            cron_expr=learn_cron,
            description=f"策略学习 user={user_id}",
            callback=lambda: self.run_learn_cycle(user_id, instance_id),
        )

    def _register_once(
        self,
        job_id: str,
        cron_expr: str,
        description: str,
        callback,
    ) -> None:
        """幂等注册：已存在的任务 ID 跳过（在已有 event loop 里异步执行）"""
        import asyncio

        async def _do_register():
            try:
                existing = await self._cron.list_jobs()
                existing_ids = {j.job_id for j in existing} if existing else set()
                if job_id in existing_ids:
                    logger.debug(f"[PredictionScheduler] Job already registered: {job_id}")
                    return
                await self._cron.add_job(
                    job_id=job_id,
                    cron_expr=cron_expr,
                    callback=callback,
                    description=description,
                )
                logger.info(f"[PredictionScheduler] Registered job: {job_id} ({cron_expr})")
            except Exception as e:
                logger.warning(f"[PredictionScheduler] Failed to register job {job_id}: {e}")

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(_do_register())
            else:
                loop.run_until_complete(_do_register())
        except Exception as e:
            logger.warning(f"[PredictionScheduler] Failed to register job {job_id}: {e}")

    async def run_overdue_check(self, user_id: int, instance_id: str) -> int:
        """
        立即检查是否有已到期的 pending 预测，有则触发验证。
        在每次对话 prepare_session 时异步调用，无需等待 cron。
        返回验证条数（0 表示无到期记录）。
        """
        try:
            pending = await self._verifier._store.get_pending(user_id, instance_id)
            if not pending:
                return 0
            logger.info(
                f"[PredictionScheduler] {len(pending)} overdue predictions found, "
                f"triggering immediate verify for user={user_id}"
            )
            return await self._verifier.run_pending_verifications(
                user_id, instance_id, max_batch=10
            )
        except Exception as e:
            logger.debug(f"[PredictionScheduler] run_overdue_check error: {e}")
            return 0

    async def run_verify_cycle(self, user_id: int, instance_id: str) -> Dict:
        """
        执行一次完整验证周期：
        1. PredictionVerifier.run_pending_verifications()
        2. 检查是否触发 StrategyLearner（错误率告警 or 样本数足够）
        3. 返回执行摘要
        """
        logger.info(f"[PredictionScheduler] Starting verify cycle user={user_id}")
        start = time.time()

        verified_count = 0
        try:
            verified_count = await self._verifier.run_pending_verifications(
                user_id, instance_id
            )
        except Exception as e:
            logger.warning(f"[PredictionScheduler] Verify cycle error: {e}")

        # 验证完成后立即触发增量学习（有新验证数据时）
        learn_result = None
        if verified_count > 0:
            try:
                learn_result = await self._learner.run_incremental(
                    user_id=user_id,
                    instance_id=instance_id,
                    triggered_by="post_verify",
                )
            except Exception as e:
                logger.warning(f"[PredictionScheduler] Incremental learn error: {e}")

        elapsed = round(time.time() - start, 1)
        summary = {
            "user_id": user_id,
            "instance_id": instance_id,
            "verified_count": verified_count,
            "weight_updates": learn_result.graph_weight_updates if learn_result else 0,
            "new_rules": len(learn_result.new_rules) if learn_result else 0,
            "elapsed_s": elapsed,
        }
        logger.info(f"[PredictionScheduler] Verify cycle done: {summary}")
        return summary

    async def run_learn_cycle(self, user_id: int, instance_id: str) -> Dict:
        """
        执行一次策略学习周期：
        1. StrategyLearner.run()
        2. 若配置开启报告，调用 send_message 发送给用户
        3. 返回学习摘要
        """
        logger.info(f"[PredictionScheduler] Starting learn cycle user={user_id}")
        start = time.time()

        result = None
        try:
            result = await self._learner.run(
                user_id=user_id,
                instance_id=instance_id,
                triggered_by="schedule",
            )
        except Exception as e:
            logger.warning(f"[PredictionScheduler] Learn cycle error: {e}")
            return {"user_id": user_id, "error": str(e)}

        # 可选：发送学习报告
        send_report = os.getenv("STRATEGY_LEARN_SEND_REPORT", "true").lower() in ("true", "1")
        if send_report and result and result.verified_count > 0:
            await self._send_report(user_id, instance_id, result)

        # 学习完成后：对比 before/after 准确率快照，下降时触发进化任务
        if self._evaluator and result and result.verified_count > 0:
            try:
                after_snapshot = await self._evaluator.take_snapshot(
                    user_id, instance_id, snapshot_type="post_learn",
                    learn_cycle_id=f"full_{int(time.time())}",
                    triggered_by="schedule",
                    new_rules_count=len(result.new_rules),
                )
                previous = await self._evaluator.get_previous_post_snapshot(
                    user_id, instance_id,
                )
                if previous and previous.accuracy_rate > 0:
                    degradation = previous.accuracy_rate - after_snapshot.accuracy_rate
                    threshold = float(os.getenv("EVAL_DEGRADATION_THRESHOLD", "0.1"))
                    if degradation > threshold and self._evolution_manager:
                        gap = (
                            f"准确率从 {previous.accuracy_rate:.0%} 下降到 "
                            f"{after_snapshot.accuracy_rate:.0%}，"
                            f"需要重新归因分析策略"
                        )
                        await self._evolution_manager.create_task(
                            gap, user_id, instance_id,
                        )
                        logger.warning(
                            f"[PredictionScheduler] Accuracy degradation detected "
                            f"({degradation:+.2%}), evolution task created"
                        )
            except Exception as e:
                logger.debug(f"[PredictionScheduler] Post-learn eval error: {e}")

        elapsed = round(time.time() - start, 1)
        summary = {
            "user_id": user_id,
            "instance_id": instance_id,
            "verified_count": result.verified_count if result else 0,
            "new_rules": len(result.new_rules) if result else 0,
            "weight_updates": result.graph_weight_updates if result else 0,
            "elapsed_s": elapsed,
        }
        logger.info(f"[PredictionScheduler] Learn cycle done: {summary}")
        return summary

    async def _send_report(self, user_id: int, instance_id: str, result) -> None:
        """通过 send_message skill 发送学习报告"""
        try:
            report_md = await self._learner.generate_report(user_id, instance_id, result)
            if not report_md.strip():
                return

            # 通过环境变量调用 send_message skill（技能层解耦）
            import httpx
            base_url = os.getenv("AGENT_SERVICE_BASE_URL", "http://localhost:8000")
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    f"{base_url}/internal/send_report",
                    json={
                        "user_id": user_id,
                        "instance_id": instance_id,
                        "content": report_md,
                        "msg_type": "markdown",
                        "title": "Agent 自学习周报",
                    },
                )
        except Exception as e:
            logger.debug(f"[PredictionScheduler] Failed to send report: {e}")
