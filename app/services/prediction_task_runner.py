"""
PredictionTaskRunner — app 层胶水代码

负责构建 PredictionVerifier / StrategyLearner / LearnEvaluator 所需的依赖，
供 CronService 的 on_job 回调直接调用，无需走 Agent 问答流程。

遵循分层约束：
- 本文件属于 app 层，可以 import agent_core
- 不得被 agent_core 或 skills 层引用

调用隔离原则：
- LearnEvaluator 仅在本文件中构建和调用
- strategy_learner.py 不持有 evaluator 引用
- 问答链路不触发任何评测代码
"""
from __future__ import annotations

import os
from uuid import uuid4
from typing import Dict

from loguru import logger


def _eval_enabled() -> bool:
    return os.getenv("LEARN_EVAL_ENABLED", "true").lower() in ("true", "1")


async def _build_components(user_id: int, instance_id: str):
    """
    构建执行一次验证/学习所需的组件。
    复用 V4NativeAgent 已初始化的 SQLite DB 和 LLM provider。
    """
    from agent_core.config import V4Config
    from agent_core.session.context_db import SessionContextDB
    from agent_core.agentloop.llm_provider import call_llm
    from agent_core.knowledge.prediction_store import PredictionStore
    from agent_core.knowledge.prediction_verifier import PredictionVerifier
    from agent_core.knowledge.strategy_learner import StrategyLearner
    from agent_core.knowledge.graph_store import GraphStore

    config = V4Config.from_env()

    # 复用同一个 SQLite 数据库文件
    db_path = config.sqlite_db_path_template.format(instance_id=instance_id or config.instance_id)
    sqlite_db = SessionContextDB(
        db_path=db_path,
        wal_mode=config.sqlite_wal_mode,
        busy_timeout_ms=config.sqlite_busy_timeout_ms,
    )

    pred_store = PredictionStore(sqlite_db)
    graph_store = GraphStore(sqlite_db)

    # KnowledgeStore（用于 StrategyLearner 写规则）
    ke_store = None
    try:
        from agent_core.knowledge.config_loader import load_knowledge_config
        from agent_core.knowledge.store import KnowledgeStore
        ke_config = load_knowledge_config(config.knowledge_engine_config_path)
        ke_store = KnowledgeStore(sqlite_db, ke_config)
    except Exception as e:
        logger.debug(f"[PredictionTaskRunner] KnowledgeStore init skipped: {e}")

    verifier = PredictionVerifier(
        prediction_store=pred_store,
        llm_call=call_llm,
    )
    learner = StrategyLearner(
        prediction_store=pred_store,
        knowledge_store=ke_store,
        graph_store=graph_store,
        llm_call=call_llm,
    )

    # LearnEvaluator — 仅在 cron 任务中使用，不影响问答链路
    evaluator = None
    if _eval_enabled():
        try:
            from agent_core.knowledge.learn_evaluator import LearnEvaluator
            evaluator = LearnEvaluator(
                prediction_store=pred_store,
                knowledge_store=ke_store,
                sqlite_db=sqlite_db,
                llm_call=call_llm,
                enabled=True,
            )
        except Exception as e:
            logger.debug(f"[PredictionTaskRunner] LearnEvaluator init skipped: {e}")

    return verifier, learner, pred_store, evaluator


async def run_verify_cycle(user_id: int, instance_id: str) -> Dict:
    """
    执行一次预测验证周期（大窗口，供凌晨全量任务调用）。
    验证完成后检查是否触发全量策略学习。
    注意：验证周期不触发评测，评测仅在学习周期中执行。
    """
    logger.info(f"[PredictionTaskRunner] run_verify_cycle user={user_id} instance={instance_id}")
    try:
        verifier, learner, pred_store, _evaluator = await _build_components(user_id, instance_id)

        # 解析该 instance 下的真实 user_id 列表（避免 default_user_id=1 与飞书用户不匹配）
        real_user_ids = await pred_store.get_distinct_user_ids(instance_id)
        if not real_user_ids:
            real_user_ids = [user_id]
        logger.info(f"[PredictionTaskRunner] real user_ids={real_user_ids} for instance={instance_id}")

        verified_count = 0
        for _uid in real_user_ids:
            verified_count += await verifier.run_pending_verifications(_uid, instance_id)

        learn_triggered = False
        if verified_count > 0:
            min_samples = int(os.getenv("STRATEGY_LEARN_MIN_SAMPLES", "5"))
            error_rate_alert = float(os.getenv("STRATEGY_LEARN_ERROR_RATE_ALERT", "0.5"))
            for real_uid in real_user_ids:
                try:
                    summary = await pred_store.get_accuracy_summary(real_uid, instance_id)
                    total_verified = summary["correct"] + summary["wrong"]
                    error_rate = summary["wrong"] / total_verified if total_verified > 0 else 0.0
                    if total_verified >= min_samples or error_rate > error_rate_alert:
                        logger.info(
                            f"[PredictionTaskRunner] Triggering full learn uid={real_uid}: "
                            f"verified={total_verified}, error_rate={error_rate:.2f}"
                        )
                        await run_learn_cycle(real_uid, instance_id)
                        learn_triggered = True
                except Exception as e:
                    logger.debug(f"[PredictionTaskRunner] learn trigger check uid={real_uid}: {e}")

        return {"verified_count": verified_count, "learn_triggered": learn_triggered}
    except Exception as e:
        logger.error(f"[PredictionTaskRunner] run_verify_cycle failed: {e}")
        return {"error": str(e)}


async def run_incremental_cycle(user_id: int, instance_id: str) -> Dict:
    """
    执行一次滑动窗口增量学习周期（30 分钟任务调用）。

    流程：
    1. 验证所有 verify_before <= now 的 pending 预测
    2. 按 subject 分组做增量归因（run_incremental）
    3. 记录评测快照（纯 SQL，不跑 baseline）
    """
    logger.info(f"[PredictionTaskRunner] run_incremental_cycle user={user_id} instance={instance_id}")
    try:
        verifier, learner, pred_store, evaluator = await _build_components(user_id, instance_id)
        learn_cycle_id = f"lc_{uuid4().hex[:8]}"

        # 解析该 instance 下的真实 user_id 列表（避免 default_user_id=1 与飞书用户不匹配）
        real_user_ids = await pred_store.get_distinct_user_ids(instance_id)
        if not real_user_ids:
            real_user_ids = [user_id]
        logger.info(f"[PredictionTaskRunner] real user_ids={real_user_ids} for instance={instance_id}")

        # 2. 验证到期预测（遍历所有真实 user_id）
        verified_count = 0
        for _uid in real_user_ids:
            verified_count += await verifier.run_pending_verifications(_uid, instance_id)
        logger.info(f"[PredictionTaskRunner] Incremental verify: {verified_count} records verified")

        # 3. 增量学习 + 评测（遍历所有真实 user_id）
        window_size = int(os.getenv("INCREMENTAL_WINDOW_SIZE", "20"))
        llm_trigger_count = int(os.getenv("INCREMENTAL_LLM_TRIGGER_COUNT", "3"))
        run_baseline = os.getenv("EVAL_INCREMENTAL_RUN_BASELINE", "false").lower() in ("true", "1")

        total_new_rules = 0
        total_weight_updates = 0
        total_learned_count = 0
        eval_dict = None

        for real_uid in real_user_ids:
            # PRE-LEARN 快照
            if evaluator:
                try:
                    await evaluator.take_snapshot(
                        real_uid, instance_id, "pre_learn", learn_cycle_id, "incremental_cron",
                    )
                except Exception as e:
                    logger.debug(f"[PredictionTaskRunner] Pre-learn snapshot uid={real_uid}: {e}")

            result = await learner.run_incremental(
                user_id=real_uid,
                instance_id=instance_id,
                triggered_by="incremental_cron",
                window_size=window_size,
                llm_trigger_count=llm_trigger_count,
            )
            total_new_rules += len(result.new_rules)
            total_weight_updates += result.graph_weight_updates
            total_learned_count += result.verified_count

            # POST-LEARN 快照 + 对比
            if evaluator:
                try:
                    post_snapshot = await evaluator.take_snapshot(
                        real_uid, instance_id, "post_learn", learn_cycle_id, "incremental_cron",
                        new_rules_count=len(result.new_rules),
                    )
                    baseline_result = None
                    if run_baseline:
                        baseline_result = await evaluator.run_baseline_check(real_uid, instance_id)
                    eval_result = await evaluator.compare_with_previous(
                        real_uid, instance_id, post_snapshot, baseline_result,
                    )
                    eval_dict = eval_result.to_dict()
                    if eval_result.alert_level == "warning":
                        logger.warning(
                            f"[PredictionTaskRunner] Incremental eval warning uid={real_uid}: {eval_result.alert_reason}"
                        )
                    elif eval_result.alert_level == "critical":
                        logger.error(
                            f"[PredictionTaskRunner] Incremental eval CRITICAL uid={real_uid}: {eval_result.alert_reason}"
                        )
                except Exception as e:
                    logger.debug(f"[PredictionTaskRunner] Incremental eval uid={real_uid}: {e}")

        summary = {
            "verified_count": verified_count,
            "learned_count": total_learned_count,
            "new_rules": total_new_rules,
            "weight_updates": total_weight_updates,
            "eval": eval_dict,
        }
        logger.info(f"[PredictionTaskRunner] Incremental cycle done: {summary}")
        return summary
    except Exception as e:
        logger.error(f"[PredictionTaskRunner] run_incremental_cycle failed: {e}")
        return {"error": str(e)}


async def run_learn_cycle(user_id: int, instance_id: str) -> Dict:
    """
    执行一次全量策略学习周期，含完整评测流程。

    评测流程（仅 Cron 链路执行，零侵入问答）：
    1. PRE-LEARN 快照（纯 SQL）
    2. 学习（现有逻辑不变）
    3. POST-LEARN 快照 + 基础评测 + 滚动对比
    4. 自动提炼基础评测用例
    """
    logger.info(f"[PredictionTaskRunner] run_learn_cycle user={user_id} instance={instance_id}")
    try:
        verifier, learner, pred_store, evaluator = await _build_components(user_id, instance_id)
        learn_cycle_id = f"lc_{uuid4().hex[:8]}"

        # 解析该 instance 下的真实 user_id 列表
        real_user_ids = await pred_store.get_distinct_user_ids(instance_id)
        if not real_user_ids:
            real_user_ids = [user_id]
        logger.info(f"[PredictionTaskRunner] run_learn_cycle real user_ids={real_user_ids}")

        eval_dict = None
        send_report = os.getenv("STRATEGY_LEARN_SEND_REPORT", "true").lower() in ("true", "1")
        agg = {"verified_count": 0, "correct_count": 0, "wrong_count": 0, "new_rules": 0, "weight_updates": 0}

        for real_uid in real_user_ids:
            # 1. PRE-LEARN 快照（纯 SQL，无 LLM）
            if evaluator:
                try:
                    await evaluator.take_snapshot(
                        real_uid, instance_id, "pre_learn", learn_cycle_id, "schedule",
                    )
                except Exception as e:
                    logger.debug(f"[PredictionTaskRunner] Pre-learn snapshot uid={real_uid}: {e}")

            # 2. 学习
            result = await learner.run(user_id=real_uid, instance_id=instance_id, triggered_by="schedule")

            # 3. POST-LEARN 评测
            if evaluator:
                try:
                    eval_result = await evaluator.evaluate_learn_cycle(
                        real_uid, instance_id, result, learn_cycle_id, "schedule",
                    )
                    eval_dict = eval_result.to_dict()
                    logger.info(
                        f"[PredictionTaskRunner] Eval uid={real_uid}: delta={eval_result.accuracy_delta:+.3f}, "
                        f"baseline={'PASS' if eval_result.baseline_pass else 'FAIL'}, "
                        f"alert={eval_result.alert_level}"
                    )
                    if eval_result.alert_level == "critical":
                        logger.error(f"[PredictionTaskRunner] CRITICAL uid={real_uid}: {eval_result.alert_reason}")

                    # 全量学习后自动补充基础评测用例
                    new_cases = await evaluator.auto_extract_baselines(real_uid, instance_id)
                    if new_cases > 0:
                        logger.info(f"[PredictionTaskRunner] Auto-extracted {new_cases} baseline cases uid={real_uid}")
                except Exception as e:
                    logger.warning(f"[PredictionTaskRunner] Learn eval uid={real_uid}: {e}")

            # 4. 报告生成（可选）
            if send_report and result.verified_count > 0:
                try:
                    report_md = await learner.generate_report(real_uid, instance_id, result)
                    logger.info(f"[PredictionTaskRunner] Learning report generated uid={real_uid} ({len(report_md)} chars)")
                except Exception as rpt_err:
                    logger.debug(f"[PredictionTaskRunner] Report generation uid={real_uid}: {rpt_err}")

            # 累计汇总
            agg["verified_count"] += result.verified_count
            agg["correct_count"] += result.correct_count
            agg["wrong_count"] += result.wrong_count
            agg["new_rules"] += len(result.new_rules)
            agg["weight_updates"] += result.graph_weight_updates

        accuracy = agg["correct_count"] / agg["verified_count"] if agg["verified_count"] > 0 else 0.0
        return {
            **agg,
            "accuracy_rate": accuracy,
            "eval": eval_dict,
        }
    except Exception as e:
        logger.error(f"[PredictionTaskRunner] run_learn_cycle failed: {e}")
        return {"error": str(e)}


async def register_prediction_jobs(cron_service, user_id: int, instance_id: str) -> None:
    """
    在 CronService 中注册预测验证和策略学习定时任务。
    幂等：按任务名去重（CronService.add_job 已按 name 覆盖）。
    """
    from app.services.cron_service import CronSchedule, CronPayload

    verify_cron = os.getenv("PREDICTION_VERIFY_CRON", "0 1 * * *")
    learn_cron = os.getenv("STRATEGY_LEARN_CRON", "0 2 * * 1")
    incremental_cron = os.getenv("PREDICTION_INCREMENTAL_CRON", "*/30 * * * *")

    verify_name = f"pred_verify_{user_id}_{instance_id}"
    learn_name = f"strategy_learn_{user_id}_{instance_id}"
    incremental_name = f"pred_incremental_{user_id}_{instance_id}"

    try:
        await cron_service.add_job(
            name=verify_name,
            schedule=CronSchedule(kind="cron", cron_expr=verify_cron),
            payload=CronPayload(
                message="",
                callback={
                    "type": "prediction_verify",
                    "user_id": user_id,
                    "instance_id": instance_id,
                },
            ),
        )
        logger.info(f"[PredictionTaskRunner] Registered verify job: {verify_name} ({verify_cron})")
    except Exception as e:
        logger.debug(f"[PredictionTaskRunner] Verify job register failed: {e}")

    try:
        await cron_service.add_job(
            name=learn_name,
            schedule=CronSchedule(kind="cron", cron_expr=learn_cron),
            payload=CronPayload(
                message="",
                callback={
                    "type": "strategy_learn",
                    "user_id": user_id,
                    "instance_id": instance_id,
                },
            ),
        )
        logger.info(f"[PredictionTaskRunner] Registered learn job: {learn_name} ({learn_cron})")
    except Exception as e:
        logger.debug(f"[PredictionTaskRunner] Learn job register failed: {e}")

    try:
        await cron_service.add_job(
            name=incremental_name,
            schedule=CronSchedule(kind="cron", cron_expr=incremental_cron),
            payload=CronPayload(
                message="",
                callback={
                    "type": "prediction_incremental",
                    "user_id": user_id,
                    "instance_id": instance_id,
                },
            ),
        )
        logger.info(f"[PredictionTaskRunner] Registered incremental job: {incremental_name} ({incremental_cron})")
    except Exception as e:
        logger.debug(f"[PredictionTaskRunner] Incremental job register failed: {e}")
