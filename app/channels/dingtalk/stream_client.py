"""
DingTalk Stream Client
钉钉 Stream 模式客户端

管理 WebSocket 连接，注册消息处理器

注意：
1. 在多 worker 环境下，只有第一个获取锁的 worker 会启动 Stream 连接
2. 钉钉连接失败不会影响主服务，是可选功能
3. 连接在后台异步运行，带有自动重试机制
"""
import asyncio
import os
import fcntl
from pathlib import Path
from loguru import logger

from app.config import settings

# 连接超时配置（秒）
_CONNECT_TIMEOUT = int(os.environ.get("DINGTALK_CONNECT_TIMEOUT", 10))
_INIT_TIMEOUT = int(os.environ.get("DINGTALK_INIT_TIMEOUT", 5))

# 全局 Stream 客户端实例
_stream_client = None
_stream_task = None
_lock_file = None
_is_primary_worker = False

# 全局 Bot handler 实例（供 cron 等内部模块复用钉钉消息发送能力）
_bot_handler = None


def get_bot_handler():
    """获取 DingTalkBotHandler 实例（需在 start_dingtalk_stream 之后调用）"""
    return _bot_handler

# 锁文件路径
_LOCK_FILE_PATH = "/tmp/dingtalk_stream.lock"


def _cleanup_stale_lock():
    """
    启动时清理残留锁文件。

    如果锁文件存在且其中记录的 PID 对应的进程已不存在，
    说明是上次容器重启/crash 后的残留，直接删除。
    """
    if not os.path.exists(_LOCK_FILE_PATH):
        return
    try:
        with open(_LOCK_FILE_PATH, 'r') as f:
            pid_str = f.read().strip()
        if pid_str and pid_str.isdigit():
            old_pid = int(pid_str)
            try:
                os.kill(old_pid, 0)  # 检查进程是否存在
                return  # 进程仍在运行，不清理
            except OSError:
                pass  # 进程不存在，清理
        # PID 为空或进程已不存在，删除残留锁
        os.remove(_LOCK_FILE_PATH)
        logger.info(f"[DingTalk] Cleaned up stale lock file (old pid={pid_str or 'empty'})")
    except Exception as e:
        logger.warning(f"[DingTalk] Failed to cleanup stale lock: {e}")


def _try_acquire_lock() -> bool:
    """
    尝试获取文件锁，确保只有一个 worker 处理钉钉消息

    Returns:
        True 如果成功获取锁，False 如果锁已被其他进程持有
    """
    global _lock_file
    lock_fd = None

    # 先清理上次重启残留的锁文件
    _cleanup_stale_lock()

    try:
        lock_fd = open(_LOCK_FILE_PATH, 'w')
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        # 写入 PID 便于调试
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()
        _lock_file = lock_fd
        return True
    except (IOError, OSError) as e:
        # 锁已被其他进程持有或发生其他错误
        if lock_fd:
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                lock_fd.close()
            except Exception:
                pass
        logger.error(f"Failed to acquire lock: {e}")
        return False


def _release_lock():
    """释放文件锁"""
    global _lock_file

    if _lock_file:
        try:
            fcntl.flock(_lock_file.fileno(), fcntl.LOCK_UN)
        except Exception as e:
            logger.warning(f"Failed to unlock: {e}")
        try:
            _lock_file.close()
        except Exception as e:
            logger.warning(f"Failed to close lock file: {e}")
        _lock_file = None


async def _refresh_token_loop(client, client_id: str, client_secret: str):
    """
    后台定期刷新 send-only handler 的 access_token。

    send-only 模式下不调用 start()，SDK 内部 token 刷新不会运行，
    ~2h 后 token 过期导致发送失败。此循环每 30 分钟主动刷新一次。
    刷新失败时采用指数退避（30s → 60s → 120s → ... → 最大 300s）后重试，
    避免 token 长时间失效。
    """
    import httpx

    _TOKEN_REFRESH_INTERVAL = 1800  # 30 分钟正常刷新
    _RETRY_BASE = 30                # 失败后首次重试等待秒数
    _RETRY_MAX = 300                # 最大重试等待秒数
    retry_delay = None              # None 表示当前处于正常周期

    while True:
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                resp = await http.post(
                    "https://api.dingtalk.com/v1.0/oauth2/accessToken",
                    json={"appKey": client_id, "appSecret": client_secret},
                )
                data = resp.json()
                token = data.get("accessToken")
                if token:
                    client.get_access_token = lambda _t=token: _t
                    logger.info(f"[DingTalk] Send-only handler token refreshed (pid={os.getpid()})")
                    retry_delay = None  # 重置为正常周期
                    await asyncio.sleep(_TOKEN_REFRESH_INTERVAL)
                else:
                    raise ValueError(f"no accessToken in response: {data}")
        except asyncio.CancelledError:
            return
        except Exception as e:
            if retry_delay is None:
                retry_delay = _RETRY_BASE
            else:
                retry_delay = min(retry_delay * 2, _RETRY_MAX)
            logger.warning(f"[DingTalk] Token refresh failed: {e}, retrying in {retry_delay}s")
            await asyncio.sleep(retry_delay)


async def _init_send_only_handler(client_id: str, client_secret: str):
    """
    为非 primary worker 创建仅用于发送消息的 DingTalkBotHandler。

    只初始化 handler + dingtalk_client 引用，不启动 stream 连接。
    这样 cron 请求无论落到哪个 worker，都能复用 bot_handler._process_message_async 发送钉钉消息。

    关键：启动后台 token 刷新循环，因为不调用 start() 时 SDK 不会自动刷新 token。
    """
    global _bot_handler
    try:
        import dingtalk_stream
        from app.channels.dingtalk.bot_handler import DingTalkBotHandler

        credential = dingtalk_stream.Credential(client_id, client_secret)
        client = dingtalk_stream.DingTalkStreamClient(credential)

        # 首次立即获取 token，确保 handler 立即可用
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as http:
                resp = await http.post(
                    "https://api.dingtalk.com/v1.0/oauth2/accessToken",
                    json={"appKey": client_id, "appSecret": client_secret},
                )
                data = resp.json()
                token = data.get("accessToken")
                if token:
                    client.get_access_token = lambda _t=token: _t
                    logger.info(f"[DingTalk] Send-only handler initial token acquired (pid={os.getpid()})")
        except Exception as e:
            logger.warning(f"[DingTalk] Initial token fetch failed (will retry in background): {e}")

        handler = DingTalkBotHandler()
        handler.dingtalk_client = client  # 手动注入，等效于 register_callback_handler 的效果
        _bot_handler = handler

        # 启动后台 token 刷新循环
        asyncio.create_task(_refresh_token_loop(client, client_id, client_secret))

        logger.info(
            f"[DingTalk] Worker {os.getpid()} initialized send-only handler "
            f"(no stream connection, token refresh started, for cron reuse)"
        )
    except Exception as e:
        logger.warning(f"[DingTalk] Worker {os.getpid()} failed to init send-only handler: {e}")


async def start_dingtalk_stream():
    """
    启动钉钉 Stream 客户端

    关键设计：
    1. 在独立的 asyncio Task 中运行，不阻塞主服务
    2. 钉钉是可选功能，连接失败不影响主服务
    3. 初始化有超时保护，避免卡住
    4. 多 worker 环境下只有一个 worker 会启动连接
    """
    global _stream_client, _stream_task, _is_primary_worker

    # 检查是否启用
    if not settings.dingtalk_enabled:
        logger.info("[DingTalk] Stream mode disabled (DINGTALK_ENABLED=false)")
        return False

    # 检查凭证
    client_id = settings.dingtalk_client_id
    client_secret = settings.dingtalk_client_secret

    if not client_id or not client_secret:
        logger.warning(
            "[DingTalk] Stream mode disabled: "
            "DINGTALK_CLIENT_ID or DINGTALK_CLIENT_SECRET not set"
        )
        return False

    # 尝试获取锁（多 worker 环境下只有一个 worker 会成功）
    if not _try_acquire_lock():
        logger.info(
            f"[DingTalk] Worker {os.getpid()} skipping Stream init "
            f"(another worker is handling it)"
        )
        # 非 primary worker：创建仅用于发送消息的 handler（不启动 stream 连接）
        # 这样 cron 请求无论落到哪个 worker 都能通过 bot_handler 发送钉钉消息
        await _init_send_only_handler(client_id, client_secret)
        return False

    _is_primary_worker = True
    logger.info(f"[DingTalk] Worker {os.getpid()} acquired lock, starting Stream...")

    # === 确保钉钉域名在 NO_PROXY 中，避免走代理 ===
    _dingtalk_domains = ".dingtalk.com,api.dingtalk.com,oapi.dingtalk.com"
    _no_proxy = os.environ.get('NO_PROXY', '')
    if 'dingtalk.com' not in _no_proxy:
        new_no_proxy = f"{_no_proxy},{_dingtalk_domains}" if _no_proxy else _dingtalk_domains
        os.environ['NO_PROXY'] = new_no_proxy
        os.environ['no_proxy'] = new_no_proxy
        logger.info(f"[DingTalk] Added dingtalk domains to NO_PROXY: {new_no_proxy}")

    # === Patch websockets.connect 默认参数，增大 ping 超时 ===
    # 钉钉 SDK 内部使用 websockets.connect(uri) 不传 ping 参数，
    # websockets 12.x 默认 ping_interval=20, ping_timeout=20，
    # 网络抖动容易导致 keepalive ping timeout 断连。
    # 这里 patch 为更宽松的参数。
    try:
        import websockets
        _orig_connect = websockets.connect

        class _PatchedConnect:
            """Wrap websockets.connect to inject default ping parameters."""
            def __init__(self, orig):
                self._orig = orig

            def __call__(self, *args, **kwargs):
                kwargs.setdefault('ping_interval', 30)
                kwargs.setdefault('ping_timeout', 60)
                kwargs.setdefault('close_timeout', 30)
                return self._orig(*args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._orig, name)

        websockets.connect = _PatchedConnect(_orig_connect)
        logger.info("[DingTalk] Patched websockets.connect: ping_interval=30, ping_timeout=60")
    except Exception as e:
        logger.warning(f"[DingTalk] Failed to patch websockets.connect: {e}")

    # 检查 SDK 是否可用
    try:
        import dingtalk_stream
    except ImportError:
        logger.error("[DingTalk] dingtalk-stream package not installed")
        _release_lock()
        return False

    # 带超时的初始化
    try:
        async def _init_client():
            global _stream_client, _bot_handler
            # 创建凭证
            credential = dingtalk_stream.Credential(client_id, client_secret)
            # 创建客户端
            _stream_client = dingtalk_stream.DingTalkStreamClient(credential)
            # 注册消息处理器
            from app.channels.dingtalk.bot_handler import DingTalkBotHandler
            handler = DingTalkBotHandler()
            _bot_handler = handler  # 保存引用供 cron 复用
            _stream_client.register_callback_handler(
                dingtalk_stream.chatbot.ChatbotMessage.TOPIC,
                handler
            )

            # 注册卡片按钮回调处理器（处理点赞/踩按钮点击）
            from app.channels.dingtalk.card_feedback_handler import FeedbackCardCallbackHandler
            card_handler = FeedbackCardCallbackHandler()
            _stream_client.register_callback_handler(
                dingtalk_stream.CallbackHandler.TOPIC_CARD_CALLBACK,
                card_handler
            )
            logger.info("[DingTalk] CardCallbackHandler registered for feedback buttons")

            # 注册群消息事件处理器（缓存文件 downloadCode，供引用消息使用）
            from app.channels.dingtalk.bot_handler import DingTalkFileEventHandler
            file_event_handler = DingTalkFileEventHandler()
            _stream_client.register_all_event_handler(file_event_handler)
            logger.info("[DingTalk] FileEventHandler registered for file caching")

        # 初始化有超时保护
        await asyncio.wait_for(_init_client(), timeout=_INIT_TIMEOUT)

        logger.info(
            f"[DingTalk] Stream client configured "
            f"(client_id={client_id[:8]}..., pid={os.getpid()})"
        )

    except asyncio.TimeoutError:
        logger.error(f"[DingTalk] Client initialization timeout ({_INIT_TIMEOUT}s), skipping")
        _release_lock()
        return False
    except Exception as e:
        logger.error(f"[DingTalk] Client initialization failed: {e}, skipping")
        _release_lock()
        return False

    # 在后台启动 Stream 连接（不等待连接成功）
    from app.utils.background_task_manager import create_background_task
    _stream_task = create_background_task(
        _run_stream_forever(),
        task_name="dingtalk_stream"
    )

    # 立即返回，不等待连接建立
    logger.info("[DingTalk] Stream task started in background (non-blocking)")
    return True


async def _run_stream_forever():
    """
    永久运行 Stream 客户端，自动重连

    关键设计：
    1. SDK 的 start() 是长期运行的协程（建立 WebSocket 后持续监听），不能加超时
    2. start() 正常情况下永远不会返回，只有断连/异常才会退出
    3. 指数退避重试
    4. 连接失败不影响主服务
    """
    global _stream_client

    retry_count = 0
    max_retries = int(os.environ.get("DINGTALK_MAX_RETRIES", 10))
    base_delay = int(os.environ.get("DINGTALK_RETRY_DELAY", 5))

    while True:
        try:
            logger.info(f"[DingTalk] Starting Stream connection (pid={os.getpid()})...")

            # start() 是长期运行的 async 方法：建立 WebSocket 连接后持续监听消息
            # 不能用 wait_for 加超时，否则会在超时时强制取消正常运行中的连接
            await _stream_client.start()

            # 如果 start() 正常返回（罕见），重置重试计数
            logger.info("[DingTalk] Stream start() returned normally, reconnecting...")
            retry_count = 0

        except asyncio.CancelledError:
            logger.info("[DingTalk] Stream client cancelled")
            break

        except Exception as e:
            retry_count += 1
            delay = min(base_delay * retry_count, 60)  # 最大 60 秒
            logger.warning(
                f"[DingTalk] Stream connection error: {e}, "
                f"retry {retry_count} in {delay}s"
            )
            await asyncio.sleep(delay)

    # 连接循环结束，释放锁
    logger.info("[DingTalk] Stream connection loop ended")
    _release_lock()


async def stop_dingtalk_stream():
    """
    停止钉钉 Stream 客户端
    """
    global _stream_client, _stream_task, _is_primary_worker

    if _stream_task and not _stream_task.done():
        _stream_task.cancel()
        try:
            await _stream_task
        except asyncio.CancelledError:
            pass
        logger.info("[DingTalk] Stream client stopped")

    _stream_client = None
    _stream_task = None

    # 释放锁
    if _is_primary_worker:
        _release_lock()
        _is_primary_worker = False


def get_stream_status() -> dict:
    """
    获取 Stream 客户端状态
    """
    global _stream_client, _stream_task, _is_primary_worker

    return {
        "enabled": settings.dingtalk_enabled,
        "configured": bool(settings.dingtalk_client_id and settings.dingtalk_client_secret),
        "running": _stream_task is not None and not _stream_task.done() if _stream_task else False,
        "is_primary_worker": _is_primary_worker,
        "worker_pid": os.getpid() if _is_primary_worker else None,
        "client_id": settings.dingtalk_client_id[:8] + "..." if settings.dingtalk_client_id else None,
    }
