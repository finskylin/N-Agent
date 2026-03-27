"""
Channel Manager

通道管理器：插件加载、消息路由、回调注入。
"""
import asyncio
from typing import Dict, Optional, Any
from loguru import logger

from app.channels.base import ChannelPlugin
from app.channels.types import ChannelMessage, InboundMessage, AgentCallbacks
from app.channels.reply_dispatcher import ReplyDispatcher
from app.channels.message_builder import AgentInputBuilder


class ChannelManager:
    """
    通道管理器

    职责：
    1. 插件注册与管理
    2. 消息路由到 Agent
    3. 回调注入
    """

    def __init__(self, agent_factory=None):
        """
        初始化通道管理器

        Args:
            agent_factory: Agent 工厂函数，用于创建 Agent 实例
        """
        self._plugins: Dict[str, ChannelPlugin] = {}
        self._agent_factory = agent_factory
        self._started = False

    def register(self, plugin: ChannelPlugin):
        """
        注册插件

        Args:
            plugin: 通道插件实例
        """
        self._plugins[plugin.id] = plugin
        plugin.set_message_handler(self._on_message)
        logger.info(f"[ChannelManager] Registered plugin: {plugin.id}")

    def unregister(self, channel_id: str):
        """
        注销插件

        Args:
            channel_id: 通道标识
        """
        if channel_id in self._plugins:
            del self._plugins[channel_id]
            logger.info(f"[ChannelManager] Unregistered plugin: {channel_id}")

    def get_plugin(self, channel_id: str) -> Optional[ChannelPlugin]:
        """
        获取插件实例

        Args:
            channel_id: 通道标识

        Returns:
            插件实例或 None
        """
        return self._plugins.get(channel_id)

    async def start_all(self):
        """启动所有插件"""
        if self._started:
            logger.warning("[ChannelManager] Already started")
            return

        for plugin in self._plugins.values():
            try:
                await plugin.start()
                logger.info(f"[ChannelManager] Started plugin: {plugin.id}")
            except Exception as e:
                logger.error(f"[ChannelManager] Failed to start plugin {plugin.id}: {e}")

        self._started = True
        logger.info(f"[ChannelManager] All plugins started, count: {len(self._plugins)}")

    async def stop_all(self):
        """停止所有插件"""
        for plugin in self._plugins.values():
            try:
                await plugin.stop()
                logger.info(f"[ChannelManager] Stopped plugin: {plugin.id}")
            except Exception as e:
                logger.error(f"[ChannelManager] Failed to stop plugin {plugin.id}: {e}")

        self._started = False
        logger.info("[ChannelManager] All plugins stopped")

    async def _on_message(self, message: ChannelMessage):
        """
        消息处理入口

        当收到用户消息时，由插件调用此方法

        Args:
            message: 标准化的通道消息
        """
        channel = self._plugins.get(message.channel)
        if not channel:
            logger.error(f"[ChannelManager] Unknown channel: {message.channel}")
            return

        try:
            # 预处理消息
            inbound_message = await channel.preprocess_message(message.raw)
            if not inbound_message:
                logger.warning(f"[ChannelManager] Failed to preprocess message: {message.message_id}")
                return

            # 构建 Agent 输入
            agent_input = AgentInputBuilder.build(inbound_message)

            # 创建回复分发器
            dispatcher = ReplyDispatcher(
                channel=channel,
                conversation_id=message.conversation_id,
                streaming_enabled=channel.supports_streaming,
            )

            # 执行 Agent
            await self._execute_agent(agent_input, dispatcher, message)

        except Exception as e:
            logger.error(f"[ChannelManager] Error processing message: {e}")
            import traceback
            traceback.print_exc()

    async def _execute_agent(
        self,
        agent_input: str,
        dispatcher: ReplyDispatcher,
        message: ChannelMessage
    ):
        """
        执行 Agent

        Args:
            agent_input: Agent 输入文本
            dispatcher: 回复分发器
            message: 原始消息
        """
        if not self._agent_factory:
            logger.error("[ChannelManager] No agent factory configured")
            return

        try:
            agent = self._agent_factory()
            callbacks = dispatcher.get_callbacks()

            # 构建 Agent 请求
            from app.agent.v4.models import V4AgentRequest
            request = V4AgentRequest(
                message=agent_input,
                session_id=f"{message.channel}_{message.conversation_id}",
                user_id=message.sender_id,
                channel=message.channel,
                callbacks=callbacks,
            )

            # 执行 Agent 流式处理
            accumulated_text = ""
            accumulated_markdown = ""

            async for event in agent.process_stream(request):
                event_type = event.get("event", "")
                event_data = event.get("data", {})

                if event_type == "text_delta":
                    delta = event_data.get("delta", "")
                    accumulated_text += delta
                    accumulated_markdown += delta

                    if callbacks.on_partial_reply:
                        await callbacks.on_partial_reply(
                            type('ReplyPayload', (), {
                                'text': accumulated_text,
                                'markdown': accumulated_markdown,
                            })()
                        )

                elif event_type == "done":
                    # 提取报告标记
                    markers = AgentInputBuilder.extract_report_markers(accumulated_text)

                    payload = type('ReplyPayload', (), {
                        'text': accumulated_text,
                        'markdown': accumulated_markdown,
                        'is_final': True,
                        'metadata': event_data,
                        'report_name': markers.get("report_name", ""),
                        'send_report_file': markers.get("send_report_file", False),
                    })()

                    if callbacks.on_finish:
                        await callbacks.on_finish(payload)

                elif event_type == "error":
                    if callbacks.on_error:
                        await callbacks.on_error(event_data.get("error", "Unknown error"))

        except Exception as e:
            logger.error(f"[ChannelManager] Agent execution error: {e}")
            import traceback
            traceback.print_exc()


# 全局单例
_channel_manager: Optional[ChannelManager] = None


def get_channel_manager() -> ChannelManager:
    """获取全局 ChannelManager 实例"""
    global _channel_manager
    if _channel_manager is None:
        _channel_manager = ChannelManager()
    return _channel_manager


def init_channel_manager(agent_factory=None) -> ChannelManager:
    """
    初始化全局 ChannelManager

    Args:
        agent_factory: Agent 工厂函数

    Returns:
        ChannelManager 实例
    """
    global _channel_manager
    _channel_manager = ChannelManager(agent_factory=agent_factory)
    return _channel_manager