"""
Message Preprocessor

消息预处理器基类，用于将渠道特定消息转换为标准化格式。
"""
from abc import ABC, abstractmethod
from typing import List, Optional
import re
from app.channels.types import (
    InboundMessage,
    MessageType,
    MediaAttachment,
    LinkInfo,
    MentionInfo,
    QuotedMessage,
)


class MessagePreprocessor(ABC):
    """
    消息预处理器基类

    职责：
    1. 解析消息类型
    2. 提取文本内容
    3. 下载媒体附件
    4. 获取引用消息
    5. 解析 @提及
    """

    # 链接提取正则
    URL_PATTERN = re.compile(
        r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+[/\w\.\-\?=%&#+]*'
    )

    def __init__(self, config: dict, media_max_bytes: int = 30 * 1024 * 1024):
        self.config = config
        self.media_max_bytes = media_max_bytes

    @property
    @abstractmethod
    def channel_id(self) -> str:
        """渠道标识"""
        pass

    @abstractmethod
    async def preprocess(self, raw_message: any) -> InboundMessage:
        """
        预处理消息，返回标准化结构

        子类实现：
        1. 解析消息类型
        2. 提取文本内容
        3. 下载媒体附件
        4. 获取引用消息
        5. 解析 @提及
        """
        pass

    @abstractmethod
    async def download_media(
        self,
        message_id: str,
        media_key: str,
        media_type: str,
        extra_params: dict = None
    ) -> Optional[MediaAttachment]:
        """
        下载媒体文件

        Args:
            message_id: 消息 ID
            media_key: 媒体标识（钉钉 download_code、飞书 file_key 等）
            media_type: 媒体类型（image, file, audio, video）
            extra_params: 额外参数

        Returns:
            MediaAttachment 或 None
        """
        pass

    @abstractmethod
    async def get_quoted_message(self, message_id: str) -> Optional[QuotedMessage]:
        """
        获取引用消息

        Args:
            message_id: 引用的消息 ID

        Returns:
            QuotedMessage 或 None
        """
        pass

    def extract_links(self, text: str) -> List[LinkInfo]:
        """从文本中提取链接"""
        links = []
        for match in self.URL_PATTERN.finditer(text):
            url = match.group(0)
            domain = self._extract_domain(url)
            links.append(LinkInfo(url=url, domain=domain))
        return links

    def _extract_domain(self, url: str) -> str:
        """提取域名"""
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            return parsed.netloc
        except Exception:
            return ""

    async def save_media_buffer(
        self,
        buffer: bytes,
        content_type: str,
        filename: Optional[str] = None
    ) -> str:
        """
        保存媒体到本地，返回路径

        Args:
            buffer: 媒体数据
            content_type: MIME 类型
            filename: 文件名（可选）

        Returns:
            本地文件路径
        """
        import hashlib

        # 生成文件名
        ext = self._get_extension(content_type)
        hash_suffix = hashlib.md5(buffer).hexdigest()[:8]
        timestamp = int(time.time() * 1000)
        filename = filename or f"media_{timestamp}_{hash_suffix}{ext}"

        # 保存路径
        media_dir = f"/tmp/channel_media/{self.channel_id}"
        os.makedirs(media_dir, exist_ok=True)
        path = os.path.join(media_dir, filename)

        with open(path, 'wb') as f:
            f.write(buffer)

        return path

    def _get_extension(self, content_type: str) -> str:
        """根据 MIME 类型获取扩展名"""
        ext_map = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "audio/mp3": ".mp3",
            "audio/ogg": ".ogg",
            "video/mp4": ".mp4",
            "application/pdf": ".pdf",
            "application/zip": ".zip",
        }
        return ext_map.get(content_type, "")

    def _detect_content_type(self, buffer: bytes, media_type: str) -> str:
        """检测内容类型"""
        # 简单的魔数检测
        if buffer[:8] == b'\x89PNG\r\n\x1a\n':
            return "image/png"
        elif buffer[:2] == b'\xff\xd8':
            return "image/jpeg"
        elif buffer[:4] == b'GIF8':
            return "image/gif"
        elif buffer[:4] == b'RIFF' and buffer[8:12] == b'WEBP':
            return "image/webp"
        elif buffer[:4] == b'%PDF':
            return "application/pdf"

        # 默认
        type_map = {
            "image": "image/png",
            "file": "application/octet-stream",
            "audio": "audio/mp3",
            "video": "video/mp4",
        }
        return type_map.get(media_type, "application/octet-stream")


# 延迟导入 os 和 time
import os
import time