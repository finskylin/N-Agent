"""
DingTalk Preprocessor

钉钉消息预处理器，将钉钉消息转换为标准化格式。
"""
import re
from typing import Optional, List
from loguru import logger

from app.channels.preprocessor import MessagePreprocessor
from app.channels.types import (
    InboundMessage,
    MessageType,
    MediaAttachment,
    LinkInfo,
    MentionInfo,
    QuotedMessage,
)


class DingTalkPreprocessor(MessagePreprocessor):
    """
    钉钉消息预处理器

    处理钉钉各种消息类型：text, picture, file, richText, audio, video
    """

    def __init__(self, config: dict, client=None):
        super().__init__(config)
        self.client = client

    @property
    def channel_id(self) -> str:
        return "dingtalk"

    async def preprocess(self, raw_message) -> InboundMessage:
        """
        预处理钉钉消息

        Args:
            raw_message: 钉钉原始消息对象

        Returns:
            标准化的 InboundMessage
        """
        msg_type_str = getattr(raw_message, 'message_type', 'text')

        # 基本信息提取
        sender_id = getattr(raw_message, 'sender_staff_id', '') or \
                    getattr(raw_message, 'sender_id', '') or \
                    getattr(raw_message, 'staff_id', '')
        sender_name = getattr(raw_message, 'sender_nick', '') or \
                      getattr(raw_message, 'sender_name', '')

        # 初始化结果
        text_content = ""
        attachments: List[MediaAttachment] = []
        links: List[LinkInfo] = []
        mentions: List[MentionInfo] = []
        quoted_message = None

        # 根据消息类型处理
        msg_type = self._map_message_type(msg_type_str)

        if msg_type == MessageType.TEXT:
            text_content = getattr(raw_message, 'content', '') or ""
            links = self.extract_links(text_content)

        elif msg_type == MessageType.IMAGE:
            # 下载图片
            download_code = getattr(raw_message, 'download_code', '')
            robot_code = getattr(raw_message, 'robot_code', '')

            if download_code:
                attachment = await self.download_media(
                    message_id=getattr(raw_message, 'message_id', ''),
                    media_key=download_code,
                    media_type="image",
                    extra_params={"robot_code": robot_code}
                )
                if attachment:
                    attachments.append(attachment)
                    text_content = "<media:image>"

        elif msg_type == MessageType.FILE:
            # 下载文件
            download_code = getattr(raw_message, 'download_code', '')
            filename = getattr(raw_message, 'file_name', 'unknown')
            robot_code = getattr(raw_message, 'robot_code', '')

            if download_code:
                attachment = await self.download_media(
                    message_id=getattr(raw_message, 'message_id', ''),
                    media_key=download_code,
                    media_type="file",
                    extra_params={"robot_code": robot_code, "filename": filename}
                )
                if attachment:
                    attachment.filename = filename
                    attachments.append(attachment)
                    text_content = f"<media:file:{filename}>"

        elif msg_type == MessageType.RICH_TEXT:
            # 解析富文本
            content = getattr(raw_message, 'content', '') or ""
            parsed = self._parse_rich_text(content)
            text_content = parsed["text"]
            attachments.extend(parsed["attachments"])
            links.extend(parsed["links"])

        elif msg_type == MessageType.AUDIO:
            # 下载语音
            download_code = getattr(raw_message, 'download_code', '')
            if download_code:
                attachment = await self.download_media(
                    message_id=getattr(raw_message, 'message_id', ''),
                    media_key=download_code,
                    media_type="audio"
                )
                if attachment:
                    attachments.append(attachment)
                    text_content = "<media:audio>"

        # 获取引用消息
        parent_id = getattr(raw_message, 'reply_to_message_id', None)
        if parent_id:
            quoted_message = await self.get_quoted_message(parent_id)

        # 解析 @提及
        at_users = getattr(raw_message, 'at_users', []) or []
        for user in at_users:
            mentions.append(MentionInfo(
                user_id=user.get('staff_id', '') or user.get('userid', ''),
                user_name=user.get('name', ''),
            ))

        # 判断是否 @ 了机器人
        mentioned_bot = len(mentions) > 0 or \
                        (hasattr(raw_message, 'is_in_at_list') and raw_message.is_in_at_list)

        return InboundMessage(
            message_id=getattr(raw_message, 'message_id', ''),
            conversation_id=getattr(raw_message, 'conversation_id', ''),
            sender_id=sender_id,
            sender_name=sender_name,
            channel="dingtalk",
            message_type=msg_type,
            raw_content=getattr(raw_message, 'content', '') or "",
            text_content=text_content,
            attachments=attachments,
            links=links,
            mentions=mentions,
            quoted_message=quoted_message,
            thread_root_id=getattr(raw_message, 'conversation_id', None),
            parent_message_id=parent_id,
            mentioned_bot=mentioned_bot,
            raw=raw_message,
        )

    def _map_message_type(self, msg_type_str: str) -> MessageType:
        """映射钉钉消息类型"""
        mapping = {
            "text": MessageType.TEXT,
            "picture": MessageType.IMAGE,
            "file": MessageType.FILE,
            "richText": MessageType.RICH_TEXT,
            "audio": MessageType.AUDIO,
            "video": MessageType.VIDEO,
        }
        return mapping.get(msg_type_str, MessageType.TEXT)

    def _parse_rich_text(self, content: str) -> dict:
        """
        解析钉钉富文本消息

        钉钉富文本结构（Markdown 或 JSON）
        """
        result = {
            "text": content,
            "attachments": [],
            "links": [],
        }

        # 提取链接
        result["links"] = self.extract_links(content)

        # 提取 Markdown 图片 ![](url)
        img_pattern = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')
        for match in img_pattern.finditer(content):
            alt_text = match.group(1)
            img_url = match.group(2)
            result["attachments"].append(MediaAttachment(
                type="image",
                path="",  # 需要后续下载
                url=img_url,
            ))

        # 移除图片标记，保留纯文本
        text = img_pattern.sub('', content)
        result["text"] = text.strip()

        return result

    async def download_media(
        self,
        message_id: str,
        media_key: str,
        media_type: str,
        extra_params: dict = None
    ) -> Optional[MediaAttachment]:
        """
        下载钉钉媒体文件

        Args:
            message_id: 消息 ID
            media_key: 下载码
            media_type: 媒体类型
            extra_params: 额外参数

        Returns:
            MediaAttachment 或 None
        """
        try:
            from app.channels.dingtalk.uploader import DingTalkUploader

            uploader = DingTalkUploader(self.config, self.client)
            robot_code = extra_params.get('robot_code', '') if extra_params else ''

            # 调用钉钉下载接口
            buffer = await uploader.download_file(
                download_code=media_key,
                robot_code=robot_code,
            )

            if not buffer:
                return None

            # 检测 MIME 类型
            content_type = self._detect_content_type(buffer, media_type)

            # 保存到本地
            filename = extra_params.get('filename') if extra_params else None
            path = await self.save_media_buffer(buffer, content_type, filename)

            return MediaAttachment(
                type=media_type,
                path=path,
                content_type=content_type,
                size_bytes=len(buffer),
            )

        except Exception as e:
            logger.error(f"[DingTalk] Failed to download media: {e}")
            return None

    async def get_quoted_message(self, message_id: str) -> Optional[QuotedMessage]:
        """
        获取钉钉引用消息

        Args:
            message_id: 引用的消息 ID

        Returns:
            QuotedMessage 或 None
        """
        try:
            # 钉钉暂不支持通过 API 获取历史消息
            # 这里返回一个简化版本
            return QuotedMessage(
                message_id=message_id,
                content="[引用消息]",
            )

        except Exception as e:
            logger.error(f"[DingTalk] Failed to get quoted message: {e}")
            return None