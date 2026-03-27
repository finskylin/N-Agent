"""
Feishu Bot Handler
飞书机器人消息处理器

负责：
- 接收飞书 Webhook 事件（im.message.receive_v1）
- 去重防止重复处理
- 异步处理用户消息并调用 V4 Agent
- 回复消息（post 格式 Markdown）

设计：先 ACK，后处理。飞书要求在 3 秒内响应，超时会重试。
"""
import asyncio
import hashlib
import json
import time
from typing import Optional, Dict, Any, List
from loguru import logger

from app.channels.feishu.client import (
    reply_message,
    send_message,
    download_resource,
    get_message,
    build_post_content,
    build_interactive_card,
    reply_interactive,
    send_interactive,
    upload_file,
    send_file,
)

# 消息去重 — SQLite 跨进程原子写入（替代进程内内存字典，多 worker 安全）
_MESSAGE_CACHE_TTL = 300  # 5 分钟内的重复消息会被忽略
_dedup_db_path: Optional[str] = None
_dedup_db_lock = asyncio.Lock()


def _get_dedup_db_path() -> str:
    global _dedup_db_path
    if _dedup_db_path:
        return _dedup_db_path
    import os
    data_dir = os.environ.get("DATA_DIR", "/app/app/data")
    _dedup_db_path = os.path.join(data_dir, "feishu_dedup.db")
    return _dedup_db_path


async def _ensure_dedup_table() -> None:
    """确保去重表存在"""
    import aiosqlite
    async with aiosqlite.connect(_get_dedup_db_path()) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS feishu_processed_messages "
            "(message_id TEXT PRIMARY KEY, processed_at REAL NOT NULL)"
        )
        await db.commit()


async def _is_duplicate_message(message_id: str) -> bool:
    """
    跨进程去重：SQLite INSERT OR IGNORE 原子操作。
    同一 message_id 只有第一个 worker 能插入成功，其余返回 True（重复）。
    """
    if not message_id:
        return False

    import aiosqlite
    now = time.time()
    expire_before = now - _MESSAGE_CACHE_TTL

    try:
        async with _dedup_db_lock:
            async with aiosqlite.connect(_get_dedup_db_path()) as db:
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS feishu_processed_messages "
                    "(message_id TEXT PRIMARY KEY, processed_at REAL NOT NULL)"
                )
                # 清理过期记录
                await db.execute(
                    "DELETE FROM feishu_processed_messages WHERE processed_at < ?",
                    (expire_before,),
                )
                # 原子插入：若已存在则忽略，通过 rowcount 判断是否重复
                cursor = await db.execute(
                    "INSERT OR IGNORE INTO feishu_processed_messages (message_id, processed_at) VALUES (?, ?)",
                    (message_id, now),
                )
                await db.commit()
                if cursor.rowcount == 0:
                    logger.warning(f"[Feishu] Duplicate message, skipping: {message_id[:20]}...")
                    return True
    except Exception as e:
        logger.warning(f"[Feishu] Dedup check error (fallback allow): {e}")

    return False


async def _fetch_quoted_message_text(message_id: str) -> str:
    """
    拉取被引用消息的文本内容。

    支持 text / post 类型消息的文本提取；其他类型返回类型标签。
    失败时返回空字符串，调用方降级为仅注入 message_id。
    """
    try:
        msg = await get_message(message_id)
        if not msg:
            return ""
        msg_type = msg.get("msg_type", "")
        content_str = msg.get("body", {}).get("content", "{}")
        content = json.loads(content_str) if isinstance(content_str, str) else content_str

        if msg_type == "text":
            return content.get("text", "").strip()

        elif msg_type == "post":
            parts = []
            post_body = content.get("zh_cn") or content.get("en_us") or {}
            for row in post_body.get("content", []):
                for item in row:
                    if item.get("tag") == "text":
                        parts.append(item.get("text", ""))
            return "".join(parts).strip()

        elif msg_type in ("image", "file", "audio", "video"):
            return f"[{msg_type}]"

        return ""
    except Exception as e:
        logger.warning(f"[Feishu] _fetch_quoted_message_text error (id={message_id}): {e}")
        return ""


class FeishuBotHandler:
    """
    飞书机器人消息处理器

    将飞书消息转发给 V4 Agent 处理，并将结果回复给用户
    """

    def __init__(self):
        self._agent = None

    def _get_agent(self):
        """延迟初始化 V4 Agent"""
        if self._agent is None:
            from app.agent.v4.native_agent import V4NativeAgent
            try:
                self._agent = V4NativeAgent()
                logger.info("[Feishu] V4NativeAgent initialized for bot")
            except Exception as e:
                logger.error(f"[Feishu] V4NativeAgent init failed: {e}", exc_info=True)
                raise
        return self._agent

    async def extract_message_content(self, event: Dict[str, Any]) -> tuple[str, List[dict]]:
        """
        从飞书事件中提取消息文本和附件列表。

        支持：text / post / image / file / audio / video / merge_forward
        以及含 parent_id 的引用消息（在文本前注入引用提示）。

        Returns:
            (text_content, attached_files)
        """
        message = event.get("message", {})
        msg_type = message.get("message_type", "text")
        content_str = message.get("content", "{}")
        message_id = message.get("message_id", "")
        parent_id = message.get("parent_id", "")  # 引用消息时非空

        try:
            content = json.loads(content_str)
        except Exception:
            content = {}

        attached_files = []
        text_content = ""

        if msg_type == "text":
            text_content = content.get("text", "")

        elif msg_type == "post":
            parts = []
            post_body = content.get("zh_cn") or content.get("en_us") or {}
            for row in post_body.get("content", []):
                for item in row:
                    tag = item.get("tag", "")
                    if tag == "text":
                        parts.append(item.get("text", ""))
                    elif tag == "img":
                        image_key = item.get("image_key", "")
                        if image_key:
                            attached_files.append({
                                "type": "image",
                                "file_key": image_key,
                                "message_id": message_id,
                            })
                    elif tag == "file":
                        file_key = item.get("file_key", "")
                        file_name = item.get("file_name", "file")
                        if file_key:
                            attached_files.append({
                                "type": "file",
                                "file_key": file_key,
                                "file_name": file_name,
                                "message_id": message_id,
                            })
            text_content = "".join(parts)

        elif msg_type == "image":
            image_key = content.get("image_key", "")
            if image_key:
                attached_files.append({
                    "type": "image",
                    "file_key": image_key,
                    "message_id": message_id,
                })
            text_content = "[图片]"

        elif msg_type == "file":
            file_key = content.get("file_key", "")
            file_name = content.get("file_name", "file")
            if file_key:
                attached_files.append({
                    "type": "file",
                    "file_key": file_key,
                    "file_name": file_name,
                    "message_id": message_id,
                })
            text_content = f"[文件: {file_name}]"

        elif msg_type in ("audio", "video"):
            file_key = content.get("file_key", "")
            if file_key:
                attached_files.append({
                    "type": msg_type,
                    "file_key": file_key,
                    "message_id": message_id,
                })
            text_content = f"[{msg_type}]"

        elif msg_type == "merge_forward":
            # 合并转发消息：content 是 {"chat_id": "...", "root_id": "..."} 不含文本
            chat_id = content.get("chat_id", "")
            root_id = content.get("root_id", "")
            text_content = f"[合并转发消息]"
            if root_id:
                attached_files.append({
                    "type": "merge_forward",
                    "file_key": root_id,
                    "message_id": message_id,
                    "chat_id": chat_id,
                })

        # 引用消息：parent_id 非空时，表示用户引用了另一条消息作为上下文
        # 通过 API 拉取被引用消息的实际内容注入上下文
        if parent_id:
            quoted_text = await _fetch_quoted_message_text(parent_id)
            if quoted_text:
                text_content = f"[引用消息内容: {quoted_text}]\n{text_content}"
            else:
                text_content = f"[引用消息 ID: {parent_id}]\n{text_content}"

        # 去除 @机器人 的文字（飞书 mention key 格式: @_user_1）
        if "@_user_" in text_content:
            for mention in message.get("mentions", []):
                key = mention.get("key", "")
                if key:
                    text_content = text_content.replace(key, "").strip()

        return text_content.strip(), attached_files

    async def process_event(self, event_data: Dict[str, Any]) -> None:
        """
        处理飞书 im.message.receive_v1 事件（异步，后台运行）

        Args:
            event_data: 飞书事件的 event 字段内容
        """
        message = event_data.get("message", {})
        sender = event_data.get("sender", {})

        message_id = message.get("message_id", "")
        chat_id = message.get("chat_id", "")
        chat_type = message.get("chat_type", "p2p")  # p2p 或 group
        sender_id_info = sender.get("sender_id", {})
        open_id = sender_id_info.get("open_id", "")

        # 提取消息内容
        text_content, attached_files = await self.extract_message_content(event_data)

        if not text_content and not attached_files:
            logger.debug(f"[Feishu] Empty message, skipping: {message_id}")
            return

        logger.info(f"[Feishu] Processing: msg={message_id[:20]}, chat={chat_type}, text={text_content[:50]}")

        # 附件处理：下载到本地并注入 document_reader 调用指令（渠道层责任）
        if attached_files:
            text_content = await self._inject_attachment_directives(
                text_content, attached_files
            )

        # 构建 session_id（用户隔离）
        user_key = hashlib.md5(open_id.encode()).hexdigest()[:10]
        session_id = f"feishu_{chat_id}_{user_key}"

        # 生成内部 user_id
        from app.channels.feishu.utils import generate_feishu_user_id
        try:
            user_id = generate_feishu_user_id(open_id)
        except ValueError:
            logger.warning(f"[Feishu] Empty open_id, rejecting message: {message_id}")
            return

        agent = self._get_agent()

        from app.agent.v4.native_agent import V4AgentRequest, CHANNEL_DINGTALK

        agent_request = V4AgentRequest(
            message=text_content,
            session_id=session_id,
            user_id=user_id,
            params={
                "auto_approve_plan": True,
                "feishu_open_id": open_id,
                "feishu_chat_id": chat_id,
                "feishu_chat_type": chat_type,
                "feishu_message_id": message_id,
            },
            output_format="markdown",
            render_mode="text_only",
            channel="feishu",
            attached_files=attached_files,
        )

        # Langfuse 追踪
        try:
            from app.utils.langfuse_client import langfuse
            lf_trace = langfuse.trace(
                name="feishu_chat",
                user_id=str(user_id),
                session_id=session_id,
                input={"message": text_content, "open_id": open_id},
                metadata={"channel": "feishu", "chat_type": chat_type},
            )
            agent_request.langfuse_trace = lf_trace
        except Exception:
            pass

        from app.agent.v4.markdown_adapter import adapt_markdown_for_channel

        # 发送 post 消息的便捷方法（ACK / send_message 中间消息用）
        async def _send_post(text: str) -> Optional[str]:
            content = build_post_content(text)
            if message_id:
                mid = await reply_message(message_id, content, msg_type="post")
                if mid:
                    return mid
            receive_id = chat_id if chat_type == "group" else open_id
            receive_id_type = "chat_id" if chat_type == "group" else "open_id"
            return await send_message(receive_id, receive_id_type, content, msg_type="post")

        text_chunks: list = []
        ack_sent = False
        ack_buffer: list = []
        result = ""

        try:
            async for event in agent.process_stream(agent_request):
                event_type = event.get("event", "")
                event_data_inner = event.get("data", {})

                if event_type == "text_delta":
                    delta = event_data_inner.get("delta", "")
                    if not delta:
                        continue
                    if not ack_sent:
                        # 缓冲 LLM 第一句作为 ACK，遇到句末标点或累积 ≥50 字时立即发出
                        ack_buffer.append(delta)
                        ack_text = "".join(ack_buffer).strip()
                        _sentence_end = any(c in ack_text for c in "。！？.!?")
                        if ack_text and (_sentence_end or len(ack_text) >= 50):
                            # 截断到第一个句末标点
                            cut = -1
                            for i, c in enumerate(ack_text):
                                if c in "。！？.!?\n":
                                    cut = i
                                    break
                            if cut >= 0:
                                tail = ack_text[cut + 1:]
                                ack_text = ack_text[:cut + 1]
                                if tail:
                                    text_chunks.append(tail)
                            ack_adapted = adapt_markdown_for_channel(ack_text, "feishu")
                            try:
                                await _send_post(ack_adapted)
                                logger.info(f"[Feishu] ACK sent: {ack_text[:60]}")
                            except Exception as _e:
                                logger.warning(f"[Feishu] ACK send failed: {_e}")
                            ack_sent = True
                            ack_buffer.clear()
                    else:
                        text_chunks.append(delta)

                elif event_type == "send_message":
                    # LLM 通过 send_message skill 主动发的中间消息
                    _sm_content = event_data_inner.get("content", "")
                    _sm_type = event_data_inner.get("msg_type", "text")
                    if _sm_content:
                        try:
                            if _sm_type == "file" and _sm_content.strip():
                                # 上传本地文件到飞书，再发文件消息
                                import os
                                _file_path = _sm_content.strip()
                                if os.path.exists(_file_path):
                                    _file_key = await upload_file(_file_path)
                                    if _file_key:
                                        _receive_id = chat_id if chat_type == "group" else open_id
                                        _receive_id_type = "chat_id" if chat_type == "group" else "open_id"
                                        await send_file(_receive_id, _receive_id_type, _file_key)
                                        logger.info(f"[Feishu] send_message file sent: {_file_path} -> {_file_key}")
                                    else:
                                        # 上传失败，降级发文字提示
                                        _fname = os.path.basename(_file_path)
                                        await _send_post(f"📎 报告文件已生成：`{_fname}`（文件上传失败，请通过下载链接获取）")
                                        logger.warning(f"[Feishu] upload_file failed, sent text fallback")
                                else:
                                    logger.warning(f"[Feishu] send_message file not found: {_file_path}")
                                    await _send_post(f"📎 报告文件路径不存在：`{_file_path}`")
                            elif _sm_type == "image" and _sm_content.strip():
                                # 图片类型直接发文字（图片上传另行处理）
                                _sm_adapted = adapt_markdown_for_channel(_sm_content, "feishu")
                                await _send_post(_sm_adapted)
                            else:
                                _sm_adapted = adapt_markdown_for_channel(_sm_content, "feishu")
                                await _send_post(_sm_adapted)
                            logger.info(f"[Feishu] send_message dispatched ({_sm_type}, {len(_sm_content)} chars)")
                        except Exception as _e:
                            logger.warning(f"[Feishu] send_message failed: {_e}")
                    if not ack_sent:
                        ack_sent = True

                elif event_type == "error":
                    logger.warning(f"[Feishu] Agent error: {event_data_inner.get('error', '')}")

            # 将 ack_buffer 残留（未达到句末触发条件）并入正文
            if ack_buffer:
                text_chunks = ack_buffer + text_chunks
                ack_buffer.clear()

            result = "".join(text_chunks)
            if not result.strip():
                result = "抱歉，我暂时无法处理您的请求，请稍后再试。"

            result = adapt_markdown_for_channel(result, "feishu")

        except Exception as e:
            logger.error(f"[Feishu] Agent error: {e}")
            result = "处理请求时遇到问题，请稍后重试。"

        # 最终报告用交互卡片发送
        await self._send_reply(
            message_id=message_id,
            chat_id=chat_id,
            chat_type=chat_type,
            open_id=open_id,
            text=result,
        )

        # Langfuse 结束
        try:
            if hasattr(agent_request, "langfuse_trace") and agent_request.langfuse_trace:
                agent_request.langfuse_trace.update(output={"response": result})
                agent_request.langfuse_trace.end()
            from app.utils.langfuse_client import langfuse
            langfuse.flush()
        except Exception:
            pass

    async def _send_reply(
        self,
        message_id: str,
        chat_id: str,
        chat_type: str,
        open_id: str,
        text: str,
    ) -> None:
        """发送最终报告（交互卡片，支持长文分段）"""
        try:
            if message_id:
                result_id = await reply_interactive(message_id, text)
                if result_id:
                    logger.info(f"[Feishu] Reply sent (card): {result_id}")
                    return

            receive_id = chat_id if chat_type == "group" else open_id
            receive_id_type = "chat_id" if chat_type == "group" else "open_id"
            result_id = await send_interactive(receive_id, receive_id_type, text)
            if result_id:
                logger.info(f"[Feishu] Message sent (card): {result_id}")
            else:
                logger.error("[Feishu] Failed to send reply card")

        except Exception as e:
            logger.error(f"[Feishu] Send reply error: {e}")

    async def _inject_attachment_directives(
        self,
        text_content: str,
        attached_files: List[dict],
    ) -> str:
        """
        将飞书附件下载到本地存储，并在消息文本中注入 document_reader 调用指令。

        飞书附件结构：{type, file_key, message_id, file_name}
        下载后保存到本地，注入 file_url 参数，让 LLM 通过 document_reader(file_url=...) 读取。
        """
        import os
        from pathlib import Path

        directives = []
        for i, af in enumerate(attached_files):
            file_key = af.get("file_key", "")
            msg_id = af.get("message_id", "")
            ftype = af.get("type", "file")
            fname = af.get("file_name", f"attachment_{i}")

            if not file_key:
                continue

            try:
                resource_type = "image" if ftype == "image" else "file"
                file_bytes = await download_resource(msg_id, file_key, resource_type)
                if not file_bytes:
                    logger.warning(f"[Feishu] Failed to download attachment: file_key={file_key}")
                    continue

                # 保存到本地对象存储
                store_dir = Path(os.getenv("LOCAL_OBJECT_STORE_DIR", "app/data/object_storage"))
                dest = store_dir / "uploads" / "feishu" / f"{file_key}_{fname}"
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(file_bytes)

                base_url = (
                    os.getenv("AGENT_PUBLIC_BASE_URL")
                    or f"http://{os.getenv('AGENT_EXTERNAL_HOST', '127.0.0.1')}:{os.getenv('AGENT_SERVICE_PORT', '8000')}"
                )
                from urllib.parse import quote
                token = f"object_storage/uploads/feishu/{file_key}_{fname}"
                file_url = f"{base_url.rstrip('/')}/api/files/download?path={quote(token, safe='/:_-.()')}"

                name_hint = f"（文件名: {fname}）" if fname else ""
                directives.append(
                    f"- 附件{i}{name_hint}: 类型={ftype}，file_url={file_url}"
                )
                logger.info(f"[Feishu] Attachment saved locally: {file_url}")

            except Exception as e:
                logger.warning(f"[Feishu] Attachment download error (file_key={file_key}): {e}")

        if directives:
            text_content = (
                text_content
                + "\n\n【附件读取指令 — 必须执行】\n"
                "用户发送了以下附件，你必须立即调用 document_reader 工具读取其内容，"
                "然后根据内容回答用户的问题。\n"
                "调用参数：使用下方提供的 file_url 参数。\n"
                + "\n".join(directives)
            )

        return text_content

    async def send_text(
        self,
        chat_id: str,
        text: str,
        open_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        主动发送消息（供外部调用，如 cron 任务）

        Args:
            chat_id: 群聊 ID（优先），或 None 时使用 open_id 私信
            text: Markdown 文本内容
            open_id: 用户 open_id（私信时使用）

        Returns:
            message_id 或 None
        """
        content = build_post_content(text)

        if chat_id:
            return await send_message(chat_id, "chat_id", content, msg_type="post")
        elif open_id:
            return await send_message(open_id, "open_id", content, msg_type="post")
        else:
            logger.warning("[Feishu] send_text: no chat_id or open_id provided")
            return None


# 全局单例
_bot_handler: Optional[FeishuBotHandler] = None


def get_bot_handler() -> Optional[FeishuBotHandler]:
    """获取全局 FeishuBotHandler 实例"""
    return _bot_handler


def _ensure_bot_handler() -> FeishuBotHandler:
    """确保全局 handler 已初始化"""
    global _bot_handler
    if _bot_handler is None:
        _bot_handler = FeishuBotHandler()
    return _bot_handler
