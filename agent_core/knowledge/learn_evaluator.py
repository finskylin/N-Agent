"""
LearnEvaluator - 学习有效性评测器

两层保障机制:
1. 滚动准确率快照对比 - 每次学习前后记录准确率，跨周期对比
2. 基础评测集回归验证 - 确保学习不破坏基础能力

调用隔离原则:
- 仅在 prediction_task_runner.py (app层 cron) 中构建和调用
- strategy_learner.py 不持有本模块引用
- 问答链路不触发任何评测代码
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Dict, List, Optional, Callable

from loguru import logger

from .models import LearnSnapshot, BaselineCase, EvalResult


class LearnEvaluator:
    """学习有效性评测器"""

    _BASELINE_PROMPT = """\
你是一个预测策略评估器。以下是系统当前掌握的分析规则：

{strategy_rules}

基于以上规则，回答以下问题：
问题：{question}
主体：{subject}

请判断方向（仅回答 up/down/stable/other 之一）和理由（20字以内）。
输出 JSON: {{"direction": "...", "reason": "..."}}"""

    _EXTRACT_PROMPT = """\
将以下预测文本改写为一个简短的疑问句（15字以内），保留核心观点方向：

预测文本：{prediction_text}
主体：{subject}
方向：{direction}

仅输出疑问句，不要其他内容。"""

    def __init__(
        self,
        prediction_store,
        knowledge_store,
        sqlite_db,
        llm_call: Callable,
        enabled: bool = True,
    ):
        self._pred_store = prediction_store
        self._ke_store = knowledge_store
        self._db = sqlite_db
        self._llm_call = llm_call
        self._enabled = enabled

        # 可配置参数
        self._window_days = int(os.getenv("EVAL_WINDOW_DAYS", "7"))
        self._min_samples = int(os.getenv("EVAL_MIN_SAMPLES", "3"))
        self._degradation_threshold = float(os.getenv("EVAL_DEGRADATION_THRESHOLD", "0.1"))
        self._baseline_min_score = float(os.getenv("EVAL_BASELINE_MIN_SCORE", "0.6"))
        self._max_baseline_per_subject = int(os.getenv("EVAL_MAX_BASELINE_PER_SUBJECT", "10"))

    # ──── 快照 ────

    async def take_snapshot(
        self,
        user_id: int,
        instance_id: str,
        snapshot_type: str,
        learn_cycle_id: str,
        triggered_by: str,
        new_rules_count: int = 0,
    ) -> LearnSnapshot:
        """记录当前准确率快照（纯 SQL，无 LLM 调用）"""
        if not self._enabled:
            return LearnSnapshot(snapshot_type=snapshot_type, learn_cycle_id=learn_cycle_id)

        # 1. 查询时间窗口内的分 subject 准确率
        accuracy_data = await self._pred_store.get_accuracy_by_time_window(
            user_id, instance_id, window_days=self._window_days,
        )

        overall = accuracy_data.get("overall", {})
        by_subject = accuracy_data.get("by_subject", {})

        # 2. 统计活跃 strategy_rule 数
        active_rules_count = 0
        if self._ke_store:
            try:
                active_rules_count = await self._count_active_rules(user_id, instance_id)
            except Exception as e:
                logger.debug(f"[LearnEvaluator] count rules failed: {e}")

        # 3. 构建快照
        snapshot = LearnSnapshot(
            user_id=user_id,
            instance_id=instance_id,
            snapshot_type=snapshot_type,
            learn_cycle_id=learn_cycle_id,
            triggered_by=triggered_by,
            total_verified=overall.get("verified", 0),
            correct_count=overall.get("correct", 0),
            wrong_count=overall.get("verified", 0) - overall.get("correct", 0),
            accuracy_rate=overall.get("accuracy", 0.0),
            subject_stats=by_subject,
            active_rules_count=active_rules_count,
            new_rules_count=new_rules_count,
        )

        # 4. 写入数据库
        await self._save_snapshot(snapshot)
        logger.info(
            f"[LearnEvaluator] Snapshot {snapshot_type}: accuracy={snapshot.accuracy_rate:.3f}, "
            f"verified={snapshot.total_verified}, rules={active_rules_count}"
        )
        return snapshot

    async def get_previous_post_snapshot(
        self,
        user_id: int,
        instance_id: str,
    ) -> Optional[LearnSnapshot]:
        """获取最近一次 post_learn 快照"""
        await self._db._ensure_init()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            async with db.execute(
                "SELECT * FROM learn_snapshots "
                "WHERE instance_id=? AND user_id=? AND snapshot_type='post_learn' "
                "ORDER BY created_at DESC LIMIT 1",
                (instance_id, user_id),
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        return self._row_to_snapshot(dict(row))

    # ──── 基础评测 ────

    async def run_baseline_check(
        self,
        user_id: int,
        instance_id: str,
    ) -> Dict:
        """
        运行基础评测集。

        返回: {"total": N, "pass": M, "fail": K, "score": M/N, "cases": [...]}
        """
        if not self._enabled:
            return {"total": 0, "pass": 0, "fail": 0, "score": 1.0, "cases": []}

        cases = await self._get_active_baseline_cases(user_id, instance_id)
        if not cases:
            logger.info("[LearnEvaluator] No baseline cases, skipping check")
            return {"total": 0, "pass": 0, "fail": 0, "score": 1.0, "cases": []}

        pass_count = 0
        fail_count = 0
        case_results = []

        for case in cases:
            try:
                # 检索相关 strategy_rules
                rules_text = await self._get_strategy_rules_text(
                    user_id, instance_id, case["subject"]
                )

                # 构造 prompt 调用 LLM
                prompt = self._BASELINE_PROMPT.format(
                    strategy_rules=rules_text or "(无已有规则)",
                    question=case["question"],
                    subject=case["subject"],
                )

                llm_response = await self._llm_call(
                    messages=[{"role": "user", "content": prompt}],
                    model_preference="small_fast",
                )

                # 解析方向
                predicted_direction = self._parse_direction(llm_response)
                expected = case["expected_direction"]
                passed = predicted_direction == expected

                if passed:
                    pass_count += 1
                else:
                    fail_count += 1

                case_results.append({
                    "case_id": case["case_id"],
                    "subject": case["subject"],
                    "expected": expected,
                    "predicted": predicted_direction,
                    "pass": passed,
                })
            except Exception as e:
                fail_count += 1
                case_results.append({
                    "case_id": case.get("case_id", "?"),
                    "subject": case.get("subject", "?"),
                    "error": str(e),
                    "pass": False,
                })

        total = pass_count + fail_count
        score = pass_count / total if total > 0 else 1.0

        logger.info(
            f"[LearnEvaluator] Baseline check: {pass_count}/{total} passed, score={score:.2f}"
        )
        return {
            "total": total,
            "pass": pass_count,
            "fail": fail_count,
            "score": round(score, 3),
            "cases": case_results,
        }

    async def auto_extract_baselines(
        self,
        user_id: int,
        instance_id: str,
        max_per_subject: int = None,
    ) -> int:
        """从高置信预测中自动提炼基础评测用例，返回新增数量"""
        if not self._enabled:
            return 0

        max_per_subject = max_per_subject or self._max_baseline_per_subject

        # 获取高置信已验证记录
        records = await self._pred_store.get_high_confidence_verified(
            user_id, instance_id, min_accuracy=0.9, limit=50,
        )
        if not records:
            return 0

        # 按 subject 分组
        by_subject: Dict[str, List[Dict]] = {}
        for r in records:
            subj = r.get("subject", "")
            if subj:
                by_subject.setdefault(subj, []).append(r)

        # 查询每个 subject 已有的 baseline_cases 数量
        existing_counts = await self._count_baseline_by_subject(user_id, instance_id)

        new_count = 0
        for subject, recs in by_subject.items():
            existing = existing_counts.get(subject, 0)
            slots = max_per_subject - existing
            if slots <= 0:
                continue

            # 去重: 排除已经从同一 pred_id 提炼过的
            existing_pred_ids = await self._get_existing_source_pred_ids(
                user_id, instance_id, subject
            )

            for rec in recs[:slots]:
                pred_id = rec.get("pred_id", "")
                if pred_id in existing_pred_ids:
                    continue

                try:
                    # LLM 轻量改写为疑问句
                    question = await self._rewrite_to_question(
                        rec.get("prediction_text", ""),
                        subject,
                        rec.get("direction", ""),
                    )
                    if not question:
                        continue

                    case = BaselineCase(
                        user_id=user_id,
                        instance_id=instance_id,
                        subject=subject,
                        category=self._infer_category(subject),
                        question=question,
                        expected_direction=rec.get("direction", "other"),
                        expected_keywords=self._extract_keywords(
                            rec.get("actual_outcome", "")
                        ),
                        source="auto_extracted",
                        source_pred_id=pred_id,
                    )
                    await self._save_baseline_case(case)
                    new_count += 1
                except Exception as e:
                    logger.debug(f"[LearnEvaluator] Extract baseline failed: {e}")

        if new_count > 0:
            logger.info(f"[LearnEvaluator] Auto-extracted {new_count} baseline cases")
        return new_count

    # ──── 对比评估 ────

    async def compare_with_previous(
        self,
        user_id: int,
        instance_id: str,
        curr_post: LearnSnapshot,
        baseline_result: Dict = None,
    ) -> EvalResult:
        """与上次 post_learn 快照对比，返回 EvalResult"""
        prev_post = await self.get_previous_post_snapshot(user_id, instance_id)

        prev_accuracy = prev_post.accuracy_rate if prev_post else 0.0
        curr_accuracy = curr_post.accuracy_rate
        accuracy_delta = curr_accuracy - prev_accuracy

        # 按 subject 计算 delta
        subject_deltas: Dict[str, float] = {}
        if prev_post and prev_post.subject_stats:
            for subj, curr_stat in curr_post.subject_stats.items():
                prev_stat = prev_post.subject_stats.get(subj, {})
                prev_acc = prev_stat.get("accuracy", 0.0)
                curr_acc = curr_stat.get("accuracy", 0.0)
                subject_deltas[subj] = round(curr_acc - prev_acc, 4)

        # 基础评测结果
        baseline_pass = True
        baseline_score = 1.0
        if baseline_result:
            baseline_score = baseline_result.get("score", 1.0)
            baseline_pass = baseline_score >= self._baseline_min_score

        # 判定告警等级
        alert_level, alert_reason = self._determine_alert(
            accuracy_delta, subject_deltas, baseline_pass, curr_post.total_verified,
        )

        overall_pass = baseline_pass and accuracy_delta >= -self._degradation_threshold

        result = EvalResult(
            learn_cycle_id=curr_post.learn_cycle_id,
            triggered_by=curr_post.triggered_by,
            prev_accuracy=round(prev_accuracy, 4),
            curr_accuracy=round(curr_accuracy, 4),
            accuracy_delta=round(accuracy_delta, 4),
            subject_deltas=subject_deltas,
            baseline_pass=baseline_pass,
            baseline_score=round(baseline_score, 3),
            overall_pass=overall_pass,
            alert_level=alert_level,
            alert_reason=alert_reason,
        )
        return result

    # ──── 一站式 ────

    async def evaluate_learn_cycle(
        self,
        user_id: int,
        instance_id: str,
        learn_result,
        learn_cycle_id: str,
        triggered_by: str,
    ) -> EvalResult:
        """
        学习完成后的一站式评测。

        1. take_snapshot("post_learn")
        2. run_baseline_check()
        3. compare_with_previous()
        4. 返回 EvalResult
        """
        if not self._enabled:
            return EvalResult(learn_cycle_id=learn_cycle_id, triggered_by=triggered_by)

        new_rules_count = len(learn_result.new_rules) if hasattr(learn_result, "new_rules") else 0

        # 1. POST-LEARN 快照
        post_snapshot = await self.take_snapshot(
            user_id, instance_id, "post_learn",
            learn_cycle_id, triggered_by,
            new_rules_count=new_rules_count,
        )

        # 2. 基础评测
        baseline_result = await self.run_baseline_check(user_id, instance_id)

        # 更新快照中的 baseline 字段
        post_snapshot.baseline_pass = baseline_result.get("score", 1.0) >= self._baseline_min_score
        post_snapshot.baseline_score = baseline_result.get("score", 1.0)
        post_snapshot.baseline_detail = baseline_result
        await self._update_snapshot_baseline(post_snapshot)

        # 3. 滚动对比
        eval_result = await self.compare_with_previous(
            user_id, instance_id, post_snapshot, baseline_result,
        )
        return eval_result

    # ──── 内部方法 ────

    def _determine_alert(
        self,
        accuracy_delta: float,
        subject_deltas: Dict[str, float],
        baseline_pass: bool,
        total_verified: int,
    ) -> tuple:
        """判定告警等级"""
        # 样本不足时不触发告警
        if total_verified < self._min_samples:
            return "none", ""

        # critical: 基础能力退化
        if not baseline_pass:
            return "critical", "基础评测未通过，学习可能导致基础能力退化"

        # critical: 多主体同时大幅下降
        subjects_degraded = [s for s, d in subject_deltas.items() if d < -self._degradation_threshold]
        if accuracy_delta < -self._degradation_threshold and len(subjects_degraded) >= 3:
            return "critical", (
                f"准确率下降 {accuracy_delta:+.3f}，"
                f"{len(subjects_degraded)} 个主体同时退化: {', '.join(subjects_degraded[:5])}"
            )

        # warning: 任何程度的下降
        if accuracy_delta < 0:
            return "warning", f"准确率下降 {accuracy_delta:+.3f}"

        # warning: 某个 subject 大幅下降
        severe_subject = [(s, d) for s, d in subject_deltas.items() if d < -0.2]
        if severe_subject:
            subj, delta = severe_subject[0]
            return "warning", f"主体 {subj} 准确率大幅下降 {delta:+.3f}"

        return "none", ""

    async def _count_active_rules(self, user_id: int, instance_id: str) -> int:
        """统计活跃的 strategy_rule 知识单元数"""
        await self._db._ensure_init()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            async with db.execute(
                "SELECT COUNT(*) FROM knowledge_units "
                "WHERE user_id=? AND instance_id=? AND category='strategy_rule' "
                "AND valid_until IS NULL",
                (user_id, instance_id),
            ) as cur:
                row = await cur.fetchone()
        return row[0] if row else 0

    async def _get_strategy_rules_text(
        self, user_id: int, instance_id: str, subject: str
    ) -> str:
        """获取与 subject 相关的 strategy_rules 文本"""
        await self._db._ensure_init()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            async with db.execute(
                "SELECT text FROM knowledge_units "
                "WHERE user_id=? AND instance_id=? AND category='strategy_rule' "
                "AND valid_until IS NULL AND text LIKE ? "
                "ORDER BY created_at DESC LIMIT 10",
                (user_id, instance_id, f"%{subject}%"),
            ) as cur:
                rows = await cur.fetchall()
        if not rows:
            return ""
        return "\n".join(f"- {row[0]}" for row in rows)

    def _parse_direction(self, llm_response: str) -> str:
        """从 LLM 响应中解析方向"""
        text = llm_response.strip()
        # 尝试 JSON 解析
        try:
            obj = json.loads(text)
            return obj.get("direction", "other").lower()
        except (json.JSONDecodeError, AttributeError):
            pass
        # 尝试从 ```json``` 提取
        if "```json" in text:
            start = text.index("```json") + 7
            end = text.index("```", start) if "```" in text[start:] else len(text)
            try:
                obj = json.loads(text[start:end].strip())
                return obj.get("direction", "other").lower()
            except (json.JSONDecodeError, ValueError):
                pass
        # 关键词匹配
        text_lower = text.lower()
        for direction in ["up", "down", "stable"]:
            if direction in text_lower:
                return direction
        return "other"

    async def _rewrite_to_question(
        self, prediction_text: str, subject: str, direction: str
    ) -> str:
        """用 LLM 将预测文本改写为疑问句"""
        prompt = self._EXTRACT_PROMPT.format(
            prediction_text=prediction_text[:200],
            subject=subject,
            direction=direction,
        )
        try:
            result = await self._llm_call(
                messages=[{"role": "user", "content": prompt}],
                model_preference="small_fast",
            )
            question = result.strip().strip('"').strip("'")
            if len(question) > 50:
                question = question[:50]
            return question
        except Exception as e:
            logger.debug(f"[LearnEvaluator] rewrite failed: {e}")
            # 降级: 直接截断
            return f"{subject}未来走势如何？"

    @staticmethod
    def _infer_category(subject: str) -> str:
        """从 subject 推断类别"""
        # 简单规则兜底
        if any(kw in subject for kw in ["股", "指数", "板块", "A股", "基金"]):
            return "stock"
        if any(kw in subject for kw in ["军", "武器", "战", "舰", "机"]):
            return "military"
        if any(kw in subject for kw in ["国", "政", "外交", "制裁"]):
            return "geopolitical"
        return "general"

    @staticmethod
    def _extract_keywords(outcome_text: str) -> List[str]:
        """从实际结果文本提取关键词"""
        if not outcome_text:
            return []
        # 取前 100 字，按标点分割后取前 5 个非空段
        text = outcome_text[:100]
        segments = []
        for sep in ["，", ",", "；", ";", "。", ".", "、"]:
            text = text.replace(sep, "|")
        parts = [p.strip() for p in text.split("|") if p.strip()]
        return parts[:5]

    # ──── 数据库操作 ────

    async def _save_snapshot(self, snapshot: LearnSnapshot) -> None:
        """写入 learn_snapshots 表"""
        await self._db._ensure_init()
        d = snapshot.to_dict()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            await db.execute(
                """INSERT OR REPLACE INTO learn_snapshots
                   (snapshot_id, user_id, instance_id, snapshot_type, learn_cycle_id,
                    triggered_by, total_verified, correct_count, wrong_count,
                    accuracy_rate, subject_stats, active_rules_count, new_rules_count,
                    baseline_pass, baseline_score, baseline_detail, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (d["snapshot_id"], d["user_id"], d["instance_id"],
                 d["snapshot_type"], d["learn_cycle_id"], d["triggered_by"],
                 d["total_verified"], d["correct_count"], d["wrong_count"],
                 d["accuracy_rate"], d["subject_stats"], d["active_rules_count"],
                 d["new_rules_count"], d["baseline_pass"], d["baseline_score"],
                 d["baseline_detail"], d["created_at"]),
            )
            await db.commit()

    async def _update_snapshot_baseline(self, snapshot: LearnSnapshot) -> None:
        """更新快照的 baseline 字段"""
        await self._db._ensure_init()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            await db.execute(
                "UPDATE learn_snapshots SET baseline_pass=?, baseline_score=?, baseline_detail=? "
                "WHERE snapshot_id=?",
                (
                    1 if snapshot.baseline_pass else 0,
                    snapshot.baseline_score,
                    json.dumps(snapshot.baseline_detail, ensure_ascii=False),
                    snapshot.snapshot_id,
                ),
            )
            await db.commit()

    async def _get_active_baseline_cases(
        self, user_id: int, instance_id: str
    ) -> List[Dict]:
        """查询所有活跃的基础评测用例"""
        await self._db._ensure_init()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            async with db.execute(
                "SELECT * FROM baseline_cases "
                "WHERE user_id=? AND instance_id=? AND is_active=1 "
                "ORDER BY created_at ASC",
                (user_id, instance_id),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def _save_baseline_case(self, case: BaselineCase) -> None:
        """写入 baseline_cases 表"""
        await self._db._ensure_init()
        d = case.to_dict()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            await db.execute(
                """INSERT OR REPLACE INTO baseline_cases
                   (case_id, user_id, instance_id, subject, category, question,
                    expected_direction, expected_keywords, difficulty, source,
                    source_pred_id, created_at, is_active)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (d["case_id"], d["user_id"], d["instance_id"],
                 d["subject"], d["category"], d["question"],
                 d["expected_direction"], d["expected_keywords"],
                 d["difficulty"], d["source"], d["source_pred_id"],
                 d["created_at"], d["is_active"]),
            )
            await db.commit()

    async def _count_baseline_by_subject(
        self, user_id: int, instance_id: str
    ) -> Dict[str, int]:
        """统计每个 subject 的 baseline_cases 数量"""
        await self._db._ensure_init()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            async with db.execute(
                "SELECT subject, COUNT(*) as cnt FROM baseline_cases "
                "WHERE user_id=? AND instance_id=? AND is_active=1 "
                "GROUP BY subject",
                (user_id, instance_id),
            ) as cur:
                rows = await cur.fetchall()
        return {row["subject"]: row["cnt"] for row in rows}

    async def _get_existing_source_pred_ids(
        self, user_id: int, instance_id: str, subject: str
    ) -> set:
        """获取已有 baseline_cases 的 source_pred_id 集合"""
        await self._db._ensure_init()
        async with self._db._connect() as db:
            await self._db._setup_conn(db)
            async with db.execute(
                "SELECT source_pred_id FROM baseline_cases "
                "WHERE user_id=? AND instance_id=? AND subject=? AND source_pred_id IS NOT NULL",
                (user_id, instance_id, subject),
            ) as cur:
                rows = await cur.fetchall()
        return {row[0] for row in rows if row[0]}

    @staticmethod
    def _row_to_snapshot(d: dict) -> LearnSnapshot:
        """将数据库行转换为 LearnSnapshot"""
        subject_stats = d.get("subject_stats", "{}")
        if isinstance(subject_stats, str):
            try:
                subject_stats = json.loads(subject_stats)
            except (json.JSONDecodeError, TypeError):
                subject_stats = {}
        baseline_detail = d.get("baseline_detail", "{}")
        if isinstance(baseline_detail, str):
            try:
                baseline_detail = json.loads(baseline_detail)
            except (json.JSONDecodeError, TypeError):
                baseline_detail = {}
        return LearnSnapshot(
            snapshot_id=d.get("snapshot_id", ""),
            user_id=d.get("user_id", 0),
            instance_id=d.get("instance_id", ""),
            snapshot_type=d.get("snapshot_type", ""),
            learn_cycle_id=d.get("learn_cycle_id", ""),
            triggered_by=d.get("triggered_by", ""),
            total_verified=d.get("total_verified", 0),
            correct_count=d.get("correct_count", 0),
            wrong_count=d.get("wrong_count", 0),
            accuracy_rate=d.get("accuracy_rate", 0.0),
            subject_stats=subject_stats,
            active_rules_count=d.get("active_rules_count", 0),
            new_rules_count=d.get("new_rules_count", 0),
            baseline_pass=bool(d.get("baseline_pass", 1)),
            baseline_score=d.get("baseline_score", 0.0),
            baseline_detail=baseline_detail,
            created_at=d.get("created_at", 0.0),
        )
