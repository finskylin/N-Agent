"""
DingTalk Bot Handler
钉钉机器人消息处理器

继承 dingtalk_stream.ChatbotHandler，处理用户消息并调用 V4 Agent

重要：钉钉 Stream 模式要求快速返回 ACK，否则会重试发送消息。
因此我们采用"先 ACK，后处理"的异步模式，并使用消息去重防止重复处理。
"""
import asyncio
import hashlib
from typing import Optional, Set
from loguru import logger
import time
import json

try:
    import dingtalk_stream
    from dingtalk_stream import AckMessage
    DINGTALK_AVAILABLE = True
except ImportError:
    DINGTALK_AVAILABLE = False
    logger.warning("[DingTalk] dingtalk-stream not installed, bot disabled")


# 消息去重缓存（messageId -> timestamp）
# 用于防止钉钉重试导致的重复处理
_processed_messages: dict = {}
_processed_messages_lock = asyncio.Lock()
_MESSAGE_CACHE_TTL = 300  # 5分钟内的重复消息会被忽略

async def _is_duplicate_message(message_id: str) -> bool:
    """检查消息是否已处理过（去重，异步锁保护）"""
    global _processed_messages

    if not message_id:
        return False

    now = time.time()

    async with _processed_messages_lock:
        # 清理过期缓存
        expired_keys = [k for k, v in _processed_messages.items() if now - v > _MESSAGE_CACHE_TTL]
        for k in expired_keys:
            del _processed_messages[k]

        # 检查是否重复
        if message_id in _processed_messages:
            logger.warning(f"[DingTalk] Duplicate message detected, skipping: {message_id[:20]}...")
            return True

        # 标记为已处理
        _processed_messages[message_id] = now
        return False


class AgentBotHandler:
    """
    钉钉机器人消息处理器

    将钉钉消息转发给 V4 Agent 处理，并将结果回复给用户
    """

    def __init__(self):
        self._agent = None

    def _get_agent(self):
        """延迟初始化 V4 Agent（复用现有实例）"""
        if self._agent is None:
            from app.agent.v4.native_agent import V4NativeAgent
            self._agent = V4NativeAgent()
            logger.info("[DingTalk] V4NativeAgent initialized for bot")
        return self._agent

    async def process_message(
        self,
        message: str,
        conversation_id: str,
        sender_id: str,
        sender_nick: Optional[str] = None,
    ) -> str:
        """
        处理用户消息

        Args:
            message: 用户发送的消息内容
            conversation_id: 会话 ID（群聊 ID 或单聊 ID）
            sender_id: 发送者 ID
            sender_nick: 发送者昵称

        Returns:
            回复消息（Markdown 格式）
        """
        agent = self._get_agent()

        from app.agent.v4.native_agent import V4AgentRequest, CHANNEL_DINGTALK

        # 构建 Agent 请求
        # 使用 text_only 模式，不返回 UI 组件
        # 使用 markdown 格式输出
        # auto_approve_plan=true 自动批准计划
        # channel=dingtalk 标识钉钉渠道，用于 Markdown 格式适配
        # 构建用户隔离的 session_id
        user_key = hashlib.md5(sender_id.encode()).hexdigest()[:10]
        user_session_id = f"dingtalk_{conversation_id}_{user_key}"

        from app.channels.dingtalk.utils import generate_dingtalk_user_id
        try:
            dingtalk_user_id = generate_dingtalk_user_id(sender_id)
        except ValueError:
            logger.warning(f"[DingTalk] Empty sender_id, rejecting message")
            return "无法识别您的身份，请重试或联系管理员。"

        agent_request = V4AgentRequest(
            message=message,
            session_id=user_session_id,
            user_id=dingtalk_user_id,
            params={
                "auto_approve_plan": True,
                "dingtalk_sender": sender_nick or sender_id,
                "dingtalk_sender_id": sender_id,
                "dingtalk_staff_id": "",
                "dingtalk_conversation_id": conversation_id,
                "dingtalk_conversation_type": "",
                "dingtalk_robot_code": "",
            },
            output_format="markdown",
            render_mode="text_only",
            channel=CHANNEL_DINGTALK,  # 钉钉渠道，自动适配 Markdown 格式
            attached_files=[],  # process_message 同步入口无附件
        )

        # Langfuse: 创建 Trace（钉钉同步入口）
        try:
            from app.utils.langfuse_client import langfuse
            lf_trace = langfuse.trace(
                name="dingtalk_chat_sync",
                user_id=str(dingtalk_user_id),
                session_id=user_session_id,
                input={
                    "message": message,
                    "sender": sender_nick or sender_id,
                },
                metadata={
                    "channel": "dingtalk",
                    "conversation_id": conversation_id,
                    "entry": "process_message_sync",
                },
            )
            agent_request.langfuse_trace = lf_trace
        except Exception as lf_err:
            logger.debug(f"[Langfuse] DingTalk sync trace creation skipped: {lf_err}")

        # 收集文本输出，acknowledgment 会作为第一部分立即返回
        text_chunks = []
        acknowledgment_sent = False

        try:
            async for event in agent.process_stream(agent_request):
                event_type = event.get("event", "")
                event_data = event.get("data", {})

                if event_type == "text_delta":
                    delta = event_data.get("delta", "")
                    if delta:
                        text_chunks.append(delta)

                        # 检测 acknowledgment（以 "---" 分隔符结尾的第一段）
                        # 如果检测到分隔符，说明 acknowledgment 已完成，可以考虑先发送
                        # 但钉钉 Stream 模式下，消息需要在回调返回后才能发送，
                        # 所以这里只做标记，后续可以扩展为分批发送
                        if not acknowledgment_sent and "---" in delta:
                            acknowledgment_sent = True
                            logger.info("[DingTalk] Acknowledgment detected in stream")

                elif event_type == "error":
                    # 不将原始错误信息暴露给用户，记录日志即可
                    error_msg = event_data.get("error", "")
                    logger.warning(f"[DingTalk] Agent error event: {error_msg}")
                    # 不添加错误到输出

            result = "".join(text_chunks)

            # 如果结果为空，返回友好提示
            if not result.strip():
                result = self._get_friendly_fallback(message)

            # 适配钉钉 Markdown 格式（表格->列表，代码块->引用）
            from app.agent.v4.markdown_adapter import adapt_markdown_for_channel
            result = adapt_markdown_for_channel(result, "dingtalk")

            # Langfuse: 结束 trace 并 flush
            try:
                if hasattr(agent_request, 'langfuse_trace') and agent_request.langfuse_trace:
                    agent_request.langfuse_trace.update(
                        output={"response": result},
                    )
                    agent_request.langfuse_trace.end()
                from app.utils.langfuse_client import langfuse
                langfuse.flush()
            except Exception:
                pass

            return result

        except Exception as e:
            logger.error(f"[DingTalk] Agent processing error: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            # Langfuse: 记录错误并结束 trace
            try:
                if hasattr(agent_request, 'langfuse_trace') and agent_request.langfuse_trace:
                    agent_request.langfuse_trace.update(
                        output={"status": "error", "error": str(e)},
                        level="ERROR",
                    )
                    agent_request.langfuse_trace.end()
                from app.utils.langfuse_client import langfuse
                langfuse.flush()
            except Exception:
                pass
            # 返回友好的错误提示，不暴露技术细节
            return self._get_friendly_error_message(message)

    def _get_friendly_fallback(self, message: str) -> str:
        """生成友好的默认回复"""
        # 根据消息内容生成合适的回复
        if any(kw in message for kw in ["大盘", "指数", "市场", "走势"]):
            return """📊 **市场分析服务**

抱歉，当前无法获取实时市场数据。您可以尝试：

1. **查询具体股票**: 发送 `@世通小助手 分析 贵州茅台`
2. **查询行业板块**: 发送 `@世通小助手 分析新能源板块`
3. **技术指标分析**: 发送 `@世通小助手 600519 技术分析`

如需帮助，请发送 `帮助` 查看完整功能列表。"""

        elif any(kw in message for kw in ["股票", "分析", "行情"]):
            return """📈 **股票分析服务**

请提供具体的股票代码或名称，例如：
- `分析 贵州茅台`
- `600519 技术分析`
- `比亚迪 财务分析`

我可以为您提供：技术指标、财务报表、估值分析、AI预测等服务。"""

        else:
            return """👋 **您好！我是世通小助手**

我可以帮您进行：
- 📊 股票分析和行情查询
- 📈 技术指标和趋势分析
- 💰 财务报表和估值分析
- 🔮 AI 智能预测
- 🌍 地缘政治和大国博弈分析

请告诉我您想了解什么？"""

    def _get_friendly_error_message(self, message: str) -> str:
        """生成友好的错误提示"""
        if any(kw in message for kw in ["大盘", "指数", "市场"]):
            return """⚠️ **服务暂时繁忙**

抱歉，当前市场数据服务暂时不可用。请稍后再试。

您也可以尝试查询具体股票：
- `分析 贵州茅台`
- `600519 行情`"""

        return """⚠️ **处理请求时遇到问题**

抱歉，您的请求处理遇到了一些问题。请稍后重试，或尝试换一种方式提问。

如需帮助，请发送 `帮助` 查看功能列表。"""


if DINGTALK_AVAILABLE:
    class DingTalkBotHandler(dingtalk_stream.ChatbotHandler):
        """
        钉钉 Stream SDK 消息处理器

        继承 ChatbotHandler，实现 process 方法处理回调

        重要设计：
        1. 使用 messageId 去重，防止钉钉重试导致重复处理
        2. 先返回 ACK，后台异步处理消息，避免超时重试
        """

        def __init__(self):
            super().__init__()
            self._handler = AgentBotHandler()

        _MSG_FILE_CACHE_PREFIX = "dingtalk:msg_file:"

        async def _cache_msg_file(self, msg_id: str, file_info: dict):
            """缓存消息附件信息（Redis 已移除，此方法为空实现）"""
            pass

        async def _lookup_msg_file(self, msg_id: str) -> dict | None:
            """从缓存回查消息附件信息（Redis 已移除，始终返回 None）"""
            return None

        async def _store_sender_info(
            self,
            session_id: str,
            sender_id: str,
            sender_nick: Optional[str],
        ):
            """存储发送者信息（Redis 已移除，此方法为空实现）"""
            pass

        async def process(self, callback: dingtalk_stream.CallbackMessage):
            """
            处理钉钉消息回调

            采用"先 ACK，后处理"模式：
            1. 立即提取消息信息
            2. 检查消息去重
            3. 立即返回 ACK（防止钉钉重试）
            4. 在后台 Task 中异步处理消息

            Args:
                callback: 钉钉回调消息

            Returns:
                (status, message) 元组
            """
            incoming = None
            try:
                # 解析消息
                incoming = dingtalk_stream.ChatbotMessage.from_dict(callback.data)

                # DEBUG: 记录原始回调数据的 key，帮助发现 quote/reply 相关字段
                raw_keys = list(callback.data.keys()) if isinstance(callback.data, dict) else []
                extensions = getattr(incoming, 'extensions', {}) or {}
                if extensions:
                    logger.debug(f"[DingTalk] Message extensions keys: {list(extensions.keys())}")
                if raw_keys:
                    logger.debug(f"[DingTalk] Raw callback data keys: {raw_keys}")

                # 提取 messageId 用于去重
                message_id = getattr(incoming, 'msgId', '') or getattr(incoming, 'msg_id', '') or ''

                # 消息去重检查
                if await _is_duplicate_message(message_id):
                    return AckMessage.STATUS_OK, 'OK'

                # 提取会话和发送者信息
                conversation_id = getattr(incoming, 'conversationId', '') or getattr(incoming, 'conversation_id', '') or 'unknown'
                sender_id = getattr(incoming, 'senderId', '') or getattr(incoming, 'sender_id', '') or 'unknown'
                sender_nick = getattr(incoming, 'senderNick', '') or getattr(incoming, 'sender_nick', '') or None
                sender_staff_id = getattr(incoming, 'sender_staff_id', '') or ''
                conversation_type = getattr(incoming, 'conversation_type', '') or ''
                robot_code = getattr(incoming, 'robot_code', '') or ''
                if not robot_code:
                    from app.config import settings
                    robot_code = settings.dingtalk_client_id or ''

                # === 构建用户隔离的 session_id ===
                # 群聊中不同用户应有独立 session，避免上下文串话
                user_key = sender_staff_id or hashlib.md5(sender_id.encode()).hexdigest()[:10]
                user_session_id = f"dingtalk_{conversation_id}_{user_key}"

                # === 多类型消息提取 ===
                message_content = ""
                attached_files = []
                msg_type = getattr(incoming, 'message_type', 'text') or 'text'

                if msg_type == "text":
                    if hasattr(incoming, 'text') and incoming.text:
                        message_content = incoming.text.content.strip() if hasattr(incoming.text, 'content') else str(incoming.text)

                elif msg_type == "picture":
                    download_codes = incoming.get_image_list() if hasattr(incoming, 'get_image_list') else []
                    for code in download_codes:
                        attached_files.append({
                            "type": "image",
                            "download_code": code,
                            "robot_code": robot_code,
                        })
                    text_parts = incoming.get_text_list() if hasattr(incoming, 'get_text_list') else []
                    message_content = " ".join(text_parts) if text_parts else "请识别这张图片的内容"
                    # 缓存 msgId → 附件信息，供引用消息回查
                    if message_id and download_codes:
                        for code in download_codes:
                            await self._cache_msg_file(message_id, {
                                "type": "image", "download_code": code, "robot_code": robot_code,
                            })

                elif msg_type == "richText":
                    text_parts = incoming.get_text_list() if hasattr(incoming, 'get_text_list') else []
                    message_content = " ".join(text_parts)
                    # 优先从 SDK get_image_list() 取
                    image_codes = incoming.get_image_list() if hasattr(incoming, 'get_image_list') else []
                    for code in image_codes:
                        attached_files.append({
                            "type": "image",
                            "download_code": code,
                            "robot_code": robot_code,
                        })
                    # SDK get_image_list() 可能返回空（extensions.content.richText 格式）
                    # 直接从 extensions 解析图片 downloadCode 兜底
                    if not image_codes:
                        _ext = getattr(incoming, 'extensions', {}) or {}
                        _rich = _ext.get('content', {}).get('richText', []) if isinstance(_ext, dict) else []
                        for item in _rich:
                            if isinstance(item, dict) and item.get('type') == 'picture':
                                dc = item.get('downloadCode', '')
                                if dc:
                                    attached_files.append({
                                        "type": "image",
                                        "download_code": dc,
                                        "robot_code": robot_code,
                                    })
                                    logger.info(f"[DingTalk] richText image extracted from extensions: dc={dc[:20]}...")

                elif msg_type == "file":
                    extensions = getattr(incoming, 'extensions', {}) or {}
                    content_info = extensions.get('content', {}) if isinstance(extensions, dict) else {}
                    download_code = content_info.get('downloadCode', '')
                    file_name = content_info.get('fileName', '未知文件')
                    if download_code:
                        attached_files.append({
                            "type": "file",
                            "download_code": download_code,
                            "robot_code": robot_code,
                            "file_name": file_name,
                        })
                        # 缓存 msgId → 附件信息，供引用消息回查
                        await self._cache_msg_file(message_id, {
                            "type": "file", "download_code": download_code,
                            "robot_code": robot_code, "file_name": file_name,
                        })
                    message_content = f"请读取并分析文件: {file_name}"

                elif msg_type == "link":
                    extensions = getattr(incoming, 'extensions', {}) or {}
                    content_info = extensions.get('content', {}) if isinstance(extensions, dict) else {}
                    link_title = content_info.get('title', '')
                    link_text = content_info.get('text', '')
                    link_url = content_info.get('messageUrl', '') or content_info.get('url', '')
                    link_pic_url = content_info.get('picUrl', '')
                    parts = []
                    if link_title:
                        parts.append(f"链接标题: {link_title}")
                    if link_text:
                        parts.append(f"链接描述: {link_text}")
                    if link_url:
                        parts.append(f"链接地址: {link_url}")
                    if link_pic_url:
                        parts.append(f"链接图片: {link_pic_url}")
                    message_content = "\n".join(parts) if parts else "收到链接消息，未能解析"
                    logger.info(f"[DingTalk] Link message: title={link_title}, url={link_url}")

                elif msg_type == "video":
                    extensions = getattr(incoming, 'extensions', {}) or {}
                    content_info = extensions.get('content', {}) if isinstance(extensions, dict) else {}
                    download_code = content_info.get('downloadCode', '')
                    video_type = content_info.get('videoType', '')
                    duration = content_info.get('duration', '')
                    if download_code:
                        attached_files.append({
                            "type": "video",
                            "download_code": download_code,
                            "robot_code": robot_code,
                        })
                        # 缓存 msgId → 附件信息，供引用消息回查
                        await self._cache_msg_file(message_id, {
                            "type": "video", "download_code": download_code, "robot_code": robot_code,
                        })
                    message_content = f"请分析这个视频（时长: {duration}s）" if duration else "请分析这个视频"
                    logger.info(f"[DingTalk] Video message: videoType={video_type}, duration={duration}")

                elif msg_type == "audio":
                    extensions = getattr(incoming, 'extensions', {}) or {}
                    audio_content = extensions.get('content', {}) if isinstance(extensions, dict) else {}
                    recognition = audio_content.get('recognition', '')
                    message_content = recognition or "收到语音消息，未能识别"

                else:
                    # 未知类型，尝试提取文本
                    if hasattr(incoming, 'text') and incoming.text:
                        message_content = incoming.text.content.strip() if hasattr(incoming.text, 'content') else str(incoming.text)

                # === 提取引用消息内容 ===
                quote_extensions = getattr(incoming, 'extensions', {}) or {}
                if isinstance(quote_extensions, dict):
                    # 方式1: quoteContent 字段（部分场景）
                    quote_text = (
                        quote_extensions.get('quoteContent', '')
                        or quote_extensions.get('quote_content', '')
                        or ''
                    ).strip()

                    # 方式2: extensions.text.repliedMsg（引用回复场景）
                    ext_text = quote_extensions.get('text', {}) or {}
                    if isinstance(ext_text, dict) and ext_text.get('isReplyMsg'):
                        replied_msg = ext_text.get('repliedMsg', {}) or {}
                        replied_type = replied_msg.get('msgType', '')
                        replied_content = replied_msg.get('content', {}) or {}

                        if replied_type == 'picture' and isinstance(replied_content, dict):
                            download_code = replied_content.get('downloadCode', '')
                            if download_code:
                                attached_files.append({
                                    "type": "image",
                                    "download_code": download_code,
                                    "robot_code": robot_code,
                                })
                                logger.info(f"[DingTalk] Quoted image detected, downloadCode added to attached_files")
                        elif replied_type == 'file' and isinstance(replied_content, dict):
                            download_code = replied_content.get('downloadCode', '')
                            file_name = replied_content.get('fileName', '未知文件')
                            if download_code:
                                attached_files.append({
                                    "type": "file",
                                    "download_code": download_code,
                                    "robot_code": robot_code,
                                    "file_name": file_name,
                                })
                                logger.info(f"[DingTalk] Quoted file detected: {file_name}, downloadCode added to attached_files")
                        elif replied_type == 'text' and not quote_text:
                            # 被引用的是文本消息，提取文本内容
                            # 钉钉返回格式可能为 str 或 dict（{"text":"..."} 或 {"content":"..."}）
                            if isinstance(replied_content, str):
                                quote_text = replied_content.strip()
                            elif isinstance(replied_content, dict):
                                quote_text = (
                                    replied_content.get('text', '')
                                    or replied_content.get('content', '')
                                    or ''
                                ).strip()
                        elif replied_type == 'link' and isinstance(replied_content, dict):
                            # 引用的是链接消息，提取标题、描述、URL
                            link_title = replied_content.get('title', '')
                            link_text_content = replied_content.get('text', '')
                            link_url = replied_content.get('messageUrl', '') or replied_content.get('url', '')
                            link_parts = []
                            if link_title:
                                link_parts.append(f"链接标题: {link_title}")
                            if link_text_content:
                                link_parts.append(f"链接描述: {link_text_content}")
                            if link_url:
                                link_parts.append(f"链接地址: {link_url}")
                            if link_parts and not quote_text:
                                quote_text = "\n".join(link_parts)
                            logger.info(f"[DingTalk] Quoted link detected: title={link_title}, url={link_url}")
                        elif replied_type == 'richText' and isinstance(replied_content, dict):
                            # 引用的是富文本消息，提取文本和图片
                            rich_text_list = replied_content.get('richTextList', []) or []
                            rich_texts = []
                            for rt_item in rich_text_list:
                                if not isinstance(rt_item, dict):
                                    continue
                                rt_text = rt_item.get('text', '')
                                rt_download_code = rt_item.get('downloadCode', '')
                                if rt_text:
                                    rich_texts.append(rt_text)
                                if rt_download_code:
                                    attached_files.append({
                                        "type": "image",
                                        "download_code": rt_download_code,
                                        "robot_code": robot_code,
                                    })
                            if rich_texts and not quote_text:
                                quote_text = " ".join(rich_texts)
                            logger.info(f"[DingTalk] Quoted richText detected: texts={len(rich_texts)}, images={sum(1 for rt in rich_text_list if isinstance(rt, dict) and rt.get('downloadCode'))}")
                        elif replied_type == 'video' and isinstance(replied_content, dict):
                            download_code = replied_content.get('downloadCode', '')
                            if download_code:
                                attached_files.append({
                                    "type": "video",
                                    "download_code": download_code,
                                    "robot_code": robot_code,
                                })
                                logger.info(f"[DingTalk] Quoted video detected, downloadCode added to attached_files")
                            elif not quote_text:
                                quote_text = "[引用的视频无法通过引用获取，请直接发送给我]"
                        elif replied_type == 'interactiveCard':
                            # 引用的是 AI 反馈卡片（Markdown 卡片），钉钉不提供卡片内容
                            replied_msg_id = replied_msg.get('msgId', '')
                            if not quote_text:
                                quote_text = "[引用了之前的分析回答]"
                            logger.info(
                                f"[DingTalk] Quoted interactiveCard detected: "
                                f"msgId={replied_msg_id[:20]}..."
                            )
                        elif replied_type in ('unknownMsgType', ''):
                            # 钉钉对部分消息类型（PDF/文档等）返回 unknownMsgType，
                            # 不提供 downloadCode，尝试从 content 中提取
                            if isinstance(replied_content, dict):
                                download_code = replied_content.get('downloadCode', '')
                                file_name = replied_content.get('fileName', '未知文件')
                                if download_code:
                                    attached_files.append({
                                        "type": "file",
                                        "download_code": download_code,
                                        "robot_code": robot_code,
                                        "file_name": file_name,
                                    })
                                    logger.info(f"[DingTalk] Quoted unknownMsgType with downloadCode: {file_name}")
                                else:
                                    replied_msg_id = replied_msg.get('msgId', '')
                                    logger.info(f"[DingTalk] Quoted unknownMsgType but no cache available: msgId={replied_msg_id[:20]}...")
                                    if not quote_text:
                                        quote_text = "[引用的文件/文档无法通过引用获取，请直接发送文件给我]"

                    if quote_text:
                        logger.info(f"[DingTalk] Quote message detected: '{quote_text[:80]}...'")
                        if message_content:
                            message_content = f"[引用消息内容]\n{quote_text}\n[/引用消息内容]\n\n{message_content}"
                        else:
                            # 用户只引用了消息但没输入新文字，视为跟进上一轮对话
                            message_content = f"[引用消息内容]\n{quote_text}\n[/引用消息内容]\n\n请继续"

                # === 拼接附件调用指令（由渠道层负责，native_agent 无需感知钉钉参数）===
                if attached_files:
                    directives = []
                    for i, af in enumerate(attached_files):
                        dc = af.get("download_code", "")
                        rc = af.get("robot_code", "")
                        ftype = af.get("type", "file")
                        fname = af.get("file_name", "")
                        if not dc:
                            continue
                        name_hint = f"（文件名: {fname}）" if fname else ""
                        directives.append(
                            f"- 附件{i}{name_hint}: 类型={ftype}，"
                            f"dingtalk_download_code={dc}，"
                            f"dingtalk_robot_code={rc}"
                        )
                    if directives:
                        message_content = (
                            message_content
                            + "\n\n【附件读取指令 — 必须执行】\n"
                            "用户发送了以下附件，你必须立即调用 document_reader 工具读取其内容，"
                            "然后根据内容回答用户的问题。\n"
                            "调用参数：使用下方提供的 dingtalk_download_code 和 dingtalk_robot_code，"
                            "不要使用 file_path 或 file_url 参数。\n"
                            + "\n".join(directives)
                        )

                if not message_content:
                    # 打印 extensions 用于诊断引用消息为何未被处理
                    _ext_debug = getattr(incoming, 'extensions', None)
                    logger.warning(
                        f"[DingTalk] Empty message received (msg_type={msg_type}), "
                        f"extensions={_ext_debug}, skipping"
                    )
                    return AckMessage.STATUS_OK, 'OK'

                logger.info(
                    f"[DingTalk] Received message (type={msg_type}): '{message_content[:50]}...' "
                    f"from {sender_nick or sender_id} (msgId={message_id[:16]}..., staff_id={sender_staff_id[:10] if sender_staff_id else 'NONE'}, "
                    f"conv_type={conversation_type}, files={len(attached_files)})"
                )

                # DEBUG: dump incoming 全部属性，用于排查 oToMessages userId 格式
                try:
                    _incoming_dict = incoming.to_dict() if hasattr(incoming, 'to_dict') else {}
                    logger.info(f"[DingTalk-DEBUG] incoming.to_dict keys: {list(_incoming_dict.keys())}")
                    logger.info(f"[DingTalk-DEBUG] senderId={_incoming_dict.get('senderId')}")
                    logger.info(f"[DingTalk-DEBUG] senderStaffId={_incoming_dict.get('senderStaffId')}")
                    logger.info(f"[DingTalk-DEBUG] chatbotUserId={_incoming_dict.get('chatbotUserId')}")
                    logger.info(f"[DingTalk-DEBUG] senderCorpId={_incoming_dict.get('senderCorpId')}")
                    logger.info(f"[DingTalk-DEBUG] conversationId={_incoming_dict.get('conversationId')}")
                    logger.info(f"[DingTalk-DEBUG] conversationType={_incoming_dict.get('conversationType')}")
                    logger.info(f"[DingTalk-DEBUG] robotCode={_incoming_dict.get('robotCode')}")
                    # 打印 extensions 中是否有 userId
                    if hasattr(incoming, 'extensions'):
                        logger.info(f"[DingTalk-DEBUG] extensions={incoming.extensions}")
                except Exception as _de:
                    logger.warning(f"[DingTalk-DEBUG] dump failed: {_de}")

                await self._store_sender_info(
                    session_id=user_session_id,
                    sender_id=sender_id,
                    sender_nick=sender_nick,
                )

                # 在后台异步处理消息（不阻塞 ACK 返回）
                from app.utils.background_task_manager import create_background_task
                create_background_task(
                    self._process_message_async(
                        incoming=incoming,
                        message_content=message_content,
                        conversation_id=conversation_id,
                        sender_id=sender_id,
                        sender_nick=sender_nick,
                        conversation_type=conversation_type,
                        robot_code=robot_code,
                        user_session_id=user_session_id,
                        sender_staff_id=sender_staff_id,
                        attached_files=attached_files,
                    ),
                    task_name="process_dingtalk_message"
                )

                # 立即返回 ACK，避免钉钉重试
                return AckMessage.STATUS_OK, 'OK'

            except Exception as e:
                logger.error(f"[DingTalk] Error parsing message: {e}")
                import traceback
                logger.debug(traceback.format_exc())
                return AckMessage.STATUS_OK, 'OK'

        async def _process_message_async(
            self,
            incoming,
            message_content: str,
            conversation_id: str,
            sender_id: str,
            sender_nick: Optional[str],
            conversation_type: str = "",
            robot_code: str = "",
            user_session_id: str = "",
            sender_staff_id: str = "",
            extra_params: Optional[dict] = None,
            attached_files: Optional[list] = None,
        ):
            """
            后台异步处理消息

            在独立的 Task 中运行，不影响 ACK 返回

            实现"立即通知"：
            1. 检测到 acknowledgment 后立即发送第一条消息（告知用户任务已收到）
            2. 任务完成后发送最终结果
            """
            try:
                # === 检查是否有待补充的反馈（用户点击按钮后回复的补充内容） ===
                feedback_handled = await self._check_pending_feedback(
                    incoming, message_content, sender_id, sender_nick, conversation_id,
                )
                if feedback_handled:
                    return

                agent = self._handler._get_agent()
                from app.agent.v4.native_agent import V4AgentRequest, CHANNEL_DINGTALK

                params = {
                    "auto_approve_plan": True,
                    "dingtalk_sender": sender_nick or sender_id,
                    "dingtalk_sender_id": sender_id,
                    "dingtalk_staff_id": sender_staff_id,
                    "dingtalk_conversation_id": conversation_id,
                    "dingtalk_conversation_type": conversation_type,
                    "dingtalk_robot_code": robot_code,
                }
                if extra_params:
                    params.update(extra_params)

                from app.channels.dingtalk.utils import generate_dingtalk_user_id
                try:
                    dingtalk_user_id = generate_dingtalk_user_id(sender_id)
                except ValueError:
                    logger.warning(f"[DingTalk] Empty sender_id in async handler, rejecting message")
                    return

                agent_request = V4AgentRequest(
                    message=message_content,
                    session_id=user_session_id or f"dingtalk_{conversation_id}",
                    user_id=dingtalk_user_id,
                    params=params,
                    output_format="markdown",
                    render_mode="text_only",
                    channel=CHANNEL_DINGTALK,  # 钉钉渠道，确保报告链接生成
                    attached_files=attached_files or [],
                    callbacks=None,
                )

                # Langfuse: 创建 Trace（钉钉渠道）
                try:
                    from app.utils.langfuse_client import langfuse
                    lf_trace = langfuse.trace(
                        name="dingtalk_chat",
                        user_id=str(dingtalk_user_id),
                        session_id=user_session_id or f"dingtalk_{conversation_id}",
                        input={
                            "message": message_content,
                            "sender": sender_nick or sender_id,
                            "conversation_type": conversation_type,
                        },
                        metadata={
                            "channel": "dingtalk",
                            "conversation_id": conversation_id,
                            "robot_code": robot_code,
                        },
                    )
                    agent_request.langfuse_trace = lf_trace
                except Exception as lf_err:
                    logger.debug(f"[Langfuse] DingTalk trace creation skipped: {lf_err}")

                # 流式处理（Phase 0 会输出 acknowledgment 作为第一段 text_delta，以 --- 分隔）
                # 不再提前发送硬编码的确认消息，等 Phase 0 的 acknowledgment 到达后再发
                text_chunks = []
                component_events = []  # 收集待截图的组件事件
                scene_tab_events = []  # 收集场景 Tab 事件
                scene_update_events = []  # 收集场景更新事件
                has_error = False  # 标记是否遇到错误
                report_id = None  # 捕获报告 ID（用于反馈链接）
                report_urls = {}   # 报告下载链接
                report_png_md = ""  # 报告 PNG 图片 markdown（全图片模式）
                confidence_event_data = None  # 捕获置信度完整数据
                _report_lang = "zh"  # 报告语种（从 confidence 事件获取）
                # cron 模式下 Phase 0 不输出 ack+---，直接标记为已发送，跳过 ack 检测
                is_cron = bool(extra_params and extra_params.get("is_cron"))
                ack_sent = is_cron  # cron=True 则跳过 ack 检测
                ack_buffer = []   # 缓冲 Phase 0 的 acknowledgment 文本
                _ack_text_original = ""  # 记录从 LLM 文本提取的原始 ack 文字（未经 markdown 适配）
                _send_message_count = 0  # 记录 send_message skill 发送次数（cron 模式去重用）
                if is_cron:
                    logger.info("[DingTalk] Cron mode: ack detection disabled")

                from app.utils.markdown_utils import truncate_markdown_safe as _truncate_markdown_safe
                from app.utils.markdown_utils import contains_api_error as _contains_api_error

                _has_attached_files = bool(attached_files)

                async for event in agent.process_stream(agent_request):
                    event_type = event.get("event", "")
                    event_data = event.get("data", {})

                    if event_type == "text_delta":
                        delta = event_data.get("delta", "")
                        if delta:
                            if not ack_sent:
                                # Phase 1 已去掉，LLM 的第一段文字就是确认语，直接发出
                                ack_buffer.append(delta)
                                ack_text = "".join(ack_buffer).strip()
                                # 遇到句末标点立即发出，或积累到50字兜底，避免截断半句话
                                _sentence_end_chars = ("。", "！", "？", "…", ".", "!", "?")
                                _cut_pos = -1
                                for _i, _c in enumerate(ack_text):
                                    if _c in _sentence_end_chars:
                                        _cut_pos = _i
                                        break
                                if _cut_pos >= 0:
                                    # 截断到第一个句末标点（含），把后面内容退回正文
                                    _tail = ack_text[_cut_pos + 1:]
                                    ack_text = ack_text[:_cut_pos + 1]
                                    if _tail:
                                        text_chunks.append(_tail)
                                _sentence_end = _cut_pos >= 0
                                if ack_text and (_sentence_end or len(ack_text) >= 50):
                                    # 保存原始 ack 文字，供 result 为空时兜底使用
                                    _ack_text_original = ack_text
                                    # 附件场景防护
                                    if _has_attached_files and len(ack_text) > 80:
                                        first_line = ack_text.split("\n")[0].strip()
                                        ack_text = first_line if len(first_line) <= 60 else "收到，正在为您处理附件内容。"
                                    from app.agent.v4.markdown_adapter import adapt_markdown_for_channel
                                    ack_text = adapt_markdown_for_channel(ack_text, "dingtalk")
                                    ack_with_at = f"@{sender_nick} {ack_text}" if sender_nick else ack_text
                                    await self._send_sample_markdown(
                                        content=ack_with_at,
                                        title="Processing" if _report_lang == "en" else "处理中",
                                        conversation_id=conversation_id,
                                        conversation_type=conversation_type,
                                        sender_staff_id=sender_staff_id,
                                        incoming_message=incoming,
                                    )
                                    logger.info(f"[DingTalk] LLM ack sent: {ack_text[:80]}")
                                    ack_sent = True
                                    ack_buffer.clear()
                            else:
                                # ack 已发送，正常收集正文
                                text_chunks.append(delta)

                    elif event_type == "status":
                        # 收到进度状态，发送占位卡片但不标记 ack_sent
                        # 等 LLM 输出确认语 + --- 后，再发送个性化确认语
                        if not ack_sent:
                            status_msg = event_data.get("message") or event_data.get("text") or "正在处理中..."
                            from app.agent.v4.markdown_adapter import adapt_markdown_for_channel
                            status_msg = adapt_markdown_for_channel(status_msg, "dingtalk")
                            ack_with_at = f"@{sender_nick} {status_msg}" if sender_nick else status_msg
                            await self._send_sample_markdown(
                                content=ack_with_at,
                                title="Processing" if _report_lang == "en" else "处理中",
                                conversation_id=conversation_id,
                                conversation_type=conversation_type,
                                sender_staff_id=sender_staff_id,
                                incoming_message=incoming,
                            )
                            logger.info(f"[DingTalk] Status placeholder sent: {status_msg[:60]}")

                    elif event_type == "text_clear":
                        reason = event_data.get("reason", "unknown")
                        if reason == "phase2_handoff" and not ack_sent:
                            # Phase 1 → Phase 2 切换：将已累积文本作为个性化确认语发送
                            ack_text = "".join(ack_buffer + text_chunks).strip()
                            # 附件场景防护：Phase 1 不应分析附件内容，截断过长 ack
                            if _has_attached_files and len(ack_text) > 80:
                                first_line = ack_text.split("\n")[0].strip()
                                ack_text = first_line if len(first_line) <= 60 else "收到，正在为您处理附件内容。"
                                logger.info(f"[DingTalk] Truncated phase2_handoff ack for attached file: {ack_text}")
                            if ack_text:
                                from app.agent.v4.markdown_adapter import adapt_markdown_for_channel
                                ack_text = adapt_markdown_for_channel(ack_text, "dingtalk")
                                ack_with_at = f"@{sender_nick} {ack_text}" if sender_nick else ack_text
                                self.reply_markdown_card(
                                    markdown=ack_with_at,
                                    incoming_message=incoming,
                                    title="Processing" if _report_lang == "en" else "处理中",
                                    at_sender=True,
                                )
                                logger.info(f"[DingTalk] Phase1 ack sent: {ack_text[:80]}...")
                            ack_sent = True
                        else:
                            # Policy error 或 content regeneration 时，确保不再缓冲 ack
                            ack_sent = True
                        text_chunks.clear()
                        ack_buffer.clear()
                        logger.info(f"[DingTalk] text_clear: cleared (reason={reason})")

                    elif event_type == "report_ready":
                        # 捕获报告 ID，用于在最终结果末尾追加反馈链接
                        report_id = event_data.get("report_id", "")
                        report_urls = event_data.get("urls", {})
                        public_urls = event_data.get("public_urls", {})
                        logger.info(f"[DingTalk] Report ready captured: {report_id}")

                        # 捕获报告 PNG 图片（钉钉 media_id），嵌入消息中展示
                        png_media_id = public_urls.get("png", "")
                        if png_media_id:
                            report_png_md = f"\n\n![报告预览]({png_media_id})"
                            logger.info(f"[DingTalk] Report PNG captured: {png_media_id[:60]}...")

                    elif event_type == "confidence":
                        # 捕获置信度完整数据，用于在最终消息中展示精简格式
                        try:
                            confidence_event_data = event_data
                            _report_lang = event_data.get("report_lang", "zh")
                        except Exception:
                            pass
                        logger.info(f"[DingTalk] Confidence event captured")

                    elif event_type == "component_for_render":
                        # 收集待截图的组件事件
                        component_events.append(event_data)
                        logger.info(
                            f"[DingTalk] Collected component_for_render: "
                            f"{event_data.get('component', 'unknown')}"
                        )

                    elif event_type == "scene_tab":
                        scene_tab_events.append(event_data)
                        logger.info(f"[DingTalk] Scene tab event: {event_data.get('scene_type', 'unknown')}")

                    elif event_type == "scene_update":
                        scene_update_events.append(event_data)
                        logger.info(f"[DingTalk] Scene update event: {event_data.get('skill_name', 'unknown')}")

                    elif event_type == "send_message":
                        # LLM 主动发送的中间消息（通过 send_message MCP Tool 触发）
                        _sm_content = event_data.get("content", "")
                        _sm_type = event_data.get("msg_type", "text")
                        _sm_title = event_data.get("title", "分析进展")
                        # EventBridge 事件中可能携带会话类型（用于区分群聊/单聊）
                        _sm_conv_type = event_data.get("conversation_type", "") or conversation_type
                        _sm_staff_id = event_data.get("sender_staff_id", "") or sender_staff_id

                        if _sm_content:
                            try:
                                if _sm_type == "text":
                                    from app.agent.v4.markdown_adapter import adapt_markdown_for_channel
                                    _sm_adapted = adapt_markdown_for_channel(_sm_content, "dingtalk")
                                    _sm_with_at = f"@{sender_nick} {_sm_adapted}" if sender_nick else _sm_adapted
                                    await self._send_sample_markdown(
                                        content=_sm_with_at,
                                        title=_sm_title,
                                        conversation_id=conversation_id,
                                        conversation_type=_sm_conv_type,
                                        sender_staff_id=_sm_staff_id,
                                        incoming_message=incoming,
                                    )
                                    _send_message_count += 1
                                    logger.info(f"[DingTalk] send_message dispatched: text ({len(_sm_content)} chars)")

                                elif _sm_type == "image":
                                    await self._send_image_message(
                                        image_url=_sm_content,
                                        conversation_id=conversation_id,
                                        robot_code=robot_code,
                                        conversation_type=_sm_conv_type,
                                        sender_staff_id=_sm_staff_id,
                                    )
                                    logger.info(f"[DingTalk] send_message dispatched: image")

                                elif _sm_type == "file":
                                    await self._send_file_via_url(
                                        file_url=_sm_content,
                                        conversation_id=conversation_id,
                                        robot_code=robot_code,
                                        conversation_type=_sm_conv_type,
                                        sender_staff_id=_sm_staff_id,
                                    )
                                    logger.info(f"[DingTalk] send_message dispatched: file")

                            except Exception as _sm_err:
                                logger.warning(f"[DingTalk] send_message failed: {_sm_err}")

                        # 中间消息视为 ack 的超集
                        if not ack_sent:
                            ack_sent = True

                    elif event_type == "error":
                        error_msg = event_data.get("error", "")
                        has_error = True
                        logger.warning(f"[DingTalk] Agent error event: {error_msg}")

                # 如果 Phase 0 没有输出 ack（没遇到 --- 分隔符），
                # 把缓冲内容合并到正文，并补发一条默认确认
                if not ack_sent and ack_buffer:
                    text_chunks = ack_buffer + text_chunks

                # 处理完成，发送最终结果
                result = "".join(text_chunks)
                # 兜底：如果正文为空，但 LLM 第一句话曾被发送为 ack，则把 ack 原文作为正文
                # 场景：LLM 回答极短（整个回答只有一句），被全部消耗为 ack，导致 text_chunks 为空
                if not result.strip() and _ack_text_original:
                    result = _ack_text_original
                    logger.info(f"[DingTalk] Fallback: ack_text used as result ({len(result)} chars)")
                if not result.strip():
                    result = "⚠️ 抱歉，当前请求未能获得有效响应，请稍后重试或换个方式提问。"
                    logger.warning(f"[DingTalk] Empty result fallback: sending user-friendly error message")
                logger.info(f"[DingTalk] Assembled result: {len(result)} chars, has_error={has_error}")

                # 兼容清理：移除 LLM 可能残留的旧版魔法标记（不再使用）
                import re as _re_bot
                _report_name = ""
                _rn_match = _re_bot.search(r'\[REPORT_NAME:([a-z][a-z0-9_]*)\]', result or "")
                if _rn_match:
                    _report_name = _rn_match.group(1)
                    result = result.replace(_rn_match.group(0), "").rstrip()
                if "[SEND_REPORT_FILE]" in (result or ""):
                    result = result.replace("[SEND_REPORT_FILE]", "").rstrip()

                # 如果整体内容包含 API 错误，过滤掉错误信息，只保留友好提示
                is_api_error = _contains_api_error(result)
                if is_api_error:
                    logger.info(f"[DingTalk] API error detected in short response ({len(result)} chars): {result[:200]}")
                if has_error or is_api_error:
                    # 查找友好提示部分（以 ⚠️ 开头的行）
                    friendly_lines = []
                    for line in result.split('\n'):
                        line_stripped = line.strip()
                        # 保留友好提示和分隔符
                        if line_stripped.startswith('⚠️') or line_stripped == '---':
                            friendly_lines.append(line)

                    if friendly_lines:
                        final_result = '\n'.join(friendly_lines)
                        logger.info(f"[DingTalk] Filtered API error, showing friendly message")
                    else:
                        # 没有找到友好提示，使用默认错误消息
                        final_result = "⚠️ 抱歉，遇到了一些问题，请稍后重试或换个问法。"
                        logger.warning(f"[DingTalk] No friendly message found, using default error message")

                    # 即使文本流有 API 错误，报告可能已成功生成（工具执行正常、仅 LLM 回复含错误文本）
                    # 此时仍应把报告 PNG 预览和下载链接追加给用户
                    if report_id:
                        if report_png_md:
                            final_result = final_result.rstrip() + report_png_md
                        # 从原始 result 中提取报告下载区
                        _sep = "\n---\n"
                        _sep_idx = result.rfind(_sep)
                        _sep_tail = result[_sep_idx:] if _sep_idx >= 0 else ""
                        if _sep_idx >= 0 and ("**报告下载**" in _sep_tail or "**Report Downloads**" in _sep_tail):
                            final_result += result[_sep_idx:]
                        elif report_urls:
                            # 原始文本中无下载区，用 report_urls 构建
                            _dl_header = "Report Downloads" if _report_lang == "en" else "报告下载"
                            _dl_parts = [f"\n\n---\n**{_dl_header}**\n"]
                            for _fmt, _url in report_urls.items():
                                if _url:
                                    _dl_parts.append(f"- [{_fmt.upper()}]({_url})")
                            if len(_dl_parts) > 1:
                                final_result += "\n".join(_dl_parts)
                        logger.info(f"[DingTalk] Appended report links despite API error (report_id={report_id})")
                else:

                    # 有 REPORT_NAME 时：发送 LLM 精简摘要 + 报告下载链接
                    # 没有 REPORT_NAME 时直接用原文（避免简单回答被不必要地压缩）
                    if _report_name:
                        from app.agent.v4.llm_helpers import summarize_for_dingtalk
                        summary = await summarize_for_dingtalk(
                            full_analysis=result,
                            user_query=message_content,
                            report_lang=_report_lang,
                        )
                        final_result = summary or result
                        logger.info(
                            f"[DingTalk] Summary mode: {len(result)} -> {len(final_result)} chars"
                        )
                        # 摘要末尾追加报告下载链接
                        if report_urls:
                            _dl_header = "Report Downloads" if _report_lang == "en" else "报告下载"
                            _dl_parts = [f"\n\n---\n**{_dl_header}**\n"]
                            for _fmt, _url in report_urls.items():
                                if _url:
                                    _dl_parts.append(f"- [{_fmt.upper()}]({_url})")
                            if len(_dl_parts) > 1:
                                final_result += "\n".join(_dl_parts)
                    else:
                        final_result = result

                    from app.agent.v4.markdown_adapter import adapt_markdown_for_channel
                    final_result = adapt_markdown_for_channel(final_result, "dingtalk")

                # [Disabled] 不再生成 MinIO 托管的“查看完整分析页面”入口

                # 追加置信度信息（钉钉渠道使用精简单行格式，不用 <details> 折叠）
                if confidence_event_data:
                    eval_method = confidence_event_data.get("evaluation_method", "")
                    percent = confidence_event_data.get("percent", "")
                    level = confidence_event_data.get("level", "")
                    recommendation = confidence_event_data.get("recommendation", "")
                    _is_en = _report_lang == "en"
                    _invalid_label = "Invalid" if _is_en else "无效"

                    if eval_method in ("error_detected", "no_data_detected", "no_data"):
                        # 错误/无数据场景：显示警告提示
                        if recommendation:
                            compact = f"\n\n---\n> ⚠️ {recommendation}"
                            final_result = final_result.rstrip() + compact
                    elif percent and level and level != _invalid_label:
                        # 正常场景：显示置信度
                        _overall_label = "Overall Confidence" if _is_en else "综合置信度"
                        compact = f"\n\n---\n**{_overall_label}**: {percent} ({level})"
                        if recommendation:
                            compact += f"\n> {recommendation}"
                        final_result = final_result.rstrip() + compact

                # 适配钉钉 Markdown 格式
                from app.agent.v4.markdown_adapter import adapt_markdown_for_channel
                final_result = adapt_markdown_for_channel(final_result, "dingtalk")

                # cron 模式下若已通过 send_message skill 发过内容，跳过 Final result 避免重复发送
                if is_cron and _send_message_count > 0:
                    logger.info(f"[DingTalk] Cron mode: skipping final result (send_message already sent {_send_message_count} time(s))")
                    final_result = ""

                # 发送最终结果（通过 channel 架构，使用 sampleMarkdown 获取完整 Markdown 渲染）
                if final_result.strip():
                    final_with_at = f"@{sender_nick} {final_result}" if sender_nick else final_result

                    # 追加反馈 URL 链接
                    _feedback_id = report_id or agent_request.session_id
                    try:
                        feedback_url = self._build_feedback_url(_feedback_id)
                        final_with_at = final_with_at.rstrip() + f"\n\n[👍 有帮助]({feedback_url}&rating=like)　[👎 需改进]({feedback_url}&rating=dislike)"
                    except Exception:
                        pass

                    # 通过 channel plugin 发送（sampleMarkdown）
                    try:
                        from app.channels.manager import get_channel_manager
                        from app.channels.types import ReplyPayload
                        _plugin = get_channel_manager().get_plugin("dingtalk")
                        if _plugin:
                            _payload = ReplyPayload(
                                markdown=final_with_at,
                                metadata={
                                    "title": "分析结果",
                                    "conversation_type": conversation_type,
                                    "sender_staff_id": sender_staff_id,
                                },
                            )
                            await _plugin.send_message(conversation_id, _payload)
                        else:
                            raise RuntimeError("DingTalk plugin not available")
                    except Exception as _err:
                        logger.warning(f"[DingTalk] Channel send failed, fallback: {_err}")
                        self.reply_markdown_card(
                            markdown=final_with_at,
                            incoming_message=incoming,
                            title="分析结果",
                            at_sender=True,
                        )

                    logger.info(f"[DingTalk] Final result sent ({len(final_result)} chars) with @{sender_nick}")


                # Langfuse: 结束 trace 并 flush
                try:
                    if hasattr(agent_request, 'langfuse_trace') and agent_request.langfuse_trace:
                        agent_request.langfuse_trace.update(
                            output={"response": final_result, "has_report": bool(report_id)},
                        )
                        agent_request.langfuse_trace.end()
                    from app.utils.langfuse_client import langfuse
                    langfuse.flush()
                except Exception:
                    pass

            except Exception as e:
                logger.error(f"[DingTalk] Error processing message: {e}")
                import traceback
                logger.debug(traceback.format_exc())

                # 尝试回复错误信息
                try:
                    error_msg = self._handler._get_friendly_error_message(message_content)
                    self.reply_markdown_card(
                        markdown=error_msg,
                        incoming_message=incoming,
                        title="处理结果",
                        at_sender=True  # 自动 @ 提问者
                    )
                except Exception:
                    pass

        @staticmethod
        def _build_feedback_url(report_id: str) -> str:
            """构建报告反馈页面 URL"""
            from app.config import settings
            public_base = getattr(settings, 'public_base_url', '') or ''
            if not public_base:
                ext_host = getattr(settings, 'agent_external_host', '') or ''
                ext_port = getattr(settings, 'agent_service_port', '') or getattr(settings, 'port', 8000)
                if ext_host:
                    public_base = f"http://{ext_host}:{ext_port}"
                else:
                    public_base = f"http://{getattr(settings, 'host', '0.0.0.0')}:{getattr(settings, 'port', 8000)}"
            return f"{public_base}/api/v1/chat/v4/report-feedback-page/{report_id}?channel=dingtalk"

        async def _save_card_report_mapping(
            self, card_instance_id: str, report_id: str, markdown: str = "",
            conversation_id: str = "", conversation_type: str = "", sender_staff_id: str = "",
        ):
            """保存卡片实例 ID 到报告 ID 的映射（Redis 已移除，此方法为空实现）"""
            pass

        @staticmethod
        def _is_previous_report_image_request(message: str) -> bool:
            """判断用户是否想要把上一轮报告转成图片发回。"""
            if not message:
                return False

            import re as _re_intent

            normalized = _re_intent.sub(r"\s+", "", message).lower()
            if not normalized:
                return False

            image_keywords = ("图片报告", "报告图片", "报告截图", "发送png", "发png", "截图给我", "转成图片", "转图片", "html截图")
            context_keywords = ("上次", "上一轮", "上一条", "刚才", "刚刚", "这个", "那份", "那条", "问答", "报告", "html")

            has_image = any(k in normalized for k in image_keywords)
            has_context = any(k in normalized for k in context_keywords)
            return has_image and has_context

        @staticmethod
        def _extract_report_html_urls_from_text(content: str) -> list[str]:
            """从历史 assistant 文本中提取报告 HTML 链接。"""
            if not content or ".html" not in content:
                return []

            import re as _re_html

            candidates = []
            patterns = [
                r'\((https?://[^\s)]+\.html(?:\?[^\s)]*)?)\)',
                r'(https?://[^\s]+\.html(?:\?[^\s]+)?)',
            ]
            for pattern in patterns:
                for match in _re_html.findall(pattern, content):
                    url = match.strip()
                    if url not in candidates:
                        candidates.append(url)
            return candidates

        async def _find_latest_report_html_url(self, agent, session_id: str) -> str:
            """从同一会话历史中找到最近一轮报告的 HTML 链接。"""
            try:
                agent._ensure_context_managers()
                history_store = getattr(agent, "_history", None)
                if not history_store:
                    return ""

                history = await history_store.get(session_id)
                for item in reversed(history or []):
                    if item.get("role") != "assistant":
                        continue
                    content = item.get("content", "")
                    urls = self._extract_report_html_urls_from_text(content)
                    if not urls:
                        continue

                    preferred = [
                        url for url in urls
                        if "report.html" in url or "/pages/" in url or "report_" in url
                    ]
                    return (preferred or urls)[0]
            except Exception as e:
                logger.warning(f"[DingTalk] Failed to locate previous report HTML: {e}")
            return ""

        async def _capture_report_png_from_html(
            self,
            agent,
            html_url: str,
            session_id: str,
            user_id: int,
        ) -> str:
            """对报告 HTML 截图，返回可公开访问的 PNG URL。"""
            if not html_url:
                return ""

            try:
                from app.services.component_screenshot import ComponentScreenshot

                result = await ComponentScreenshot.screenshot_html_url(
                    html_url=html_url,
                    title="report",
                    upload_to_dingtalk=True,
                    wait_seconds=3,
                )
            except Exception as e:
                logger.warning(f"[DingTalk] Screenshot execution failed: {e}")
                return ""

            if not result.get("success"):
                logger.warning(f"[DingTalk] Screenshot failed: {result.get('error', '')}")
                return ""

            return result.get("dingtalk_image_url") or result.get("screenshot_url", "")

        async def _try_send_previous_report_image(
            self,
            agent,
            message_content: str,
            incoming,
            sender_nick: Optional[str],
            conversation_id: str,
            conversation_type: str,
            robot_code: str,
            session_id: str,
            sender_staff_id: str,
            user_id: int,
        ) -> bool:
            """
            命中“把上一轮报告转图片”意图时，直接从历史里找 HTML 截图并发图。

            Returns:
                True 表示已处理（无论成功/失败都不再走通用 Agent 流程）
            """
            if not self._is_previous_report_image_request(message_content):
                return False

            html_url = await self._find_latest_report_html_url(agent, session_id)
            if not html_url:
                return False

            status_text = f"@{sender_nick} 正在提取上一轮报告并生成图片预览..." if sender_nick else "正在提取上一轮报告并生成图片预览..."
            await self._send_sample_markdown(
                content=status_text,
                title="处理中",
                conversation_id=conversation_id,
                conversation_type=conversation_type,
                sender_staff_id=sender_staff_id,
                incoming_message=incoming,
            )

            png_url = await self._capture_report_png_from_html(
                agent=agent,
                html_url=html_url,
                session_id=session_id,
                user_id=user_id,
            )

            if not png_url:
                fallback_text = (
                    f"@{sender_nick} 已找到上一轮报告 HTML，但截图失败。您可以先打开 HTML：{html_url}"
                    if sender_nick else
                    f"已找到上一轮报告 HTML，但截图失败。您可以先打开 HTML：{html_url}"
                )
                await self._send_sample_markdown(
                    content=fallback_text,
                    title="截图失败",
                    conversation_id=conversation_id,
                    conversation_type=conversation_type,
                    sender_staff_id=sender_staff_id,
                    incoming_message=incoming,
                )
                return True

            await self._send_image_message(
                image_url=png_url,
                conversation_id=conversation_id,
                robot_code=robot_code,
                conversation_type=conversation_type,
                sender_staff_id=sender_staff_id,
            )
            done_text = f"@{sender_nick} 已将上一轮报告截图发给您。" if sender_nick else "已将上一轮报告截图发给您。"
            await self._send_sample_markdown(
                content=done_text,
                title="已发送",
                conversation_id=conversation_id,
                conversation_type=conversation_type,
                sender_staff_id=sender_staff_id,
                incoming_message=incoming,
            )
            logger.info(f"[DingTalk] Previous report image sent from HTML: {html_url}")
            return True

        async def _send_report_md_file(
            self,
            incoming,
            report_urls: dict,
            result: str,
            sender_nick: Optional[str],
            conversation_id: str,
            conversation_type: str,
            robot_code: str,
            report_name: str = "",
            sender_staff_id: str = "",
        ):
            """
            自动发送详细报告 MD 文件到钉钉群聊

            流程：
            1. 从 report_urls 中获取 MD 文件下载地址
            2. 通过 DingTalkUploader 下载并上传到钉钉获取 media_id
            3. 使用钉钉 REST API 发送文件消息

            如果 report_urls 中无 MD 链接，则将 result 内容生成临时 MD 文件上传。
            """
            import httpx

            # 根据 report_name 生成文件名（Phase 2 LLM 生成的英文名）
            _default_filename = f"{report_name}.md" if report_name else "report.md"

            # 获取 MD 文件内容
            md_url = report_urls.get("md", "")
            md_bytes = None
            filename = _default_filename

            if md_url:
                # 从 MinIO 下载 MD 文件
                try:
                    from app.services.dingtalk_uploader import DingTalkUploader
                    import os
                    import re
                    _ext_host = os.getenv("AGENT_EXTERNAL_HOST", "localhost")
                    minio_public = os.getenv("MINIO_PUBLIC_URL", f"http://{_ext_host}:19000")
                    minio_endpoint = os.getenv("MINIO_ENDPOINT", "minio:9000")
                    minio_secure = os.getenv("MINIO_SECURE", "false").lower() == "true"
                    minio_scheme = "https" if minio_secure else "http"
                    minio_internal = f"{minio_scheme}://{minio_endpoint}"

                    download_url = md_url.replace(minio_public, minio_internal)
                    download_url = re.sub(
                        r'http://\d+\.\d+\.\d+\.\d+:1?9000',
                        minio_internal,
                        download_url,
                    )

                    async with httpx.AsyncClient(timeout=30) as client:
                        resp = await client.get(download_url)
                        resp.raise_for_status()
                        md_bytes = resp.content
                    # 使用 report_name 作为文件名，降级到 URL 中的文件名
                    filename = _default_filename
                    logger.info(f"[DingTalk] Downloaded MD file: {len(md_bytes)} bytes, filename={filename}")
                except Exception as e:
                    logger.warning(f"[DingTalk] Failed to download MD from MinIO: {e}")

            # Fallback: 将 result 文本转为 MD 文件
            if not md_bytes and result:
                md_bytes = result.encode("utf-8")
                filename = _default_filename
                logger.info(f"[DingTalk] Using result text as MD file: {len(md_bytes)} bytes")

            if not md_bytes:
                logger.warning("[DingTalk] No MD content to send")
                return

            # 上传文件到钉钉获取 media_id
            from app.services.dingtalk_uploader import DingTalkUploader
            media_id = await DingTalkUploader.upload_file(
                file_bytes=md_bytes,
                filename=filename,
                filetype="file",
            )
            if not media_id:
                logger.warning("[DingTalk] Failed to upload MD file to DingTalk")
                return

            logger.info(f"[DingTalk] MD file uploaded, media_id={media_id[:30]}...")

            # 使用钉钉 REST API 发送文件消息到群聊
            from app.config import settings
            _robot_code = robot_code or settings.dingtalk_client_id
            access_token = None
            try:
                from app.services.dingtalk_uploader import _get_access_token
                access_token = await _get_access_token()
            except Exception:
                pass

            if not access_token:
                logger.warning("[DingTalk] No access token, cannot send file message")
                return

            # 钉钉机器人消息发送 API（群聊/单聊自动路由）
            headers = {
                "x-acs-dingtalk-access-token": access_token,
                "Content-Type": "application/json",
            }
            msg_param = json.dumps({
                "mediaId": media_id,
                "fileName": filename,
                "fileType": "md",
            }, ensure_ascii=False)

            if conversation_type == "2":
                url = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
                payload = {
                    "robotCode": _robot_code,
                    "openConversationId": conversation_id,
                    "msgKey": "sampleFile",
                    "msgParam": msg_param,
                }
            else:
                # 单聊：需要 sender_staff_id
                url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
                payload = {
                    "robotCode": _robot_code,
                    "userIds": [sender_staff_id] if sender_staff_id else [],
                    "msgKey": "sampleFile",
                    "msgParam": msg_param,
                }
                if not sender_staff_id:
                    logger.warning("[DingTalk] MD file single-chat: sender_staff_id missing, skip file send")
                    return

            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.post(url, headers=headers, json=payload)
                    if resp.status_code == 200:
                        logger.info(
                            f"[DingTalk] Report MD file sent (conv_type={conversation_type}): "
                            f"conversation={conversation_id[:20]}, file={filename}"
                        )
                    else:
                        logger.warning(
                            f"[DingTalk] Send file message failed: "
                            f"status={resp.status_code}, body={resp.text[:200]}"
                        )
            except Exception as e:
                logger.warning(f"[DingTalk] Send file message request error: {e}")

        async def _send_sample_markdown(
            self, content: str, title: str = "消息",
            conversation_id: str = "",
            conversation_type: str = "",
            sender_staff_id: str = "",
            incoming_message=None,
        ):
            """统一用 sampleMarkdown REST API 发消息，失败降级到 reply_markdown_card"""
            try:
                from app.channels.manager import get_channel_manager
                from app.channels.types import ReplyPayload
                _plugin = get_channel_manager().get_plugin("dingtalk")
                if _plugin:
                    _payload = ReplyPayload(
                        markdown=content,
                        metadata={
                            "title": title,
                            "conversation_type": conversation_type,
                            "sender_staff_id": sender_staff_id,
                        },
                    )
                    await _plugin.send_message(conversation_id, _payload)
                    return
            except Exception as _e:
                logger.warning(f"[DingTalk] sampleMarkdown failed, fallback: {_e}")
            # 降级
            if incoming_message:
                self.reply_markdown_card(
                    markdown=content,
                    incoming_message=incoming_message,
                    title=title,
                    at_sender=True,
                )

        async def _send_image_message(
            self, image_url: str, conversation_id: str, robot_code: str,
            conversation_type: str = "", sender_staff_id: str = "",
        ):
            """将图片（文件路径或 URL）上传后作为独立图片消息发送（群聊/单聊自动路由）"""
            import httpx
            import os as _os

            from app.services.dingtalk_uploader import DingTalkUploader, _get_access_token
            from app.services.file_generator import resolve_local_path_from_download_value
            from app.config import settings

            # 1. 已是钉钉 mediaId（@开头）或钉钉公网URL，直接使用
            if image_url.startswith("@") or "dingtalk" in image_url or "aliyuncs" in image_url:
                dingtalk_url = image_url

            # 2. 本地文件路径，直接上传
            elif not image_url.startswith("http") and _os.path.isfile(image_url):
                dingtalk_url = await DingTalkUploader.get_public_url(image_url)

            # 3. 自身 API 下载 URL（/api/files/download?path=...），转为本地文件读取避免 HTTP 中转
            elif "/api/files/download" in image_url:
                local_path = resolve_local_path_from_download_value(image_url)
                if local_path and local_path.is_file():
                    logger.info(f"[DingTalk] Image: resolved local file {local_path}")
                    dingtalk_url = await DingTalkUploader.get_public_url(str(local_path))
                else:
                    logger.warning(f"[DingTalk] Image: local file not found for {image_url}, falling back to HTTP")
                    dingtalk_url = await DingTalkUploader.upload_from_url(image_url)

            # 4. 其他 HTTP URL
            else:
                dingtalk_url = await DingTalkUploader.upload_from_url(image_url)

            if not dingtalk_url:
                logger.warning(f"[DingTalk] Image upload failed: {image_url[:80]}")
                return

            access_token = await _get_access_token()
            if not access_token:
                return

            _robot_code = robot_code or settings.dingtalk_client_id
            headers = {
                "x-acs-dingtalk-access-token": access_token,
                "Content-Type": "application/json",
            }
            msg_param = json.dumps({"photoURL": dingtalk_url})

            if conversation_type == "2":
                # 群聊
                url = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
                payload = {
                    "robotCode": _robot_code,
                    "openConversationId": conversation_id,
                    "msgKey": "sampleImageMsg",
                    "msgParam": msg_param,
                }
            else:
                # 单聊
                url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
                payload = {
                    "robotCode": _robot_code,
                    "userIds": [sender_staff_id] if sender_staff_id else [],
                    "msgKey": "sampleImageMsg",
                    "msgParam": msg_param,
                }
                if not sender_staff_id:
                    logger.warning("[DingTalk] Image single-chat: sender_staff_id missing, skip")
                    return

            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code == 200:
                    logger.info(f"[DingTalk] Image sent (conv_type={conversation_type}): {image_url[:60]}")
                else:
                    logger.warning(f"[DingTalk] Image send failed: {resp.status_code} {resp.text[:200]}")

        async def _send_file_via_url(
            self, file_url: str, conversation_id: str, robot_code: str,
            conversation_type: str = "", sender_staff_id: str = "",
        ):
            """将文件（本地路径或 URL）上传后作为独立文件消息发送"""
            import httpx
            import os
            import re as _re_file
            from pathlib import Path

            from app.services.dingtalk_uploader import DingTalkUploader, _get_access_token
            from app.config import settings

            # 从路径/URL 中提取文件名
            filename = Path(file_url.split("?")[0]).name or "file"

            # 优先从本地文件路径直接读取
            if not file_url.startswith("http") and os.path.isfile(file_url):
                try:
                    with open(file_url, "rb") as f:
                        file_bytes = f.read()
                except Exception as e:
                    logger.warning(f"[DingTalk] File read failed: {e}, path={file_url[:100]}")
                    return
            else:
                # HTTP URL：将 localhost/外部IP 替换为容器内可访问地址后下载
                _ext_host = os.getenv("AGENT_EXTERNAL_HOST", "localhost")
                _agent_port = os.getenv("AGENT_SERVICE_PORT", "8000")
                _agent_internal = os.getenv("AGENT_INTERNAL_BASE_URL", f"http://host.docker.internal:{_agent_port}")
                download_url = _re_file.sub(
                    rf'http://(?:localhost|127\.0\.0\.1|{_re_file.escape(_ext_host)}):{_agent_port}',
                    _agent_internal,
                    file_url,
                )
                try:
                    async with httpx.AsyncClient(timeout=30) as client:
                        resp = await client.get(download_url)
                        resp.raise_for_status()
                        file_bytes = resp.content
                except Exception as e:
                    logger.warning(f"[DingTalk] File download failed: {e}, url={download_url[:100]}")
                    return

            # 上传到钉钉
            media_id = await DingTalkUploader.upload_file(
                file_bytes=file_bytes,
                filename=filename,
                filetype="file",
            )
            if not media_id:
                logger.warning("[DingTalk] File upload to DingTalk failed")
                return

            access_token = await _get_access_token()
            if not access_token:
                return

            _robot_code = robot_code or settings.dingtalk_client_id
            headers = {
                "x-acs-dingtalk-access-token": access_token,
                "Content-Type": "application/json",
            }
            msg_param = json.dumps({
                "mediaId": media_id,
                "fileName": filename,
                "fileType": Path(filename).suffix.lstrip(".") or "file",
            }, ensure_ascii=False)

            if conversation_type == "2":
                # 群聊
                url = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
                payload = {
                    "robotCode": _robot_code,
                    "openConversationId": conversation_id,
                    "msgKey": "sampleFile",
                    "msgParam": msg_param,
                }
            else:
                # 单聊
                url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
                payload = {
                    "robotCode": _robot_code,
                    "userIds": [sender_staff_id] if sender_staff_id else [],
                    "msgKey": "sampleFile",
                    "msgParam": msg_param,
                }
                if not sender_staff_id:
                    logger.warning("[DingTalk] File single-chat: sender_staff_id missing, skip")
                    return

            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code == 200:
                    logger.info(f"[DingTalk] File sent (conv_type={conversation_type}): {filename}")
                else:
                    logger.warning(f"[DingTalk] File send failed: {resp.status_code} {resp.text[:200]}")

        async def _check_pending_feedback(
            self,
            incoming,
            message_content: str,
            sender_id: str,
            sender_nick: Optional[str],
            conversation_id: str,
        ) -> bool:
            """Deprecated – Redis removed. Always return False."""
            return False

        async def _render_component_screenshots(
            self, component_events: list
        ) -> str:
            """
            将组件事件渲染为截图并上传到钉钉，返回 Markdown 片段

            流程：
            1. ComponentScreenshot.render_and_screenshot() 渲染 HTML + Playwright 截图
            2. DingTalkUploader.upload_image() 上传截图到钉钉图床
            3. 拼接 Markdown（截图 + 交互链接）

            Args:
                component_events: component_for_render 事件的 data 列表

            Returns:
                Markdown 文本，包含截图和交互链接
            """
            markdown_parts = []

            for comp in component_events:
                component_name = comp.get("component", "")
                component_data = comp.get("data", {})
                component_title = comp.get("title", component_name)

                if not component_name or not component_data:
                    continue

                try:
                    from app.services.component_screenshot import ComponentScreenshot

                    # 渲染 HTML + 截图
                    result = await ComponentScreenshot.render_and_screenshot(
                        component=component_name,
                        data=component_data,
                        title=component_title,
                        upload_to_dingtalk=True,
                    )

                    if result.get("success"):
                        # 优先使用钉钉图床 URL
                        image_url = result.get("dingtalk_image_url", "")
                        interactive_url = result.get("interactive_url", "")

                        if image_url:
                            markdown_parts.append(
                                f"![{component_title}]({image_url})"
                            )
                        if interactive_url:
                            markdown_parts.append(
                                f"[查看交互版本]({interactive_url})"
                            )

                        logger.info(
                            f"[DingTalk] Component screenshot ready: "
                            f"{component_name}, elapsed={result.get('elapsed_seconds', '?')}s"
                        )
                    else:
                        # 截图失败，提供交互链接作为降级
                        interactive_url = result.get("interactive_url", "")
                        if interactive_url:
                            markdown_parts.append(
                                f"[查看{component_title}]({interactive_url})"
                            )
                        logger.warning(
                            f"[DingTalk] Screenshot failed for {component_name}: "
                            f"{result.get('error', 'unknown')}"
                        )

                except Exception as e:
                    logger.error(
                        f"[DingTalk] Component screenshot error for "
                        f"{component_name}: {e}"
                    )
                    import traceback
                    logger.debug(traceback.format_exc())

            return "\n\n".join(markdown_parts)

    class DingTalkFileEventHandler(dingtalk_stream.EventHandler):
        """
        钉钉群消息事件处理器（仅缓存文件 downloadCode）

        订阅 EVENT topic，接收所有群消息事件（包括不带 @ 的文件消息）。
        只处理 msg_type == "file" 的消息，将 downloadCode 缓存到 Redis，
        供用户引用那条消息 + @Agent 提问时使用。
        不触发 Agent 业务逻辑。
        """

        _MSG_FILE_CACHE_PREFIX = "dingtalk:msg_file:"
        _MSG_FILE_CACHE_TTL = 86400 * 7  # 7天

        async def process(self, event: dingtalk_stream.EventMessage):
            try:
                data = event.data or {}
                # 打印完整结构，用于确认字段名（首次调试用）
                logger.info(f"[DingTalk-FileEvent] EVENT data keys={list(data.keys())}, data={json.dumps(data, ensure_ascii=False)[:500]}")
                # 钉钉群消息事件结构: data.msgtype / data.content / data.msgId
                msg_type = data.get("msgtype", "") or data.get("msgType", "")
                if msg_type != "file":
                    return dingtalk_stream.AckMessage.STATUS_OK, "OK"

                msg_id = data.get("msgId", "") or data.get("msgid", "")
                content = data.get("content", {}) or {}
                if isinstance(content, str):
                    try:
                        content = json.loads(content)
                    except Exception:
                        content = {}

                download_code = content.get("downloadCode", "")
                file_name = content.get("fileName", "") or content.get("filename", "")
                robot_code = data.get("robotCode", "") or data.get("robotcode", "")

                if not msg_id or not download_code:
                    return dingtalk_stream.AckMessage.STATUS_OK, "OK"

                logger.info(
                    f"[DingTalk] File event received (no-op, Redis removed): "
                    f"msgId={msg_id[:20]}..., file={file_name}"
                )
            except Exception as e:
                logger.warning(f"[DingTalk] FileEventHandler error: {e}")

            return dingtalk_stream.AckMessage.STATUS_OK, "OK"

else:
    # dingtalk-stream 未安装时的占位类
    class DingTalkBotHandler:
        def __init__(self):
            raise ImportError("dingtalk-stream not installed")

    class DingTalkFileEventHandler:
        def __init__(self):
            raise ImportError("dingtalk-stream not installed")
