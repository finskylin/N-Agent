"""
SessionManager — 会话生命周期管理

负责协调 session_store、conversation_history、experience_store
的初始化和生命周期管理。
"""
from typing import Optional
from loguru import logger

from .context_db import SessionContextDB
from .session_store import CLISessionStoreCore
from .conversation_history import ConversationHistoryCore
from .experience_store import ExperienceStoreCore


class SessionManager:
    """会话管理器 — 协调三大存储组件的初始化"""

    def __init__(
        self,
        db_path: str = "app/data/sessions/default/memory.db",
        user_id: int = 1,
        instance_id: str = "default",
        wal_mode: bool = True,
        busy_timeout_ms: int = 5000,
        config=None,
    ):
        self._db_path = db_path
        self._user_id = user_id
        self._instance_id = instance_id

        self._sqlite_db = SessionContextDB(
            db_path=db_path,
            wal_mode=wal_mode,
            busy_timeout_ms=busy_timeout_ms,
        )

        self.session_store = CLISessionStoreCore(
            user_id=user_id,
            instance_id=instance_id,
            sqlite_db=self._sqlite_db,
        )
        self.conversation_history = ConversationHistoryCore(
            user_id=user_id,
            instance_id=instance_id,
            sqlite_db=self._sqlite_db,
        )
        self.experience_store = ExperienceStoreCore(
            user_id=user_id,
            instance_id=instance_id,
            sqlite_db=self._sqlite_db,
        )

        # Knowledge Engine (optional)
        self.knowledge_store = None
        self.episode_tracker = None
        self.temporal_manager = None
        self.knowledge_distiller = None
        self.feedback_learner = None
        self.reflection_engine = None
        self.evolution_task_manager = None
        self.skill_crystallizer = None
        if config and getattr(config, 'knowledge_engine_enabled', False):
            try:
                from agent_core.knowledge.config_loader import load_knowledge_config
                from agent_core.knowledge.store import KnowledgeStore
                from agent_core.knowledge.episode_tracker import EpisodeTracker
                from agent_core.knowledge.temporal import TemporalKnowledgeManager

                ke_config = load_knowledge_config(config.knowledge_engine_config_path)
                self.knowledge_store = KnowledgeStore(self._sqlite_db, ke_config)
                self.episode_tracker = EpisodeTracker(ke_config)
                self.temporal_manager = TemporalKnowledgeManager(self.knowledge_store, ke_config)

                # Opt 7: 知识引擎辅助 LLM 调用使用全局 provider（由 native_agent 注册）
                _llm_call = None
                try:
                    from agent_core.agentloop.llm_provider import call_llm
                    _llm_call = call_llm
                    logger.info("[SessionManager] Knowledge engine using global LLM provider")
                except Exception:
                    logger.warning("[SessionManager] No LLM available for knowledge engine")

                # 初始化高级模块（全部 try-import，失败不影响基础功能）
                try:
                    from agent_core.knowledge.distiller import KnowledgeDistiller
                    self.knowledge_distiller = KnowledgeDistiller(
                        self.knowledge_store, ke_config.get("distiller", {}),
                        llm_call=_llm_call,
                    )
                except Exception as e:
                    logger.debug(f"[SessionManager] Distiller init skipped: {e}")

                try:
                    from agent_core.knowledge.feedback_learner import FeedbackLearner
                    self.feedback_learner = FeedbackLearner(
                        self.knowledge_store, ke_config.get("feedback_learner", {}),
                        llm_call=_llm_call,
                    )
                except Exception as e:
                    logger.debug(f"[SessionManager] FeedbackLearner init skipped: {e}")

                try:
                    from agent_core.knowledge.reflection import ReflectionEngine
                    self.reflection_engine = ReflectionEngine(
                        self.knowledge_store, ke_config.get("reflection", {}),
                        llm_call=_llm_call,
                        temporal_manager=self.temporal_manager,
                    )
                except Exception as e:
                    logger.debug(f"[SessionManager] ReflectionEngine init skipped: {e}")

                try:
                    from agent_core.knowledge.evolution_task import EvolutionTaskManager
                    self.evolution_task_manager = EvolutionTaskManager(
                        self.knowledge_store, ke_config.get("evolution", {}),
                        llm_call=_llm_call,
                        temporal_manager=self.temporal_manager,
                    )
                except Exception as e:
                    logger.debug(f"[SessionManager] EvolutionTaskManager init skipped: {e}")

                try:
                    from agent_core.knowledge.skill_crystallizer import SkillCrystallizer
                    self.skill_crystallizer = SkillCrystallizer(
                        self.knowledge_store, ke_config.get("crystallizer", {}),
                        llm_call=_llm_call,
                    )
                except Exception as e:
                    logger.debug(f"[SessionManager] SkillCrystallizer init skipped: {e}")

                logger.info("[SessionManager] Knowledge engine initialized")
            except Exception as e:
                logger.warning(f"[SessionManager] Knowledge engine init failed: {e}")

        logger.info(
            f"[SessionManager] Initialized with db={db_path}, "
            f"user={user_id}, instance={instance_id}"
        )

    @property
    def sqlite_db(self) -> SessionContextDB:
        return self._sqlite_db

    async def close(self):
        """关闭所有资源"""
        await self._sqlite_db.close()
