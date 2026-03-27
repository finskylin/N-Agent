"""
Channel Plugin Base

通道插件基类，定义所有通道必须实现的接口。
"""
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any
from app.channels.types import (
    ChannelCapability,
    ChannelMessage,
    ReplyPayload,
    InboundMessage,
)


class ChannelPlugin(ABC):
    """
    通道插件基类

    所有通道（钉钉、飞书、企微）必须继承此类并实现所有抽象方法。
    """

    def __init__(self, config: dict):
        """
        初始化插件

        Args:
            config: 插件配置（从 plugin.json 或环境变量加载）
        """
        self.config = config
        self._message_handler = None
        self._logger = None

    # ========== 元信息 ==========

    @property
    @abstractmethod
    def id(self) -> str:
        """通道标识（如 'dingtalk', 'feishu'）"""
        pass

    @property
    @abstractmethod
    def capabilities(self) -> List[ChannelCapability]:
        """声明支持的能力"""
        pass

    # ========== 生命周期 ==========

    @abstractmethod
    async def start(self):
        """
        启动插件

        包括：连接 WebSocket、注册回调、初始化资源等
        """
        pass

    @abstractmethod
    async def stop(self):
        """停止插件，释放资源"""
        pass

    # ========== 消息发送 ==========

    @abstractmethod
    async def send_message(self, conversation_id: str, payload: ReplyPayload) -> bool:
        """
        发送消息

        Args:
            conversation_id: 会话 ID
            payload: 回复载荷

        Returns:
            是否发送成功
        """
        pass

    @abstractmethod
    async def send_streaming_start(self, conversation_id: str, initial_text: str = "", incoming_message=None) -> str:
        """
        开始流式会话

        Args:
            conversation_id: 会话 ID
            initial_text: 初始文本（如"正在思考..."）
            incoming_message: 原始消息对象（用于回复，钉钉等渠道需要）

        Returns:
            session_id: 用于后续更新的会话 ID
        """
        pass

    @abstractmethod
    async def send_streaming_update(self, session_id: str, text: str):
        """
        更新流式内容

        Args:
            session_id: 会话 ID
            text: 更新后的完整文本
        """
        pass

    @abstractmethod
    async def send_streaming_finish(self, session_id: str, final_text: str, metadata: dict = None):
        """
        结束流式会话

        Args:
            session_id: 会话 ID
            final_text: 最终文本
            metadata: 元数据（如 report_id、report_name、send_report_file）
        """
        pass

    # ========== 媒体上传 ==========

    @abstractmethod
    async def upload_image(self, image_url: str) -> Optional[str]:
        """
        上传图片

        Args:
            image_url: 图片 URL

        Returns:
            平台内部的 media_id 或 URL
        """
        pass

    # ========== 文件消息 ==========

    async def send_file_message(
        self,
        conversation_id: str,
        filepath: str,
        filename: str
    ) -> Optional[str]:
        """
        发送文件消息

        Args:
            conversation_id: 会话 ID
            filepath: 本地文件路径
            filename: 文件名

        Returns:
            文件下载 URL 或 None
        """
        # 默认实现：子类可覆盖
        return None

    # ========== 消息接收回调 ==========

    def set_message_handler(self, handler):
        """
        设置消息处理器

        当收到用户消息时，调用 handler(message: ChannelMessage)

        Args:
            handler: 异步函数，签名为 async def handler(message: ChannelMessage)
        """
        self._message_handler = handler

    async def _on_message(self, message: ChannelMessage):
        """内部消息处理，调用外部设置的 handler"""
        if self._message_handler:
            await self._message_handler(message)

    # ========== 消息预处理 ==========

    @abstractmethod
    async def preprocess_message(self, raw_message: Any) -> InboundMessage:
        """
        预处理原始消息

        将渠道特定的消息格式转换为标准化的 InboundMessage

        Args:
            raw_message: 原始消息对象

        Returns:
            标准化的 InboundMessage
        """
        pass

    # ========== 辅助方法 ==========

    def has_capability(self, capability: ChannelCapability) -> bool:
        """检查是否支持某能力"""
        return capability in self.capabilities

    @property
    def supports_streaming(self) -> bool:
        """是否支持流式输出"""
        return ChannelCapability.STREAMING in self.capabilities

    @property
    def supports_markdown(self) -> bool:
        """是否支持 Markdown"""
        return ChannelCapability.MARKDOWN in self.capabilities

    @property
    def supports_file(self) -> bool:
        """是否支持文件"""
        return ChannelCapability.FILE in self.capabilities