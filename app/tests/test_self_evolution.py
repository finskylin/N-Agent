"""
三环自进化系统 — 功能测试

覆盖:
  Ring 1: 规则反馈闭环
    1.1 V4Config 新增 8 个配置字段（Ring 2 + Ring 3）
    1.2 StrategyLearner 高置信规则同步到 user_experiences
    1.3 PredictionScheduler 准确率降级检测 → 触发 EvolutionTask

  Ring 2: DGM Skill Patch
    2.1 SkillEvolver.collect_failing_skills 过滤逻辑
    2.2 SkillEvolver._parse_patch_response JSON 解析
    2.3 SkillEvolver.run_benchmark 评分

  Ring 3: 能力盲区自发现
    3.1 capability_gaps 表 schema 创建
    3.2 CapabilityGapCounter 关键词匹配 + 累积计数
    3.3 CapabilityGapCounter 阈值触发 + 冷却去重
    3.4 EvolutionTaskManager.create_task 基本流程
    3.5 EvolutionTaskManager._llm_judge_needs_skill

  集成:
    4.1 CapabilityGapPlugin Hook 注册（模拟 POST_TOOL_USE）
    4.2 完整流程: 工具失败 → 计数 → 触发 → 创建进化任务

运行:
  pytest app/tests/test_self_evolution.py -v -s
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 确保项目根目录在 sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
async def sqlite_db():
    """创建临时 SQLite memory.db，运行 migration，返回 SessionContextDB 实例"""
    from agent_core.session.context_db import SessionContextDB

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "memory.db")
        db = SessionContextDB(db_path=db_path)
        await db._ensure_init()
        yield db


@pytest.fixture
async def knowledge_store(sqlite_db):
    """创建 KnowledgeStore"""
    from agent_core.knowledge.store import KnowledgeStore
    return KnowledgeStore(sqlite_db, {})


@pytest.fixture
async def prediction_store(sqlite_db):
    """创建 PredictionStore"""
    from agent_core.knowledge.prediction_store import PredictionStore
    return PredictionStore(sqlite_db)


USER_ID = 1
INSTANCE_ID = "test-evolution"


# ═══════════════════════════════════════════════════════════════════════════════
# Ring 1: 规则反馈闭环
# ═══════════════════════════════════════════════════════════════════════════════

class TestRing1Config:
    """1.1 V4Config 新增配置字段"""

    def test_config_ring2_fields_exist(self):
        from agent_core.config import V4Config
        c = V4Config()
        assert hasattr(c, "skill_evolution_enabled")
        assert hasattr(c, "skill_evolution_error_threshold")
        assert hasattr(c, "skill_evolution_min_calls")
        assert hasattr(c, "skill_evolution_window_days")
        assert hasattr(c, "skill_evolution_patch_per_day")

    def test_config_ring3_fields_exist(self):
        from agent_core.config import V4Config
        c = V4Config()
        assert hasattr(c, "capability_gap_detection_enabled")
        assert hasattr(c, "capability_gap_trigger_threshold")
        assert hasattr(c, "capability_gap_cooldown_hours")

    def test_config_ring2_defaults(self):
        from agent_core.config import V4Config
        c = V4Config()
        assert c.skill_evolution_enabled is False
        assert c.skill_evolution_error_threshold == 0.3
        assert c.skill_evolution_min_calls == 5
        assert c.skill_evolution_window_days == 7
        assert c.skill_evolution_patch_per_day == 1

    def test_config_ring3_defaults(self):
        from agent_core.config import V4Config
        c = V4Config()
        assert c.capability_gap_detection_enabled is True
        assert c.capability_gap_trigger_threshold == 3
        assert c.capability_gap_cooldown_hours == 24

    def test_config_from_dict(self):
        from agent_core.config import V4Config
        c = V4Config.from_dict({
            "skill_evolution_enabled": True,
            "skill_evolution_error_threshold": 0.5,
            "capability_gap_trigger_threshold": 5,
            "capability_gap_cooldown_hours": 48,
        })
        assert c.skill_evolution_enabled is True
        assert c.skill_evolution_error_threshold == 0.5
        assert c.capability_gap_trigger_threshold == 5
        assert c.capability_gap_cooldown_hours == 48

    def test_config_from_env(self):
        from agent_core.config import V4Config
        with patch.dict(os.environ, {
            "SKILL_EVOLUTION_ENABLED": "true",
            "CAPABILITY_GAP_TRIGGER_THRESHOLD": "10",
        }):
            c = V4Config.from_env()
            assert c.skill_evolution_enabled is True
            assert c.capability_gap_trigger_threshold == 10


class TestRing1StrategyLearner:
    """1.2 StrategyLearner 高置信规则同步到 user_experiences"""

    @pytest.mark.asyncio
    async def test_save_rules_syncs_to_user_experiences(self, sqlite_db, knowledge_store):
        """高置信规则 (>=0.7) 应同步到 user_experiences 表"""
        from agent_core.knowledge.strategy_learner import StrategyLearner

        mock_llm = AsyncMock(return_value="[]")
        mock_pred_store = MagicMock()

        learner = StrategyLearner(
            prediction_store=mock_pred_store,
            knowledge_store=knowledge_store,
            graph_store=None,
            llm_call=mock_llm,
            config={},
            context_db=sqlite_db,
        )

        # 直接调用 _save_rules_to_knowledge（签名: user_id, instance_id, rules, subject）
        rules = [
            {"rule": "MACD金叉看涨", "condition": "MACD金叉", "confidence": 0.85},
        ]
        await learner._save_rules_to_knowledge(
            USER_ID, INSTANCE_ID, rules, "茅台",
        )

        # 验证 user_experiences 表有新记录
        async with sqlite_db._connect() as db:
            await sqlite_db._setup_conn(db)
            cursor = await db.execute(
                "SELECT text, score, dimension FROM user_experiences "
                "WHERE user_id = ? AND instance_id = ?",
                (USER_ID, INSTANCE_ID),
            )
            rows = await cursor.fetchall()

        assert len(rows) >= 1, "高置信规则应同步到 user_experiences"
        row = rows[0]
        assert "分析规则" in row[0], "text 应包含 [分析规则] 前缀"
        assert row[1] == 0.85, "score 应等于规则 confidence"
        assert row[2] == "system_knowledge", "dimension 应为 system_knowledge"

    @pytest.mark.asyncio
    async def test_low_confidence_not_synced(self, sqlite_db, knowledge_store):
        """低置信规则 (<0.7) 不应同步到 user_experiences"""
        from agent_core.knowledge.strategy_learner import StrategyLearner

        mock_llm = AsyncMock(return_value="[]")
        learner = StrategyLearner(
            prediction_store=MagicMock(),
            knowledge_store=knowledge_store,
            graph_store=None,
            llm_call=mock_llm,
            config={},
            context_db=sqlite_db,
        )

        rules = [
            {"rule": "弱信号看跌", "condition": "弱信号", "confidence": 0.4},
        ]
        await learner._save_rules_to_knowledge(USER_ID, INSTANCE_ID, rules, "五粮液")

        async with sqlite_db._connect() as db:
            await sqlite_db._setup_conn(db)
            cursor = await db.execute(
                "SELECT COUNT(*) FROM user_experiences "
                "WHERE user_id = ? AND instance_id = ?",
                (USER_ID, INSTANCE_ID),
            )
            row = await cursor.fetchone()

        assert row[0] == 0, "低置信规则不应同步到 user_experiences"

    @pytest.mark.asyncio
    async def test_no_context_db_no_crash(self, knowledge_store):
        """context_db=None 时不应崩溃"""
        from agent_core.knowledge.strategy_learner import StrategyLearner

        learner = StrategyLearner(
            prediction_store=MagicMock(),
            knowledge_store=knowledge_store,
            graph_store=None,
            llm_call=AsyncMock(return_value="[]"),
            config={},
            context_db=None,  # 无 context_db
        )

        rules = [{"rule": "test rule", "condition": "test", "confidence": 0.9}]
        # 不应抛异常
        await learner._save_rules_to_knowledge(USER_ID, INSTANCE_ID, rules, "test")


class TestRing1PredictionScheduler:
    """1.3 PredictionScheduler 准确率降级检测"""

    @pytest.mark.asyncio
    async def test_scheduler_accepts_new_params(self):
        """PredictionScheduler 应接受 learn_evaluator 和 evolution_manager 参数"""
        from agent_core.knowledge.prediction_scheduler import PredictionScheduler

        mock_cron = MagicMock()
        mock_cron.list_jobs.return_value = []
        mock_cron.add_job = MagicMock()

        scheduler = PredictionScheduler(
            prediction_verifier=MagicMock(),
            strategy_learner=MagicMock(),
            cron_service=mock_cron,
            config={},
            learn_evaluator=MagicMock(),      # 新参数
            evolution_manager=MagicMock(),     # 新参数
        )
        assert scheduler._evaluator is not None
        assert scheduler._evolution_manager is not None

    @pytest.mark.asyncio
    async def test_degradation_triggers_evolution(self):
        """准确率下降超过阈值应触发进化任务"""
        from agent_core.knowledge.prediction_scheduler import PredictionScheduler
        from agent_core.knowledge.models import LearnSnapshot

        # 构建 mock 对象
        mock_verifier = MagicMock()
        mock_verifier.run_pending_verifications = AsyncMock(return_value=5)

        mock_learner = MagicMock()
        mock_learn_result = MagicMock()
        mock_learn_result.verified_count = 5
        mock_learn_result.new_rules = ["rule1"]
        mock_learn_result.graph_weight_updates = 2
        mock_learner.run = AsyncMock(return_value=mock_learn_result)
        mock_learner.generate_report = AsyncMock(return_value="")

        # LearnEvaluator mock
        after_snapshot = LearnSnapshot(
            snapshot_type="post_learn", accuracy_rate=0.5,
            total_verified=10, correct_count=5,
        )
        prev_snapshot = LearnSnapshot(
            snapshot_type="post_learn", accuracy_rate=0.8,
            total_verified=10, correct_count=8,
        )
        mock_evaluator = MagicMock()
        mock_evaluator.take_snapshot = AsyncMock(return_value=after_snapshot)
        mock_evaluator.get_previous_post_snapshot = AsyncMock(return_value=prev_snapshot)

        # EvolutionManager mock
        mock_evolution = MagicMock()
        mock_evolution.create_task = AsyncMock(return_value=None)

        mock_cron = MagicMock()
        mock_cron.list_jobs.return_value = []
        mock_cron.add_job = MagicMock()

        scheduler = PredictionScheduler(
            prediction_verifier=mock_verifier,
            strategy_learner=mock_learner,
            cron_service=mock_cron,
            config={},
            learn_evaluator=mock_evaluator,
            evolution_manager=mock_evolution,
        )

        # 执行学习周期
        with patch.dict(os.environ, {"EVAL_DEGRADATION_THRESHOLD": "0.1", "STRATEGY_LEARN_SEND_REPORT": "false"}):
            result = await scheduler.run_learn_cycle(USER_ID, INSTANCE_ID)

        # 验证准确率下降 0.3 > 阈值 0.1，应触发进化任务
        mock_evolution.create_task.assert_called_once()
        call_args = mock_evolution.create_task.call_args
        gap_text = call_args[0][0]
        assert "下降" in gap_text or "准确率" in gap_text


# ═══════════════════════════════════════════════════════════════════════════════
# Ring 2: DGM Skill Patch
# ═══════════════════════════════════════════════════════════════════════════════

class TestRing2SkillEvolver:
    """Ring 2: SkillEvolver DGM Patch 机制"""

    def _make_evolver(self, llm_call=None, get_stats=None):
        from agent_core.knowledge.skill_evolver import SkillEvolver

        return SkillEvolver(
            knowledge_store=MagicMock(),
            prediction_store=MagicMock(),
            skills_dir="/tmp/nonexistent_skills",
            llm_call=llm_call or AsyncMock(return_value="YES"),
            config={
                "skill_evolution_enabled": True,
                "skill_evolution_error_threshold": 0.3,
                "skill_evolution_min_calls": 5,
                "skill_evolution_window_days": 7,
                "skill_evolution_patch_per_day": 1,
            },
            get_skill_error_stats=get_stats,
        )

    @pytest.mark.asyncio
    async def test_collect_failing_skills_filter(self):
        """collect_failing_skills 应正确过滤低错误率/调用量不足的 skill"""
        stats = [
            {"skill_name": "good_skill", "total_calls": 100, "error_count": 5, "error_rate": 0.05},
            {"skill_name": "bad_skill", "total_calls": 20, "error_count": 10, "error_rate": 0.50},
            {"skill_name": "low_calls", "total_calls": 2, "error_count": 2, "error_rate": 1.0},
        ]
        mock_stats = AsyncMock(return_value=stats)
        evolver = self._make_evolver(get_stats=mock_stats)

        failing = await evolver.collect_failing_skills()

        skill_names = [s["skill_name"] for s in failing]
        assert "bad_skill" in skill_names, "高错误率 skill 应被选中"
        assert "good_skill" not in skill_names, "低错误率 skill 不应被选中"
        assert "low_calls" not in skill_names, "调用量不足的 skill 不应被选中"

    def test_parse_patch_response_json(self):
        """_parse_patch_response 应正确解析 JSON"""
        evolver = self._make_evolver()

        # 标准 JSON
        result = evolver._parse_patch_response(json.dumps({
            "new_description": "改进后的描述",
            "new_examples": "示例",
            "summary": "摘要",
        }))
        assert result is not None
        assert result["new_description"] == "改进后的描述"

    def test_parse_patch_response_code_block(self):
        """_parse_patch_response 应从代码块中提取 JSON"""
        evolver = self._make_evolver()

        text = '分析如下:\n```json\n{"new_description": "优化描述", "new_examples": "", "summary": "优化"}\n```'
        result = evolver._parse_patch_response(text)
        assert result is not None
        assert result["new_description"] == "优化描述"

    def test_parse_patch_response_invalid(self):
        """_parse_patch_response 应对无效输入返回 None"""
        evolver = self._make_evolver()

        assert evolver._parse_patch_response("") is None
        assert evolver._parse_patch_response("这不是 JSON") is None
        assert evolver._parse_patch_response('{"no_desc": "missing"}') is None

    @pytest.mark.asyncio
    async def test_run_benchmark_scoring(self):
        """run_benchmark 应返回 0~1 评分"""
        mock_llm = AsyncMock(return_value="YES")
        evolver = self._make_evolver(llm_call=mock_llm)

        test_records = [
            {"prediction_text": "茅台下周会涨吗", "query": "茅台预测"},
            {"prediction_text": "五粮液走势", "query": "五粮液分析"},
        ]

        score = await evolver.run_benchmark("test_skill", "测试描述", test_records)
        assert 0.0 <= score <= 1.0
        assert score == 1.0, "全 YES 回答应得 1.0 分"

    @pytest.mark.asyncio
    async def test_run_benchmark_empty_records(self):
        """空测试集应返回 0.5"""
        evolver = self._make_evolver()
        score = await evolver.run_benchmark("test_skill", "desc", [])
        assert score == 0.5

    @pytest.mark.asyncio
    async def test_collect_no_callback_returns_empty(self):
        """没有 get_skill_error_stats 回调时应返回空列表"""
        evolver = self._make_evolver(get_stats=None)
        result = await evolver.collect_failing_skills()
        assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
# Ring 3: 能力盲区自发现
# ═══════════════════════════════════════════════════════════════════════════════

class TestRing3CapabilityGaps:
    """3.1 capability_gaps 表 schema"""

    @pytest.mark.asyncio
    async def test_capability_gaps_table_exists(self, sqlite_db):
        """capability_gaps 表应在 migration 后存在"""
        async with sqlite_db._connect() as db:
            await sqlite_db._setup_conn(db)
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='capability_gaps'"
            )
            rows = await cursor.fetchall()
        assert len(rows) == 1, "capability_gaps 表应存在"

    @pytest.mark.asyncio
    async def test_capability_gaps_table_schema(self, sqlite_db):
        """capability_gaps 表应有正确的列"""
        async with sqlite_db._connect() as db:
            await sqlite_db._setup_conn(db)
            cursor = await db.execute("PRAGMA table_info(capability_gaps)")
            cols = await cursor.fetchall()
        col_names = {c[1] for c in cols}
        expected = {"tool_name", "error_summary", "session_id", "count", "last_triggered", "created_at", "updated_at"}
        assert expected.issubset(col_names), f"缺少列: {expected - col_names}"


class TestRing3CapabilityGapCounter:
    """3.2~3.3 CapabilityGapCounter"""

    @pytest.mark.asyncio
    async def test_keyword_matching(self, sqlite_db):
        """只有匹配 GAP_KEYWORDS 的错误才计数"""
        from agent_core.agentloop.capability_gap_counter import CapabilityGapCounter

        counter = CapabilityGapCounter(
            context_db=sqlite_db,
            config={"capability_gap_detection_enabled": True, "capability_gap_trigger_threshold": 3},
        )

        # 匹配关键词: "not found"
        count = await counter.increment("my_tool", "API endpoint not found", "sess1")
        assert count == 1, "匹配关键词应计数"

        # 不匹配关键词
        count = await counter.increment("my_tool", "一般性错误，超时了", "sess1")
        assert count == 0, "不匹配关键词不应计数（返回0）"

    @pytest.mark.asyncio
    async def test_count_accumulation(self, sqlite_db):
        """多次失败应累积计数"""
        from agent_core.agentloop.capability_gap_counter import CapabilityGapCounter

        counter = CapabilityGapCounter(
            context_db=sqlite_db,
            config={"capability_gap_detection_enabled": True, "capability_gap_trigger_threshold": 5},
        )

        for i in range(4):
            count = await counter.increment("test_tool", f"not found #{i}", f"sess_{i}")
        assert count == 4

    @pytest.mark.asyncio
    async def test_trigger_threshold(self, sqlite_db):
        """达到阈值应触发"""
        from agent_core.agentloop.capability_gap_counter import CapabilityGapCounter

        counter = CapabilityGapCounter(
            context_db=sqlite_db,
            config={
                "capability_gap_detection_enabled": True,
                "capability_gap_trigger_threshold": 3,
                "capability_gap_cooldown_hours": 24,
            },
        )

        # 不够阈值
        await counter.increment("tool_a", "not found", "s1")
        await counter.increment("tool_a", "not found", "s2")
        should = await counter.should_trigger("tool_a", "s3")
        assert should is False, "未达阈值不应触发"

        # 达到阈值
        await counter.increment("tool_a", "not found", "s3")
        should = await counter.should_trigger("tool_a", "s4")
        assert should is True, "达到阈值应触发"

    @pytest.mark.asyncio
    async def test_mark_triggered_resets_count(self, sqlite_db):
        """mark_triggered 后应重置计数，再次触发需重新累积"""
        from agent_core.agentloop.capability_gap_counter import CapabilityGapCounter

        counter = CapabilityGapCounter(
            context_db=sqlite_db,
            config={
                "capability_gap_detection_enabled": True,
                "capability_gap_trigger_threshold": 2,
                "capability_gap_cooldown_hours": 0,  # 关闭冷却方便测试
            },
        )

        await counter.increment("tool_b", "not found", "s1")
        await counter.increment("tool_b", "missing data", "s2")
        assert await counter.should_trigger("tool_b", "s3") is True

        await counter.mark_triggered("tool_b", "s3")

        # 重置后应为 False
        counter.clear_session_state()  # 清除 session 去重
        assert await counter.should_trigger("tool_b", "s4") is False

    @pytest.mark.asyncio
    async def test_session_dedup(self, sqlite_db):
        """同 session 内同一工具只触发一次"""
        from agent_core.agentloop.capability_gap_counter import CapabilityGapCounter

        counter = CapabilityGapCounter(
            context_db=sqlite_db,
            config={
                "capability_gap_detection_enabled": True,
                "capability_gap_trigger_threshold": 2,
                "capability_gap_cooldown_hours": 0,
            },
        )

        await counter.increment("tool_c", "not found", "sess_x")
        await counter.increment("tool_c", "not found", "sess_x")
        assert await counter.should_trigger("tool_c", "sess_x") is True

        await counter.mark_triggered("tool_c", "sess_x")
        # 同 session 再次查询应 False（session 去重）
        assert await counter.should_trigger("tool_c", "sess_x") is False

    @pytest.mark.asyncio
    async def test_disabled_returns_zero(self, sqlite_db):
        """禁用时 increment 返回 0"""
        from agent_core.agentloop.capability_gap_counter import CapabilityGapCounter

        counter = CapabilityGapCounter(
            context_db=sqlite_db,
            config={"capability_gap_detection_enabled": False},
        )
        count = await counter.increment("tool", "not found", "s1")
        assert count == 0

    @pytest.mark.asyncio
    async def test_reset(self, sqlite_db):
        """reset 应清除指定工具的计数"""
        from agent_core.agentloop.capability_gap_counter import CapabilityGapCounter

        counter = CapabilityGapCounter(
            context_db=sqlite_db,
            config={"capability_gap_detection_enabled": True, "capability_gap_trigger_threshold": 2},
        )

        await counter.increment("tool_d", "not found", "s1")
        await counter.increment("tool_d", "missing", "s2")
        await counter.reset("tool_d")

        counter.clear_session_state()
        assert await counter.should_trigger("tool_d") is False


class TestRing3EvolutionTask:
    """3.4~3.5 EvolutionTaskManager"""

    @pytest.mark.asyncio
    async def test_create_task(self, sqlite_db, knowledge_store):
        """create_task 应创建进化任务并写入存储"""
        from agent_core.knowledge.evolution_task import EvolutionTaskManager

        manager = EvolutionTaskManager(
            store=knowledge_store,
            config={"evolution": {"enabled": True, "max_tasks_per_user": 40, "max_concurrent_tasks": 3}},
            llm_call=AsyncMock(return_value="{}"),
        )

        task = await manager.create_task("工具 X 无法处理 Y", USER_ID, INSTANCE_ID)
        assert task is not None
        assert task.gap_description == "工具 X 无法处理 Y"
        assert task.status == "pending"
        assert task.phase == "gap"

    @pytest.mark.asyncio
    async def test_create_task_disabled(self, knowledge_store):
        """evolution.enabled=False 时应返回 None"""
        from agent_core.knowledge.evolution_task import EvolutionTaskManager

        manager = EvolutionTaskManager(
            store=knowledge_store,
            config={"evolution": {"enabled": False}},
        )
        task = await manager.create_task("test", USER_ID, INSTANCE_ID)
        assert task is None

    @pytest.mark.asyncio
    async def test_max_concurrent_limit(self, sqlite_db, knowledge_store):
        """达到最大并发数后应返回 None"""
        from agent_core.knowledge.evolution_task import EvolutionTaskManager

        manager = EvolutionTaskManager(
            store=knowledge_store,
            config={"evolution": {"enabled": True, "max_concurrent_tasks": 2, "max_tasks_per_user": 40}},
            llm_call=AsyncMock(return_value="{}"),
        )

        t1 = await manager.create_task("gap1", USER_ID, INSTANCE_ID)
        t2 = await manager.create_task("gap2", USER_ID, INSTANCE_ID)
        assert t1 is not None and t2 is not None

        t3 = await manager.create_task("gap3", USER_ID, INSTANCE_ID)
        assert t3 is None, "超过最大并发数应返回 None"

    @pytest.mark.asyncio
    async def test_llm_judge_needs_skill_yes(self):
        """LLM 返回 YES 时 _llm_judge_needs_skill 应返回 True"""
        from agent_core.knowledge.evolution_task import EvolutionTaskManager

        mock_llm = AsyncMock(return_value="YES")
        manager = EvolutionTaskManager(
            store=MagicMock(),
            config={},
            llm_call=mock_llm,
        )
        result = await manager._llm_judge_needs_skill("需要新 API 工具", "调研结果")
        assert result is True

    @pytest.mark.asyncio
    async def test_llm_judge_needs_skill_no(self):
        """LLM 返回 NO 时应返回 False"""
        from agent_core.knowledge.evolution_task import EvolutionTaskManager

        mock_llm = AsyncMock(return_value="NO，知识规则可以解决")
        manager = EvolutionTaskManager(
            store=MagicMock(),
            config={},
            llm_call=mock_llm,
        )
        result = await manager._llm_judge_needs_skill("补充规则即可", "已有规则")
        assert result is False

    @pytest.mark.asyncio
    async def test_llm_judge_no_llm(self):
        """无 LLM 时应返回 False"""
        from agent_core.knowledge.evolution_task import EvolutionTaskManager

        manager = EvolutionTaskManager(
            store=MagicMock(),
            config={},
            llm_call=None,
        )
        result = await manager._llm_judge_needs_skill("test", "test")
        assert result is False

    @pytest.mark.asyncio
    async def test_skill_executor_param(self, knowledge_store):
        """EvolutionTaskManager 应接受 skill_executor 参数"""
        from agent_core.knowledge.evolution_task import EvolutionTaskManager

        mock_executor = MagicMock()
        manager = EvolutionTaskManager(
            store=knowledge_store,
            config={},
            llm_call=AsyncMock(),
            skill_executor=mock_executor,
        )
        assert manager._skill_executor is mock_executor


# ═══════════════════════════════════════════════════════════════════════════════
# 集成测试
# ═══════════════════════════════════════════════════════════════════════════════

class TestIntegration:
    """4.1~4.2 完整集成"""

    @pytest.mark.asyncio
    async def test_capability_gap_plugin_hook_registration(self):
        """CapabilityGapPlugin 应成功注册到 HookEngine"""
        from agent_core.agentloop.hook_engine import HookEngine, HookPlugin, HookPoint, HookRegistration
        from agent_core.agentloop.capability_gap_counter import CapabilityGapCounter

        # 构建简易 mock sqlite_db
        mock_db = MagicMock()
        mock_db._ensure_init = AsyncMock()
        mock_db._connect = MagicMock()

        # 定义 CapabilityGapPlugin（与 native_agent.py 中一致）
        class _CapabilityGapPlugin(HookPlugin):
            name = "capability_gap"

            def __init__(self):
                pass

            async def _on_post_tool(self, ctx: dict) -> dict:
                return ctx

            def get_hooks(self):
                return [
                    HookRegistration(
                        "capability_gap_post_tool",
                        HookPoint.POST_TOOL_USE,
                        self._on_post_tool,
                        priority=90,
                    ),
                ]

        engine = HookEngine()
        plugin = _CapabilityGapPlugin()
        engine.register_plugin(plugin)

        # 验证已注册（HookEngine 会给 name 加 plugin 前缀 "capability_gap::"）
        hooks = engine._hooks.get(HookPoint.POST_TOOL_USE, [])
        hook_names = [h.name for h in hooks]
        assert any("capability_gap_post_tool" in n for n in hook_names), f"Hook not found in {hook_names}"

    @pytest.mark.asyncio
    async def test_full_flow_tool_failure_to_evolution(self, sqlite_db, knowledge_store):
        """
        完整流程: 工具失败 3 次 → 达阈值 → 创建进化任务
        """
        from agent_core.agentloop.capability_gap_counter import CapabilityGapCounter
        from agent_core.knowledge.evolution_task import EvolutionTaskManager

        # 1. 设置 CapabilityGapCounter
        counter = CapabilityGapCounter(
            context_db=sqlite_db,
            config={
                "capability_gap_detection_enabled": True,
                "capability_gap_trigger_threshold": 3,
                "capability_gap_cooldown_hours": 0,
            },
        )

        # 2. 设置 EvolutionTaskManager
        evolution = EvolutionTaskManager(
            store=knowledge_store,
            config={"evolution": {"enabled": True, "max_tasks_per_user": 40, "max_concurrent_tasks": 5}},
            llm_call=AsyncMock(return_value="{}"),
        )

        # 3. 模拟 3 次工具失败
        tool_name = "broken_tool"
        for i in range(3):
            await counter.increment(tool_name, f"not found: attempt {i}", f"sess_{i}")

        # 4. 检查是否应触发
        should = await counter.should_trigger(tool_name, "final_sess")
        assert should is True, "3 次失败应达到阈值"

        # 5. 创建进化任务
        error_text = "not found: broken_tool 多次调用失败"
        gap = f"工具 {tool_name} 多次失败: {error_text[:100]}"
        task = await evolution.create_task(gap, USER_ID, INSTANCE_ID)
        assert task is not None
        assert "broken_tool" in task.gap_description

        # 6. 标记已触发
        await counter.mark_triggered(tool_name, "final_sess")

        # 7. 再次检查不应触发（session 去重）
        should2 = await counter.should_trigger(tool_name, "final_sess")
        assert should2 is False

    @pytest.mark.asyncio
    async def test_evolution_task_execute_phases(self, sqlite_db, knowledge_store):
        """进化任务执行应经过四个阶段"""
        from agent_core.knowledge.evolution_task import EvolutionTaskManager

        # 模拟 LLM 各阶段返回
        call_count = 0
        async def mock_llm(prompt, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return "分析完成"
            elif call_count == 3:
                # synthesize 阶段返回 JSON 知识
                return json.dumps([{
                    "category": "domain_fact",
                    "text": "测试知识",
                    "tags": ["test"],
                    "utility": 0.8,
                    "confidence": 0.7,
                }])
            else:
                return "NO"  # _llm_judge_needs_skill

        manager = EvolutionTaskManager(
            store=knowledge_store,
            config={"evolution": {
                "enabled": True, "max_tasks_per_user": 40, "max_concurrent_tasks": 5,
                "seek_timeout_seconds": 30,
            }},
            llm_call=mock_llm,
        )

        # 创建并执行任务
        task = await manager.create_task("测试缺口", USER_ID, INSTANCE_ID)
        completed = await manager.execute_pending_tasks(USER_ID, INSTANCE_ID)

        assert len(completed) == 1
        assert completed[0] == task.task_id

        # 验证知识已写入
        all_knowledge = await knowledge_store.get_all_knowledge(USER_ID, INSTANCE_ID)
        texts = [k.text for k in all_knowledge]
        assert "测试知识" in texts, "进化任务应产出新知识"

    @pytest.mark.asyncio
    async def test_imports_and_exports(self):
        """验证所有新增模块可正常导入"""
        from agent_core.agentloop import CapabilityGapCounter
        from agent_core.knowledge import SkillEvolver
        from agent_core.knowledge.evolution_task import EvolutionTaskManager
        from agent_core.knowledge.prediction_scheduler import PredictionScheduler
        from agent_core.knowledge.strategy_learner import StrategyLearner
        from agent_core.config import V4Config

        # 验证类存在且可实例化（最基本的烟雾测试）
        assert CapabilityGapCounter is not None
        assert SkillEvolver is not None
        assert EvolutionTaskManager is not None
        assert V4Config is not None

    @pytest.mark.asyncio
    async def test_gap_keyword_matching_comprehensive(self):
        """测试所有 GAP_KEYWORDS 是否生效"""
        from agent_core.agentloop.capability_gap_counter import _matches_gap_keyword

        # 应匹配
        assert _matches_gap_keyword("API endpoint not found") is True
        assert _matches_gap_keyword("该功能不支持") is True
        assert _matches_gap_keyword("无法处理此类请求") is True
        assert _matches_gap_keyword("tool missing from registry") is True
        assert _matches_gap_keyword("data unsupported format") is True
        assert _matches_gap_keyword("接口不存在") is True
        assert _matches_gap_keyword("功能未实现") is True
        assert _matches_gap_keyword("Not Implemented") is True
        assert _matches_gap_keyword("HTTP 404 error") is True
        assert _matches_gap_keyword("service unavailable") is True
        assert _matches_gap_keyword("no data returned") is True
        assert _matches_gap_keyword("查询无数据返回") is True

        # 不应匹配
        assert _matches_gap_keyword("timeout after 30s") is False
        assert _matches_gap_keyword("network connection reset") is False
        assert _matches_gap_keyword("rate limit exceeded") is False
        assert _matches_gap_keyword("") is False


# ═══════════════════════════════════════════════════════════════════════════════
# 模块 __init__ 导出验证
# ═══════════════════════════════════════════════════════════════════════════════

class TestModuleExports:
    """验证模块导出正确"""

    def test_agentloop_exports_capability_gap(self):
        import agent_core.agentloop as al
        assert hasattr(al, "CapabilityGapCounter")
        assert "CapabilityGapCounter" in al.__all__

    def test_knowledge_exports_skill_evolver(self):
        import agent_core.knowledge as kn
        assert hasattr(kn, "SkillEvolver")
        assert "SkillEvolver" in kn.__all__
