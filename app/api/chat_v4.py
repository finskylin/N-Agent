"""
Chat V4 API — 基于 Claude Agent SDK 原生架构

接口列表:
- POST /chat/v4/stream  — 流式发送消息 (SSE)
- POST /chat/v4/send    — 同步发送消息
- GET  /chat/v4/skills  — 获取可用技能列表
- GET  /chat/v4/status  — V4 Agent 状态
"""
import json
import math
import asyncio
import uuid
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Optional, Dict, Any, List

# 当前用户消息的上下文变量（在异步处理链中传递原始输入）
current_user_message: ContextVar[str] = ContextVar("current_user_message", default="")
from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from loguru import logger


def _sanitize_for_json(obj):
    """递归清理 NaN/inf 值，确保 JSON 序列化安全"""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    elif isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(item) for item in obj]
    return obj

from app.utils.tracing import generate_trace_id, set_trace_id

# SSE 心跳间隔（秒）— 防止长时间无数据导致连接断开
_HEARTBEAT_INTERVAL = 15


router = APIRouter(prefix="/chat/v4", tags=["Chat V4"])


# ========== 用户认证辅助函数 ==========

async def _get_user_id_from_request(request: Request, default_user_id: int = 1) -> int:
    """
    从请求中获取 user_id

    优先级:
    1. Authorization header 中的 JWT token
    2. X-User-ID header
    3. 默认值
    """
    # 1. 尝试从 JWT token 获取
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        try:
            from jose import jwt
            from app.config import settings
            payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
            user_id = payload.get("id")
            if user_id:
                return int(user_id)
        except Exception as e:
            logger.debug(f"[V4 API] JWT decode failed: {e}")

    # 2. 尝试从 X-User-ID header 获取
    x_user_id = request.headers.get("X-User-ID")
    if x_user_id:
        try:
            return int(x_user_id)
        except ValueError:
            pass

    # 3. 返回默认值
    return default_user_id


# ========== 请求/响应模型 ==========

class V4ChatRequest(BaseModel):
    """V4 聊天请求 — 完整版"""
    session_id: Optional[int] = None
    user_id: Optional[int] = None
    message: str
    ts_code: Optional[str] = None
    params: Optional[Dict[str, Any]] = None
    history: List[Dict[str, str]] = []
    output_format: Optional[str] = "markdown"  # markdown | mermaid
    knowledge_dir: Optional[str] = None
    extra_knowledge_dirs: List[str] = []
    render_mode: str = "auto"  # auto | text_only
    auto_approve_plan: bool = False  # 是否自动批准计划（跳过用户确认）
    # @skill 功能支持
    force_skills: List[str] = []  # 强制使用的技能列表（通过 @skill 语法指定）
    skill_params: Optional[Dict[str, Any]] = None  # 技能参数（@skill(params)）
    # 文件上传支持
    attached_files: List[Dict[str, str]] = []  # [{"file_id": "xxx", "file_path": "...", "file_name": "报告.pdf", "minio_url": "..."}]
    # 场景类型（可选，用户指定或 LLM 推断）
    scene_type: Optional[str] = None  # default | map | stock | intel
    # eval 模式：跳过记忆/蒸馏/反思等后处理写入（仅 eval 评测时使用）
    skip_memory: bool = False


class V4ChatResponse(BaseModel):
    """V4 聊天响应"""
    text: str
    components: List[Dict[str, Any]] = []
    result: Optional[Dict[str, Any]] = None
    agent_log: Optional[Dict[str, Any]] = None
    report_ready: Optional[Dict[str, Any]] = None
    components_for_render: List[Dict[str, Any]] = []


# ========== 全局 Agent 实例 ==========

_v4_agent = None


def _get_v4_agent():
    """延迟初始化 V4 Agent"""
    global _v4_agent
    if _v4_agent is None:
        from app.agent.v4.native_agent import V4NativeAgent
        _v4_agent = V4NativeAgent()
        logger.info("[V4 API] V4NativeAgent initialized")
    return _v4_agent


# ========== API 路由 ==========

# 文件上传白名单
_ALLOWED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xlsx", ".xls", ".png", ".jpg", ".jpeg",
    ".txt", ".md", ".py", ".js", ".ts", ".java", ".go", ".rs", ".c", ".cpp",
    ".h", ".hpp", ".css", ".html", ".xml", ".json", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".conf", ".sh", ".bat", ".sql", ".csv", ".log", ".env",
    ".vue", ".jsx", ".tsx",
}
_MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB


@router.post("/upload")
async def upload_file(request: Request, file: UploadFile = File(...)):
    """
    上传文件 → 本地暂存 + 本地对象存储持久化，返回 file_id 和访问 URL

    支持格式: PDF, DOCX, XLSX, PNG, JPG
    大小限制: 20MB
    """
    ext = Path(file.filename).suffix.lower() if file.filename else ""
    if ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"不支持的文件格式: {ext}")

    content = await file.read()
    if len(content) > _MAX_FILE_SIZE:
        raise HTTPException(400, "文件不能超过 20MB")

    user_id = await _get_user_id_from_request(request, default_user_id=1)

    # 1. 保存到本地（供 document_reader 直接读取）
    save_dir = Path("app/data/uploads")
    save_dir.mkdir(parents=True, exist_ok=True)
    file_id = f"{int(time.time())}_{uuid.uuid4().hex[:8]}"
    save_path = save_dir / f"{file_id}{ext}"

    with open(save_path, "wb") as f:
        f.write(content)

    # 2. 持久化到本地对象存储
    storage_url = ""
    try:
        from app.services.file_generator import upload_file_to_local_storage
        storage_url = upload_file_to_local_storage(
            file_data=content,
            user_id=user_id,
            file_name=file.filename,
            content_type=file.content_type or "application/octet-stream",
        )
    except Exception as e:
        logger.warning(f"[V4 Upload] Local object storage failed (non-fatal): {e}")

    logger.info(f"[V4 Upload] File saved: {save_path} ({len(content)} bytes), storage={bool(storage_url)}")

    return {"code": 200, "data": {
        "file_id": file_id,
        "file_name": file.filename,
        "file_path": str(save_path),
        "file_size": len(content),
        "file_type": ext.lstrip("."),
        "storage_url": storage_url,
    }}


@router.post("/stream")
async def v4_stream_message(fastapi_req: Request, request: V4ChatRequest):
    """
    V4 流式发送消息 (SSE)

    架构: ClaudeSDKClient 原生 Agent Loop + Hooks 注入
    事件类型:
    - text_delta: 文本流
    - thinking: 思维过程
    - thinking_step / thinking_step_done: 思维步骤
    - tool_call / tool_done: 工具调用
    - component: UI 组件
    - result: 执行结果摘要
    - error: 错误
    - done: 完成
    """
    agent = _get_v4_agent()

    # 从 JWT token 或 X-User-ID header 获取真实 user_id
    from app.agent.v4.v4_config import V4Config
    _cfg = V4Config.from_settings()
    jwt_user_id = await _get_user_id_from_request(fastapi_req, _cfg.default_user_id)
    # 优先使用 JWT 中的 user_id，其次使用请求体中的 user_id
    effective_user_id = jwt_user_id or request.user_id or _cfg.default_user_id

    async def event_generator():
        trace_id = generate_trace_id()
        set_trace_id(trace_id)

        # 设置当前用户消息上下文
        token = current_user_message.set(request.message)

        session_id_int = request.session_id or 0

        try:
            # 组装附件标记到消息中
            final_message = request.message
            if request.attached_files:
                tags = []
                for af in request.attached_files:
                    tags.append(
                        f"[attached_file|type=upload|path={af.get('file_path', '')}"
                        f"|name={af.get('file_name', '')}"
                        f"|url={af.get('minio_url', '')}]"
                    )
                final_message = request.message + "\n\n" + "\n".join(tags)

            logger.info(
                f"[V4 Stream] Request: {final_message[:80]}... "
                f"session={session_id_int}, render_mode={request.render_mode}, files={len(request.attached_files)}"
            )

            from app.agent.v4.native_agent import V4AgentRequest
            agent_request = V4AgentRequest(
                message=final_message,
                session_id=str(request.session_id or "default"),
                user_id=effective_user_id,  # 使用从 JWT 获取的 user_id
                ts_code=request.ts_code,
                params=request.params or {},
                history=request.history,
                output_format=request.output_format,
                knowledge_dir=request.knowledge_dir,
                extra_knowledge_dirs=request.extra_knowledge_dirs or [],
                render_mode=request.render_mode,
                auto_approve_plan=request.auto_approve_plan,
                force_skills=request.force_skills or [],
                skill_params=request.skill_params or {},
                scene_type=request.scene_type,
                skip_memory=request.skip_memory,
            )

            # Langfuse: 创建 Trace（不可用时返回 NoOp，不影响后续逻辑）
            try:
                from app.utils.langfuse_client import langfuse
                lf_trace = langfuse.trace(
                    name="chat_stream",
                    user_id=str(effective_user_id),
                    session_id=str(request.session_id or "default"),
                    input={
                        "message": request.message,
                        "ts_code": request.ts_code,
                        "render_mode": request.render_mode,
                    },
                    metadata={
                        "trace_id": trace_id,
                        "force_skills": request.force_skills or [],
                    },
                )
                agent_request.langfuse_trace = lf_trace
            except Exception as lf_err:
                logger.debug(f"[Langfuse] Trace creation skipped: {lf_err}")

            accumulated_text = []
            # 捕获 report_ready 事件用于 metadata 持久化
            captured_report_feedback = None

            # Phase 0 acknowledgment 检测（与 DingTalk bot_handler 同逻辑）
            # LLM 会在 Phase 0 输出一段确认文本，以 "---" 分隔符结束
            # Web 端需要将 ack 部分立即推送为 acknowledgment 事件，让前端快速展示
            ack_sent = False
            ack_buffer = []

            # 用 Queue 解耦 agent stream 和 SSE 输出，
            # 这样可以在 agent 长时间无事件时发送心跳保活
            _SENTINEL = object()
            queue: asyncio.Queue = asyncio.Queue()
            stream_done = False

            async def _pump_agent_events():
                """将 agent 事件推入 queue"""
                nonlocal stream_done
                try:
                    async for event in agent.process_stream(agent_request):
                        await queue.put(event)
                except Exception as exc:
                    await queue.put({"event": "error", "data": {"error": str(exc)}})
                finally:
                    stream_done = True
                    await queue.put(_SENTINEL)

            pump_task = asyncio.create_task(_pump_agent_events())

            try:
                while True:
                    try:
                        item = await asyncio.wait_for(
                            queue.get(), timeout=_HEARTBEAT_INTERVAL
                        )
                    except asyncio.TimeoutError:
                        # 超时无事件 → 发送 SSE comment 心跳保活
                        yield ": heartbeat\n\n"
                        continue

                    if item is _SENTINEL:
                        break

                    event_type = item.get("event", "")
                    event_data = item.get("data", {})

                    # ── Phase 1→2 切换: text_clear(reason=phase2_handoff) ──
                    # Phase 1 输出确认语后，text_clear 清空累积文本，Phase 2 重新输出
                    if event_type == "text_clear":
                        reason = event_data.get("reason", "unknown")
                        if reason == "phase2_handoff":
                            ack_sent = True
                        accumulated_text.clear()
                        # 推送 text_clear 给前端，让其清空显示
                        clear_data = json.dumps(
                            _sanitize_for_json(event_data), ensure_ascii=False
                        )
                        yield f"event: text_clear\ndata: {clear_data}\n\n"
                        continue

                    # ── Phase 0 acknowledgment: 剥离 --- 分隔符（兼容旧逻辑）──
                    if event_type == "text_delta" and not ack_sent:
                        delta = event_data.get("delta", "")
                        if delta:
                            if "---" in delta:
                                parts = delta.split("---", 1)
                                before_sep = parts[0]
                                if before_sep.strip():
                                    before_data = json.dumps(
                                        {"delta": before_sep}, ensure_ascii=False
                                    )
                                    yield f"event: text_delta\ndata: {before_data}\n\n"
                                    accumulated_text.append(before_sep)
                                ack_sent = True
                                remaining = parts[1] if len(parts) > 1 else ""
                                if remaining.strip():
                                    rem_data = json.dumps(
                                        {"delta": remaining}, ensure_ascii=False
                                    )
                                    yield f"event: text_delta\ndata: {rem_data}\n\n"
                                    accumulated_text.append(remaining)
                                continue
                            else:
                                pass  # 落入下方正常 text_delta 处理

                    # 累积文本
                    if event_type == "text_delta":
                        delta = event_data.get("delta", "")
                        if delta:
                            accumulated_text.append(delta)

                    # 捕获 report_ready 用于 metadata 保存
                    if event_type == "report_ready":
                        captured_report_feedback = event_data

                    # 序列化并发送 SSE（清理 NaN/inf）
                    data_json = json.dumps(
                        _sanitize_for_json(event_data), ensure_ascii=False, default=str
                    )
                    yield f"event: {event_type}\ndata: {data_json}\n\n"
            finally:
                if not pump_task.done():
                    pump_task.cancel()
                    try:
                        await pump_task
                    except (asyncio.CancelledError, Exception):
                        pass

            # 将 report_feedback 保存到最后一条 assistant 消息的 metadata
            if captured_report_feedback:
                try:
                    metadata = {
                        "report_feedback": _sanitize_for_json(captured_report_feedback)
                    }

                    logger.debug("[V4 Stream] metadata captured (session context stored in agent_core/session)")
                except Exception as meta_err:
                    logger.warning(f"[V4 Stream] Failed to save metadata: {meta_err}")

            # Langfuse: 完成 Trace（v3: update + end + flush）
            try:
                if hasattr(agent_request, 'langfuse_trace') and agent_request.langfuse_trace:
                    agent_request.langfuse_trace.update(output={"response": "".join(accumulated_text)})
                    agent_request.langfuse_trace.end()
                from app.utils.langfuse_client import langfuse
                langfuse.flush()
            except Exception:
                pass

            logger.info("[V4 Stream] Completed")

        except Exception as e:
            logger.error(f"[V4 Stream] Error: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            error_data = json.dumps({"error": str(e)})
            yield f"event: error\ndata: {error_data}\n\n"
        finally:
            current_user_message.reset(token)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/send", response_model=V4ChatResponse)
async def v4_send_message(fastapi_req: Request, request: V4ChatRequest):
    """
    V4 同步发送消息

    收集所有流式事件，组装为同步响应
    """
    trace_id = generate_trace_id()
    set_trace_id(trace_id)
    logger.info(f"[V4 API] Request: {request.message[:80]}...")

    agent = _get_v4_agent()

    # 从 JWT token 或 X-User-ID header 获取真实 user_id
    from app.agent.v4.v4_config import V4Config
    _cfg = V4Config.from_settings()
    jwt_user_id = await _get_user_id_from_request(fastapi_req, _cfg.default_user_id)
    effective_user_id = jwt_user_id or request.user_id or _cfg.default_user_id

    from app.agent.v4.native_agent import V4AgentRequest
    agent_request = V4AgentRequest(
        message=request.message,
        session_id=str(request.session_id or "default"),
        user_id=effective_user_id,
        ts_code=request.ts_code,
        params=request.params or {},
        history=request.history,
        output_format=request.output_format,
        knowledge_dir=request.knowledge_dir,
        extra_knowledge_dirs=request.extra_knowledge_dirs or [],
        render_mode=request.render_mode,
        auto_approve_plan=request.auto_approve_plan,
        force_skills=request.force_skills or [],
        skill_params=request.skill_params or {},
        scene_type=request.scene_type,
    )

    # Langfuse: 创建 Trace（同步发送消息入口）
    try:
        from app.utils.langfuse_client import langfuse
        lf_trace = langfuse.trace(
            name="chat_send",
            user_id=str(effective_user_id),
            session_id=str(request.session_id or "default"),
            input={
                "message": request.message,
                "ts_code": request.ts_code,
                "render_mode": request.render_mode,
            },
            metadata={
                "trace_id": trace_id,
                "force_skills": request.force_skills or [],
            },
        )
        agent_request.langfuse_trace = lf_trace
    except Exception as lf_err:
        logger.debug(f"[Langfuse] Trace creation skipped: {lf_err}")

    text_chunks = []
    components = []
    result_data = None
    captured_report_ready = None
    components_for_render = []

    try:
        async for event in agent.process_stream(agent_request):
            event_type = event.get("event", "")
            event_data = event.get("data", {})

            if event_type == "text_delta":
                text_chunks.append(event_data.get("delta", ""))
            elif event_type == "component":
                components.append(event_data)
            elif event_type == "result":
                result_data = event_data
            elif event_type == "report_ready":
                captured_report_ready = event_data
            elif event_type == "component_for_render":
                components_for_render.append(event_data)
            elif event_type == "error":
                text_chunks.append(
                    f"\n[Error: {event_data.get('error', '')}]"
                )

        # 将 report_ready 保存到最后一条 assistant 消息的 metadata
        if captured_report_ready:
            try:
                metadata = {"report_feedback": _sanitize_for_json(captured_report_ready)}

                logger.debug("[V4 Send] metadata captured (session context stored in agent_core/session)")
            except Exception as meta_err:
                logger.warning(f"[V4 Send] Failed to save metadata: {meta_err}")

        # Langfuse: 完成 Trace
        try:
            if hasattr(agent_request, 'langfuse_trace') and agent_request.langfuse_trace:
                agent_request.langfuse_trace.update(
                    output={"response": "".join(text_chunks)},
                )
                agent_request.langfuse_trace.end()
            from app.utils.langfuse_client import langfuse
            langfuse.flush()
        except Exception:
            pass

        return V4ChatResponse(
            text="".join(text_chunks),
            components=_sanitize_for_json(components),
            result=_sanitize_for_json(result_data),
            agent_log={
                "trace_id": trace_id,
                "method": "v4_native_sdk",
            },
            report_ready=_sanitize_for_json(captured_report_ready),
            components_for_render=_sanitize_for_json(components_for_render),
        )
    except Exception as e:
        logger.error(f"[V4 API] Error: {e}")
        # Langfuse: 记录错误并结束 Trace
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
        raise HTTPException(status_code=500, detail=str(e))


# ========== 定时任务专用端点 ==========

class V4CronExecuteRequest(BaseModel):
    """定时任务执行请求"""
    message: str
    session_id: Optional[str] = None
    auto_approve_plan: bool = True
    callback: Optional[Dict[str, Any]] = None  # 回调上下文
    alert_threshold: Optional[str] = None      # 触发阈值描述（自然语言），如"任意股票涨跌幅超过3%"
    silent_if_no_signal: bool = False           # 未达到阈值时静默不发消息


class V4CronExecuteResponse(BaseModel):
    """定时任务执行响应 — 已完成钉钉后处理"""
    success: bool
    text: str
    status: Optional[str] = None  # "sent" 表示已通过 bot_handler 派发，cron_task_runner 无需再发送
    page_url: Optional[str] = None
    report_ready: Optional[Dict[str, Any]] = None  # 报告元数据
    error: Optional[str] = None


def _build_fake_chatbot_message(callback: dict):
    """
    构造一个满足 CardReplier / AIMarkdownCardInstance 需求的 fake ChatbotMessage 对象。

    钉钉 SDK 的 CardReplier 使用的字段：
    - sender_id, sender_corp_id, conversation_id, message_id → gen_card_id()
    - conversation_type → 群聊/单聊路由
    - sender_staff_id, sender_nick → @用户
    - hosting_context → 可选
    """
    import uuid as _uuid

    class FakeChatbotMessage:
        def __init__(self, cb):
            self.sender_id = cb.get("sender_id", "cron_system")
            self.sender_corp_id = cb.get("corp_id", "")
            self.conversation_id = cb.get("conversation_id", "")
            self.message_id = f"cron_{_uuid.uuid4().hex[:16]}"
            self.conversation_type = cb.get("conversation_type", "2")  # 默认群聊
            self.sender_staff_id = cb.get("staff_id", "")
            self.sender_nick = cb.get("sender_nick", "定时任务")
            self.hosting_context = None
            self.extensions = {}  # _check_pending_feedback 通过 getattr 读取

    return FakeChatbotMessage(callback)


async def _resolve_sender_nick(handler, sender_id: str) -> str:
    """回查 sender_nick（Redis 已移除，始终返回空字符串）"""
    return ""


async def execute_cron_request(request: V4CronExecuteRequest) -> V4CronExecuteResponse:
    """
    定时任务专用端点 — 一次性返回钉钉可用的最终结果

    当请求包含钉钉回调上下文（callback.channel == "dingtalk"）且 bot_handler 可用时，
    直接复用 bot_handler._process_message_async 完整链路（AI 卡片、图片、反馈按钮），
    异步执行不阻塞，立即返回 {"status": "sent"}。

    无钉钉上下文或 bot_handler 不可用时，走原有的独立后处理管线。
    """
    trace_id = generate_trace_id()
    set_trace_id(trace_id)
    logger.info(f"[V4 CronExecute] Request: {request.message[:80]}...")

    # === 钉钉回调上下文：复用 bot_handler 完整链路 ===
    callback = request.callback or {}
    if callback.get("channel") == "dingtalk":
        try:
            from app.channels.dingtalk.stream_client import get_bot_handler
            handler = get_bot_handler()
            if handler and hasattr(handler, 'dingtalk_client') and handler.dingtalk_client:
                fake_incoming = _build_fake_chatbot_message(callback)

                # 构建用户隔离的 session_id（与 bot_handler.process 中一致）
                import hashlib as _hashlib
                _sender_id = callback.get("sender_id", "cron_system")
                _conv_id = callback.get("conversation_id", "")
                _staff_id = callback.get("staff_id", "")
                _user_key = _staff_id or _hashlib.md5(_sender_id.encode()).hexdigest()[:10]
                _user_session_id = str(request.session_id) if request.session_id else f"dingtalk_{_conv_id}_{_user_key}"

                # sender_nick 解析：callback > Redis 回查 > 默认值
                _sender_nick = callback.get("sender_nick", "")
                _invalid_nicks = {"", "User", "user", "定时任务", "cron_system"}
                if _sender_nick in _invalid_nicks:
                    # 尝试从 Redis 回查真实 sender_nick
                    _resolved_nick = await _resolve_sender_nick(handler, _sender_id)
                    if _resolved_nick:
                        _sender_nick = _resolved_nick
                        logger.info(f"[V4 CronExecute] Resolved sender_nick from Redis: {_sender_nick}")
                    else:
                        _sender_nick = _sender_nick or "定时任务"
                        logger.warning(f"[V4 CronExecute] sender_nick unresolved, using: {_sender_nick}")

                # 异步执行（不阻塞 cron_task_runner）
                from app.utils.background_task_manager import create_background_task
                create_background_task(
                    handler._process_message_async(
                        incoming=fake_incoming,
                        message_content=request.message,
                        conversation_id=_conv_id,
                        sender_id=_sender_id,
                        sender_nick=_sender_nick,
                        conversation_type=callback.get("conversation_type", "2"),
                        robot_code=callback.get("robot_code", ""),
                        user_session_id=_user_session_id,
                        sender_staff_id=_staff_id,
                        extra_params={"is_cron": True},
                    ),
                    task_name="cron_dingtalk_via_bot_handler"
                )

                logger.info(f"[V4 CronExecute] Dispatched to bot_handler (session={_user_session_id}, nick={_sender_nick})")
                return V4CronExecuteResponse(
                    success=True,
                    text="",
                    status="sent",
                )
            else:
                logger.warning("[V4 CronExecute] bot_handler not available, falling back to independent pipeline")
        except Exception as e:
            logger.warning(f"[V4 CronExecute] Failed to dispatch to bot_handler: {e}, falling back")

    # === 飞书渠道回调 ===
    if callback.get("channel") == "feishu":
        try:
            from app.channels.feishu.bot_handler import get_bot_handler as _get_feishu_handler
            from app.channels.feishu.utils import generate_feishu_user_id
            handler = _get_feishu_handler()
            if handler:
                open_id = callback.get("open_id", "")
                chat_id = callback.get("chat_id", "")
                chat_type = callback.get("chat_type", "p2p")
                message_id = callback.get("message_id", "")

                import hashlib as _hashlib
                _user_key = _hashlib.md5(open_id.encode()).hexdigest()[:10] if open_id else "cron"
                _user_session_id = str(request.session_id) if request.session_id else f"feishu_{chat_id}_{_user_key}"
                user_id = generate_feishu_user_id(open_id) if open_id else 1

                from app.agent.v4.native_agent import V4AgentRequest
                from app.utils.background_task_manager import create_background_task

                agent = _get_v4_agent()
                _cron_params: Dict[str, Any] = {
                    "auto_approve_plan": request.auto_approve_plan,
                    "is_cron": True,
                    "feishu_open_id": open_id,
                    "feishu_chat_id": chat_id,
                    "feishu_chat_type": chat_type,
                    "feishu_message_id": message_id,
                }
                if request.alert_threshold:
                    _cron_params["cron_alert_threshold"] = request.alert_threshold
                if request.silent_if_no_signal:
                    _cron_params["cron_silent_if_no_signal"] = True

                agent_request = V4AgentRequest(
                    message=request.message,
                    session_id=_user_session_id,
                    user_id=user_id,
                    params=_cron_params,
                    output_format="markdown",
                    render_mode="text_only",
                    channel="feishu",
                )

                _silent = request.silent_if_no_signal

                async def _run_feishu_cron():
                    text_chunks = []
                    try:
                        async for event in agent.process_stream(agent_request):
                            if event.get("event") == "text_delta":
                                delta = event.get("data", {}).get("delta", "")
                                if delta:
                                    text_chunks.append(delta)
                        result = "".join(text_chunks) or "定时任务已执行。"
                        # 静默检查：Agent 在无信号时会在文本中包含 [SILENT] 标记
                        if _silent and "[SILENT]" in result:
                            logger.info("[V4 CronExecute/Feishu] No signal detected, silent mode — skipping send")
                            return
                        from app.agent.v4.markdown_adapter import adapt_markdown_for_channel
                        result = adapt_markdown_for_channel(result, "feishu")
                    except Exception as exc:
                        logger.error(f"[V4 CronExecute/Feishu] Agent error: {exc}")
                        result = "定时任务执行时遇到问题，请稍后重试。"
                    await handler._send_reply(
                        message_id=message_id,
                        chat_id=chat_id,
                        chat_type=chat_type,
                        open_id=open_id,
                        text=result,
                    )

                create_background_task(_run_feishu_cron(), task_name="cron_feishu")
                logger.info(f"[V4 CronExecute] Dispatched to feishu (session={_user_session_id})")
                return V4CronExecuteResponse(success=True, text="", status="sent")
            else:
                logger.warning("[V4 CronExecute] feishu handler not available, falling back")
        except Exception as e:
            logger.warning(f"[V4 CronExecute] Failed to dispatch to feishu: {e}, falling back")

    # === 无渠道上下文或渠道 handler 不可用：走原有独立管线 ===
    agent = _get_v4_agent()

    from app.agent.v4.native_agent import V4AgentRequest, CHANNEL_DINGTALK

    # 从 callback 上下文注入钉钉参数
    params = {"auto_approve_plan": True, "is_cron": True}
    cron_sender_id = ""
    if request.callback:
        cron_sender_id = request.callback.get("sender_id", "")
        params.update({
            "dingtalk_sender": request.callback.get("sender_nick", ""),
            "dingtalk_sender_id": cron_sender_id,
            "dingtalk_staff_id": request.callback.get("staff_id", ""),
            "dingtalk_conversation_id": request.callback.get("conversation_id", ""),
            "dingtalk_conversation_type": request.callback.get("conversation_type", ""),
            "dingtalk_robot_code": request.callback.get("robot_code", ""),
        })

    # 动态生成钉钉 user_id（避免所有钉钉用户共享 user_id=1）
    from app.channels.dingtalk.utils import generate_dingtalk_user_id
    cron_user_id = generate_dingtalk_user_id(cron_sender_id) if cron_sender_id else 1

    agent_request = V4AgentRequest(
        message=request.message,
        session_id=str(request.session_id or "default"),
        user_id=cron_user_id,
        params=params,
        output_format="markdown",
        render_mode="text_only",
        auto_approve_plan=request.auto_approve_plan,
        channel=CHANNEL_DINGTALK,
    )

    # Langfuse: 创建 Trace
    try:
        from app.utils.langfuse_client import langfuse
        lf_trace = langfuse.trace(
            name="cron_execute",
            user_id=str(cron_user_id),
            session_id=str(request.session_id or "default"),
            input={"message": request.message},
            metadata={"trace_id": trace_id, "channel": "cron_dingtalk"},
        )
        agent_request.langfuse_trace = lf_trace
    except Exception as lf_err:
        logger.debug(f"[Langfuse] CronExecute trace creation skipped: {lf_err}")

    text_chunks = []
    component_events = []
    captured_report_ready = None
    has_error = False
    _report_lang = "zh"

    try:
        async for event in agent.process_stream(agent_request):
            event_type = event.get("event", "")
            event_data = event.get("data", {})

            if event_type == "text_delta":
                delta = event_data.get("delta", "")
                if delta:
                    text_chunks.append(delta)
            elif event_type == "component_for_render":
                component_events.append(event_data)
            elif event_type == "report_ready":
                captured_report_ready = event_data
            elif event_type == "error":
                has_error = True
                logger.warning(f"[V4 CronExecute] Agent error event: {event_data.get('error', '')}")

        # === 后处理管线（复用 bot_handler 逻辑） ===
        from app.utils.markdown_utils import truncate_markdown_safe, contains_api_error

        result = "".join(text_chunks)
        page_url = None
        logger.info(f"[V4 CronExecute] Assembled result: {len(result)} chars, has_error={has_error}")

        # 1. 错误检测
        # 仅在文本本身就是 API 错误消息时才过滤为友好提示
        # 如果有 error 事件但文本仍有大量有意义内容，保留文本并追加警告
        is_api_error = contains_api_error(result)
        if is_api_error:
            # 纯 API 错误文本 → 过滤为友好提示
            friendly_lines = []
            for line in result.split('\n'):
                line_stripped = line.strip()
                if line_stripped.startswith('\u26a0\ufe0f') or line_stripped == '---':
                    friendly_lines.append(line)
            if friendly_lines:
                final_result = '\n'.join(friendly_lines)
            else:
                final_result = "\u26a0\ufe0f 抱歉，遇到了一些问题，请稍后重试或换个问法。"
        else:
            # 2. 报告下载区拆分
            report_download_section = ""
            body = result
            separator = "\n---\n"
            sep_idx = result.rfind(separator)
            _tail = result[sep_idx:] if sep_idx >= 0 else ""
            if sep_idx >= 0 and ("**报告下载**" in _tail or "**Report Downloads**" in _tail):
                body = result[:sep_idx]
                report_download_section = result[sep_idx:]

            # 3. 正文超 800 字截断
            MAX_BODY_CHARS = 800
            if len(body) > MAX_BODY_CHARS:
                body = truncate_markdown_safe(body, MAX_BODY_CHARS)
                _truncate_hint = "Response truncated for DingTalk display" if _report_lang == "en" else "钉钉展示已截断"
                body = body.rstrip() + f"\n\n> **{_truncate_hint}**"
                logger.info(f"[V4 CronExecute] Condensed: {len(result)} -> {len(body)} chars")

            final_result = body + report_download_section

            # 有 error 事件但文本有意义内容时追加警告
            if has_error:
                final_result = final_result.rstrip() + "\n\n> \u26a0\ufe0f 部分数据获取异常，分析结果可能不完整"

        # 4. [Disabled] 不再生成 MinIO 托管的完整分析页面入口

        # 5. 适配钉钉 Markdown 格式
        from app.agent.v4.markdown_adapter import adapt_markdown_for_channel
        final_result = adapt_markdown_for_channel(final_result, "dingtalk")

        # Langfuse: 结束 trace
        try:
            if hasattr(agent_request, 'langfuse_trace') and agent_request.langfuse_trace:
                agent_request.langfuse_trace.update(
                    output={"response": final_result, "page_url": page_url},
                )
                agent_request.langfuse_trace.end()
            from app.utils.langfuse_client import langfuse
            langfuse.flush()
        except Exception:
            pass

        logger.info(f"[V4 CronExecute] Completed, result={len(final_result)} chars, page_url={bool(page_url)}")

        return V4CronExecuteResponse(
            success=True,
            text=final_result,
            page_url=page_url,
            report_ready=_sanitize_for_json(captured_report_ready),
        )

    except Exception as e:
        logger.error(f"[V4 CronExecute] Error: {e}")
        import traceback
        logger.debug(traceback.format_exc())
        # Langfuse: 记录错误
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
        return V4CronExecuteResponse(
            success=False,
            text="",
            error=str(e),
        )


@router.post("/cron-execute")
async def v4_cron_execute(request: V4CronExecuteRequest):
    return await execute_cron_request(request)


@router.get("/skills")
async def v4_list_skills(request: Request):
    """
    获取 V4 可用技能列表

    支持 X-User-ID header 返回用户可见的动态技能（自己的 + 共享的）
    默认 user_id="1"（简化处理）
    """
    user_id = request.headers.get("x-user-id", "1")  # 默认用户 ID 为 1
    agent = _get_v4_agent()
    return {"skills": await agent.get_available_skills(user_id=user_id)}

class CreateSessionRequest(BaseModel):
    """创建会话请求"""
    title: Optional[str] = None


@router.post("/sessions")
async def v4_create_session(request: Request, create_req: CreateSessionRequest):
    """
    创建新会话

    从 JWT token 或 X-User-ID header 获取 user_id
    需要 database_url 已配置
    """
    from app.agent.v4.v4_config import V4Config
    cfg = V4Config.from_settings()

    # 从 JWT token 获取 user_id
    user_id = await _get_user_id_from_request(request, cfg.default_user_id)

    from app.agent.v4.persistence import V4PersistenceLayer
    persistence = V4PersistenceLayer(default_user_id=cfg.default_user_id)
    try:
        new_session = await persistence.create_session(user_id=user_id, title=create_req.title)
        return new_session
    except Exception as e:
        logger.error(f"[V4 API] Failed to create session: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create session: {str(e)}")


@router.get("/sessions")
async def v4_list_sessions(request: Request, limit: int = 50, offset: int = 0):
    """
    列出用户的所有会话

    从 JWT token 或 X-User-ID header 获取 user_id
    需要 database_url 已配置
    """
    from app.agent.v4.v4_config import V4Config
    cfg = V4Config.from_settings()

    # 从 JWT token 获取 user_id
    user_id = await _get_user_id_from_request(request, cfg.default_user_id)

    from app.agent.v4.persistence import V4PersistenceLayer
    persistence = V4PersistenceLayer(default_user_id=cfg.default_user_id)
    sessions = await persistence.list_sessions(user_id=user_id, limit=limit, offset=offset)
    return {"sessions": sessions, "total": len(sessions)}


@router.get("/sessions/{session_id}")
async def v4_get_session(request: Request, session_id: str):
    """
    获取会话详情
    """
    from app.agent.v4.v4_config import V4Config
    cfg = V4Config.from_settings()

    user_id = await _get_user_id_from_request(request, cfg.default_user_id)

    from app.agent.v4.persistence import V4PersistenceLayer
    persistence = V4PersistenceLayer(default_user_id=cfg.default_user_id)
    detail = await persistence.get_session_detail(session_id, user_id=user_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Session not found")
    return _sanitize_for_json(detail)


@router.delete("/sessions/{session_id}")
async def v4_delete_session(request: Request, session_id: str):
    """
    删除会话
    """
    from app.agent.v4.v4_config import V4Config
    cfg = V4Config.from_settings()

    user_id = await _get_user_id_from_request(request, cfg.default_user_id)

    from app.agent.v4.persistence import V4PersistenceLayer
    persistence = V4PersistenceLayer(default_user_id=cfg.default_user_id)
    success = await persistence.delete_session(session_id, user_id=user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"success": True, "message": f"Session {session_id} deleted"}


class UpdateSessionRequest(BaseModel):
    """更新会话请求"""
    title: Optional[str] = None


@router.patch("/sessions/{session_id}")
async def v4_update_session(request: Request, session_id: str, update_req: UpdateSessionRequest):
    """
    更新会话（如修改标题）
    """
    from app.agent.v4.v4_config import V4Config
    cfg = V4Config.from_settings()

    user_id = await _get_user_id_from_request(request, cfg.default_user_id)

    from app.agent.v4.persistence import V4PersistenceLayer
    persistence = V4PersistenceLayer(default_user_id=cfg.default_user_id)
    success = await persistence.update_session(session_id, user_id=user_id, title=update_req.title)
    if not success:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"success": True, "message": f"Session {session_id} updated"}


@router.get("/sessions/{session_id}/history")
async def v4_get_session_history(request: Request, session_id: str, limit: int = 40):
    """获取会话的对话历史（从 agent_core/session 读取）"""
    from app.agent.v4.v4_config import V4Config
    cfg = V4Config.from_settings()
    user_id = await _get_user_id_from_request(request, cfg.default_user_id)

    try:
        from agent_core.session.context_db import SessionContextDB
        db_path = cfg.sqlite_db_path_template.format(instance_id=cfg.instance_id)
        db = SessionContextDB(db_path=db_path)
        messages = await db.get_conversation_history(session_id, user_id=user_id, limit=limit)
    except Exception as e:
        logger.warning(f"[v4_get_session_history] agent_core read failed: {e}")
        messages = []
    return {"session_id": session_id, "messages": messages, "total": len(messages)}


@router.get("/sessions/{session_id}/experience")
async def v4_get_session_experience(request: Request, session_id: str):
    """获取会话的经验数据（从 agent_core/session 读取）"""
    from app.agent.v4.v4_config import V4Config
    cfg = V4Config.from_settings()
    user_id = await _get_user_id_from_request(request, cfg.default_user_id)

    try:
        from agent_core.session.context_db import SessionContextDB
        db_path = cfg.sqlite_db_path_template.format(instance_id=cfg.instance_id)
        db = SessionContextDB(db_path=db_path)
        experience = await db.get_experiences(session_id, user_id=user_id)
    except Exception as e:
        logger.warning(f"[v4_get_session_experience] agent_core read failed: {e}")
        experience = {}
    total = sum(len(v) if isinstance(v, list) else 1 for v in experience.values())
    return {"session_id": session_id, "experience": experience, "total": total}


@router.get("/sessions/{session_id}/outputs")
async def v4_get_session_outputs(
    session_id: str,
    user_id: Optional[int] = None,
    skill_name: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """
    获取会话的所有 Skill 执行输出

    用于历史 Session 的 UI 组件恢复和原始数据查询

    Args:
        session_id: 会话 ID
        user_id: 用户 ID（可选）
        skill_name: 技能名称筛选（可选）
        limit: 最大返回数量
        offset: 偏移量

    Returns:
        {
            "session_id": "xxx",
            "outputs": [
                {
                    "execution_id": "uuid",
                    "skill_name": "web_search",
                    "query": "马斯克最新动态",
                    "items_count": 15,
                    "duration_ms": 12500,
                    "executed_at": "2026-02-09T22:30:00",
                    "ui_components": [...],
                    "raw_data_url": "http://..."
                }
            ],
            "total": 5
        }
    """
    from app.agent.v4.v4_config import V4Config
    cfg = V4Config.from_settings()

    from app.agent.v4.persistence import V4PersistenceLayer
    persistence = V4PersistenceLayer(default_user_id=cfg.default_user_id)

    outputs = await persistence.load_skill_outputs(
        session_id=session_id,
        user_id=user_id,
        skill_name=skill_name,
        limit=limit,
        offset=offset,
    )
    total = await persistence.count_skill_outputs(session_id=session_id, user_id=user_id)

    return _sanitize_for_json({
        "session_id": session_id,
        "outputs": outputs,
        "total": total,
    })


@router.get("/sessions/{session_id}/tabs/{tab_id}/outputs")
async def v4_get_tab_outputs(
    request: Request,
    session_id: str,
    tab_id: str,
):
    """
    获取特定场景 Tab 的所有 skill 输出（用于历史 session 场景重建）

    前端切换到历史 session 时，按 tab_id 加载关联的 skill outputs，
    重建场景 Tab 中的 layers/panels/standalone 组件。

    Args:
        session_id: 会话 ID
        tab_id: 场景 Tab ID

    Returns:
        {
            "tab_id": "tab_xxx",
            "outputs": [
                {
                    "execution_id": "uuid",
                    "skill_name": "geo_search",
                    "ui_component": "geo_search_card",
                    "scene_role": "panel",
                    "raw_data_json": {...},
                    "executed_at": "..."
                }
            ]
        }
    """
    from app.agent.v4.v4_config import V4Config
    cfg = V4Config.from_settings()

    user_id = await _get_user_id_from_request(request, cfg.default_user_id)

    from app.agent.v4.persistence import V4PersistenceLayer
    persistence = V4PersistenceLayer(default_user_id=cfg.default_user_id)

    outputs = await persistence.get_tab_skill_outputs(
        session_id=session_id,
        tab_id=tab_id,
        user_id=user_id,
    )

    return _sanitize_for_json({
        "tab_id": tab_id,
        "outputs": outputs,
    })


@router.get("/outputs/{execution_id}")
async def v4_get_output_detail(
    execution_id: str,
    user_id: Optional[int] = None,
    include_raw_data: bool = False,
):
    """
    获取单次 Skill 执行的完整数据

    Args:
        execution_id: 执行 UUID
        user_id: 用户 ID（可选）
        include_raw_data: 是否内联返回原始数据

    Returns:
        {
            "execution_id": "uuid",
            "skill_name": "web_search",
            "query": "马斯克最新动态",
            "executed_at": "2026-02-09T22:30:00",
            "duration_ms": 12500,
            "success": true,
            "ui_components": [...],
            "raw_data": [...] or null,
            "raw_data_url": "http://...",
            "items_count": 15,
            "raw_data_size": 125000
        }
    """
    from app.agent.v4.v4_config import V4Config
    cfg = V4Config.from_settings()

    from app.agent.v4.persistence import V4PersistenceLayer
    persistence = V4PersistenceLayer(default_user_id=cfg.default_user_id)

    detail = await persistence.get_skill_output(
        execution_id=execution_id,
        user_id=user_id,
        include_raw_data=include_raw_data,
    )

    if not detail:
        raise HTTPException(status_code=404, detail="Skill output not found")

    return detail


@router.get("/scenes")
async def v4_list_scenes():
    """
    获取可用场景列表

    从 app/config/ui/scenes.json 读取场景配置
    """
    import json as _json
    from pathlib import Path

    config_path = Path(__file__).resolve().parents[1] / "config" / "ui" / "scenes.json"
    if config_path.exists():
        try:
            config = _json.loads(config_path.read_text(encoding="utf-8"))
            scenes = config.get("scenes", [])
            return {"scenes": scenes, "total": len(scenes)}
        except Exception as e:
            logger.warning(f"[V4] Failed to load scenes config: {e}")

    # Fallback
    return {
        "scenes": [
            {"type": "default", "label": "默认", "canvas_type": None},
            {"type": "map", "label": "地图分析", "canvas_type": "map"},
            {"type": "stock", "label": "股票分析", "canvas_type": "chart"},
            {"type": "intel", "label": "情报分析", "canvas_type": "graph"},
        ],
        "total": 4,
    }


@router.get("/status")
async def v4_status():
    """V4 Agent 状态"""
    # 获取钉钉状态
    dingtalk_status = {}
    try:
        from app.channels.dingtalk.stream_client import get_stream_status
        dingtalk_status = get_stream_status()
    except Exception:
        dingtalk_status = {"enabled": False, "error": "module not available"}

    return {
        "status": "ready",
        "version": "4.0.0",
        "architecture": "claude_sdk_native",
        "features": {
            "agent_loop": "sdk_native",
            "tool_execution": "sdk_native_skill_and_bash",
            "hooks": ["PreToolUse", "PostToolUse", "Stop"],
            "ui_rendering": "hook_based",
            "thinking_chain": "hook_based",
            "context_management": "redis_persistent",
            "experience_learning": "llm_extracted",
            "knowledge_directory": "sdk_cwd_add_dirs",
            "render_mode": ["auto", "text_only"],
            "output_format": ["markdown", "mermaid"],
            "auto_approve_plan": True,
        },
        "dingtalk": dingtalk_status,
    }


# ========== 报告反馈 ==========

REPORT_FEEDBACK_TAGS = {
    "positive": ["数据准确", "分析深入", "建议有用"],
    "negative": ["数据有误", "分析太浅", "需要更多细节"],
}


class ReportFeedbackRequest(BaseModel):
    """报告反馈请求"""
    report_id: str
    session_id: str = ""
    rating: str  # like | dislike
    tags: List[str] = []
    comment: Optional[str] = None
    channel: str = "web"  # web | dingtalk


@router.post("/report-feedback")
async def submit_report_feedback(request: Request, body: ReportFeedbackRequest):
    """提交报告反馈（点赞/踩 + 标签 + 文本）"""
    if body.rating not in ("like", "dislike"):
        raise HTTPException(400, "rating must be 'like' or 'dislike'")

    user_id = await _get_user_id_from_request(request, default_user_id=1)
    tags_str = ",".join(body.tags) if body.tags else None

    from app.agent.v4.persistence import V4PersistenceLayer
    persistence = V4PersistenceLayer()
    await persistence.save_report_feedback(
        report_id=body.report_id,
        session_id=body.session_id,
        rating=body.rating,
        tags=tags_str,
        comment=body.comment,
        channel=body.channel,
        user_id=user_id,
    )

    # ── 桥接知识引擎反馈学习（异步，失败不影响反馈保存） ──
    try:
        await _bridge_feedback_to_knowledge_engine(
            user_id=user_id,
            session_id=body.session_id,
            rating=body.rating,
            comment=body.comment,
            tags=body.tags,
        )
    except Exception as e:
        logger.debug(f"[ReportFeedback] Knowledge engine bridge failed (non-fatal): {e}")

    return {"code": 200, "message": "反馈已提交"}


async def _bridge_feedback_to_knowledge_engine(
    user_id: int, session_id: str, rating: str,
    comment: Optional[str] = None, tags: Optional[List[str]] = None,
):
    """
    将用户反馈桥接到 agent_core 知识引擎的 FeedbackLearner。

    通过 session_id 查找最近的 Episode，然后调用 FeedbackLearner.learn_from_feedback()。
    """
    agent = _get_v4_agent()
    if not agent:
        return

    # 确保 SQLite / KnowledgeEngine 已初始化（反馈可能落到未处理过聊天的 worker）
    if not getattr(agent, "_context_initialized", False):
        try:
            agent._ensure_context_managers()
        except Exception as e:
            logger.debug(f"[ReportFeedback] ensure_context_managers failed: {e}")
            return

    feedback_learner = getattr(agent, "_ke_feedback_learner", None)
    ke_store = getattr(agent, "_ke_store", None)
    if not feedback_learner or not ke_store:
        logger.debug("[ReportFeedback] FeedbackLearner or KnowledgeStore not available, skipping")
        return

    # 通过 session_id 查找最近的 Episode
    episode = None
    if session_id:
        try:
            recent = await ke_store.get_recent_episodes(
                user_id=user_id, instance_id="default", limit=5,
            )
            for ep in recent:
                if ep.get("session_id") == session_id:
                    episode = ep
                    break
            # 没有匹配 session 的就用最近一条
            if not episode and recent:
                episode = recent[0]
        except Exception:
            pass

    # 构建 feedback_data
    feedback_data = {}
    if comment:
        feedback_data["comment"] = comment
    if tags:
        feedback_data["tags"] = tags

    # 调用 FeedbackLearner
    feedback_type = rating  # "like" or "dislike"
    if comment:
        # 有评论时额外触发 comment 类型学习
        await feedback_learner.learn_from_feedback(
            feedback_type="comment",
            feedback_data=feedback_data,
            episode=episode,
            user_id=user_id,
            instance_id="default",
        )

    await feedback_learner.learn_from_feedback(
        feedback_type=feedback_type,
        feedback_data=feedback_data,
        episode=episode,
        user_id=user_id,
        instance_id="default",
    )


@router.get("/report-feedback/{report_id}")
async def get_report_feedback(request: Request, report_id: str):
    """查询当前用户对该报告的反馈"""
    user_id = await _get_user_id_from_request(request, default_user_id=1)

    from app.agent.v4.persistence import V4PersistenceLayer
    persistence = V4PersistenceLayer()
    feedback = await persistence.get_report_feedback(report_id=report_id, user_id=user_id)
    return {"code": 200, "data": feedback}


@router.get("/report-feedback-tags")
async def get_report_feedback_tags():
    """获取预设反馈标签"""
    return {"code": 200, "data": REPORT_FEEDBACK_TAGS}


@router.get("/report-feedback-page/{report_id}", response_class=HTMLResponse)
async def report_feedback_page(report_id: str, channel: str = "dingtalk"):
    """独立反馈页面（钉钉内置浏览器打开）"""
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no">
<title>报告反馈</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:#f5f7fa;color:#333;padding:16px;max-width:480px;margin:0 auto}}
.header{{text-align:center;padding:20px 0 16px}}
.header h2{{font-size:18px;color:#1a1a1a}}
.header p{{font-size:13px;color:#999;margin-top:4px}}
.rating-row{{display:flex;gap:16px;justify-content:center;margin:20px 0}}
.rating-btn{{flex:1;max-width:140px;padding:14px 0;border:2px solid #e0e0e0;border-radius:12px;
background:#fff;font-size:28px;cursor:pointer;transition:all .2s;text-align:center}}
.rating-btn:active{{transform:scale(.95)}}
.rating-btn.selected-like{{border-color:#22c55e;background:#f0fdf4}}
.rating-btn.selected-dislike{{border-color:#ef4444;background:#fef2f2}}
.rating-btn .label{{font-size:13px;color:#666;margin-top:4px;display:block}}
.tags-section{{margin:16px 0;display:none}}
.tags-section.visible{{display:block}}
.tags-title{{font-size:13px;color:#999;margin-bottom:8px}}
.tags-row{{display:flex;flex-wrap:wrap;gap:8px}}
.tag-chip{{padding:6px 14px;border-radius:16px;border:1px solid #d0d5dd;background:#fff;
font-size:13px;cursor:pointer;transition:all .15s}}
.tag-chip.selected{{background:#eff6ff;border-color:#3b82f6;color:#3b82f6}}
.comment-section{{margin:16px 0;display:none}}
.comment-section.visible{{display:block}}
.comment-section textarea{{width:100%;border:1px solid #d0d5dd;border-radius:10px;padding:10px 12px;
font-size:14px;resize:none;height:80px;font-family:inherit}}
.comment-section textarea:focus{{outline:none;border-color:#3b82f6}}
.submit-btn{{width:100%;padding:12px;border:none;border-radius:10px;background:#3b82f6;color:#fff;
font-size:15px;font-weight:600;cursor:pointer;margin-top:12px;display:none}}
.submit-btn.visible{{display:block}}
.submit-btn:active{{background:#2563eb}}
.submit-btn:disabled{{background:#94a3b8;cursor:not-allowed}}
.success-msg{{text-align:center;padding:32px 0;display:none}}
.success-msg.visible{{display:block}}
.success-msg .icon{{font-size:48px}}
.success-msg p{{font-size:15px;color:#666;margin-top:8px}}
</style>
</head>
<body>
<div id="form">
  <div class="header">
    <h2>对报告有什么看法？</h2>
    <p>您的反馈帮助我们改进分析质量</p>
  </div>
  <div class="rating-row">
    <div class="rating-btn" id="btn-like" onclick="selectRating('like')">
      👍<span class="label">有帮助</span>
    </div>
    <div class="rating-btn" id="btn-dislike" onclick="selectRating('dislike')">
      👎<span class="label">需改进</span>
    </div>
  </div>
  <div class="tags-section" id="tags-section">
    <div class="tags-title">选择标签（可多选）</div>
    <div class="tags-row" id="tags-row"></div>
  </div>
  <div class="comment-section" id="comment-section">
    <textarea id="comment" placeholder="补充说明（可选）"></textarea>
  </div>
  <button class="submit-btn" id="submit-btn" onclick="submitFeedback()">提交反馈</button>
</div>
<div class="success-msg" id="success-msg">
  <div class="icon">✅</div>
  <p>感谢您的反馈！</p>
</div>
<script>
const REPORT_ID = "{report_id}";
const CHANNEL = "{channel}";
const TAGS = {{
  like: ["数据准确", "分析深入", "建议有用"],
  dislike: ["数据有误", "分析太浅", "需要更多细节"]
}};
let currentRating = null;
let selectedTags = new Set();

// 从 URL 参数自动选中评分（钉钉卡片按钮跳转）
(function autoSelectFromUrl() {{
  const params = new URLSearchParams(window.location.search);
  const rating = params.get('rating');
  if (rating === 'like' || rating === 'dislike') {{
    setTimeout(() => selectRating(rating), 100);
  }}
}})();

function selectRating(r) {{
  currentRating = r;
  document.getElementById('btn-like').className = 'rating-btn' + (r==='like' ? ' selected-like' : '');
  document.getElementById('btn-dislike').className = 'rating-btn' + (r==='dislike' ? ' selected-dislike' : '');
  // show tags
  const tagsRow = document.getElementById('tags-row');
  tagsRow.innerHTML = '';
  selectedTags.clear();
  (TAGS[r]||[]).forEach(t => {{
    const chip = document.createElement('span');
    chip.className = 'tag-chip';
    chip.textContent = t;
    chip.onclick = () => {{ toggleTag(chip, t) }};
    tagsRow.appendChild(chip);
  }});
  document.getElementById('tags-section').className = 'tags-section visible';
  document.getElementById('comment-section').className = 'comment-section visible';
  document.getElementById('submit-btn').className = 'submit-btn visible';
}}

function toggleTag(el, tag) {{
  if (selectedTags.has(tag)) {{ selectedTags.delete(tag); el.className='tag-chip'; }}
  else {{ selectedTags.add(tag); el.className='tag-chip selected'; }}
}}

async function submitFeedback() {{
  if (!currentRating) return;
  const btn = document.getElementById('submit-btn');
  btn.disabled = true;
  btn.textContent = '提交中...';
  try {{
    const resp = await fetch('/api/v1/chat/v4/report-feedback', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{
        report_id: REPORT_ID,
        session_id: '',
        rating: currentRating,
        tags: Array.from(selectedTags),
        comment: document.getElementById('comment').value || null,
        channel: CHANNEL
      }})
    }});
    if (resp.ok) {{
      document.getElementById('form').style.display = 'none';
      document.getElementById('success-msg').className = 'success-msg visible';
    }} else {{
      btn.disabled = false;
      btn.textContent = '提交失败，请重试';
    }}
  }} catch(e) {{
    btn.disabled = false;
    btn.textContent = '网络错误，请重试';
  }}
}}
</script>
</body>
</html>"""
    return HTMLResponse(content=html)
