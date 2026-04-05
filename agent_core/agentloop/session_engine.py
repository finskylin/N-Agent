"""
SessionEngine — Session/记忆/学习集成引擎

编排已有 agent_core/session/ 和 agent_core/knowledge/ 组件，
提供统一的 session 生命周期管理接口。

不重新实现任何能力，仅编排已有组件:
- ConversationHistory (对话历史 + 摘要)
- ExperienceStore (用户经验 6维度)
- ContextWindowGuard (token 预算)
- KnowledgeStore + EpisodeTracker + ReflectionEngine (知识学习)
- MemoryRetriever (记忆检索)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from loguru import logger

from .message_types import SessionContext


class SessionEngine:
    """
    集成 session、记忆、学习的统一引擎

    替换前: CLI session ID → Redis → .jsonl 文件 → SDK --resume 参数
    替换后: ConversationHistory.get_with_summary() + ExperienceStore.get() → 注入 system prompt
    """

    def __init__(self, config):
        """
        Args:
            config: V4Config 对象，各组件从 native_agent 注入（see inject_components）
        """
        self._config = config

        # 由 inject_components() 注入（native_agent 已初始化的实例）
        self._history = None            # ConversationHistoryCore
        self._experience = None         # ExperienceStoreCore
        self._context_guard = None      # ContextWindowGuard
        self._knowledge_store = None    # KnowledgeStore
        self._episode_tracker = None    # EpisodeTracker
        self._reflection = None         # ReflectionEngine
        self._memory_engine: Optional[Dict] = None  # {"retriever": ..., "updater": ..., ...}
        self._graph_retriever = None    # GraphRetriever（可选）
        self._prediction_store = None      # PredictionStore（可选）
        self._prediction_extractor = None  # PredictionExtractor（可选）
        self._prediction_scheduler = None  # PredictionScheduler（可选，用于实时 overdue check）
        self._dream_consolidator = None    # DreamConsolidator（可选）
        self.skip_memory = False           # eval 模式：跳过后处理写入

    def inject_components(
        self,
        history=None,
        experience=None,
        context_guard=None,
        knowledge_store=None,
        episode_tracker=None,
        reflection=None,
        memory_engine: Optional[Dict] = None,
        graph_retriever=None,
        prediction_store=None,
        prediction_extractor=None,
        prediction_scheduler=None,
        dream_consolidator=None,
    ) -> None:
        """
        注入已有组件实例（由 native_agent 调用，避免重复初始化）
        """
        self._history = history
        self._experience = experience
        self._context_guard = context_guard
        self._knowledge_store = knowledge_store
        self._episode_tracker = episode_tracker
        self._reflection = reflection
        self._memory_engine = memory_engine
        self._graph_retriever = graph_retriever
        self._prediction_store = prediction_store
        self._prediction_extractor = prediction_extractor
        self._prediction_scheduler = prediction_scheduler
        self._dream_consolidator = dream_consolidator

    async def prepare_session(
        self,
        session_id: str,
        user_id: int,
        query: str = "",
    ) -> SessionContext:
        """
        请求开始时准备 session 上下文

        返回 SessionContext:
        - history_messages: 最近对话（OpenAI 格式）
        - summary: 历史摘要
        - experience: 用户经验
        - knowledge: 知识引擎注入文本
        - token_budget: token 分配
        """
        ctx = SessionContext(
            session_id=session_id,
            user_id=user_id,
        )

        # 1. 获取历史对话和摘要
        if self._history:
            try:
                history_data = await self._history.get_with_summary(session_id)
                ctx.summary = history_data.get("summary")
                recent = history_data.get("recent", [])
                ctx.history_messages = self._convert_history_to_openai(recent)
            except Exception as e:
                logger.warning(f"[SessionEngine] Failed to get history: {e}")

        # 2. 获取用户经验
        if self._experience:
            try:
                ctx.experience = await self._experience.get(session_id)
            except Exception as e:
                logger.warning(f"[SessionEngine] Failed to get experience: {e}")

        # 3. 获取 token 预算
        if self._context_guard:
            try:
                ctx.token_budget = self._context_guard.allocate_budget()
            except Exception as e:
                logger.warning(f"[SessionEngine] Failed to allocate token budget: {e}")

        # 4. 一次性计算 query embedding（供知识引擎和 MemoryOS 共享，避免重复 API 调用）
        query_vec = None
        if query and self._memory_engine:
            embedding_client = self._memory_engine.get("embedding_client")
            if embedding_client and embedding_client.enabled:
                try:
                    query_vec = await embedding_client.embed(query)
                except Exception as e:
                    logger.debug(f"[SessionEngine] query embedding failed, fallback: {e}")

        # 5. 知识引擎：检索相关知识
        if self._knowledge_store and query:
            try:
                from agent_core.knowledge.retriever import KnowledgeRetriever
                knowledge_config = {}
                if self._memory_engine:
                    knowledge_config = self._memory_engine.get("knowledge_config", {})
                retriever = KnowledgeRetriever(
                    self._knowledge_store, knowledge_config,
                    graph_retriever=self._graph_retriever,
                    prediction_store=self._prediction_store,
                )
                token_budget = (
                    ctx.token_budget.knowledge_budget
                    if ctx.token_budget else 2048
                )
                tags = _extract_tags(query)
                knowledge_result = await retriever.retrieve_for_prompt(
                    user_id=str(user_id),
                    instance_id="default",
                    query_tags=tags,
                    token_budget=token_budget,
                    query_vec=query_vec,
                )
                ctx.knowledge = knowledge_result or ""
            except Exception as e:
                logger.debug(f"[SessionEngine] Knowledge retrieval skipped: {e}")

        # 6. MemoryOS 记忆召回
        _memory_text = ""
        if self._memory_engine and query:
            try:
                retriever = self._memory_engine.get("retriever")
                if retriever:
                    memory_budget = (
                        ctx.token_budget.memory_budget
                        if ctx.token_budget else 0
                    )
                    mem_ctx = await retriever.retrieve(
                        session_id=session_id,
                        query=query,
                        query_topics=_extract_tags(query),
                        memory_budget=memory_budget,
                        query_vec=query_vec,
                    )
                    _memory_text = retriever.format_for_prompt(mem_ctx)
                    if _memory_text:
                        logger.info(
                            f"[SessionEngine] Memory context: "
                            f"tokens~{getattr(mem_ctx, 'total_tokens', 0)}"
                        )
            except Exception as e:
                logger.warning(f"[SessionEngine] Memory retrieval failed: {e}")

        # 合并 knowledge 和 memory
        if _memory_text:
            ctx.knowledge = (ctx.knowledge + "\n" + _memory_text).strip() if ctx.knowledge else _memory_text

        # 异步检查是否有到期 pending 预测，有则立即触发验证（不阻塞当前请求）
        if self._prediction_scheduler:
            import asyncio
            asyncio.ensure_future(
                self._prediction_scheduler.run_overdue_check(
                    user_id=user_id,
                    instance_id=getattr(self._config, "instance_id", "default"),
                )
            )

        # 异步 Dream 整合检查（不阻塞当前请求）
        if self._dream_consolidator and self._dream_consolidator.should_run(user_id):
            import asyncio
            instance_id = getattr(self._config, "instance_id", "default")
            asyncio.ensure_future(
                self._dream_consolidator.run(user_id=user_id, instance_id=instance_id)
            )

        return ctx

    def start_episode(self, query: str) -> None:
        """开始追踪本次请求的执行（KnowledgeEngine EpisodeTracker）"""
        if self._episode_tracker:
            try:
                self._episode_tracker.start(query=query)
            except Exception as e:
                logger.debug(f"[SessionEngine] start_episode failed: {e}")

    def record_tool_execution(
        self,
        skill_name: str,
        params: Dict[str, Any],
        result_summary: str,
        duration_ms: float,
    ) -> None:
        """记录工具执行（EpisodeTracker）"""
        if self._episode_tracker:
            try:
                self._episode_tracker.record_tool(
                    tool_name=skill_name,
                    params=params,
                    result_summary=result_summary,
                    duration_ms=duration_ms,
                )
            except Exception as e:
                logger.debug(f"[SessionEngine] record_tool failed: {e}")

    async def finalize(
        self,
        session_id: str,
        user_id: int,
        assistant_text: str,
        request_message: str,
        tool_calls_summary: Optional[List[str]] = None,
    ) -> None:
        """
        请求结束时执行收尾工作:
        1. 保存对话历史
        2. 提取经验
        3. 完成 episode → 触发知识蒸馏/反思
        4. 检查是否需要摘要压缩

        skip_memory=True 时（eval 模式）：跳过所有后处理写入，避免污染知识库和抢占 SQLite 锁
        """
        if self.skip_memory:
            logger.info(f"[SessionEngine] skip_memory=True, skipping finalize for session={session_id}")
            return

        # 1+3. 并行执行：保存对话历史 + MemoryOS 更新
        # 注意：episode_tracker.finish() 已删除（方法名错误，蒸馏职责由 Stop hook 的 knowledge_guard 负责）
        import asyncio as _asyncio
        _tasks = []

        if self._history:
            async def _append_history():
                try:
                    await self._history.append_text_exchange(
                        session_id=session_id,
                        user_text=request_message,
                        assistant_text=assistant_text,
                    )
                except Exception as e:
                    logger.warning(f"[SessionEngine] Failed to save history: {e}")
            _tasks.append(_append_history())

        if self._memory_engine:
            updater = self._memory_engine.get("updater")
            if updater:
                async def _update_memory():
                    try:
                        await updater.on_turn_end(
                            session_id=session_id,
                            user_msg=request_message,
                            assistant_msg=assistant_text,
                        )
                        logger.debug(f"[SessionEngine] MemoryOS updated for session={session_id}")
                    except Exception as e:
                        logger.warning(f"[SessionEngine] memory update failed: {e}")
                _tasks.append(_update_memory())

        if _tasks:
            await _asyncio.gather(*_tasks, return_exceptions=True)

        # 4. 预测提取（异步，不阻塞）
        if self._prediction_extractor and assistant_text:
            import asyncio
            asyncio.ensure_future(
                self._prediction_extractor.extract_and_save(
                    user_id=user_id,
                    instance_id=getattr(self._config, "instance_id", "default"),
                    session_id=session_id,
                    user_query=request_message,
                    assistant_text=assistant_text,
                )
            )

        # Dream 计数器累加（每次 session 完成 +1）
        if self._dream_consolidator:
            self._dream_consolidator.on_session_complete(user_id)

    async def learn_from_feedback(
        self,
        session_id: str,
        feedback_type: str,
        content: str,
    ) -> None:
        """从用户反馈学习（触发 FeedbackLearner）"""
        try:
            from agent_core.knowledge.feedback_learner import FeedbackLearner
            if hasattr(self, "_ke_feedback_learner") and self._ke_feedback_learner:
                await self._ke_feedback_learner.process_feedback(
                    session_id=session_id,
                    feedback_type=feedback_type,
                    content=content,
                )
        except Exception as e:
            logger.debug(f"[SessionEngine] learn_from_feedback failed: {e}")

    # ──────────────── 内部工具 ────────────────

    def _convert_history_to_openai(
        self,
        history_records: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        将 ConversationHistory 格式的记录转为 OpenAI messages 格式

        ConversationHistory 记录格式:
            {"role": "user"|"assistant", "content": str, ...}

        OpenAI 格式:
            {"role": "user"|"assistant", "content": str}
        """
        result = []
        for record in history_records:
            role = record.get("role", "")
            content = record.get("content", "")
            if role in ("user", "assistant") and content:
                result.append({"role": role, "content": str(content)})
        return result


def _extract_tags(query: str) -> List[str]:
    """从查询中提取标签（用于知识检索）"""
    words = query.replace("，", " ").replace("。", " ").replace("、", " ").split()
    return [w for w in words if len(w) >= 2][:10]
