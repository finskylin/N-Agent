"""
StrategyLearner — 分析逻辑自我演化引擎

批量归因分析已验证的预测记录，提炼 strategy_rule 类型的知识单元，
同步更新图谱边权重，可选生成学习报告。

与 ReflectionEngine 的区别：
- ReflectionEngine：输入=近期 episodes，输出=通用知识
- StrategyLearner：输入=已验证预测记录，输出=strategy_rule 知识 + 图谱权重更新
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Dict, Optional

from loguru import logger


@dataclass
class StrategyLearnResult:
    user_id: int
    instance_id: str
    verified_count: int
    correct_count: int
    wrong_count: int
    accuracy_rate: float
    new_rules: List[Dict] = field(default_factory=list)
    graph_weight_updates: int = 0
    triggered_by: str = "schedule"
    created_at: float = field(default_factory=time.time)


class StrategyLearner:
    """分析逻辑自我演化引擎"""

    _ATTRIBUTION_PROMPT = """\
以下是近期 {total} 条已验证的预测记录，其中正确 {correct} 条，错误 {wrong} 条（胜率 {rate}%）。

预测记录：
{records_text}

请分析：
1. 哪些分析维度（技术面/基本面/资金面/消息面/估值面）在正确预测中被体现？
2. 哪些维度在错误预测中被忽略或误判？
3. 提炼 1-3 条可操作的分析规则。

输出格式（JSON 数组，不要有任何其他文字）：
[
  {{
    "rule": "规则描述（如：PE高位时必须同时验证北向资金方向）",
    "condition": "触发条件（什么情况下应用此规则）",
    "action": "推荐行动（应该怎么做）",
    "evidence": "支撑证据（引用具体预测记录摘要）",
    "confidence": 0.0到1.0（规则置信度）
  }}
]

如果数据不足以提炼规则，返回空数组 []。
"""

    def __init__(
        self,
        prediction_store,
        knowledge_store,
        graph_store,
        llm_call,
        config: dict = None,
        context_db=None,
    ):
        self._pred_store = prediction_store
        self._knowledge_store = knowledge_store
        self._graph_store = graph_store
        self._llm_call = llm_call
        self._config = (config or {}).get("strategy_learner", {})
        self._context_db = context_db  # SessionContextDB，用于写 user_experiences

    # 滑动窗口水位线：key = "{user_id}:{instance_id}:{subject}"
    _incremental_watermark: Dict[str, float] = {}

    async def run_incremental(
        self,
        user_id: int,
        instance_id: str,
        triggered_by: str = "post_verify",
        window_size: int = 20,
        llm_trigger_count: int = 5,
    ) -> StrategyLearnResult:
        """
        滑动窗口增量学习：按 subject 分组，只处理各主体上次水位线之后的新验证记录。

        - 图谱权重调整：有新记录就执行（无需 LLM）
        - LLM 归因：每个 subject 新增 >= llm_trigger_count 才调用
        - 水位线：按 subject 独立维护，互不影响
        """
        subjects = await self._pred_store.get_subjects_with_enough_data(
            user_id, instance_id, min_verified=1, since_ts=time.time() - 7 * 86400,
        )

        total_all = correct_all = wrong_all = weight_updates_all = 0
        all_new_rules: List[Dict] = []

        for subj_row in subjects:
            subject = subj_row["subject"]
            key = f"{user_id}:{instance_id}:{subject}"
            since_ts = self._incremental_watermark.get(key, time.time() - 7 * 86400)

            verified = await self._pred_store.get_verified_for_learning(
                user_id, instance_id,
                limit=window_size,
                since_ts=since_ts,
                subject=subject,
            )
            if not verified:
                continue

            correct = [p for p in verified if p["status"] == "verified_correct"]
            wrong = [p for p in verified if p["status"] == "verified_wrong"]
            total = len(verified)

            # 图谱权重增量更新（无需 LLM）
            weight_updates = await self._update_graph_weights(user_id, instance_id, verified)
            weight_updates_all += weight_updates

            # LLM 归因：该主体新增样本足够时才触发
            if total >= llm_trigger_count:
                subj_result = StrategyLearnResult(
                    user_id=user_id, instance_id=instance_id,
                    verified_count=total,
                    correct_count=len(correct), wrong_count=len(wrong),
                    accuracy_rate=round(len(correct) / total, 2) if total > 0 else 0.0,
                    triggered_by=triggered_by,
                )
                try:
                    new_rules = await self._call_llm_for_rules(verified, subj_result)
                    if new_rules:
                        await self._save_rules_to_knowledge(
                            user_id, instance_id, new_rules, subject=subject,
                        )
                        all_new_rules.extend(new_rules)
                except Exception as e:
                    logger.warning(f"[StrategyLearner] Incremental LLM failed for {subject}: {e}")

            # 推进该 subject 水位线
            max_ts = max((p.get("verified_at") or 0) for p in verified)
            if max_ts > since_ts:
                self._incremental_watermark[key] = max_ts

            total_all += total
            correct_all += len(correct)
            wrong_all += len(wrong)

        result = StrategyLearnResult(
            user_id=user_id, instance_id=instance_id,
            verified_count=total_all,
            correct_count=correct_all, wrong_count=wrong_all,
            accuracy_rate=round(correct_all / total_all, 2) if total_all > 0 else 0.0,
            new_rules=all_new_rules,
            graph_weight_updates=weight_updates_all,
            triggered_by=triggered_by,
        )
        logger.info(
            f"[StrategyLearner] Incremental: subjects={len(subjects)}, new={total_all}, "
            f"weight_updates={weight_updates_all}, rules={len(all_new_rules)}"
        )
        return result

    async def run(
        self,
        user_id: int,
        instance_id: str,
        triggered_by: str = "schedule",
        since_days: int = 30,
    ) -> StrategyLearnResult:
        """
        全量策略学习：按 subject 分组，每个主体数据充足时独立做 LLM 深度归因。

        流程：
        1. 查找近 since_days 天内验证数 >= min_samples 的所有 subject
        2. 每个 subject 独立拉取历史记录（最多 per_subject_limit 条）
        3. 调用 LLM 做该主体的归因分析，写入 strategy_rule（带 subject tag）
        4. 增量更新图谱边权重（与增量学习共用逻辑，不重复处理）
        5. 返回汇总学习摘要
        """
        min_samples = self._config.get("min_samples", 5)
        per_subject_limit = self._config.get("per_subject_limit", 30)
        since_ts = time.time() - since_days * 86400

        subjects = await self._pred_store.get_subjects_with_enough_data(
            user_id, instance_id,
            min_verified=min_samples,
            since_ts=since_ts,
        )

        if not subjects:
            logger.info(
                f"[StrategyLearner] No subject with >= {min_samples} verified samples, skipping"
            )
            return StrategyLearnResult(
                user_id=user_id, instance_id=instance_id,
                verified_count=0, correct_count=0, wrong_count=0,
                accuracy_rate=0.0, triggered_by=triggered_by,
            )

        total_all = correct_all = wrong_all = weight_updates_all = 0
        all_new_rules: List[Dict] = []

        for subj_row in subjects:
            subject = subj_row["subject"]
            verified = await self._pred_store.get_verified_for_learning(
                user_id, instance_id,
                limit=per_subject_limit,
                since_ts=since_ts,
                subject=subject,
            )
            if not verified:
                continue

            correct = [p for p in verified if p["status"] == "verified_correct"]
            wrong = [p for p in verified if p["status"] == "verified_wrong"]
            total = len(verified)

            subj_result = StrategyLearnResult(
                user_id=user_id, instance_id=instance_id,
                verified_count=total,
                correct_count=len(correct), wrong_count=len(wrong),
                accuracy_rate=round(len(correct) / total, 2) if total > 0 else 0.0,
                triggered_by=triggered_by,
            )

            # LLM 深度归因（全量模式，不限 llm_trigger_count）
            try:
                new_rules = await self._call_llm_for_rules(verified, subj_result)
                if new_rules:
                    await self._save_rules_to_knowledge(
                        user_id, instance_id, new_rules, subject=subject,
                    )
                    all_new_rules.extend(new_rules)
            except Exception as e:
                logger.warning(f"[StrategyLearner] Full LLM attribution failed for {subject}: {e}")

            # 图谱权重更新
            weight_updates = await self._update_graph_weights(user_id, instance_id, verified)
            weight_updates_all += weight_updates

            total_all += total
            correct_all += len(correct)
            wrong_all += len(wrong)

            logger.debug(
                f"[StrategyLearner] Subject={subject}: total={total}, "
                f"rules={len(new_rules) if 'new_rules' in dir() else 0}, weights={weight_updates}"
            )

        result = StrategyLearnResult(
            user_id=user_id, instance_id=instance_id,
            verified_count=total_all,
            correct_count=correct_all, wrong_count=wrong_all,
            accuracy_rate=round(correct_all / total_all, 2) if total_all > 0 else 0.0,
            new_rules=all_new_rules,
            graph_weight_updates=weight_updates_all,
            triggered_by=triggered_by,
        )
        logger.info(
            f"[StrategyLearner] Full run: subjects={len(subjects)}, total={total_all}, "
            f"rules={len(all_new_rules)}, weight_updates={weight_updates_all}"
        )
        return result

    async def _call_llm_for_rules(
        self,
        verified_preds: List[Dict],
        result: StrategyLearnResult,
    ) -> List[Dict]:
        """调用 LLM 做批量归因分析，返回 strategy_rule 列表"""
        import asyncio

        # 格式化预测记录摘要
        lines = []
        for p in verified_preds[:20]:
            status_label = "✅正确" if p["status"] == "verified_correct" else "❌错误"
            outcome = p.get("actual_outcome") or ""
            lines.append(
                f"{status_label} 主体={p['subject']} 预测=「{p['prediction_text'][:50]}」"
                + (f" 实际=「{outcome[:40]}」" if outcome else "")
            )
        records_text = "\n".join(lines)

        rate = int(result.accuracy_rate * 100)
        prompt = self._ATTRIBUTION_PROMPT.format(
            total=result.verified_count,
            correct=result.correct_count,
            wrong=result.wrong_count,
            rate=rate,
            records_text=records_text,
        )

        try:
            text = await asyncio.wait_for(
                self._llm_call(
                    prompt=prompt,
                    use_small_fast=False,
                    max_tokens=1024,
                    timeout=35.0,
                ),
                timeout=40.0,
            )
        except asyncio.TimeoutError:
            logger.debug("[StrategyLearner] LLM call timed out (30s)")
            return []

        if not text:
            return []

        # 解析 JSON
        t = text.strip()
        if "```" in t:
            import re
            m = re.search(r"```(?:json)?\s*([\s\S]*?)```", t)
            if m:
                t = m.group(1).strip()
        try:
            data = json.loads(t)
            if isinstance(data, list):
                return [r for r in data if isinstance(r, dict) and r.get("rule")]
        except json.JSONDecodeError:
            pass
        try:
            start, end = t.find("["), t.rfind("]")
            if start != -1 and end > start:
                data = json.loads(t[start:end + 1])
                if isinstance(data, list):
                    return [r for r in data if isinstance(r, dict) and r.get("rule")]
        except json.JSONDecodeError:
            pass
        return []

    async def _save_rules_to_knowledge(
        self,
        user_id: int,
        instance_id: str,
        rules: List[Dict],
        subject: str = "",
    ) -> None:
        """将规则写入 knowledge_units（category=strategy_rule，带 subject tag）"""
        from agent_core.knowledge.models import KnowledgeUnit
        now = time.time()
        for rule in rules:
            rule_text = rule.get("rule", "")
            if not rule_text:
                continue
            condition = rule.get("condition", "")
            action = rule.get("action", "")
            evidence = rule.get("evidence", "")
            confidence = float(rule.get("confidence") or 0.5)
            confidence = max(0.0, min(1.0, confidence))

            full_text = rule_text
            if condition:
                full_text += f" 条件：{condition}"
            if action:
                full_text += f" 行动：{action}"
            if evidence:
                full_text += f" 依据：{evidence[:80]}"

            tags = ["strategy_rule", "auto_learned"]
            if subject:
                tags.append(subject)

            unit = KnowledgeUnit(
                unit_id=f"ku_sl_{uuid.uuid4().hex[:8]}",
                category="strategy_rule",
                text=full_text,
                tags=tags,
                utility=0.85,
                confidence=confidence,
                ingestion_time=now,
                valid_from=now,
                created_at=now,
                last_accessed=now,
            )

            try:
                await self._knowledge_store.save_knowledge(unit, user_id, instance_id)
            except Exception as e:
                logger.debug(f"[StrategyLearner] Failed to save rule: {e}")

            # 高置信规则同步到 user_experiences（下轮对话 prompt 可召回）
            if self._context_db and confidence >= 0.7:
                try:
                    await self._context_db.upsert_user_experience(
                        user_id=user_id,
                        instance_id=instance_id,
                        dimension="system_knowledge",
                        text=f"[分析规则] {full_text}",
                        score=confidence,
                        source_session=f"strategy_learner:{subject}",
                    )
                except Exception as e:
                    logger.debug(f"[StrategyLearner] Failed to sync to user_experiences: {e}")

    async def _update_graph_weights(
        self,
        user_id: int,
        instance_id: str,
        verified_preds: List[Dict],
    ) -> int:
        """
        根据验证结果更新图谱边权重：
        - 预测正确 → weight += 0.1（上限 1.0）
        - 预测错误 → weight -= 0.15（下限 0.1）
        通过 source_edge_id 关联 prediction_records 和 knowledge_edges。
        """
        if not self._graph_store:
            return 0

        updates = 0
        for p in verified_preds:
            edge_id = p.get("source_edge_id")
            if not edge_id:
                continue
            try:
                is_correct = p["status"] == "verified_correct"
                delta = 0.1 if is_correct else -0.15
                await self._graph_store.update_edge_weight(
                    user_id=user_id,
                    instance_id=instance_id,
                    edge_id=edge_id,
                    delta=delta,
                )
                updates += 1
            except Exception as e:
                logger.debug(f"[StrategyLearner] Edge weight update failed for {edge_id}: {e}")

        return updates

    async def generate_report(
        self,
        user_id: int,
        instance_id: str,
        result: StrategyLearnResult,
    ) -> str:
        """生成 markdown 格式的学习报告"""
        from datetime import datetime
        date_str = datetime.now().strftime("%Y-%m-%d")
        rate = int(result.accuracy_rate * 100) if result.verified_count > 0 else 0

        lines = [
            f"## Agent 自学习周报（{date_str}）",
            "",
            "### 预测验证情况",
            f"- 本期验证：{result.verified_count} 条 | 正确：{result.correct_count} 条 | "
            f"错误：{result.wrong_count} 条 | 胜率：{rate}%",
            "",
        ]

        if result.new_rules:
            lines.append("### 发现的分析规律")
            for i, rule in enumerate(result.new_rules, 1):
                conf = int(float(rule.get("confidence") or 0) * 100)
                lines.append(f"{i}. {rule.get('rule', '')} （置信度 {conf}%）")
                if rule.get("condition"):
                    lines.append(f"   - 条件：{rule['condition']}")
                if rule.get("action"):
                    lines.append(f"   - 行动：{rule['action']}")
            lines.append("")

        if result.graph_weight_updates:
            lines.append("### 图谱权重更新")
            lines.append(f"- 本期更新了 {result.graph_weight_updates} 条图谱边权重")
            lines.append("")

        if result.verified_count == 0:
            lines.append("*本期暂无足够验证数据，下周继续积累。*")

        return "\n".join(lines)
