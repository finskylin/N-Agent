"""
Reply Dispatcher

回复分发器：处理节流、队列、回调触发。
"""
import asyncio
import time
from typing import Optional
from app.channels.types import ReplyPayload, AgentCallbacks
from app.channels.base import ChannelPlugin


class ReplyDispatcher:
    """
    回复分发器

    职责：
    1. 节流：避免 API 调用过快（默认 300ms）
    2. 队列：确保消息顺序
    3. 回调触发：将 Agent 事件转换为 Channel 调用
    """

    def __init__(
        self,
        channel: ChannelPlugin,
        conversation_id: str,
        throttle_ms: int = 300,
        streaming_enabled: bool = True,
    ):
        """
        初始化分发器

        Args:
            channel: 通道插件实例
            conversation_id: 会话 ID
            throttle_ms: 节流间隔（毫秒）
            streaming_enabled: 是否启用流式输出
        """
        self.channel = channel
        self.conversation_id = conversation_id
        self.throttle_ms = throttle_ms
        self.streaming_enabled = streaming_enabled

        # 状态
        self._session_id: Optional[str] = None
        self._accumulated_text = ""
        self._last_update_time = 0
        self._pending_text: Optional[str] = None
        self._is_streaming = False
        self._lock = asyncio.Lock()

    async def start_streaming(self):
        """开始流式会话"""
        if not self.streaming_enabled:
            return

        async with self._lock:
            if self._is_streaming:
                return

            self._session_id = await self.channel.send_streaming_start(
                self.conversation_id,
                initial_text="⏳ 正在思考..."
            )
            self._is_streaming = True

    async def on_partial_reply(self, payload: ReplyPayload):
        """
        处理部分回复（节流）

        当 Agent 产生 text_delta 时调用
        """
        if not payload.text:
            return

        async with self._lock:
            self._accumulated_text = payload.text

            if not self._is_streaming:
                await self.start_streaming()

            # 节流检查
            now = time.time()
            elapsed_ms = (now - self._last_update_time) * 1000

            if elapsed_ms < self.throttle_ms:
                # 记录待更新文本，跳过本次更新
                self._pending_text = payload.text
                return

            # 执行更新
            self._pending_text = None
            self._last_update_time = now

        await self.channel.send_streaming_update(
            self._session_id,
            self._accumulated_text
        )

    async def on_tool_result(self, tool_name: str, result: dict):
        """
        处理工具执行结果

        可用于更新状态提示
        """
        # 目前不处理，可扩展为推送工具状态
        pass

    async def on_finish(self, payload: ReplyPayload):
        """完成流式会话"""
        async with self._lock:
            if not self._is_streaming or not self._session_id:
                # 没有流式会话，直接发送
                await self.channel.send_message(self.conversation_id, payload)
                return

            # 发送最终更新
            final_text = self._pending_text or self._accumulated_text
            if payload.text:
                final_text = payload.text

            self._is_streaming = False
            session_id = self._session_id
            self._session_id = None

        # 在锁外执行，避免阻塞
        await self.channel.send_streaming_finish(
            session_id,
            final_text,
            metadata=payload.metadata
        )

        # 检查是否需要发送报告文件
        if payload.send_report_file and payload.report_name:
            await self._send_report_file(payload)

    async def _send_report_file(self, payload: ReplyPayload):
        """发送报告文件消息"""
        import os
        import time as time_module

        # 生成文件
        filename = f"{payload.report_name}_{int(time_module.time() * 1000)}.md"
        report_dir = "/opt/agent-workspace/reports"
        os.makedirs(report_dir, exist_ok=True)
        filepath = os.path.join(report_dir, filename)

        # 写入文件
        content = payload.markdown or payload.text
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)

        # 发送文件消息
        await self.channel.send_file_message(
            conversation_id=self.conversation_id,
            filepath=filepath,
            filename=filename,
        )

    async def on_error(self, error: str):
        """处理错误"""
        if self._is_streaming and self._session_id:
            await self.channel.send_streaming_finish(
                self._session_id,
                f"❌ 发生错误: {error}",
                metadata=None
            )
            self._is_streaming = False
            self._session_id = None

    def get_callbacks(self) -> AgentCallbacks:
        """获取 Agent 回调接口"""
        return AgentCallbacks(
            on_partial_reply=self.on_partial_reply,
            on_tool_result=self.on_tool_result,
            on_finish=self.on_finish,
            on_error=self.on_error,
        )