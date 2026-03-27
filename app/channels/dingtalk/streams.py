"""
DingTalk Streaming Session

钉钉流式会话封装，处理 ai_start → ai_streaming → ai_finish 生命周期。
"""
import uuid
from typing import Optional, List
from loguru import logger


class DingTalkStreamingSession:
    """
    钉钉流式会话

    封装 AIMarkdownCardInstance 的生命周期：
    ai_start() → ai_streaming() × N → ai_finish()
    """

    def __init__(self, bot_handler, conversation_id: str, incoming_message=None):
        """
        初始化流式会话

        Args:
            bot_handler: DingTalkBotHandler 实例
            conversation_id: 会话 ID
            incoming_message: 原始消息对象（用于回复）
        """
        self.bot_handler = bot_handler
        self.conversation_id = conversation_id
        self.incoming_message = incoming_message

        self._card = None
        self._session_id: Optional[str] = None
        self._is_active = False

    async def start(self, initial_text: str = "") -> str:
        """
        开始流式会话

        Args:
            initial_text: 初始文本

        Returns:
            会话 ID
        """
        try:
            from dingtalk_stream.card_instance import AIMarkdownCardInstance

            self._session_id = str(uuid.uuid4())

            # 创建卡片实例
            self._card = AIMarkdownCardInstance(self.bot_handler, self.incoming_message)
            self._card.set_title_and_logo("分析中...", "")
            self._card.set_order(["msgTitle", "msgContent"])

            # 开始流式
            self._card.ai_start()
            self._is_active = True

            # 发送初始文本
            if initial_text:
                self._card.ai_streaming(markdown=initial_text, append=True)

            logger.info(f"[DingTalk][Streaming] Session started: {self._session_id}")
            return self._session_id

        except Exception as e:
            logger.error(f"[DingTalk][Streaming] Failed to start session: {e}")
            self._is_active = False
            return ""

    async def update(self, text: str):
        """
        更新流式内容

        Args:
            text: 更新后的完整文本
        """
        if not self._is_active or not self._card:
            return

        try:
            self._card.ai_streaming(markdown=text, append=False)
        except Exception as e:
            logger.error(f"[DingTalk][Streaming] Failed to update: {e}")

    async def finish(self, final_text: str, button_list: List[dict] = None):
        """
        结束流式会话

        Args:
            final_text: 最终文本
            button_list: 按钮列表
        """
        if not self._is_active or not self._card:
            return

        try:
            self._card.ai_finish(markdown=final_text, button_list=button_list or [])
            self._is_active = False

            logger.info(f"[DingTalk][Streaming] Session finished: {self._session_id}")

        except Exception as e:
            logger.error(f"[DingTalk][Streaming] Failed to finish: {e}")
            self._is_active = False

    @property
    def is_active(self) -> bool:
        return self._is_active