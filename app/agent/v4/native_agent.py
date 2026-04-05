"""
V4 Native Agent -- 基于 Claude Agent SDK 原生架构的核心编排类

职责:
1. 整合所有 V4 子组件: MCPToolBuilder, HookManager, EventBridge, PromptBuilder
2. 管理会话上下文: ConversationHistory, ExperienceStore
3. 构建 ClaudeAgentOptions 并调用 SDK
4. 消费 SDK 消息流 + Hook 旁路事件，输出统一 SSE 事件流
5. 支持知识库目录配置
6. 支持 render_mode 控制 UI 渲染
7. 置信度评估和报告生成

V4 解耦:
- 不导入 ResourceRegistry, ResourceLoader, SkillRegistry
- 技能发现: SkillDiscovery (SKILL.md)
- 技能执行: V4SkillExecutor (独立实例)
- MCP 构建: MCPToolBuilder
- UI 选择: V4UISelector
- 置信度: ConfidenceCalculator
"""
import re
import asyncio
import time
from contextlib import suppress

from typing import Dict, Any, List, Optional, AsyncIterator
from datetime import datetime
from loguru import logger

from app.prompts.loader import load_prompt as _load_prompt

try:
    from .knowledge import KnowledgeExtractor, KnowledgeStore
    _KNOWLEDGE_AVAILABLE = True
except ImportError:
    _KNOWLEDGE_AVAILABLE = False


from agent_core.agent import (
    V4AgentRequest, DataCollector,
    RENDER_MODE_AUTO, RENDER_MODE_TEXT_ONLY,
    CHANNEL_WEB, CHANNEL_DINGTALK, CHANNEL_FEISHU, CHANNEL_API,
    _SKILL_MENTION_RE,
)

class V4NativeAgent:
    """
    V4 Agent -- 完全独立于 V3 的原生 SDK Agent

    架构:
    - Agent Loop 由 ClaudeSDKClient 原生管理
    - Tool Execution 通过 MCP Server 协议驱动
    - 自定义逻辑通过 Hooks 注入
    - 技能发现基于 SKILL.md（不依赖 V3 SkillRegistry）
    - 技能执行使用独立实例（不共享 V3 实例）
    """

    _shared_evolver = None       # 类级别共享 SkillEvolver 实例
    _shared_knowledge_store = None  # 类级别共享 KnowledgeStore 实例

    @classmethod
    def get_skill_evolver(cls):
        """返回共享 SkillEvolver 实例（由首个 Agent 实例初始化时注册）"""
        return cls._shared_evolver

    @classmethod
    def get_knowledge_store(cls):
        """返回共享 KnowledgeStore 实例（用于分步评测回退）"""
        return cls._shared_knowledge_store

    def __init__(self):
        # V4 配置
        from .v4_config import V4Config
        self._config = V4Config.from_settings()

        # CLAW: 加载 skill_framework 配置
        self._skill_framework_config = self._load_skill_framework_config()

        # CLAW: 创建 SkillQualifier（资格检查）
        self._qualifier = None
        qualifier_config = self._skill_framework_config.get("qualifier", {})
        if qualifier_config.get("enabled", False):
            try:
                from agent_core.skill_qualifier import SkillQualifier
                self._qualifier = SkillQualifier(qualifier_config)
                logger.info("[V4] SkillQualifier initialized")
            except Exception as e:
                logger.warning(f"[V4] SkillQualifier init failed: {e}")

        # V4 技能发现（CLAW: 增强为多层发现）
        from agent_core.skill_discovery import SkillDiscovery
        discovery_config = self._skill_framework_config.get("discovery", {})
        extra_dirs = []
        bundled_dir = None
        if discovery_config.get("enabled", False):
            for layer in discovery_config.get("layers", []):
                if layer.get("name") == "extra" and layer.get("paths"):
                    extra_dirs = layer["paths"]
                elif layer.get("name") == "bundled" and layer.get("path"):
                    bundled_dir = layer["path"]

        self._discovery = SkillDiscovery(
            skills_dir=self._config.skills_dir,
            extra_dirs=extra_dirs,
            bundled_dir=bundled_dir,
            qualifier=self._qualifier,
        )
        self._discovery.scan()

        # V4 技能执行器
        from agent_core.skill_executor import V4SkillExecutor
        self._executor = V4SkillExecutor(self._discovery)
        self._executor.load_all()

        # 动态技能加载器（延迟初始化，需要数据库连接）

        # V4 MCP 工具构建器
        from .mcp_tool_builder import MCPToolBuilder
        self._mcp_builder = MCPToolBuilder(
            config=self._config,
            discovery=self._discovery,
            executor=self._executor,
        )

        # V4 UI 选择器
        from .ui_selector import V4UISelector
        self._ui_selector = V4UISelector(self._discovery)

        # V4 Persistence Layer（skill 输出持久化，用于 data2ui 历史恢复）
        _persistence = None
        self._persistence = None
        try:
            from .persistence import V4PersistenceLayer
            _persistence = V4PersistenceLayer(
                default_user_id=self._config.default_user_id,
            )
            self._persistence = _persistence
        except Exception as e:
            logger.debug(f"[V4] V4PersistenceLayer init skipped: {e}")

        # V4 Hook 管理器
        from .hook_manager import HookManager
        self._hook_manager = HookManager(
            ui_selector=self._ui_selector,
            discovery=self._discovery,
            persistence=_persistence,
        )

        # V4 事件桥接（已解耦，无 V3 依赖）
        from .event_bridge import EventBridge
        self._event_bridge = EventBridge()

        # CLAW: 创建 PromptBudgetGuard（提示词预算控制）
        self._prompt_budget_guard = None
        budget_config = self._skill_framework_config.get("prompt_budget", {})
        if budget_config.get("enabled", False):
            try:
                from .prompt_budget_guard import PromptBudgetGuard
                self._prompt_budget_guard = PromptBudgetGuard(budget_config)
                logger.info("[V4] PromptBudgetGuard initialized")
            except Exception as e:
                logger.warning(f"[V4] PromptBudgetGuard init failed: {e}")

        # V4 Prompt 构建器（CLAW: 注入 budget guard）
        from .prompt_builder import PromptBuilder
        self._prompt_builder = PromptBuilder(
            discovery=self._discovery,
            prompt_budget_guard=self._prompt_budget_guard,
        )

        # V4 智能规划链（LLM 输出解析器）
        from .thinking_chain import V4PlanningChain
        self._planning_chain = V4PlanningChain()

        self._skill_registry = None

        # CLAW: ComposableSkillEngine（延迟初始化，需要 llm_client）
        self._composable_engine = None

        # 上下文管理和经验学习（延迟初始化，依赖 Redis/SQLite）
        self._history = None
        self._experience = None
        self._context_initialized = False

        # Session 上下文增强组件（延迟初始化）
        self._sqlite_db = None          # SQLite 存储层
        self._context_guard = None      # Token 预算分配器
        self._progressive_summarizer = None  # 渐进式摘要引擎

        # MemoryOS 三层记忆引擎（延迟初始化，memory_engine_enabled=true 时生效）
        self._memory_engine = None

        # Knowledge Engine (agent_core) — 全量知识引擎组件
        self._ke_store = None
        self._ke_episode_tracker = None
        self._ke_temporal = None
        self._ke_distiller = None
        self._ke_feedback_learner = None
        self._ke_reflection = None
        self._ke_evolution = None
        self._ke_crystallizer = None
        self._ke_skill_evolver = None     # Ring 2: DGM Skill Patch
        self._ke_guard = None

        # Session 管理器 v2（延迟初始化）

        # 热加载检查时间戳
        self._last_reload_check = 0.0

        self._knowledge_extractor = None
        self._knowledge_store = None

        # 输出处理器（延迟初始化）
        self._output_processor = None
        self._output_processor_initialized = False

        # Agentic Loop 反思增强（配置驱动）
        self._agentic_config = self._load_agentic_loop_config()
        self._agentic_reflection = None
        if self._agentic_config.get("reflection", {}).get("enabled", False):
            try:
                from .agentic_reflection import AgenticReflection
                self._agentic_reflection = AgenticReflection(
                    self._agentic_config.get("reflection", {}),
                )
                logger.info("[V4] AgenticReflection initialized")
            except Exception as e:
                logger.warning(f"[V4] AgenticReflection init failed: {e}")

        logger.info("[V4] NativeAgent initialized (fully decoupled)")

    def _ensure_context_managers(self):
        """延迟初始化上下文管理器（需要 Redis/SQLite 连接）"""
        if self._context_initialized:
            return

        try:
            # ── 初始化 ContextWindowGuard（不依赖外部服务） ──
            try:
                from agent_core.session.context_window_guard import ContextWindowGuard
                self._context_guard = ContextWindowGuard(
                    model_name=getattr(self._config, "anthropic_model", "") or "",
                    context_window_override=getattr(self._config, "context_window_override", 0) or 0,
                )
                logger.info("[V4] ContextWindowGuard initialized")
            except Exception as e:
                logger.warning(f"[V4] ContextWindowGuard init failed: {e}")

            # ── 初始化 SQLite 存储层（根据配置） ──
            storage_backend = getattr(self._config, "session_storage_backend", "sqlite")
            if storage_backend in ("sqlite", "both"):
                try:
                    from agent_core.session.context_db import SessionContextDB
                    db_path_template = getattr(
                        self._config, "sqlite_db_path_template",
                        "app/data/sessions/{instance_id}/memory.db",
                    )
                    db_path = db_path_template.format(
                        instance_id=self._config.instance_id,
                    )
                    self._sqlite_db = SessionContextDB(
                        db_path=db_path,
                        wal_mode=getattr(self._config, "sqlite_wal_mode", True),
                        busy_timeout_ms=getattr(self._config, "sqlite_busy_timeout_ms", 5000),
                    )
                    logger.info(
                        f"[V4] SessionContextDB initialized "
                        f"(path={db_path}, backend={storage_backend})"
                    )
                except Exception as e:
                    logger.warning(f"[V4] SQLite init failed: {e}")

            # ── 初始化 ProgressiveSummarizer（如果启用） ──
            if (
                getattr(self._config, "progressive_summarize_enabled", True)
                and self._context_guard
            ):
                try:
                    from agent_core.session.progressive_summarizer import ProgressiveSummarizer
                    self._progressive_summarizer = ProgressiveSummarizer(
                        config=self._config,
                        guard=self._context_guard,
                    )
                    logger.info("[V4] ProgressiveSummarizer initialized")
                except Exception as e:
                    logger.warning(f"[V4] ProgressiveSummarizer init failed: {e}")

            if self._sqlite_db:
                from agent_core.session.conversation_history import (
                    ConversationHistoryCore as ConversationHistory,
                )
                from agent_core.session.experience_store import (
                    ExperienceStoreCore as ExperienceStore,
                )

                user_id = self._config.default_user_id

                self._history = ConversationHistory(
                    history_ttl=self._config.conversation_history_ttl,
                    summary_ttl=self._config.conversation_summary_ttl,
                    user_id=user_id,
                    sqlite_db=self._sqlite_db,
                )
                self._experience = ExperienceStore(
                    ttl=self._config.experience_ttl,
                    max_items={
                        "user_preferences": self._config.experience_max_preferences,
                        "stock_insights": self._config.experience_max_insights,
                        "learned_patterns": self._config.experience_max_patterns,
                        "corrections": self._config.experience_max_corrections,
                    },
                    user_id=user_id,
                    sqlite_db=self._sqlite_db,
                )

                logger.info(
                    f"[V4] Context managers initialized "
                    f"(backend={storage_backend}, "
                    f"sqlite={'yes' if self._sqlite_db else 'no'})"
                )
            else:
                logger.warning("[V4] No storage backend available, context management disabled")
        except Exception as e:
            logger.warning(f"[V4] Failed to initialize context managers: {e}")

        # 初始化 MemoryOS 三层记忆引擎（灰度开关控制）
        self._init_memory_engine()

        # 初始化 Knowledge Engine (agent_core) — 全量进化学习引擎
        self._init_knowledge_engine()

        self._context_initialized = True

    @staticmethod
    def _load_skill_framework_config() -> dict:
        """
        从 app/config/skill_framework.json 加载 CLAW Skill 框架配置

        Returns:
            配置字典，加载失败返回空 dict（降级兼容）
        """
        try:
            import json
            from pathlib import Path
            config_path = (
                Path(__file__).parent.parent.parent
                / "config" / "skill_framework.json"
            )
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                logger.info(f"[V4] Loaded skill_framework.json: {list(config.keys())}")
                return config
        except Exception as e:
            logger.warning(f"[V4] Failed to load skill_framework.json: {e}")
        return {}

    @staticmethod
    def _load_agentic_loop_config() -> dict:
        """
        从 agent_core/agentloop/agentic_loop.json 加载 Agentic Loop 配置
        """
        try:
            import json as _json
            from pathlib import Path
            config_path = (
                Path(__file__).parent.parent.parent.parent
                / "agent_core" / "agentloop" / "agentic_loop.json"
            )
            if config_path.exists():
                config = _json.loads(config_path.read_text(encoding="utf-8"))
                logger.info(f"[V4] Loaded agentic_loop.json: {list(config.keys())}")
                return config
        except Exception as e:
            logger.warning(f"[V4] Failed to load agentic_loop.json: {e}")
        return {}

    def _init_memory_engine(self):
        """
        初始化 MemoryOS 三层记忆引擎

        仅在 memory_engine_enabled=True 且 SQLite 可用时初始化。
        灰度开关关闭时完全不加载任何记忆模块。
        """
        if not getattr(self._config, "memory_engine_enabled", False):
            logger.debug("[V4] Memory engine disabled by config")
            return

        if not self._sqlite_db:
            logger.warning("[V4] Memory engine requires SQLite, skipping")
            return

        try:
            from agent_core.memory.mid_term_memory import MidTermMemory
            from agent_core.memory.long_term_memory import UserProfileStore
            from agent_core.memory.memory_retriever import MemoryRetriever
            from agent_core.memory.memory_updater import MemoryUpdater
            from agent_core.memory.memory_cleanup import MemoryCleanupScheduler
            from agent_core.memory.embedding_client import EmbeddingClient
            from .llm_helpers import update_user_profile, extract_knowledge

            user_id = getattr(self._config, "default_user_id", 1)
            instance_id = self._config.instance_id

            embedding_client = EmbeddingClient()
            if not embedding_client.enabled:
                logger.info("[V4] EmbeddingClient: no API key, all layers use heat/score-based recall")
                embedding_client = None

            # 将 embedding_client 注入 ExperienceStore（后台写 BLOB + 语义召回）
            if self._experience is not None and embedding_client is not None:
                self._experience._embedding_client = embedding_client

            mtm = MidTermMemory(
                sqlite_db=self._sqlite_db,
                user_id=user_id,
                instance_id=instance_id,
                max_pages=self._config.mtm_max_pages,
                alpha=self._config.mtm_heat_alpha,
                beta=self._config.mtm_heat_beta,
                gamma=self._config.mtm_heat_gamma,
                tau=self._config.mtm_heat_tau,
                embedding_client=embedding_client,
            )

            profile_store = UserProfileStore(
                sqlite_db=self._sqlite_db,
                user_id=user_id,
                instance_id=instance_id,
            )

            retriever = MemoryRetriever(
                mtm=mtm,
                profile_store=profile_store,
                experience_store=self._experience,
                guard=self._context_guard,
                embedding_client=embedding_client,
            )

            updater = MemoryUpdater(
                mtm=mtm,
                profile_store=profile_store,
                experience_store=self._experience,
                profile_update_fn=update_user_profile,
                knowledge_extract_fn=extract_knowledge,
                ltm_promotion_threshold=self._config.ltm_promotion_threshold,
            )

            cleanup = MemoryCleanupScheduler(
                mtm=mtm,
                interval_hours=self._config.memory_cleanup_interval_hours,
                max_age_days=self._config.mtm_max_age_days,
            )
            cleanup.start()

            self._memory_engine = {
                "mtm": mtm,
                "profile": profile_store,
                "retriever": retriever,
                "updater": updater,
                "cleanup": cleanup,
                "embedding_client": embedding_client,
                "knowledge_config": {},  # session_engine 中 KnowledgeRetriever 的配置占位
            }
            logger.info(
                f"[V4] MemoryOS engine initialized "
                f"(mtm_max={self._config.mtm_max_pages}, "
                f"ltm_threshold={self._config.ltm_promotion_threshold})"
            )
        except Exception as e:
            logger.warning(f"[V4] Memory engine init failed: {e}")
            self._memory_engine = None

    def _init_knowledge_engine(self):
        """
        初始化 agent_core Knowledge Engine（全量进化学习引擎）

        仅在 knowledge_engine_enabled=True 且 SQLite 可用时初始化。
        通过 HookManager.set_knowledge_tracker() 挂载到问答流程。
        失败不影响主流程。
        """
        if not getattr(self._config, "knowledge_engine_enabled", True):
            logger.debug("[V4] Knowledge engine disabled by config")
            return

        if not self._sqlite_db:
            logger.warning("[V4] Knowledge engine requires SQLite, skipping")
            return

        try:
            from agent_core.knowledge.config_loader import load_knowledge_config
            from agent_core.knowledge.store import KnowledgeStore
            from agent_core.knowledge.episode_tracker import EpisodeTracker
            from agent_core.knowledge.temporal import TemporalKnowledgeManager
            from agent_core.knowledge.guard import KnowledgeEngineGuard

            ke_config = load_knowledge_config(
                getattr(self._config, "knowledge_engine_config_path", "agent_core/knowledge/knowledge_engine.json")
            )

            # 复用已有 embedding_client（若 MemoryOS 已初始化）
            _ke_embedding_client = (
                self._memory_engine.get("embedding_client")
                if self._memory_engine else None
            )
            self._ke_store = KnowledgeStore(self._sqlite_db, ke_config, embedding_client=_ke_embedding_client)
            V4NativeAgent._shared_knowledge_store = self._ke_store  # 暴露给 cron 分步评测
            self._ke_episode_tracker = EpisodeTracker(ke_config)
            self._ke_temporal = TemporalKnowledgeManager(self._ke_store, ke_config)
            self._ke_guard = KnowledgeEngineGuard(
                retrieve_timeout_ms=getattr(self._config, "knowledge_engine_retrieve_timeout_ms", 50),
                finalize_timeout_ms=30_000,  # 30s，足够完成 Episode 写入 + LLM 蒸馏
            )

            # Opt 7: 辅助 LLM 调用（全局 provider）
            from agent_core.agentloop.llm_provider import call_llm
            _llm_call = call_llm

            # 高级模块（全部 try-import，失败不影响基础功能）
            # 知识图谱（graph_enabled 控制开关）
            _graph_enabled = getattr(self._config, "knowledge_graph_enabled", True)
            _graph_store = None
            _graph_distiller = None
            _graph_retriever = None
            if _graph_enabled:
                try:
                    from agent_core.knowledge.graph_store import GraphStore
                    from agent_core.knowledge.graph_distiller import GraphDistiller
                    from agent_core.knowledge.graph_retriever import GraphRetriever
                    _graph_store = GraphStore(self._sqlite_db)
                    _graph_distiller = GraphDistiller(_graph_store)
                    _graph_retriever = GraphRetriever(
                        _graph_store,
                        max_hops=getattr(self._config, "knowledge_graph_max_hops", 2),
                        max_edges=getattr(self._config, "knowledge_graph_max_edges_in_prompt", 15),
                    )
                    self._ke_graph_retriever = _graph_retriever
                    logger.info("[V4] Knowledge graph initialized")
                except Exception as e:
                    logger.debug(f"[V4] Knowledge graph init skipped: {e}")

            try:
                from agent_core.knowledge.distiller import KnowledgeDistiller
                self._ke_distiller = KnowledgeDistiller(
                    self._ke_store, ke_config.get("distiller", {}),
                    llm_call=_llm_call,
                    graph_distiller=_graph_distiller,
                )
            except Exception as e:
                logger.debug(f"[V4] KE Distiller init skipped: {e}")

            try:
                from agent_core.knowledge.feedback_learner import FeedbackLearner
                self._ke_feedback_learner = FeedbackLearner(
                    self._ke_store, ke_config.get("feedback_learner", {}),
                    llm_call=_llm_call,
                )
            except Exception as e:
                logger.debug(f"[V4] KE FeedbackLearner init skipped: {e}")

            try:
                from agent_core.knowledge.reflection import ReflectionEngine
                self._ke_reflection = ReflectionEngine(
                    self._ke_store, ke_config.get("reflection", {}),
                    llm_call=_llm_call,
                    temporal_manager=self._ke_temporal,
                )
            except Exception as e:
                logger.debug(f"[V4] KE ReflectionEngine init skipped: {e}")

            try:
                from agent_core.knowledge.evolution_task import EvolutionTaskManager
                self._ke_evolution = EvolutionTaskManager(
                    self._ke_store, ke_config.get("evolution", {}),
                    llm_call=_llm_call,
                    temporal_manager=self._ke_temporal,
                    skill_executor=self._executor,  # Ring 3: 进化任务可触发 skill-creator
                )
            except Exception as e:
                logger.debug(f"[V4] KE EvolutionTaskManager init skipped: {e}")

            try:
                from agent_core.knowledge.skill_crystallizer import SkillCrystallizer
                self._ke_crystallizer = SkillCrystallizer(
                    self._ke_store, ke_config.get("crystallizer", {}),
                    llm_call=_llm_call,
                )
            except Exception as e:
                logger.debug(f"[V4] KE SkillCrystallizer init skipped: {e}")

            # 注入下游模块到 EpisodeTracker（蒸馏 + 反思）
            if self._ke_episode_tracker:
                self._ke_episode_tracker.set_downstream(
                    distiller=getattr(self, "_ke_distiller", None),
                    reflection=getattr(self, "_ke_reflection", None),
                    temporal_manager=self._ke_temporal,
                )

            # 挂载到 HookManager（通过 set_knowledge_tracker 连接到问答流程）
            if self._hook_manager and self._ke_episode_tracker:
                self._hook_manager.set_knowledge_tracker(
                    tracker=self._ke_episode_tracker,
                    store=self._ke_store,
                    guard=self._ke_guard,
                )

            # 初始化预测验证模块（PredictionStore + Extractor + Scheduler）
            _prediction_store = None
            _prediction_extractor = None
            try:
                from agent_core.knowledge.prediction_store import PredictionStore
                from agent_core.knowledge.prediction_extractor import PredictionExtractor
                _prediction_store = PredictionStore(self._sqlite_db)
                _pred_enabled = getattr(self._config, "prediction_extraction_enabled", True)
                _prediction_extractor = PredictionExtractor(
                    llm_call=_llm_call,
                    prediction_store=_prediction_store,
                    enabled=_pred_enabled,
                )
                self._ke_prediction_store = _prediction_store
                self._ke_prediction_extractor = _prediction_extractor
                logger.info("[V4] PredictionStore + Extractor initialized")
            except Exception as e:
                logger.debug(f"[V4] PredictionStore init skipped: {e}")

            # 保存 prediction_store 引用（per-request inject_components 时注入）
            self._ke_prediction_store = _prediction_store

            # 注入 prediction_extractor 到 hook_manager（用于 Stop Hook 异步提取）
            if _prediction_extractor and self._hook_manager:
                try:
                    self._hook_manager.set_prediction_extractor(_prediction_extractor)
                except Exception as e:
                    logger.debug(f"[V4] inject prediction_extractor to hook_manager failed: {e}")

            # 初始化 PredictionScheduler（注册定时任务）
            try:
                from agent_core.knowledge.prediction_verifier import PredictionVerifier
                from agent_core.knowledge.strategy_learner import StrategyLearner
                from agent_core.knowledge.prediction_scheduler import PredictionScheduler

                _verifier = PredictionVerifier(
                    prediction_store=_prediction_store,
                    llm_call=_llm_call,
                )
                _learner = StrategyLearner(
                    prediction_store=_prediction_store,
                    knowledge_store=self._ke_store,
                    graph_store=_graph_store,
                    llm_call=_llm_call,
                    config=ke_config,
                    context_db=self._sqlite_db,  # Ring 1: 高置信规则同步到 user_experiences
                )

                # Ring 1: 构建 LearnEvaluator（准确率快照对比）
                _learn_evaluator = None
                try:
                    from agent_core.knowledge.learn_evaluator import LearnEvaluator
                    _learn_evaluator = LearnEvaluator(
                        prediction_store=_prediction_store,
                        knowledge_store=self._ke_store,
                        sqlite_db=self._sqlite_db,
                        llm_call=_llm_call,
                    )
                except Exception as e:
                    logger.debug(f"[V4] LearnEvaluator init skipped: {e}")

                # 获取 CronService（startup 已通过 register_prediction_jobs 注册，此处仅作补充）
                try:
                    from app.main import get_cron_service as _get_cron
                    _cron_service = _get_cron()
                except Exception:
                    _cron_service = None
                if _cron_service and _prediction_store:
                    _scheduler = PredictionScheduler(
                        prediction_verifier=_verifier,
                        strategy_learner=_learner,
                        cron_service=_cron_service,
                        config=ke_config,
                        learn_evaluator=_learn_evaluator,       # Ring 1: 准确率快照
                        evolution_manager=self._ke_evolution,   # Ring 1: 准确率下降时触发进化
                    )
                    _default_user_id = getattr(self._config, "default_user_id", 1)
                    _instance_id = getattr(self._config, "instance_id", "default")
                    _scheduler.register_jobs(
                        user_id=_default_user_id,
                        instance_id=_instance_id,
                    )
                    self._ke_prediction_scheduler = _scheduler
                    logger.info("[V4] PredictionScheduler registered")
            except Exception as e:
                logger.debug(f"[V4] PredictionScheduler init skipped: {e}")

            # Ring 2: SkillEvolver（DGM Patch 机制）
            self._ke_skill_evolver = None
            try:
                if getattr(self._config, "skill_evolution_enabled", False):
                    from agent_core.knowledge.skill_evolver import SkillEvolver

                    # 构建 get_skill_error_stats 回调（app 层 → agent_core 层桥接）
                    async def _get_skill_error_stats(window_days: int):
                        if self._persistence:
                            return await self._persistence.get_skill_error_stats(window_days)
                        return []

                    self._ke_skill_evolver = SkillEvolver(
                        knowledge_store=self._ke_store,
                        prediction_store=_prediction_store,
                        skills_dir=self._config.skills_dir,
                        llm_call=_llm_call,
                        config={
                            "skill_evolution_enabled": True,
                            "skill_evolution_error_threshold": getattr(self._config, "skill_evolution_error_threshold", 0.3),
                            "skill_evolution_min_calls": getattr(self._config, "skill_evolution_min_calls", 5),
                            "skill_evolution_window_days": getattr(self._config, "skill_evolution_window_days", 7),
                            "skill_evolution_patch_per_day": getattr(self._config, "skill_evolution_patch_per_day", 1),
                        },
                        get_skill_error_stats=_get_skill_error_stats,
                    )

                    # 注册为类级别共享实例，供 main.py cron handler 使用
                    V4NativeAgent._shared_evolver = self._ke_skill_evolver
                    logger.info("[V4] SkillEvolver initialized (Ring 2)")
            except Exception as e:
                logger.debug(f"[V4] SkillEvolver init skipped: {e}")

            # AutoDream: DreamConsolidator（周期性深度记忆整合）
            self._ke_dream_consolidator = None
            try:
                if getattr(self._config, "dream_enabled", True):
                    from agent_core.knowledge.dream_consolidator import DreamConsolidator
                    self._ke_dream_consolidator = DreamConsolidator(
                        config=self._config,
                        knowledge_store=self._ke_store,
                        graph_store=_graph_store,
                        mtm=self._memory_engine.get("mtm") if self._memory_engine else None,
                        sqlite_db=self._sqlite_db,
                        llm_call=_llm_call,
                    )
                    logger.info("[V4] DreamConsolidator initialized")
            except Exception as e:
                logger.warning(f"[V4] DreamConsolidator init skipped: {e}")

            logger.info(
                f"[V4] Knowledge engine initialized "
                f"(store={'ok' if self._ke_store else 'no'}, "
                f"tracker={'ok' if self._ke_episode_tracker else 'no'}, "
                f"distiller={'ok' if self._ke_distiller else 'no'}, "
                f"feedback={'ok' if self._ke_feedback_learner else 'no'}, "
                f"reflection={'ok' if self._ke_reflection else 'no'}, "
                f"evolution={'ok' if self._ke_evolution else 'no'}, "
                f"crystallizer={'ok' if self._ke_crystallizer else 'no'}, "
                f"prediction={'ok' if _prediction_store else 'no'}, "
                f"skill_evolver={'ok' if self._ke_skill_evolver else 'no'}, "
                f"dream={'ok' if self._ke_dream_consolidator else 'no'})"
            )
        except Exception as e:
            logger.warning(f"[V4] Knowledge engine init failed: {e}")

    def _init_output_processor(self):
        """初始化输出处理器"""
        if self._output_processor_initialized:
            return

        try:
            from .output_processor import OutputProcessor
            self._output_processor = OutputProcessor()
            self._output_processor_initialized = True
            logger.info("[V4] OutputProcessor initialized")
        except Exception as e:
            logger.warning(f"[V4] Failed to initialize OutputProcessor: {e}")

    async def _analyze_quality_focus(
        self,
        rewritten_query: str,
        matched_tools: List[str],
    ) -> Dict[str, float]:
        """
        分析查询的质量倾向权重

        使用 LLM 从查询语义推断 4 维质量需求权重，
        失败时降级到基于工具映射的默认值。

        Args:
            rewritten_query: Phase 0 改写后的查询
            matched_tools: Phase 0 匹配到的工具列表

        Returns:
            质量倾向权重 {"correctness": 0.0-1.0, ...}
        """
        import json as _json

        try:
            prompt = _load_prompt(
                "v4_quality_focus",
                current_datetime=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                rewritten_query=rewritten_query,
                matched_tools=", ".join(matched_tools) if matched_tools else "无",
            )

            if not prompt:
                logger.warning("[V4-QualityFocus] Prompt template not found, using defaults")
                return self._get_default_quality_focus(matched_tools)

            # 使用 LLMCallerWrapper 调用 LLM（复用置信度模块的 wrapper）
            result_text = ""
            try:
                async for chunk in self._stream_llm_format(prompt):
                    result_text += chunk
            except Exception as e:
                logger.warning(f"[V4-QualityFocus] LLM call failed: {e}")
                return self._get_default_quality_focus(matched_tools)

            if not result_text.strip():
                return self._get_default_quality_focus(matched_tools)

            # 解析 JSON
            text = result_text.strip()
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0]
            elif "```" in text:
                parts = text.split("```")
                if len(parts) >= 2:
                    text = parts[1]

            parsed = _json.loads(text.strip())

            # 校验 4 个 key 且值在 [0, 1] 范围
            required_keys = ["correctness", "timeliness", "coverage", "validity"]
            result = {}
            for key in required_keys:
                val = float(parsed.get(key, 0.5))
                result[key] = max(0.0, min(1.0, val))

            # 提取 search_time_range（默认 "none"，无效值也回退 "none"）
            _valid_ranges = {"1d", "7d", "30d", "1y", "none"}
            _raw_range = str(parsed.get("search_time_range", "none")).strip().lower()
            result["search_time_range"] = _raw_range if _raw_range in _valid_ranges else "none"

            logger.info(f"[V4-QualityFocus] Analyzed: {result}")
            return result

        except Exception as e:
            logger.warning(f"[V4-QualityFocus] Analysis failed: {e}")
            return self._get_default_quality_focus(matched_tools)

    def _get_default_quality_focus(
        self,
        matched_tools: List[str] = None,
    ) -> Dict[str, float]:
        """LLM 不可用时的兜底质量倾向（均衡默认值）"""
        return {"correctness": 0.5, "timeliness": 0.5, "coverage": 0.5, "validity": 0.5, "search_time_range": "none"}

    @staticmethod
    def _dimension_to_warning(dim_name: str, score: int, report_lang: str = "zh") -> str:
        """
        将置信度弱维度转为质量警告

        Args:
            dim_name: 维度名称
            score: 维度分数（0-100）
            report_lang: 报告语种

        Returns:
            警告文本，未知维度返回空字符串
        """
        if report_lang == "en":
            warnings_map = {
                "source_authority": f"Source authority is low ({score}), cross-verification recommended",
                "data_freshness": f"Data freshness is insufficient ({score}), information may be outdated",
                "data_verification": f"Data verification is low ({score}), some information unverified",
                "content_objectivity": f"Content objectivity is insufficient ({score}), potential bias",
                "question_coverage": f"Question coverage is low ({score}), some aspects not addressed",
                "multi_source": f"Multi-source validation is insufficient ({score}), single source dependency",
            }
        else:
            warnings_map = {
                "source_authority": f"数据来源权威性偏低（{score}分），建议交叉验证",
                "data_freshness": f"数据时效性不足（{score}分），信息可能已过时",
                "data_verification": f"数据验证度偏低（{score}分），部分信息未经核实",
                "content_objectivity": f"内容客观性不足（{score}分），可能存在偏见",
                "question_coverage": f"问题覆盖度不足（{score}分），部分方面未涉及",
                "multi_source": f"多源验证不足（{score}分），信息来源单一",
            }
        return warnings_map.get(dim_name, "")

    async def _process_skill_output(
        self,
        result: any,
        skill_name: str,
        user_id: int,
        session_id: str,
        query: str,
        start_time: float,
        db_session=None,
    ):
        """
        处理技能输出并持久化

        Args:
            result: 技能执行结果
            skill_name: 技能名称
            user_id: 用户 ID
            session_id: 会话 ID
            query: 原始查询
            start_time: 开始时间
            db_session: 数据库会话

        Returns:
            ProcessResult 或 None
        """
        # 确保 OutputProcessor 已初始化
        self._init_output_processor()

        if not self._output_processor:
            return None

        try:
            from agent_core.skill_output_adapter import adapt_skill_result

            # 转换为统一格式
            skill_output = adapt_skill_result(
                result=result,
                skill_name=skill_name,
                user_id=user_id,
                session_id=session_id,
                query=query,
                start_time=start_time,
            )

            # 处理并持久化
            process_result = await self._output_processor.process(
                output=skill_output,
                db_session=db_session,
            )

            logger.debug(
                f"[V4] Skill output processed: skill={skill_name}, "
                f"db_id={process_result.db_id}, items={skill_output.items_count}"
            )

            return process_result

        except Exception as e:
            logger.warning(f"[V4] Failed to process skill output: {e}")
            return None

    async def _process_attached_images(self, attached_files: List[Dict[str, Any]]) -> str:
        """
        @deprecated 已弃用 — 图片/文件附件改为在 Phase 2 由 LLM 调用 document_reader 工具处理。
        保留方法体仅供回退参考，后续版本将删除。

        原职责: 处理附件中的图片：下载并调用视觉模型识别内容
        """
        import warnings
        warnings.warn(
            "_process_attached_images is deprecated, use document_reader skill in Phase 2 instead",
            DeprecationWarning,
            stacklevel=2,
        )
        import httpx
        import base64

        image_contents = []

        for af in attached_files:
            if af.get("type") != "image":
                continue

            download_code = af.get("download_code", "")
            robot_code = af.get("robot_code", "")

            if not download_code:
                continue

            try:
                # 下载图片
                from app.channels.dingtalk.media import download_dingtalk_media
                image_bytes = await download_dingtalk_media(
                    download_code=download_code,
                    robot_code=robot_code,
                )

                if not image_bytes:
                    logger.warning(f"[V4] Failed to download image: download_code={download_code[:20]}...")
                    continue

                # 调用视觉模型识别图片内容
                image_base64 = base64.b64encode(image_bytes).decode("utf-8")

                # 使用配置的视觉模型
                vision_model = getattr(self._config, 'vision_model', 'qwen3-vl-plus-2025-09-23') or 'qwen3-vl-plus-2025-09-23'
                vision_api_key = getattr(self._config, 'vision_api_key', None) or getattr(self._config, 'anthropic_api_key', '')
                vision_base_url = getattr(self._config, 'vision_api_base_url', None) or getattr(self._config, 'anthropic_base_url', '')

                if not vision_api_key or not vision_base_url:
                    logger.warning("[V4] Vision API not configured, skipping image analysis")
                    continue

                # 调用视觉 API
                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.post(
                        f"{vision_base_url}/messages",
                        headers={
                            "Authorization": f"Bearer {vision_api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": vision_model,
                            "max_tokens": 1024,
                            "messages": [
                                {
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "image",
                                            "source": {
                                                "type": "base64",
                                                "media_type": "image/png",
                                                "data": image_base64,
                                            },
                                        },
                                        {
                                            "type": "text",
                                            "text": "请详细描述这张图片的内容，包括文字、图表、数据等所有可见信息。",
                                        },
                                    ],
                                }
                            ],
                        },
                    )

                    if response.status_code == 200:
                        result = response.json()
                        content = result.get("content", [])
                        if content and len(content) > 0:
                            text = content[0].get("text", "")
                            if text:
                                image_contents.append(text)
                                logger.info(f"[V4] Image analyzed: {text[:100]}...")
                    else:
                        logger.warning(f"[V4] Vision API failed: {response.status_code} - {response.text[:200]}")

            except Exception as e:
                logger.error(f"[V4] Image processing failed: {e}")

        return "\n\n".join(image_contents)

    def _preprocess_message(self, request: V4AgentRequest) -> tuple:
        """
        预处理消息: 解析 @skill_name 提及 或 使用 force_skills 参数

        Returns:
            (cleaned_message, mentioned_skills, skill_params)
            - cleaned_message: 移除 @xxx 后的消息
            - mentioned_skills: 匹配到的技能名列表
            - skill_params: 技能参数（来自 request.skill_params 或 @skill(params) 解析）
        """
        message = request.message
        skill_params = request.skill_params or {}

        # 1. 优先使用 force_skills 参数（前端已解析）
        if request.force_skills:
            all_skill_names = {m.name for m in self._discovery.get_all()}
            valid_skills = [s for s in request.force_skills if s in all_skill_names]
            if valid_skills:
                logger.info(
                    f"[V4] force_skills provided: {valid_skills}, "
                    f"params: {skill_params}"
                )
                return message, valid_skills, skill_params

        # 2. 回退到 @skill 正则解析
        mentions = _SKILL_MENTION_RE.findall(message)

        if not mentions:
            return message, [], {}

        # 验证 @mention 是否为已知技能
        all_skill_names = {m.name for m in self._discovery.get_all()}
        valid_skills = [m for m in mentions if m in all_skill_names]

        if not valid_skills:
            return message, [], {}

        # 清理消息: 移除 @xxx
        cleaned = _SKILL_MENTION_RE.sub("", message).strip()
        cleaned = re.sub(r"\s{2,}", " ", cleaned)

        logger.info(
            f"[V4] @skill mentions detected: {valid_skills}, "
            f"cleaned message: '{cleaned[:60]}...'"
        )

        return cleaned, valid_skills, skill_params

    async def process_stream(
        self, request: V4AgentRequest
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        流式处理用户请求

        完整流程:
        0. @skill 预处理
        0.1 附件文件透传（Phase 2 由 document_reader 工具处理）
        1. 热加载检查
        2. 获取历史上下文和经验
        3. 构建 system prompt
        4. 构建 MCP Tools
        5. 构建 Hooks
        6. 构建 ClaudeAgentOptions
        7. 创建 SDK Client 并执行
        8. 消费 SDK 消息流 + Hook 旁路事件
        9. 保存历史和经验
        """
        # 优化 9: 流式进度推送 — 已迁移到两阶段架构
        # Phase 1 (_phase1_quick_respond) 会直接输出个性化确认语或直接回答，
        # 不再需要固定的 "正在理解您的问题..." 占位消息。

        # 0. @skill 预处理
        cleaned_message, mentioned_skills, skill_params = self._preprocess_message(request)
        if cleaned_message != request.message:
            request.message = cleaned_message
        if skill_params:
            request.skill_params = skill_params

        # 0.1 附件文件透传到 Phase 2（由 LLM 调用 document_reader 工具处理）
        # 不再在 Phase 0 下载+视觉分析，避免 5-30 秒阻塞
        if request.attached_files:
            logger.info(f"[V4] {len(request.attached_files)} attached file(s) detected, will be handled by Phase 2 document_reader")

        # 1. 热加载检查
        await self._check_hot_reload()

        # 确保上下文管理器已初始化
        self._ensure_context_managers()

        # 确保 MemoryCleanupScheduler 后台 task 已在 async 上下文中启动（幂等）
        if self._memory_engine and self._memory_engine.get("cleanup"):
            self._memory_engine["cleanup"].ensure_started()

        # process_stream 层面的 callback 支持（用于 _unified_stream 作用域外的 yield）
        _ps_callbacks = getattr(request, "callbacks", None)

        async def _ps_trigger_callback(event: dict):
            """process_stream 层面触发 Channel 回调"""
            if not _ps_callbacks:
                return
            event_type = event.get("event", "")
            event_data = event.get("data", {})
            try:
                if event_type == "text_clear" and _ps_callbacks.on_text_clear:
                    await _ps_callbacks.on_text_clear(event_data.get("reason", ""))
                elif event_type == "done" and _ps_callbacks.on_finish:
                    from app.channels.types import ReplyPayload
                    payload = ReplyPayload(text="", is_final=True, metadata=event_data)
                    await _ps_callbacks.on_finish(payload)
            except Exception as cb_err:
                logger.warning(f"[V4-ProcessStream] Callback error: {cb_err}")

        session_id = request.session_id

        # ======== 动态更新 user_id（修复历史会话消息归属问题）========
        # 从请求中获取真实的 user_id，并同步到各持久化组件
        request_user_id = request.user_id or self._config.default_user_id
        if self._history and self._history._user_id != request_user_id:
            self._history._user_id = request_user_id
            logger.debug(f"[V4] Updated ConversationHistory user_id to {request_user_id}")
        if self._experience and self._experience._user_id != request_user_id:
            self._experience._user_id = request_user_id
            logger.debug(f"[V4] Updated ExperienceStore user_id to {request_user_id}")

        # ======== @skill 强制路由：走完整 SDK 流程，但强制 LLM 调用指定工具 ========
        if mentioned_skills:
            request.force_skills = mentioned_skills
            logger.info(
                f"[V4] @skill directive detected: {mentioned_skills}, "
                f"routing through full SDK pipeline with forced tool constraint"
            )

        # 2. 获取历史上下文和经验
        #    - ExperienceStore: 始终读取（结构化经验是 SDK 不具备的长期记忆）
        #    - ConversationHistory: 始终读取（用于 system prompt 注入历史摘要）
        history_ctx = {"summary": None, "recent": []}
        experience = {}

        if self._history:
            try:
                history_ctx = await self._history.get_with_summary(session_id)
            except Exception as e:
                logger.warning(f"[V4] Failed to get history: {e}")

        if self._experience:
            try:
                experience = await self._experience.get(session_id)
            except Exception as e:
                logger.warning(f"[V4] Failed to get experience: {e}")

        original_message = request.message

        # 3. 构建 MCP Tools
        mcp_servers = self._mcp_builder.build_mcp_servers()

        # 4. 创建数据收集器
        data_collector = DataCollector()

        # 4.5 Agentic Loop: 记录请求开始时间 + 创建工具执行跟踪器
        _request_start = time.monotonic()
        _tracker = None
        if self._agentic_reflection:
            from agent_core.tool_execution_tracker import ToolExecutionTracker
            _tracker_config = self._agentic_config.get("reflection", {}).get("tracker", {})
            _tracker = ToolExecutionTracker(_tracker_config)
            # 每次请求重置反思轮次计数
            self._agentic_reflection.reset()

        # 创建 accumulated_text 引用（供 Stop hook 读取当前输出）
        accumulated_text = []

        # 5. 构建 Hooks（传入 data_collector + Agentic Loop 参数）
        scene_info = None
        self._event_bridge.reset()
        hooks = self._hook_manager.build_hooks(
            event_bridge=self._event_bridge,
            request=request,
            data_collector=data_collector,
            scene_info=scene_info,
            # Agentic Loop 参数
            reflection=self._agentic_reflection,
            tracker=_tracker,
            accumulated_text_ref=accumulated_text,
            request_start_time=_request_start,
        )

        # 5.5 CLAW: 触发 SessionStart hook
        try:
            await self._hook_manager.fire_session_start(
                session_id=session_id,
                user_id=request.user_id or self._config.default_user_id,
                metadata={
                    "channel": request.channel,
                    "render_mode": request.render_mode,
                    "has_resume": False,
                },
            )
        except Exception as e:
            logger.debug(f"[V4] SessionStart hook fire failed: {e}")

        # 6. 启动 AgentLoop
        logger.info("[V4] Starting AgentLoop")

        async for event in self._custom_loop_stream(
            request=request,
            session_id=session_id,
            hooks=hooks,
            experience=experience,
            history_ctx=history_ctx,
            data_collector=data_collector,
            accumulated_text_ref=accumulated_text,
            original_message=original_message,
            lazy_qf=self._qualifier,
        ):
            yield event
    # 自建 AgentLoop 路径
    # ─────────────────────────────────────────────────────────

    async def _custom_loop_stream(
        self,
        request: "V4AgentRequest",
        session_id: str,
        hooks: dict,
        experience: dict = None,
        history_ctx: dict = None,
        data_collector: "DataCollector" = None,
        accumulated_text_ref: list = None,
        original_message: str = None,
        lazy_qf=None,
        **kwargs,
    ):
        """
        自建 AgentLoop 流式处理路径

        使用 AgentLoop 驱动 LLM + Skill 调用。
        使用 agent_core/agentloop/ 中的 AgentLoop 驱动 LLM + Skill 调用。

        核心修复: PostToolUse hook 可以拿到 SkillResult.raw_data 完整数据。
        """
        from agent_core.agentloop import (
            AgentLoop, LiteLLMProvider, SkillInvoker, HookEngine,
            ContextBuilder, SessionEngine, LegacyHookPlugin, HookPoint,
            HookRegistration, HookPlugin,
            ParallelToolExecutor, ContextCompactor, PermissionGuard, SubAgentExecutor,
        )

        if data_collector is None:
            data_collector = DataCollector()

        # 如果使用了历史上下文（session 记忆），标记数据收集器
        if history_ctx and (history_ctx.get("recent") or history_ctx.get("summary")):
            data_collector.used_session_context = True

        # ── 构建 system prompt（与 _unified_stream 一致） ──
        has_resume = False  # custom loop 不使用 CLI session
        _prompt_budget = None
        if self._context_guard:
            try:
                _prompt_budget = self._context_guard.allocate_budget()
            except Exception:
                pass

        _memory_context = ""
        if self._memory_engine:
            try:
                retriever = self._memory_engine["retriever"]
                memory_budget = _prompt_budget.memory_budget if _prompt_budget else 0
                sub_ratios = None
                if self._context_guard and memory_budget > 0:
                    sub_ratios = self._context_guard._load_memory_sub_ratios()
                from .llm_helpers import extract_query_keywords
                _query_text = request.message or ""
                mem_ctx = await retriever.retrieve(
                    session_id=session_id,
                    query=_query_text,
                    query_topics=extract_query_keywords(_query_text),
                    memory_budget=memory_budget,
                    sub_ratios=sub_ratios,
                )
                _memory_context = retriever.format_for_prompt(mem_ctx)
            except Exception as e:
                logger.warning(f"[V4-CustomLoop] Memory retrieval failed: {e}")

        skill_exec_times = await self._fetch_skill_exec_times()
        system_prompt = self._prompt_builder.build_unified_system_prompt(
            history=history_ctx.get("recent", []) if history_ctx else [],
            summary=history_ctx.get("summary") if history_ctx else None,
            experience=experience,
            ts_code=request.ts_code,
            params=request.params,
            skill_exec_times=skill_exec_times,
            output_format=request.output_format,
            render_mode=request.render_mode,
            has_resume=has_resume,
            budget=_prompt_budget,
            memory_context=_memory_context,
            phase0_intents=None,
            phase0_topics=None,
            quality_focus=data_collector.quality_focus,
            report_lang=request.report_lang,
            as_blocks=True,
        )

        # ── 构建用户消息（运行时上下文前缀 + @skill 强制路由 + 钉钉 cron 指令）──
        # 运行时上下文（时间 + 钉钉会话）注入用户消息前缀，不占用 system prompt prefix cache
        _runtime_prefix = self._prompt_builder.build_runtime_context_prefix(request.params)
        user_message = f"{_runtime_prefix}\n\n{request.message}" if _runtime_prefix else request.message

        if request.force_skills:
            forced_tools_str = "、".join(request.force_skills)
            user_message = (
                f"{request.message}\n\n"
                f"【⚠️ 强制工具指令】\n"
                f"用户通过 @{forced_tools_str} 明确指定了要使用的工具。\n"
                f"你必须调用以下工具：{forced_tools_str}。\n"
                f"这是强制要求，不可忽略，不可替换为其他工具。\n"
                f"请使用该工具处理用户的问题后再输出结果。"
            )
            logger.info(f"[V4-CustomLoop] Injected force_skills directive: {request.force_skills}")

        _is_dingtalk = bool(request.params and request.params.get("dingtalk_conversation_id"))
        _sched_keywords = ("定时", "每天", "每周", "每小时", "周期", "自动执行", "自动发送")
        if _is_dingtalk and any(kw in request.message for kw in _sched_keywords):
            import json as _json_cron
            _dt_p = request.params
            _callback_json = _json_cron.dumps({
                "channel": "dingtalk",
                "user_id": _dt_p.get("dingtalk_sender_id", ""),
                "sender_id": _dt_p.get("dingtalk_sender_id", ""),
                "staff_id": _dt_p.get("dingtalk_staff_id", ""),
                "sender_nick": _dt_p.get("dingtalk_sender", ""),
                "conversation_id": _dt_p.get("dingtalk_conversation_id", ""),
                "conversation_type": _dt_p.get("dingtalk_conversation_type", ""),
                "robot_code": _dt_p.get("dingtalk_robot_code", ""),
            }, ensure_ascii=False)
            user_message += (
                "\n\n【⚠️ 定时任务强制指令 — 钉钉渠道】\n"
                "你必须在完成数据分析后，调用 cron skill 创建定时任务。\n"
                "调用时必须传入 callback 参数，并原样复制以下 JSON：\n"
                f"{_callback_json}\n"
                "禁止使用旧的定时实现方式。"
            )

        # ── 注入 cron 阈值/静默指令 ──
        _cron_threshold = request.params.get("cron_alert_threshold") if request.params else None
        _cron_silent = request.params.get("cron_silent_if_no_signal", False) if request.params else False
        if _cron_threshold or _cron_silent:
            _cron_directive = "\n\n【⚠️ 定时任务执行指令】\n"
            if _cron_threshold:
                _cron_directive += f"本次分析的触发阈值为：{_cron_threshold}\n"
                _cron_directive += "请在分析完成后，明确判断是否达到了上述阈值。\n"
            if _cron_silent:
                _cron_directive += (
                    "如果本次分析结果**未达到触发阈值**，或没有值得通知用户的信号，"
                    "请在回复的最开头输出 [SILENT] 标记（仅此标记，不要其他文字），然后停止。"
                    "系统将自动静默，不会把这条消息发送给用户。\n"
                    "如果达到了阈值，则正常输出完整分析报告，不要加 [SILENT]。\n"
                )
            user_message += _cron_directive

        # ── 构建 AgentLoop 组件 ──
        self._event_bridge.reset()

        # 1. LiteLLMProvider
        llm_provider = LiteLLMProvider(config=self._config)
        # 注册全局 provider，供辅助模块（reflection、scene_context 等）使用
        from agent_core.agentloop.llm_provider import set_global_provider
        set_global_provider(llm_provider)

        # Phase 6: SubAgentExecutor（opt-in，默认禁用）
        # 注意：SubAgentExecutor 需要 parent_event_bridge，在此先占位，
        # SkillInvoker / HookEngine 构建完成后再补全引用。
        _subagent_executor = None

        # 2. SkillInvoker（进程内直接调用，替代 MCP stdio 子进程）
        skill_invoker = SkillInvoker(
            executor=self._executor,
            discovery=self._discovery,
            subagent_executor=None,  # 先传 None，SubAgent 启用时在下方注入
        )

        # 3. HookEngine（注册 LegacyHookPlugin 桥接已有 HookManager）
        hook_engine = HookEngine()
        hook_manager_hooks_kwargs = hooks  # hooks 是 build_hooks 返回的 dict

        # 从 build_hooks kwargs 重建 LegacyHookPlugin 需要的参数
        _hook_kwargs = {
            "data_collector": data_collector,
            "tracker": kwargs.get("tracker", None),
            "reflection": kwargs.get("reflection", None),
            "accumulated_text_ref": accumulated_text_ref,
            "request_start_time": kwargs.get("request_start_time", None),
            "skip_ui_rendering": request.render_mode == "text_only",
            "dingtalk_render": _is_dingtalk,
        }
        try:
            legacy_plugin = LegacyHookPlugin(
                hook_manager=self._hook_manager,
                event_bridge=self._event_bridge,
                request=request,
                mentioned_skills=request.force_skills or None,
                **_hook_kwargs,
            )
            hook_engine.register_plugin(legacy_plugin)
        except Exception as e:
            logger.warning(f"[V4-CustomLoop] LegacyHookPlugin registration failed: {e}")

        # Ring 3: CapabilityGapCounter — POST_TOOL_USE 时记录失败并触发进化任务
        try:
            from agent_core.agentloop import CapabilityGapCounter
            _gap_enabled = getattr(self._config, "capability_gap_detection_enabled", True)
            if _gap_enabled and self._sqlite_db:
                _gap_counter = CapabilityGapCounter(
                    context_db=self._sqlite_db,
                    config={
                        "capability_gap_detection_enabled": True,
                        "capability_gap_trigger_threshold": getattr(self._config, "capability_gap_trigger_threshold", 3),
                        "capability_gap_cooldown_hours": getattr(self._config, "capability_gap_cooldown_hours", 24),
                    },
                )
                _evolution_mgr = getattr(self, "_ke_evolution", None)
                _default_uid = request.user_id or getattr(self._config, "default_user_id", 1)
                _inst_id = getattr(self._config, "instance_id", "default")

                class _CapabilityGapPlugin(HookPlugin):
                    """Ring 3: 工具失败时累积计数 → 达阈值触发进化任务"""
                    name = "capability_gap"

                    def __init__(self, counter, evolution_mgr, user_id, instance_id, sid):
                        self._counter = counter
                        self._evolution = evolution_mgr
                        self._uid = user_id
                        self._iid = instance_id
                        self._sid = sid

                    async def _on_post_tool(self, ctx: dict) -> dict:
                        tool_result = ctx.get("tool_result")
                        tool_name = ctx.get("tool_name", "")
                        if tool_result and getattr(tool_result, "is_error", False):
                            error_text = str(getattr(tool_result, "content", ""))[:500]
                            await self._counter.increment(tool_name, error_text, self._sid)
                            if await self._counter.should_trigger(tool_name, self._sid):
                                if self._evolution:
                                    gap = f"工具 {tool_name} 多次失败: {error_text[:100]}"
                                    try:
                                        await self._evolution.create_task(gap, self._uid, self._iid)
                                        await self._counter.mark_triggered(tool_name, self._sid)
                                        logger.info(f"[Ring3] Evolution task created for gap: {tool_name}")
                                    except Exception as e:
                                        logger.debug(f"[Ring3] Evolution task creation failed: {e}")
                        return ctx

                    def get_hooks(self):
                        return [
                            HookRegistration(
                                "capability_gap_post_tool",
                                HookPoint.POST_TOOL_USE,
                                self._on_post_tool,
                                priority=90,  # 低优先级，在主逻辑之后执行
                            ),
                        ]

                _gap_plugin = _CapabilityGapPlugin(
                    _gap_counter, _evolution_mgr, _default_uid, _inst_id, session_id,
                )
                hook_engine.register_plugin(_gap_plugin)
                logger.debug("[V4-CustomLoop] CapabilityGapPlugin registered (Ring 3)")
        except Exception as e:
            logger.debug(f"[V4-CustomLoop] CapabilityGapPlugin init skipped: {e}")

        # 4. ContextBuilder
        context_builder = ContextBuilder()

        # 5. SessionEngine（注入已有组件，不重新初始化）
        _skip_memory = getattr(request, "skip_memory", False)

        session_engine = SessionEngine(config=self._config)
        session_engine.skip_memory = _skip_memory
        self._hook_manager.skip_memory = _skip_memory
        session_engine.inject_components(
            history=self._history,
            experience=self._experience,
            context_guard=self._context_guard,
            knowledge_store=self._ke_store,
            episode_tracker=self._ke_episode_tracker,
            reflection=self._ke_reflection,
            memory_engine=self._memory_engine,
            graph_retriever=getattr(self, "_ke_graph_retriever", None),
            prediction_store=getattr(self, "_ke_prediction_store", None),
            prediction_extractor=getattr(self, "_ke_prediction_extractor", None),
            prediction_scheduler=getattr(self, "_ke_prediction_scheduler", None),
            dream_consolidator=getattr(self, "_ke_dream_consolidator", None),
        )

        # Phase 6: 构建 SubAgentExecutor（在 hook_engine + skill_invoker 就绪后）
        if getattr(self._config, "subagent_enabled", False):
            # SubAgentStore：复用现有 DB 路径（优先从 _sqlite_db 获取，它是已验证的路径）
            from agent_core.session.subagent_store import SubAgentStore
            _db_path = getattr(self._sqlite_db, "db_path", None) if self._sqlite_db else None
            if _db_path is None:
                _db_path = getattr(self._history, "_db_path", None) if self._history else None
            if _db_path is None:
                # 从 config 推断
                _db_path = getattr(self._config, "db_path", "/app/app/data/agent.db")
            _subagent_store = SubAgentStore(db_path=_db_path)

            # hook_plugin_factory：每次调用 new 一个独立 LegacyHookPlugin（并发安全）
            # 捕获当前请求级别的参数（data_collector 等），子代理 plugin 共享 event_bridge
            _hook_kwargs_snapshot = dict(_hook_kwargs)
            _hm = self._hook_manager
            _eb = self._event_bridge
            _req = request

            def _make_subagent_plugin():
                # 子代理只需要 event_bridge（data2ui）和 data_collector（统计）
                # Stop hook 相关组件（tracker/reflection/accumulated_text_ref）必须置 None
                # 否则子代理 Stop 时会触发父级 finalize 流程，导致父代理提前终止
                return LegacyHookPlugin(
                    hook_manager=_hm,
                    event_bridge=_eb,
                    request=_req,
                    mentioned_skills=getattr(_req, "force_skills", None) or None,
                    data_collector=_hook_kwargs_snapshot.get("data_collector"),
                    tracker=None,
                    reflection=None,
                    accumulated_text_ref=None,
                    request_start_time=None,
                    skip_ui_rendering=_hook_kwargs_snapshot.get("skip_ui_rendering", False),
                    dingtalk_render=_hook_kwargs_snapshot.get("dingtalk_render", False),
                )

            _subagent_executor = SubAgentExecutor(
                llm_provider=LiteLLMProvider(config=self._config),
                skill_invoker=skill_invoker,
                parent_hook_engine=hook_engine,
                parent_event_bridge=self._event_bridge,
                config=self._config,
                max_depth=getattr(self._config, "subagent_max_depth", 3),
                max_iterations=getattr(self._config, "subagent_max_iterations", 10),
                enabled=True,
                session_engine=session_engine,
                hook_plugin_factory=_make_subagent_plugin,
                subagent_store=_subagent_store,
            )
            # 将 SubAgentExecutor 和 SubAgentStore 注入到已构建的 SkillInvoker
            skill_invoker._subagent_executor = _subagent_executor
            skill_invoker._subagent_store = _subagent_store

        # Phase 2: ParallelToolExecutor
        _parallel_executor = ParallelToolExecutor(
            skill_invoker=skill_invoker,
            discovery=self._discovery,
            enabled=getattr(self._config, "parallel_tool_execution", True),
            max_concurrent=getattr(self._config, "parallel_max_concurrent", 8),
            per_tool_timeout=getattr(self._config, "parallel_per_tool_timeout", 120.0),
        )

        # Phase 3: ContextCompactor
        _compactor = None
        if getattr(self._config, "context_compaction_enabled", True):
            _compactor = ContextCompactor(
                llm_provider=llm_provider,
                compaction_threshold=getattr(self._config, "context_compaction_threshold", 0.70),
                keep_recent=getattr(self._config, "context_compaction_keep_recent", 6),
                enabled=True,
            )

        # Phase 4: PermissionGuard
        _permission_guard = None
        if getattr(self._config, "permission_guard_enabled", False):
            _permission_guard = PermissionGuard(enabled=True)

        # Token 优化: ToolResultSummarizer
        from agent_core.agentloop import ToolResultSummarizer
        _summarizer = None
        if getattr(self._config, "tool_result_summarize_enabled", True):
            _summarizer = ToolResultSummarizer(
                llm_provider=llm_provider,
                threshold=getattr(self._config, "tool_result_summarize_threshold", 20000),
                hard_limit=getattr(self._config, "tool_result_summarize_hard_limit", 50000),
                enabled=True,
                timeout=getattr(self._config, "tool_result_summarize_timeout", 60.0),
            )

        # 6. AgentLoop（注入所有 opt-in 组件）
        agent_loop = AgentLoop(
            llm_provider=llm_provider,
            skill_invoker=skill_invoker,
            hook_engine=hook_engine,
            session_engine=session_engine,
            context_builder=context_builder,
            event_bridge=self._event_bridge,
            config=self._config,
            max_iterations=getattr(self._config, "max_iterations", 30),
            max_timeout_seconds=getattr(self._config, "max_timeout_seconds", 900),
            parallel_executor=_parallel_executor,
            compactor=_compactor,
            permission_guard=_permission_guard,
            summarizer=_summarizer,
            single_message_max_chars=getattr(self._config, "context_single_message_max_chars", 30000),
        )

        # ── 执行 AgentLoop，yield SSE 事件 ──
        # Channel 回调支持（与 _unified_stream 一致）
        callbacks = getattr(request, "callbacks", None)
        _accumulated_for_callback = []

        async def _trigger_callback(event: dict):
            if not callbacks:
                return
            event_type = event.get("event", "")
            event_data = event.get("data", {})
            try:
                if event_type == "text_delta":
                    _accumulated_for_callback.append(event_data.get("delta", ""))
                    if callbacks.on_partial_reply:
                        from app.channels.types import ReplyPayload
                        payload = ReplyPayload(
                            text="".join(_accumulated_for_callback),
                            is_final=False,
                        )
                        await callbacks.on_partial_reply(payload)
                elif event_type == "tool_done":
                    if callbacks.on_tool_result:
                        await callbacks.on_tool_result(event_data.get("name", ""), event_data)
                elif event_type == "done":
                    if callbacks.on_finish:
                        from app.channels.types import ReplyPayload
                        from app.channels.message_builder import AgentInputBuilder
                        full_text = "".join(_accumulated_for_callback)
                        markers = AgentInputBuilder.extract_report_markers(full_text)
                        payload = ReplyPayload(
                            text=full_text,
                            markdown=full_text,
                            is_final=True,
                            metadata=event_data,
                            report_name=markers.get("report_name", ""),
                            send_report_file=markers.get("send_report_file", False),
                        )
                        await callbacks.on_finish(payload)
                elif event_type == "error":
                    if callbacks.on_error:
                        await callbacks.on_error(event_data.get("error", "Unknown error"))
                elif event_type == "component_for_render":
                    if callbacks.on_component:
                        await callbacks.on_component(event_data)
            except Exception as cb_err:
                logger.warning(f"[V4-CustomLoop] Callback error: {cb_err}")

        accumulated_text = accumulated_text_ref if accumulated_text_ref is not None else []

        async for event in agent_loop.run(
            system_prompt=system_prompt,
            user_message=user_message,
            request=request,
            data_collector=data_collector,
        ):
            # 同步 accumulated_text（供 stop hook 读取）
            if event.get("event") == "text_delta":
                accumulated_text.append(event.get("data", {}).get("delta", ""))

            await _trigger_callback(event)
            yield event

        # 发送 report_ready 事件（前端反馈功能依赖）
        _rr_event = {
            "event": "report_ready",
            "data": {
                "report_id": session_id,
                "urls": {},
                "public_urls": {},
                "knowledge_count": 0,
            }
        }
        yield _rr_event
        await _trigger_callback(_rr_event)
        logger.info(f"[V4] report_ready sent (report_id={session_id})")

        # 后台触发历史摘要压缩（内部判断消息数是否超阈值 max_raw_messages=8）
        if self._history:
            try:
                import asyncio as _asyncio
                from .llm_helpers import summarize_conversation
                _asyncio.ensure_future(
                    self._history.maybe_summarize(
                        session_id,
                        summarize_fn=summarize_conversation,
                        progressive_summarizer=getattr(self, "_progressive_summarizer", None),
                        memory_updater=self._memory_engine.get("updater") if self._memory_engine else None,
                    )
                )
            except Exception as _e:
                logger.debug(f"[V4] Failed to schedule maybe_summarize: {_e}")

    @staticmethod
    def _build_fallback_from_collected_data(
        user_message: str, data_collector: "DataCollector"
    ) -> str:
        """
        当 httpx 和 backup 重新生成都失败时，基于已采集数据生成兜底文本。

        不调用 LLM，直接拼接数据摘要，保证用户至少能看到工具采集结果。
        """
        parts = [f"## 关于「{user_message}」的数据采集结果\n"]
        parts.append("由于内容审核限制，无法生成完整分析报告。以下是已采集到的原始数据摘要：\n")

        for result in data_collector.skill_results[:10]:
            skill_name = result.get("skill", "unknown")
            data = result.get("data", {})
            parts.append(f"\n### {skill_name}\n")
            if isinstance(data, dict):
                # 提取关键字段
                for k, v in list(data.items())[:15]:
                    if isinstance(v, list) and len(v) > 0:
                        parts.append(f"- **{k}**: {len(v)} 条记录")
                        # 显示前 3 条
                        for item in v[:3]:
                            if isinstance(item, dict):
                                summary = ", ".join(
                                    f"{ik}: {str(iv)[:80]}"
                                    for ik, iv in list(item.items())[:5]
                                )
                                parts.append(f"  - {summary}")
                            else:
                                parts.append(f"  - {str(item)[:200]}")
                    elif isinstance(v, str) and len(v) > 10:
                        parts.append(f"- **{k}**: {v[:300]}")
                    elif isinstance(v, (int, float)):
                        parts.append(f"- **{k}**: {v}")
            elif isinstance(data, str) and data.strip():
                parts.append(data[:1000])

        return "\n".join(parts)

    @staticmethod
    def _build_assistant_metadata(data_collector) -> Optional[str]:
        """构建 assistant 消息的 metadata_json（含 tool_calls，用于历史恢复）"""
        if not data_collector:
            return None

        if not data_collector.tool_calls:
            return None

        import json
        meta = {
            "tool_calls": [
                {
                    "name": tc.get("name", ""),
                    "input_keys": list(tc.get("input", {}).keys())
                    if isinstance(tc.get("input"), dict) else [],
                }
                for tc in data_collector.tool_calls[:30]
            ]
        }
        return json.dumps(meta, ensure_ascii=False)

    def _schedule_background_tasks(
        self, session_id: str, user_msg: str, assistant_text: str
    ):
        """
        调度后台任务（优化 4: 并行化）

        所有后台任务通过 create_background_task 独立并行执行:
        - SessionEnd hook
        - 经验提取（带质量门槛）
        - 对话摘要 + 话题提取
        - 用户画像更新

        配置: performance.json → post_process.parallel_enabled
        """
        from .llm_helpers import summarize_conversation, extract_experience
        _bg_start = time.monotonic()

        # 读取并行配置（默认 true — 各任务独立并行执行）
        try:
            import json as _json
            _perf_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "config", "performance.json")
            with open(_perf_path, "r") as _f:
                _post_cfg = _json.load(_f).get("post_process", {})
            _parallel_enabled = _post_cfg.get("parallel_enabled", True)
        except Exception:
            _parallel_enabled = True

        def _dispatch(coro, task_name: str):
            """根据 parallel 配置分发任务: 并行(background) 或 串行(直接 await 由调用方处理)"""
            if _parallel_enabled:
                from app.utils.background_task_manager import create_background_task
                create_background_task(coro, task_name=task_name)
            else:
                # 串行模式: 收集 coroutine 待后续顺序 await
                _serial_tasks.append((coro, task_name))

        _serial_tasks: list = []

        # CLAW: 触发 SessionEnd hook（fire-and-forget）
        try:
            _dispatch(
                self._hook_manager.fire_session_end(
                    session_id=session_id,
                    user_id=self._config.default_user_id,
                    stats={
                        "user_msg_len": len(user_msg),
                        "assistant_len": len(assistant_text),
                    },
                ),
                task_name="session_end_hook",
            )
        except Exception as e:
            logger.debug(f"[V4] SessionEnd hook schedule failed: {e}")

        if self._experience:
            # 质量门槛检查：短/平淡对话跳过经验提取，节省 LLM 调用
            user_len = len(user_msg.strip())
            assistant_len = len(assistant_text.strip())
            min_user = self._config.experience_min_user_msg_length
            min_assistant = self._config.experience_min_assistant_msg_length

            if user_len >= min_user and assistant_len >= min_assistant:
                try:
                    _dispatch(
                        self._experience.extract_and_save(
                            session_id,
                            user_msg,
                            assistant_text,
                            extract_fn=extract_experience,
                        ),
                        task_name="experience_extraction"
                    )
                except Exception as e:
                    logger.debug(f"[V4] Failed to schedule experience extraction: {e}")
            else:
                logger.debug(
                    f"[V4] Skipping experience extraction: "
                    f"user_msg={user_len} chars (min={min_user}), "
                    f"assistant={assistant_len} chars (min={min_assistant})"
                )

        if self._history:
            try:
                # 若记忆引擎已启用，传入 memory_updater 回调
                _memory_updater = None
                if self._memory_engine:
                    _memory_updater = self._memory_engine.get("updater")

                from .llm_helpers import extract_topics_and_entities
                _dispatch(
                    self._history.maybe_summarize(
                        session_id,
                        summarize_fn=summarize_conversation,
                        progressive_summarizer=self._progressive_summarizer,
                        memory_updater=_memory_updater,
                        topic_extractor=extract_topics_and_entities,
                    ),
                    task_name="conversation_summarization"
                )
            except Exception as e:
                logger.debug(f"[V4] Failed to schedule summarization: {e}")

        # MemoryOS: 后台更新用户画像和知识库
        if self._memory_engine:
            try:
                updater = self._memory_engine["updater"]
                _dispatch(
                    updater.on_turn_end(session_id, user_msg, assistant_text),
                    task_name="memory_turn_end_update",
                )
            except Exception as e:
                logger.debug(f"[V4] Failed to schedule memory update: {e}")

        # 串行模式: 顺序 await 所有收集的任务
        if not _parallel_enabled and _serial_tasks:
            import asyncio
            logger.info(f"[PostProcess] Serial mode: {len(_serial_tasks)} tasks")
            for _coro, _name in _serial_tasks:
                try:
                    asyncio.ensure_future(_coro)
                except Exception as _e:
                    logger.warning(f"[PostProcess] Serial task '{_name}' failed: {_e}")

        _bg_elapsed = (time.monotonic() - _bg_start) * 1000
        _mode = "parallel" if _parallel_enabled else "serial"
        logger.info(f"[PostProcess] Background tasks scheduled in {_bg_elapsed:.0f}ms (mode={_mode})")

    async def _fetch_skill_exec_times(self) -> Dict[str, float]:
        """
        从 Redis 获取所有技能的历史平均执行耗时

        Returns:
            {skill_name: avg_seconds}，失败返回空 dict
        """
        from agent_core.skill_executor import V4SkillExecutor

        result: Dict[str, float] = {}
        try:
            all_skills = self._discovery.get_all()
            for meta in all_skills:
                avg = await V4SkillExecutor.get_average_execution_time(meta.name)
                if avg is not None:
                    result[meta.name] = avg
        except Exception as e:
            logger.debug(f"[V4] Failed to fetch skill exec times: {e}")
        return result

    async def _check_hot_reload(self):
        """热加载检查（基于 SKILL.md 文件时间戳）"""
        now = time.time()

        if now - self._last_reload_check < self._config.reload_interval:
            return

        self._last_reload_check = now

        try:
            if self._discovery.needs_reload():
                self._discovery.scan()
                self._executor.reload()
                self._mcp_builder.invalidate_cache()
                logger.info("[V4] Hot reload triggered, all caches invalidated")
        except Exception as e:
            logger.debug(f"[V4] Hot reload check failed: {e}")

    async def get_available_skills(self, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取可用技能列表（供 API 路由使用）

        Args:
            user_id: 用户 ID，用于过滤动态技能（返回用户自己的 + 公开共享的）
        """
        skills = []

        for meta in self._discovery.get_all(user_id=user_id):
            skill_info = {
                "name": meta.name,
                "description": meta.description or "",
                "display_name": meta.display_name or meta.name,
                "has_script": meta.has_script,
                "priority": meta.priority,
            }
            # 添加动态技能标识
            if meta.is_dynamic:
                skill_info["dynamic"] = True
                skill_info["user_id"] = meta.user_id
                skill_info["is_shared"] = meta.is_shared

            skills.append(skill_info)

        return skills


    def _extract_title(self, query: str, report_lang: str = "zh") -> str:
        """从查询中提取标题"""
        # 简单处理：取前30个字符
        title = query.strip()[:30]
        if len(query) > 30:
            title += "..."
        prefix = "Analysis Report" if report_lang == "en" else "分析报告"
        return f"{prefix}: {title}"

