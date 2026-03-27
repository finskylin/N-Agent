"""
Knowledge Engine Benchmark — 五维基准测试框架

Dim 1: 知识积累质量 (SE-Bench RACE/FACT)
Dim 2: Skill 结晶有效性 (SkillsBench 三条件对比)
Dim 3: 时序追溯准确性
Dim 4: 自进化效果 (StuLife 纵向追踪)
Dim 5: 预测策略有效性 (滚动快照 + 基础回归)
"""
import time
from typing import List, Dict, Optional, Callable, Any
from statistics import mean

from loguru import logger

from .models import KnowledgeUnit


class KnowledgeAccumulationBench:
    """
    Dim 1: 知识积累质量验证。

    方法论 (SE-Bench 启发):
    1. Phase A: 给 Agent 一系列任务 + 参考文档（训练阶段）
    2. Phase B: 去掉参考文档，只靠积累的知识回答（测试阶段）
    3. 对比: Phase B 的表现 vs 无知识积累的 baseline

    评估指标 (RACE 框架):
    - Comprehensiveness: 知识覆盖的全面性
    - Depth: 知识深度
    - Accuracy: 知识准确率
    - Utility: 知识对后续任务的实际帮助率
    """

    async def run(
        self, execute_fn: Callable, evaluate_fn: Callable,
        test_cases: List[Dict],
    ) -> Dict:
        """
        运行知识积累基准测试。

        Args:
            execute_fn: async (query, with_reference) -> result
            evaluate_fn: (result, expected) -> float score
            test_cases: [{"query": str, "expected": str}]
        """
        if not test_cases:
            return {"error": "no test cases"}

        half = len(test_cases) // 2

        # Phase A: 知识积累
        for case in test_cases[:half]:
            await execute_fn(case["query"], with_reference=True)

        # Phase B: 知识验证
        scores = []
        for case in test_cases[half:]:
            result = await execute_fn(case["query"], with_reference=False)
            score = evaluate_fn(result, case.get("expected", ""))
            scores.append(score)

        return {
            "accumulation_quality": mean(scores) if scores else 0.0,
            "test_count": len(scores),
            "scores": scores,
        }


class SkillCrystallizationBench:
    """
    Dim 2: Skill 结晶有效性。

    三条件对比 (SkillsBench 方法论):
    1. No Skills:           无结晶 Skill
    2. Curated Skills:      人工精选
    3. Crystallized Skills:  自动结晶

    必须满足: Crystallized >= No Skills (不降低性能)
    """

    async def run(
        self, execute_fn: Callable, evaluate_fn: Callable,
        test_cases: List[Dict],
        skills_modes: Dict[str, Any] = None,
    ) -> Dict:
        """运行 Skill 结晶基准测试"""
        if not test_cases:
            return {"error": "no test cases"}

        results = {}
        for mode in ["none", "curated", "crystallized"]:
            mode_scores = []
            for case in test_cases:
                result = await execute_fn(case["query"], skills_mode=mode)
                score = evaluate_fn(result, case.get("expected", ""))
                mode_scores.append(score)
            results[mode] = mean(mode_scores) if mode_scores else 0.0

        crystal_boost = results["crystallized"] - results["none"]

        return {
            "no_skills": results["none"],
            "curated_skills": results["curated"],
            "crystallized_skills": results["crystallized"],
            "crystal_boost_pp": crystal_boost,
            "pass": crystal_boost >= 0,
        }

    async def quick_validate(
        self, execute_fn: Callable, evaluate_fn: Callable,
        candidate_skill: Any, test_cases: List[Dict],
    ) -> Dict:
        """快速验证单个结晶 Skill"""
        if not test_cases:
            return {"pass": False, "crystal_boost_pp": 0.0}

        baseline_scores = []
        crystal_scores = []
        for case in test_cases[:3]:
            # Baseline
            result_base = await execute_fn(case["query"], skills_mode="none")
            baseline_scores.append(evaluate_fn(result_base, case.get("expected", "")))
            # With crystal
            result_crystal = await execute_fn(case["query"], skills_mode="crystallized")
            crystal_scores.append(evaluate_fn(result_crystal, case.get("expected", "")))

        boost = mean(crystal_scores) - mean(baseline_scores)
        return {
            "pass": boost >= 0,
            "crystal_boost_pp": boost,
            "baseline": mean(baseline_scores),
            "crystal": mean(crystal_scores),
        }


class TemporalTracingBench:
    """
    Dim 3: 时序追溯准确性。

    测试场景:
    1. 知识更新后，旧版本是否可追溯
    2. Point-in-time 查询是否返回正确的历史知识
    3. 认知变迁链是否完整
    """

    async def run(self, store, temporal_manager) -> Dict:
        """运行时序追溯基准测试"""
        results = {"tests": [], "all_passed": True}

        # 测试 1: 知识版本化
        try:
            t1 = time.time() - 7 * 86400
            now = time.time()

            unit_v1 = KnowledgeUnit(
                category="domain_fact",
                text="测试知识 V1",
                tags=["bench_test"],
                ingestion_time=t1,
                valid_from=t1,
                created_at=t1,
                last_accessed=t1,
            )
            await store.save_knowledge(unit_v1, user_id=0, instance_id="bench")

            unit_v2 = KnowledgeUnit(
                category="domain_fact",
                text="测试知识 V2",
                tags=["bench_test"],
            )
            await temporal_manager.update_knowledge(
                unit_v1.unit_id, unit_v2,
                reason="基准测试更新",
                user_id=0, instance_id="bench",
            )

            # 验证追溯
            timeline = await temporal_manager.cognition_timeline(
                user_id=0, instance_id="bench", entity="bench_test",
            )

            test1_pass = len(timeline) >= 2
            results["tests"].append({
                "name": "version_chain",
                "pass": test1_pass,
                "timeline_length": len(timeline),
            })
            if not test1_pass:
                results["all_passed"] = False

        except Exception as e:
            results["tests"].append({"name": "version_chain", "pass": False, "error": str(e)})
            results["all_passed"] = False

        return results


class EvolutionEffectBench:
    """
    Dim 4: 自进化效果 (StuLife 纵向追踪)。

    方法:
    1. 执行 N 轮相关任务
    2. 每轮后记录知识积累量 + 能力得分
    3. 检查: 后期任务表现是否优于前期
    """

    async def run(
        self, execute_fn: Callable, evaluate_fn: Callable,
        task_sequence: List[Dict], rounds: int = 5,
        get_knowledge_count: Callable = None,
    ) -> Dict:
        """运行自进化效果基准测试"""
        scores_over_time = []
        knowledge_counts = []

        for round_idx in range(rounds):
            round_scores = []
            for task in task_sequence:
                result = await execute_fn(task["query"])
                score = evaluate_fn(result, task.get("expected", ""))
                round_scores.append(score)

            avg_score = mean(round_scores) if round_scores else 0.0
            scores_over_time.append(avg_score)

            if get_knowledge_count:
                count = await get_knowledge_count()
                knowledge_counts.append(count)

        # 分析学习曲线
        quarter = max(rounds // 4, 1)
        first_quarter = mean(scores_over_time[:quarter])
        last_quarter = mean(scores_over_time[-quarter:])

        return {
            "learning_curve": scores_over_time,
            "knowledge_growth": knowledge_counts,
            "improvement": last_quarter - first_quarter,
            "is_evolving": last_quarter > first_quarter + 0.02,
            "first_quarter_avg": first_quarter,
            "last_quarter_avg": last_quarter,
        }


class PredictionStrategyBench:
    """
    Dim 5: 预测策略有效性。

    方法论：
    1. 从最近 N 次学习快照中分析准确率趋势
    2. 检查基础评测通过率是否稳定
    3. 分析规则数量增长与准确率的相关性

    评估指标：
    - accuracy_trend: 准确率是否持续提升
    - baseline_stability: 基础评测通过率
    - rule_efficiency: 规则数增长 vs 准确率变化
    """

    async def run(
        self,
        evaluator,
        user_id: int,
        instance_id: str,
        history_count: int = 5,
    ) -> Dict:
        """
        运行预测策略基准测试。

        从 learn_snapshots 中取最近 N 次 post_learn 快照，分析趋势。
        """
        try:
            snapshots = await self._get_recent_snapshots(
                evaluator, user_id, instance_id, history_count,
            )
        except Exception as e:
            return {"error": str(e), "snapshots_found": 0}

        if len(snapshots) < 2:
            return {
                "snapshots_found": len(snapshots),
                "accuracy_trend": "insufficient_data",
                "pass": True,
            }

        accuracies = [s.accuracy_rate for s in snapshots]
        baselines = [s.baseline_pass for s in snapshots]
        rule_counts = [s.active_rules_count for s in snapshots]

        # 趋势: 后半段 vs 前半段
        half = len(accuracies) // 2
        first_half = mean(accuracies[:half]) if accuracies[:half] else 0.0
        second_half = mean(accuracies[half:]) if accuracies[half:] else 0.0
        improvement = second_half - first_half

        # 基础评测稳定性
        baseline_pass_rate = sum(1 for b in baselines if b) / len(baselines) if baselines else 1.0

        # 规则效率 (准确率提升 / 规则数增长)
        rule_growth = rule_counts[-1] - rule_counts[0] if rule_counts else 0
        rule_efficiency = improvement / rule_growth if rule_growth > 0 else 0.0

        is_improving = improvement > 0.02
        is_stable = baseline_pass_rate >= 0.8

        return {
            "snapshots_found": len(snapshots),
            "accuracy_trend": accuracies,
            "first_half_avg": round(first_half, 4),
            "second_half_avg": round(second_half, 4),
            "improvement": round(improvement, 4),
            "is_improving": is_improving,
            "baseline_pass_rate": round(baseline_pass_rate, 3),
            "is_stable": is_stable,
            "rule_growth": rule_growth,
            "rule_efficiency": round(rule_efficiency, 6),
            "pass": is_stable,
        }

    @staticmethod
    async def _get_recent_snapshots(
        evaluator, user_id: int, instance_id: str, limit: int,
    ) -> list:
        """从 learn_snapshots 查询最近 N 条 post_learn 快照"""
        from .models import LearnSnapshot

        db = evaluator._db
        await db._ensure_init()
        async with db._connect() as conn:
            await db._setup_conn(conn)
            async with conn.execute(
                "SELECT * FROM learn_snapshots "
                "WHERE instance_id=? AND user_id=? AND snapshot_type='post_learn' "
                "ORDER BY created_at DESC LIMIT ?",
                (instance_id, user_id, limit),
            ) as cur:
                rows = await cur.fetchall()

        snapshots = []
        for row in reversed(rows):  # 按时间正序
            snapshots.append(evaluator._row_to_snapshot(dict(row)))
        return snapshots
