"""
Feishu Plugin
飞书通道插件实现

职责：
- 消息发送（post 格式 Markdown + 交互卡片）
- 流式会话管理（编辑消息实现打字机效果）
- 消息预处理

注意：消息监听由 app/feishu/webhook.py 的 FastAPI 路由处理，本插件不重复实现。
"""
import json
import asyncio
from typing import List, Optional, Dict, Any
from loguru import logger

from app.channels.base import ChannelPlugin
from app.channels.types import (
    ChannelCapability,
    ChannelMessage,
    ReplyPayload,
    InboundMessage,
    MessageType,
)


class FeishuStreamingSession:
    """
    飞书流式消息会话

    使用"先发送占位消息，再编辑更新"实现打字机效果。
    """

    def __init__(
        self,
        chat_id: str,
        open_id: str,
        chat_type: str,
        parent_message_id: Optional[str] = None,
    ):
        self.chat_id = chat_id
        self.open_id = open_id
        self.chat_type = chat_type
        self.parent_message_id = parent_message_id
        self._message_id: Optional[str] = None
        self._last_update = 0.0
        self._update_interval = 1.5  # 至少间隔 1.5 秒更新一次，避免频率限制

    async def start(self, initial_text: str = "正在思考...") -> str:
        """发送初始消息，返回 session_id（即消息 ID）"""
        from app.channels.feishu.client import (
            reply_message, send_message, build_post_content
        )

        content = build_post_content(initial_text)

        if self.parent_message_id:
            self._message_id = await reply_message(
                self.parent_message_id, content, msg_type="post"
            )
        else:
            receive_id = self.chat_id if self.chat_type == "group" else self.open_id
            receive_id_type = "chat_id" if self.chat_type == "group" else "open_id"
            self._message_id = await send_message(
                receive_id, receive_id_type, content, msg_type="post"
            )

        if self._message_id:
            logger.debug(f"[Feishu] Streaming session started: {self._message_id}")
            return self._message_id
        return ""

    async def update(self, text: str) -> None:
        """更新消息内容（限速）"""
        if not self._message_id:
            return

        import time
        now = time.time()
        if now - self._last_update < self._update_interval:
            return

        from app.channels.feishu.client import edit_message, build_post_content
        content = build_post_content(text + "\n\n_▌正在生成..._")
        await edit_message(self._message_id, content, msg_type="post")
        self._last_update = now

    async def finish(self, final_text: str, buttons: list = None) -> None:
        """完成流式输出，发送最终内容"""
        if not self._message_id:
            return

        from app.channels.feishu.client import edit_message, build_post_content, build_interactive_card

        if buttons:
            # 有按钮时使用交互卡片
            content = build_interactive_card(final_text, buttons)
            await edit_message(self._message_id, content, msg_type="interactive")
        else:
            content = build_post_content(final_text)
            await edit_message(self._message_id, content, msg_type="post")

        logger.debug(f"[Feishu] Streaming session finished: {self._message_id}")


class FeishuPlugin(ChannelPlugin):
    """
    飞书通道插件

    实现 ChannelPlugin 接口，提供飞书消息发送能力。
    消息监听由 webhook.py 处理，本插件专注于发送。
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self._streaming_sessions: Dict[str, FeishuStreamingSession] = {}

    @property
    def id(self) -> str:
        return "feishu"

    @property
    def capabilities(self) -> List[ChannelCapability]:
        return [
            ChannelCapability.STREAMING,
            ChannelCapability.MARKDOWN,
            ChannelCapability.CARD,
            ChannelCapability.IMAGE,
            ChannelCapability.FILE,
            ChannelCapability.FEEDBACK,
            ChannelCapability.THREAD,
        ]

    async def start(self):
        """启动飞书插件（发送侧初始化）"""
        logger.info("[Feishu] Plugin started (webhook mode, events via /feishu/events)")

    async def stop(self):
        """停止飞书插件"""
        for session_id in list(self._streaming_sessions.keys()):
            try:
                del self._streaming_sessions[session_id]
            except Exception:
                pass
        logger.info("[Feishu] Plugin stopped")

    # ========== 消息发送 ==========

    async def send_message(self, conversation_id: str, payload: ReplyPayload) -> bool:
        """
        发送消息到飞书（post 格式 Markdown）

        conversation_id 格式：
        - "chat:oc_xxx" → 群聊
        - "user:ou_xxx" → 单聊
        - "oc_xxx" → 默认识别为群聊 chat_id
        """
        content_text = payload.markdown or payload.text
        metadata = payload.metadata or {}

        receive_id, receive_id_type = self._parse_conversation_id(
            conversation_id,
            metadata.get("open_id", ""),
        )

        from app.channels.feishu.client import send_message, build_post_content
        content = build_post_content(content_text)

        result_id = await send_message(receive_id, receive_id_type, content, msg_type="post")
        return result_id is not None

    async def send_streaming_start(
        self,
        conversation_id: str,
        initial_text: str = "",
        incoming_message=None,
    ) -> str:
        """开始流式会话"""
        metadata = {}
        if incoming_message:
            metadata = getattr(incoming_message, "params", {}) or {}

        chat_id = metadata.get("feishu_chat_id", "")
        open_id = metadata.get("feishu_open_id", "")
        chat_type = metadata.get("feishu_chat_type", "p2p")
        parent_message_id = metadata.get("feishu_message_id", "")

        if not chat_id and not open_id:
            # 从 conversation_id 解析
            receive_id, receive_id_type = self._parse_conversation_id(conversation_id, "")
            if receive_id_type == "chat_id":
                chat_id = receive_id
                chat_type = "group"
            else:
                open_id = receive_id

        session = FeishuStreamingSession(
            chat_id=chat_id,
            open_id=open_id,
            chat_type=chat_type,
            parent_message_id=parent_message_id,
        )
        session_id = await session.start(initial_text or "正在分析...")
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
        if not session:
            return

        buttons = []
        if metadata and metadata.get("report_id"):
            buttons = self._build_feedback_buttons(metadata["report_id"])

        await session.finish(final_text, buttons)
        try:
            del self._streaming_sessions[session_id]
        except Exception:
            pass

    def _build_feedback_buttons(self, report_id: str) -> List[dict]:
        """构建反馈按钮"""
        return [
            {"text": "👍 有帮助", "action": json.dumps({"type": "feedback", "report_id": report_id, "score": 1})},
            {"text": "👎 需改进", "action": json.dumps({"type": "feedback", "report_id": report_id, "score": -1})},
        ]

    # ========== 媒体上传 ==========

    async def upload_image(self, image_url: str) -> Optional[str]:
        """上传图片（飞书暂不实现，返回 None）"""
        return None

    # ========== 消息预处理 ==========

    async def preprocess_message(self, raw_message: Any) -> InboundMessage:
        """将飞书原始事件转换为标准 InboundMessage"""
        if isinstance(raw_message, dict):
            message = raw_message.get("message", {})
            sender = raw_message.get("sender", {})
            sender_id_info = sender.get("sender_id", {})
            open_id = sender_id_info.get("open_id", "")
            chat_id = message.get("chat_id", "")
            message_id = message.get("message_id", "")
            msg_type = message.get("message_type", "text")

            import json as _json
            content_str = message.get("content", "{}")
            try:
                content = _json.loads(content_str)
            except Exception:
                content = {}

            text_content = content.get("text", "") if msg_type == "text" else ""

            return InboundMessage(
                message_id=message_id,
                conversation_id=chat_id,
                sender_id=open_id,
                sender_name=sender.get("sender_type", ""),
                channel="feishu",
                message_type=self._map_message_type(msg_type),
                raw_content=content_str,
                text_content=text_content,
                raw=raw_message,
            )

        return InboundMessage(
            message_id="",
            conversation_id="",
            sender_id="",
            channel="feishu",
            message_type=MessageType.TEXT,
            raw_content="",
            text_content="",
            raw=raw_message,
        )

    def _map_message_type(self, msg_type_str: str) -> MessageType:
        """映射消息类型"""
        mapping = {
            "text": MessageType.TEXT,
            "post": MessageType.RICH_TEXT,
            "image": MessageType.IMAGE,
            "file": MessageType.FILE,
            "audio": MessageType.AUDIO,
            "video": MessageType.VIDEO,
        }
        return mapping.get(msg_type_str, MessageType.TEXT)

    def _parse_conversation_id(self, conversation_id: str, open_id: str = "") -> tuple[str, str]:
        """
        解析 conversation_id，返回 (receive_id, receive_id_type)

        支持格式：
        - "chat:oc_xxx" → (oc_xxx, chat_id)
        - "user:ou_xxx" → (ou_xxx, open_id)
        - "oc_xxx" / "oc_..." → (oc_xxx, chat_id)  飞书 chat_id 以 oc_ 开头
        - "ou_xxx" → (ou_xxx, open_id)              飞书 open_id 以 ou_ 开头
        - 其他 → 优先 chat_id
        """
        if conversation_id.startswith("chat:"):
            return conversation_id[5:], "chat_id"
        if conversation_id.startswith("user:"):
            return conversation_id[5:], "open_id"
        if conversation_id.startswith("oc_"):
            return conversation_id, "chat_id"
        if conversation_id.startswith("ou_"):
            return conversation_id, "open_id"
        if open_id:
            return open_id, "open_id"
        return conversation_id, "chat_id"
