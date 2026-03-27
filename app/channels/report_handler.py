"""
Report Handler

报告文件处理器：生成 MD 文件并发送文件消息。
"""
import os
import time
from typing import Optional
from app.channels.base import ChannelPlugin


class ReportHandler:
    """
    报告文件处理器

    职责：
    1. 检查是否需要发送报告文件（[SEND_REPORT_FILE] 标记）
    2. 生成 MD 文件
    3. 上传并发送文件消息（仅聊天工具）
    """

    def __init__(self, report_dir: str = "/opt/agent-workspace/reports"):
        self.report_dir = report_dir

    async def send_report_if_needed(
        self,
        channel_plugin: ChannelPlugin,
        conversation_id: str,
        markdown_content: str,
        report_name: str,
        send_report_file: bool
    ) -> Optional[str]:
        """
        如果标记了发送报告文件，则生成并发送

        Args:
            channel_plugin: 渠道插件（钉钉/飞书）
            conversation_id: 会话 ID
            markdown_content: Markdown 内容
            report_name: 报告英文名称
            send_report_file: 是否需要发送文件

        Returns:
            文件 URL 或 None
        """
        if not send_report_file:
            return None

        # Web 渠道不需要发送文件消息
        if channel_plugin.id == "web":
            return None

        # 生成 MD 文件
        filename = f"{report_name or 'report'}_{int(time.time() * 1000)}.md"
        filepath = os.path.join(self.report_dir, filename)

        os.makedirs(self.report_dir, exist_ok=True)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(markdown_content)

        # 发送文件消息
        file_url = await channel_plugin.send_file_message(
            conversation_id=conversation_id,
            filepath=filepath,
            filename=filename,
        )

        return file_url