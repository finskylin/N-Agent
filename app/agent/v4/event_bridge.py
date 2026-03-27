"""
Event Bridge — SDK Message → SSE Event 转换

职责:
1. 将 Claude Agent SDK 的 Message 对象转换为前端可消费的 SSE 事件格式
2. 管理 Hooks 产生的旁路事件队列（PreToolUse/PostToolUse 产生的 thinking_step、component 等事件）

两个事件来源:
- SDK Message 流 (receive_messages) → convert_message()
- Hook 旁路事件 (PreToolUse/PostToolUse) → push_event() + get_event_queue()
"""
import asyncio
from typing import AsyncIterator, Dict, Any, Optional
from loguru import logger


class EventBridge:
    """
    将 Claude Agent SDK 的 Message 流转换为前端 SSE 事件流

    线程安全: 使用 asyncio.Queue 跨协程传递事件
    """

    def __init__(self):
        self._event_queue: asyncio.Queue = asyncio.Queue()

    def get_event_queue(self) -> asyncio.Queue:
        """获取 Hook 旁路事件队列，供 _consume_sdk_stream 消费"""
        return self._event_queue

    def push_event(self, event: Dict[str, Any]) -> None:
        """
        Hooks 调用此方法推送旁路事件

        使用 call_soon_threadsafe 确保跨线程安全
        """
        try:
            loop = asyncio.get_running_loop()
            loop.call_soon_threadsafe(self._event_queue.put_nowait, event)
        except RuntimeError:
            # 如果没有 running loop，直接放入
            try:
                self._event_queue.put_nowait(event)
            except Exception as e:
                logger.warning(f"[EventBridge] Failed to push event: {e}")

    def reset(self) -> None:
        """重置事件队列（每次请求开始时调用）"""
        # 排空旧事件
        while not self._event_queue.empty():
            try:
                self._event_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._event_queue = asyncio.Queue()

    async def convert_message(self, message: Any) -> AsyncIterator[Dict[str, Any]]:
        """
        将 SDK Message 转换为 SSE 事件

        映射关系:
        - AssistantMessage:
            - TextBlock      → event: text_delta
            - ThinkingBlock  → event: thinking
            - ToolUseBlock   → (由 PreToolUse Hook 处理，这里忽略避免重复)
            - ToolResultBlock → (由 PostToolUse Hook 处理)
        - ResultMessage      → event: result
        - UserMessage        → (忽略，不回显)

        Args:
            message: SDK Message 对象（AssistantMessage / ResultMessage / UserMessage）

        Yields:
            SSE 事件字典 {"event": str, "data": dict}
        """
        message_type = type(message).__name__

        if message_type == "AssistantMessage":
            content_blocks = getattr(message, "content", [])
            for block in content_blocks:
                block_type = type(block).__name__

                if block_type == "TextBlock":
                    text = getattr(block, "text", "")
                    if text:
                        yield {
                            "event": "text_delta",
                            "data": {"delta": text}
                        }

                elif block_type == "ThinkingBlock":
                    # ThinkingBlock 不推送给前端，避免展示不完整的思考过程
                    # SDK 返回的 thinking 是内部推理，前端无需展示
                    pass

                elif block_type == "ToolUseBlock":
                    # ToolUseBlock 的 tool_call 事件已在 PreToolUse Hook 中推送
                    # 这里仅做日志记录，避免重复推送
                    tool_name = getattr(block, "name", "unknown")
                    logger.debug(
                        f"[EventBridge] ToolUseBlock: {tool_name} "
                        f"(event handled by PreToolUse hook)"
                    )

                elif block_type == "ToolResultBlock":
                    # ToolResultBlock 的 tool_done 事件已在 PostToolUse Hook 中推送
                    tool_use_id = getattr(block, "tool_use_id", "unknown")
                    logger.debug(
                        f"[EventBridge] ToolResultBlock: {tool_use_id} "
                        f"(event handled by PostToolUse hook)"
                    )

                else:
                    logger.debug(f"[EventBridge] Unknown block type: {block_type}")

        elif message_type == "ResultMessage":
            # 不再向前端发送 ResultMessage 的内部信息（session_id, transcript_path 等）
            # 这些是 Claude SDK 的内部状态，不应暴露给前端
            # native_agent.py 已经单独处理 ResultMessage 来提取 CLI session ID
            logger.debug(f"[EventBridge] ResultMessage received, not forwarding to frontend")
            pass

        elif message_type == "UserMessage":
            # 忽略用户消息的回显
            pass

        else:
            logger.debug(f"[EventBridge] Unhandled message type: {message_type}")
