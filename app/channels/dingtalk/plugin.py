"""
DingTalk Plugin

钉钉通道插件实现。

职责：
- 消息发送（Markdown、图片、文件）
- 流式会话管理（AI 卡片）
- 消息预处理

注意：消息监听由 app/dingtalk/stream_client.py 处理，本插件不重复实现。
"""
import json
from typing import List, Optional, Dict, Any
from loguru import logger

from app.channels.base import ChannelPlugin
from app.channels.types import (
    ChannelCapability,
    ChannelMessage,
    ReplyPayload,
    InboundMessage,
)
from app.channels.dingtalk.streams import DingTalkStreamingSession


class DingTalkPlugin(ChannelPlugin):
    """
    钉钉通道插件

    实现 ChannelPlugin 接口，提供钉钉消息发送能力。

    消息监听由 stream_client.py 处理，本插件专注于：
    1. 发送消息（Markdown、图片、文件）
    2. 流式会话管理（AI 卡片）
    3. 消息预处理
    """

    def __init__(self, config: dict):
        super().__init__(config)

        # 配置
        self.robot_code = config.get("robot_code", "")

        # 组件（延迟初始化）
        self._bot_handler = None
        self._uploader = None
        self._preprocessor = None

        # 流式会话管理
        self._streaming_sessions: Dict[str, DingTalkStreamingSession] = {}

    @property
    def id(self) -> str:
        return "dingtalk"

    @property
    def capabilities(self) -> List[ChannelCapability]:
        return [
            ChannelCapability.STREAMING,
            ChannelCapability.MARKDOWN,
            ChannelCapability.CARD,
            ChannelCapability.IMAGE,
            ChannelCapability.FILE,
            ChannelCapability.FEEDBACK,
        ]

    def _get_bot_handler(self):
        """获取 bot_handler（延迟加载）"""
        if self._bot_handler is None:
            try:
                from app.channels.dingtalk.stream_client import get_bot_handler
                self._bot_handler = get_bot_handler()
            except Exception as e:
                logger.warning(f"[DingTalk] Failed to get bot_handler: {e}")
        return self._bot_handler

    async def start(self):
        """
        启动钉钉插件

        注意：不启动 Stream 客户端，由 stream_client.py 单独管理。
        本插件只初始化发送相关的组件。
        """
        # 初始化上传器
        try:
            from app.channels.dingtalk.uploader import DingTalkUploader
            self._uploader = DingTalkUploader(self.config, None)
        except Exception as e:
            logger.warning(f"[DingTalk] Failed to init uploader: {e}")

        # 初始化预处理器
        try:
            from app.channels.dingtalk.preprocessor import DingTalkPreprocessor
            self._preprocessor = DingTalkPreprocessor(self.config, None)
        except Exception as e:
            logger.warning(f"[DingTalk] Failed to init preprocessor: {e}")

        logger.info(f"[DingTalk] Plugin started (send-only mode, robot_code={self.robot_code})")

    async def stop(self):
        """停止钉钉插件"""
        # 清理流式会话
        for session_id in list(self._streaming_sessions.keys()):
            try:
                del self._streaming_sessions[session_id]
            except Exception:
                pass
        logger.info("[DingTalk] Plugin stopped")

    # ========== 消息发送 ==========

    async def send_message(self, conversation_id: str, payload: ReplyPayload) -> bool:
        """
        发送消息（sampleMarkdown REST API，PC 端支持完整表格渲染）

        降级策略：access_token 获取失败或 REST API 调用失败时，退回 SDK reply_markdown_card。
        """
        content = payload.markdown or payload.text
        title = (payload.metadata or {}).get("title", "消息")
        conversation_type = (payload.metadata or {}).get("conversation_type", "")
        sender_staff_id = (payload.metadata or {}).get("sender_staff_id", "")

        try:
            from app.services.dingtalk_uploader import _get_access_token
            access_token = await _get_access_token()
        except Exception as e:
            logger.warning(f"[DingTalk] Failed to get access_token: {e}")
            access_token = None

        if not access_token:
            return await self._fallback_send(conversation_id, content, title)

        import httpx
        msg_param = json.dumps({"title": title, "text": content}, ensure_ascii=False)
        headers = {
            "x-acs-dingtalk-access-token": access_token,
            "Content-Type": "application/json",
        }

        if conversation_type == "2":
            # 群聊
            url = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
            body = {
                "robotCode": self.robot_code,
                "openConversationId": conversation_id,
                "msgKey": "sampleMarkdown",
                "msgParam": msg_param,
            }
        else:
            # 单聊
            url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
            body = {
                "robotCode": self.robot_code,
                "userIds": [sender_staff_id] if sender_staff_id else [],
                "msgKey": "sampleMarkdown",
                "msgParam": msg_param,
            }

        import asyncio
        max_retries = 3
        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.post(url, headers=headers, json=body)
                    if resp.status_code == 200:
                        logger.info(f"[DingTalk] sampleMarkdown sent ok, conv={conversation_id[:20]}")
                        return True
                    elif resp.status_code == 429 and attempt < max_retries - 1:
                        wait = 2 ** attempt  # 1s, 2s
                        logger.warning(
                            f"[DingTalk] sampleMarkdown throttled (429), retry {attempt+1}/{max_retries-1} in {wait}s"
                        )
                        await asyncio.sleep(wait)
                        continue
                    else:
                        logger.warning(
                            f"[DingTalk] sampleMarkdown failed: "
                            f"status={resp.status_code}, body={resp.text[:200]}"
                        )
                        return await self._fallback_send(conversation_id, content, title)
            except Exception as e:
                logger.warning(f"[DingTalk] sampleMarkdown request error: {e}")
                return await self._fallback_send(conversation_id, content, title)

    async def _fallback_send(self, conversation_id: str, content: str, title: str = "消息") -> bool:
        """降级：plugin 层无 incoming_message，返回 False 让调用方自行降级"""
        logger.warning("[DingTalk] Plugin fallback: returning False, caller should handle")
        return False

    async def send_streaming_start(self, conversation_id: str, initial_text: str = "", incoming_message=None) -> str:
        """开始流式会话"""
        bot_handler = self._get_bot_handler()
        if not bot_handler:
            logger.error("[DingTalk] bot_handler not available for streaming")
            return ""

        session = DingTalkStreamingSession(
            bot_handler=bot_handler,
            conversation_id=conversation_id,
            incoming_message=incoming_message,
        )
        session_id = await session.start(initial_text)
        if session_id:
            self._streaming_sessions[session_id] = session
        return session_id

    async def send_streaming_update(self, session_id: str, text: str):
        """更新流式内容"""
        session = self._streaming_sessions.get(session_id)
        if session:
            await session.update(text)

    async def send_streaming_finish(self, session_id: str, final_text: str, metadata: dict = None):
        """结束流式会话"""
        session = self._streaming_sessions.get(session_id)
        if session:
            # 构建按钮列表
            button_list = []
            if metadata:
                if metadata.get("report_id"):
                    button_list.extend(self._build_feedback_buttons(metadata["report_id"]))

            await session.finish(final_text, button_list)
            try:
                del self._streaming_sessions[session_id]
            except Exception:
                pass

    def _build_feedback_buttons(self, report_id: str) -> List[dict]:
        """构建反馈按钮"""
        return [
            {
                "text": "👍 有帮助",
                "action": json.dumps({"type": "feedback", "report_id": report_id, "score": 1}),
            },
            {
                "text": "👎 需改进",
                "action": json.dumps({"type": "feedback", "report_id": report_id, "score": -1}),
            },
        ]

    # ========== 媒体上传 ==========

    async def upload_image(self, image_url: str) -> Optional[str]:
        """上传图片"""
        if self._uploader:
            return await self._uploader.upload_from_url(image_url)
        return None

    async def send_file_message(
        self,
        conversation_id: str,
        filepath: str,
        filename: str
    ) -> Optional[str]:
        """发送文件消息"""
        bot_handler = self._get_bot_handler()
        if not bot_handler or not self._uploader:
            logger.warning("[DingTalk] bot_handler or uploader not available")
            return None

        try:
            # 上传文件
            media_id = await self._uploader.upload_file(filepath, filename)
            if not media_id:
                return None

            # 发送文件消息
            await bot_handler.send_file(
                conversation_id=conversation_id,
                media_id=media_id,
                file_name=filename,
            )

            return f"dingtalk://file/{media_id}"
        except Exception as e:
            logger.error(f"[DingTalk] Failed to send file message: {e}")
            return None

    # ========== 消息预处理 ==========

    async def preprocess_message(self, raw_message: Any) -> InboundMessage:
        """预处理原始消息"""
        if self._preprocessor:
            return await self._preprocessor.preprocess(raw_message)

        # 简化处理
        return InboundMessage(
            message_id=getattr(raw_message, 'message_id', ''),
            conversation_id=getattr(raw_message, 'conversation_id', ''),
            sender_id=getattr(raw_message, 'sender_staff_id', '') or
                     getattr(raw_message, 'sender_id', ''),
            sender_name=getattr(raw_message, 'sender_nick', ''),
            channel="dingtalk",
            message_type=self._map_message_type(getattr(raw_message, 'message_type', 'text')),
            raw_content=getattr(raw_message, 'content', ''),
            text_content=getattr(raw_message, 'content', ''),
            raw=raw_message,
        )

    def _map_message_type(self, msg_type_str: str):
        """映射消息类型"""
        from app.channels.types import MessageType
        mapping = {
            "text": MessageType.TEXT,
            "picture": MessageType.IMAGE,
            "file": MessageType.FILE,
            "richText": MessageType.RICH_TEXT,
            "audio": MessageType.AUDIO,
            "video": MessageType.VIDEO,
        }
        return mapping.get(msg_type_str, MessageType.TEXT)