"""
ConversationHistoryCore — 纯 SQLite 对话历史管理

Core 版本：仅使用 SessionContextDB（SQLite），无 Redis/MySQL 依赖。
摘要策略: 消息数超过 MAX_RAW_MESSAGES 时触发压缩，保留最近 KEEP_RECENT 条。
"""
import asyncio
import json
from typing import List, Dict, Optional, Callable, Awaitable, TYPE_CHECKING
from loguru import logger

if TYPE_CHECKING:
    from .context_db import SessionContextDB


def _load_compact_config():
    """
    加载压缩参数，优先从 app.agent.v4.session_compactor.SessionCompactConfig 单例读取，
    回退到直接读取 agent_core/config/session_compact.json，最终回退到硬编码默认值。
    """
    # 优先使用 SessionCompactConfig 单例（允许测试通过 mock 覆盖）
    try:
        from agent_core.session.session_compactor import SessionCompactConfig
        cfg = SessionCompactConfig.get_instance()
        return cfg.conv_max_raw_messages, cfg.conv_keep_recent, cfg.conv_max_total
    except Exception:
        pass
    # 直接读取文件
    try:
        import json as _json
        from pathlib import Path
        current = Path(__file__).resolve()
        for parent in current.parents:
            candidate = parent / "config" / "session_compact.json"
            if candidate.exists():
                raw = _json.loads(candidate.read_text(encoding="utf-8"))
                ch = raw.get("conversation_history", {})
                return (
                    ch.get("max_raw_messages", 15),
                    ch.get("keep_recent", 5),
                    ch.get("max_total", 30),
                )
    except Exception:
        pass
    return 15, 5, 30


class ConversationHistoryCore:
    """对话历史管理 — 纯 SQLite 版本"""

    def __init__(
        self,
        user_id: int = 1,
        instance_id: str = "default",
        sqlite_db: Optional["SessionContextDB"] = None,
        # 兼容 v4 ConversationHistory 参数（忽略 TTL，SQLite 不 TTL 过期）
        history_ttl: int = 0,
        summary_ttl: int = 0,
        **kwargs,
    ):
        self._user_id = user_id
        self._instance_id = instance_id
        self._sqlite = sqlite_db

        # 从 agent_core/config/session_compact.json 加载参数
        self.MAX_RAW_MESSAGES, self.KEEP_RECENT, self.MAX_TOTAL_MESSAGES = (
            _load_compact_config()
        )

    async def get(self, session_id: str) -> List[Dict[str, str]]:
        """获取原始历史消息"""
        if not self._sqlite:
            return []
        try:
            messages = await self._sqlite.get_history(
                session_id, self._user_id, self._instance_id,
                limit=self.MAX_TOTAL_MESSAGES,
            )
            return messages or []
        except Exception as e:
            logger.warning(f"[ConversationHistoryCore] get failed: {e}")
            return []

    async def save(self, session_id: str, history: List[Dict[str, str]]):
        """全量保存历史消息"""
        if not self._sqlite:
            return
        try:
            await self._sqlite.save_history(
                session_id, self._user_id, self._instance_id, history,
            )
        except Exception as e:
            logger.error(f"[ConversationHistoryCore] save failed: {e}")

    async def append(self, session_id: str, role: str, content: str,
                     metadata_json: Optional[str] = None):
        """追加一条消息"""
        if not self._sqlite:
            return
        try:
            metadata = None
            if metadata_json:
                try:
                    metadata = json.loads(metadata_json)
                except (json.JSONDecodeError, TypeError):
                    pass
            await self._sqlite.append_message(
                session_id, self._user_id, self._instance_id,
                role, content, metadata,
            )
        except Exception as e:
            logger.error(f"[ConversationHistoryCore] append failed: {e}")

    async def append_text_exchange(
        self, session_id: str, user_text: str, assistant_text: str,
    ):
        """追加一轮用户-助手对话"""
        if user_text:
            await self.append(session_id, "user", user_text)
        if assistant_text:
            await self.append(session_id, "assistant", assistant_text)

    async def update_last_assistant(self, session_id: str, content: str,
                                    metadata_json: Optional[str] = None):
        """更新最后一条 assistant 消息"""
        if not self._sqlite:
            return
        try:
            await self._sqlite.update_last_assistant(
                session_id, self._user_id, self._instance_id, content, metadata_json,
            )
        except Exception as e:
            logger.error(f"[ConversationHistoryCore] update_last_assistant failed: {e}")

    async def get_summary(self, session_id: str) -> Optional[str]:
        """获取历史摘要"""
        if not self._sqlite:
            return None
        try:
            return await self._sqlite.get_summary(
                session_id, self._user_id, self._instance_id,
            )
        except Exception as e:
            logger.warning(f"[ConversationHistoryCore] get_summary failed: {e}")
            return None

    async def save_summary(self, session_id: str, summary: str,
                           summary_chain: Optional[str] = None):
        """保存历史摘要"""
        if not self._sqlite:
            return
        try:
            await self._sqlite.save_summary(
                session_id, self._user_id, self._instance_id,
                summary, summary_chain,
            )
        except Exception as e:
            logger.error(f"[ConversationHistoryCore] save_summary failed: {e}")

    async def get_with_summary(self, session_id: str) -> dict:
        """获取摘要 + 最近原始消息"""
        history = await self.get(session_id)
        summary = await self.get_summary(session_id)
        return {
            "summary": summary,
            "recent": history[-self.MAX_RAW_MESSAGES:],
        }

    async def maybe_summarize(
        self,
        session_id: str,
        summarize_fn: Callable[[List[Dict[str, str]], Optional[str]], Awaitable[str]],
        progressive_summarizer=None,
        memory_updater=None,
        topic_extractor=None,
    ):
        """消息过多时触发摘要压缩"""
        history = await self.get(session_id)
        if len(history) <= self.MAX_RAW_MESSAGES:
            return

        old_messages = history[:-self.KEEP_RECENT]
        existing_summary = await self.get_summary(session_id)

        try:
            if progressive_summarizer:
                # 渐进式摘要（复用 app 层 ProgressiveSummarizer，按鸭子类型调用）
                try:
                    from agent_core.session.progressive_summarizer import SummaryChain
                    existing_chain = None
                    if self._sqlite:
                        try:
                            chain_json = await self._sqlite.get_summary_chain(
                                session_id, self._user_id, self._instance_id,
                            )
                            if chain_json:
                                existing_chain = SummaryChain.from_json(chain_json)
                        except Exception:
                            pass

                    budget = progressive_summarizer._guard.allocate_budget()
                    new_summary, new_chain = await progressive_summarizer.summarize(
                        old_messages, existing_chain,
                        budget.history_budget, summarize_fn,
                    )
                    if new_summary:
                        await self.save_summary(
                            session_id, new_summary,
                            summary_chain=new_chain.to_json() if new_chain else None,
                        )
                        await self.save(session_id, history[-self.KEEP_RECENT:])
                        logger.info(
                            f"[ConversationHistoryCore] Progressive summarized "
                            f"{len(old_messages)} messages for session {session_id}"
                        )
                        await self._notify_memory_updater(
                            memory_updater, topic_extractor, session_id, new_summary,
                            len(old_messages),
                        )
                except Exception as e:
                    logger.warning(
                        f"[ConversationHistoryCore] Progressive summarize failed: {e}, "
                        "falling back to simple summarize"
                    )
                    await self._simple_summarize(
                        session_id, old_messages, existing_summary, history,
                        summarize_fn, memory_updater, topic_extractor,
                    )
            else:
                await self._simple_summarize(
                    session_id, old_messages, existing_summary, history,
                    summarize_fn, memory_updater, topic_extractor,
                )
        except Exception as e:
            logger.error(f"[ConversationHistoryCore] Summarize failed: {e}")

    async def _simple_summarize(
        self, session_id, old_messages, existing_summary, history,
        summarize_fn, memory_updater, topic_extractor,
    ):
        """单次 LLM 压缩"""
        new_summary = await summarize_fn(old_messages, existing_summary)
        if new_summary:
            await self.save_summary(session_id, new_summary)
            await self.save(session_id, history[-self.KEEP_RECENT:])
            logger.info(
                f"[ConversationHistoryCore] Summarized {len(old_messages)} messages "
                f"for session {session_id}"
            )
            await self._notify_memory_updater(
                memory_updater, topic_extractor, session_id, new_summary,
                len(old_messages),
            )

    @staticmethod
    async def _notify_memory_updater(
        memory_updater, topic_extractor, session_id, summary, msg_count,
    ):
        """摘要完成后通知 MemoryUpdater 进行 STM→MTM 晋升（fire-and-forget）"""
        if not memory_updater:
            return
        try:
            topics, entities = [], []
            if topic_extractor:
                try:
                    topics, entities = await topic_extractor(summary)
                except Exception as e:
                    logger.debug(f"[ConversationHistoryCore] Topic extraction failed: {e}")

            coro = memory_updater.on_summarize(
                session_id=session_id,
                summary=summary,
                topics=topics,
                entities=entities,
                interaction_length=msg_count,
            )
            try:
                from agent_core.background_task_manager import create_background_task
                create_background_task(coro, task_name="memory_stm_to_mtm")
            except ImportError:
                asyncio.create_task(coro)
        except Exception as e:
            logger.debug(f"[ConversationHistoryCore] Memory updater notification failed: {e}")

    async def clear(self, session_id: str):
        """清空会话历史"""
        if not self._sqlite:
            return
        try:
            await self._sqlite.trim_history(
                session_id, self._user_id, self._instance_id, keep_recent=0,
            )
        except Exception as e:
            logger.warning(f"[ConversationHistoryCore] clear failed: {e}")
