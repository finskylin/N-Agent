"""
Message Builder

构建发送给 Agent 的输入文本。
"""
from typing import Optional, List
from app.channels.types import InboundMessage, MediaAttachment, LinkInfo


class AgentInputBuilder:
    """
    构建发送给 Agent 的输入文本

    参考 OpenClaw 的 buildFeishuAgentBody() 实现
    """

    @staticmethod
    def build(message: InboundMessage, bot_user_id: Optional[str] = None) -> str:
        """
        构建 Agent 输入

        格式：
        [message_id: xxx]
        [Replying to: "引用消息内容"]
        [quoted_image: /path/to/quoted_image.png]
        [quoted_file: /path/to/quoted_file.pdf]
        SenderName: 消息内容
        [attached_image: /path/to/image.png]
        [attached_file: /path/to/file.pdf|name=report.pdf]
        [link: https://example.com]
        """
        parts = []

        # 1. 消息 ID（用于后续操作）
        parts.append(f"[message_id: {message.message_id}]")

        # 2. 引用消息（包含其附件和链接）
        if message.quoted_message:
            quoted = message.quoted_message
            quoted_text = quoted.content[:300] if quoted.content else ""
            parts.append(f'[Replying to: "{quoted_text}"]')

            # 引用消息中的附件
            for att in quoted.attachments:
                if att.type == "image":
                    parts.append(f"[quoted_image:{att.path}]")
                elif att.type == "file":
                    name = att.filename or "unknown"
                    parts.append(f"[quoted_file:{att.path}|name={name}]")

            # 引用消息中的链接
            for link in quoted.links:
                parts.append(f"[quoted_link:{link.url}]")

        # 3. 发送者标识 + 消息内容
        sender = message.sender_name or message.sender_id
        parts.append(f"{sender}: {message.text_content}")

        # 4. 当前消息的附件标记
        for att in message.attachments:
            if att.type == "image":
                parts.append(f"\n[attached_image:{att.path}]")
            elif att.type == "file":
                name = att.filename or "unknown"
                parts.append(f"\n[attached_file:{att.path}|name={name}]")
            elif att.type == "audio":
                parts.append(f"\n[attached_audio:{att.path}]")
            elif att.type == "video":
                parts.append(f"\n[attached_video:{att.path}]")

        # 5. 当前消息的链接标记
        for link in message.links:
            parts.append(f"\n[link:{link.url}]")

        # 6. @提及信息（告知 Agent 需要 @ 谁）
        if message.mentions:
            mention_names = [m.user_name or m.user_id for m in message.mentions]
            parts.append(
                f"\n[System: Your reply will automatically @mention: {', '.join(mention_names)}]"
            )

        return "\n".join(parts)

    @staticmethod
    def build_with_history(
        message: InboundMessage,
        history: List[dict],
        history_format: str = "envelope"
    ) -> str:
        """
        构建包含历史消息的 Agent 输入

        Args:
            message: 当前消息
            history: 历史消息列表 [{sender, body, timestamp}, ...]
            history_format: 历史格式 (envelope / plain)
        """
        parts = []

        # 历史消息
        if history:
            for entry in history[-10:]:  # 最多 10 条
                if history_format == "envelope":
                    parts.append(
                        f"[{entry.get('timestamp', '')}] "
                        f"{entry.get('sender', 'Unknown')}: {entry.get('body', '')}"
                    )
            parts.append("\n---\n")

        # 当前消息
        parts.append(AgentInputBuilder.build(message))

        return "\n".join(parts)

    @staticmethod
    def extract_report_markers(text: str) -> dict:
        """
        从 Agent 输出中提取报告标记

        Args:
            text: Agent 输出的文本

        Returns:
            {
                "report_name": str,
                "send_report_file": bool
            }
        """
        import re

        result = {
            "report_name": "",
            "send_report_file": False
        }

        # 提取 [REPORT_NAME:xxx]
        report_name_match = re.search(r'\[REPORT_NAME:([^\]]+)\]', text)
        if report_name_match:
            result["report_name"] = report_name_match.group(1)

        # 检查 [SEND_REPORT_FILE]
        if "[SEND_REPORT_FILE]" in text:
            result["send_report_file"] = True

        return result