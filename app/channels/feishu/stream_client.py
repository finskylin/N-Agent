"""
Feishu Stream Client
飞书长连接客户端

使用 lark-oapi WebSocket 长连接模式接收飞书事件，无需公网 Webhook URL。
对标钉钉 stream_client.py 的设计：
- 文件锁保证多 worker 只有一个实例建立连接
- 后台 Task 运行，启动失败不影响主服务
- auto_reconnect=True 由 SDK 内置重连
"""
import asyncio
import os
import fcntl
from loguru import logger

from app.config import settings

# 全局状态
_stream_task = None
_lock_file = None
_is_primary_worker = False

_LOCK_FILE_PATH = "/tmp/feishu_stream.lock"
_INIT_TIMEOUT = int(os.environ.get("FEISHU_INIT_TIMEOUT", "5"))


# ── 文件锁（多 worker 互斥）─────────────────────────────────────────────────

def _get_proc_start_time(pid: int) -> float:
    """获取进程启动时间（读 /proc/{pid}/stat），用于判断 PID 是否被复用。失败返回 0.0。"""
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().split()
        # 第 22 个字段是 starttime（clock ticks since boot）
        return float(fields[21]) if len(fields) > 21 else 0.0
    except Exception:
        return 0.0


def _try_acquire_lock() -> bool:
    """
    原子获取进程级锁。

    策略：
    1. O_CREAT|O_EXCL 原子创建锁文件 —— 内核保证只有一个进程成功，无 race window。
    2. 创建成功后写入 "PID:start_time"，持有 fd 不关闭。
    3. 创建失败（文件已存在）时，读取 PID + start_time 双重验证：
       - PID 不存在 → 过期锁，删除重试
       - PID 存在但 start_time 不匹配 → PID 被复用，过期锁，删除重试
       - PID 存在且 start_time 匹配 → 进程真实持有锁，返回 False
    """
    global _lock_file

    def _try_create() -> bool:
        global _lock_file
        try:
            fd = open(_LOCK_FILE_PATH, "x")  # O_CREAT|O_EXCL，原子操作
            start_time = _get_proc_start_time(os.getpid())
            fd.write(f"{os.getpid()}:{start_time}")
            fd.flush()
            _lock_file = fd
            return True
        except FileExistsError:
            return False

    if _try_create():
        return True

    # 文件已存在，双重验证 PID + 启动时间
    try:
        with open(_LOCK_FILE_PATH) as f:
            content = f.read().strip()

        pid_str, _, start_str = content.partition(":")
        stale = True
        if pid_str.isdigit():
            pid = int(pid_str)
            try:
                os.kill(pid, 0)  # 进程存在
                # 进一步验证启动时间，防止 PID 复用
                current_start = _get_proc_start_time(pid)
                if current_start and start_str and abs(current_start - float(start_str)) < 1.0:
                    stale = False  # PID 存在且启动时间匹配，真实持有锁
            except OSError:
                pass  # 进程不存在，过期锁

        if not stale:
            return False  # 另一个 worker 真实持有锁

        # 过期锁（PID 不存在或被复用）：删除后重试
        os.remove(_LOCK_FILE_PATH)
        logger.info(f"[Feishu] Removed stale lock (content={content!r})")
    except Exception as e:
        logger.warning(f"[Feishu] Lock check error: {e}")
        return False

    return _try_create()


def _release_lock() -> None:
    global _lock_file
    if _lock_file:
        try:
            _lock_file.close()
        except Exception as e:
            logger.warning(f"[Feishu] Lock release error: {e}")
        _lock_file = None
        try:
            os.remove(_LOCK_FILE_PATH)
        except Exception:
            pass


# ── 事件处理回调 ─────────────────────────────────────────────────────────────

def _make_event_handler(main_loop: asyncio.AbstractEventLoop = None):
    """
    返回注册到 EventDispatcherHandler 的回调函数。

    SDK 在子线程同步上下文调用，通过 run_coroutine_threadsafe
    将异步处理提交到主事件循环。

    支持消息类型：text / post / image / file / audio / video / merge_forward（合并转发）
    以及 parent_id 引用消息场景。
    """
    def _on_message_receive(data) -> None:
        try:
            event = data.event
            if event is None:
                return

            sender = event.sender
            message = event.message
            if message is None:
                return

            open_id = ""
            if sender and sender.sender_id:
                open_id = sender.sender_id.open_id or ""

            mentions = []
            for m in (message.mentions or []):
                mentions.append({
                    "key": getattr(m, "key", "") or "",
                    "name": getattr(m, "name", "") or "",
                })

            event_dict = {
                "sender": {
                    "sender_id": {"open_id": open_id},
                    "sender_type": getattr(sender, "sender_type", "user") if sender else "user",
                },
                "message": {
                    "message_id": message.message_id or "",
                    "root_id": message.root_id or "",      # 引用/回复链根消息
                    "parent_id": message.parent_id or "",  # 被引用的消息 ID
                    "chat_id": message.chat_id or "",
                    "chat_type": message.chat_type or "p2p",
                    "message_type": message.message_type or "text",
                    "content": message.content or "{}",
                    "mentions": mentions,
                },
            }

            message_id = event_dict["message"]["message_id"]

            async def _dispatch():
                from app.channels.feishu.bot_handler import (
                    _is_duplicate_message,
                    _ensure_bot_handler,
                )
                if await _is_duplicate_message(message_id):
                    return
                handler = _ensure_bot_handler()
                await handler.process_event(event_dict)

            loop = main_loop or asyncio.get_event_loop()
            asyncio.run_coroutine_threadsafe(_dispatch(), loop)

        except Exception as e:
            logger.error(f"[Feishu] Event handler error: {e}")

    return _on_message_receive


# ── 长连接运行（在线程池中执行阻塞的 client.start()）───────────────────────

async def _run_stream(app_id: str, app_secret: str) -> None:
    """
    在专用子线程中运行 lark-oapi ws.Client.start()，带外层重试循环。

    - auto_reconnect=True：由 SDK 内部处理 WebSocket 断线重连（心跳保活）
    - 每次重连新建 Client 实例，确保干净状态
    - 主事件循环引用提前捕获，供事件回调通过 run_coroutine_threadsafe 使用
    """
    import threading

    main_loop = asyncio.get_event_loop()
    retry_count = 0
    max_retries = int(os.environ.get("FEISHU_MAX_RETRIES", "10"))
    retry_base = int(os.environ.get("FEISHU_RETRY_DELAY", "5"))

    while True:
        _done = threading.Event()
        _exc: list = []
        _retry_snapshot = retry_count

        def _run_in_thread():
            # lark-oapi ws/client.py 在模块级捕获 loop = asyncio.get_event_loop()
            # 必须在独立子进程中运行，彻底隔离事件循环，避免 "already running" 错误
            import subprocess
            import sys
            import json as _json

            # 子进程脚本：通过 stdout 输出 JSON 事件，父进程读取后派发到主事件循环
            script = f"""
import asyncio, json, os, sys, logging
# 将 lark-oapi 日志重定向到 stderr，避免污染 stdout（stdout 专用于 JSON 事件行）
logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
os.environ.setdefault("FEISHU_APP_ID", {app_id!r})
os.environ.setdefault("FEISHU_APP_SECRET", {app_secret!r})

from lark_oapi import EventDispatcherHandler
from lark_oapi.ws import Client as WsClient

def _on_message(data):
    try:
        event = data.event
        if not event: return
        sender = event.sender
        message = event.message
        if not message: return
        open_id = ""
        if sender and sender.sender_id:
            open_id = sender.sender_id.open_id or ""
        mentions = []
        for m in (message.mentions or []):
            mentions.append({{"key": getattr(m,"key","") or "", "name": getattr(m,"name","") or ""}})
        payload = {{
            "sender": {{"sender_id": {{"open_id": open_id}}, "sender_type": getattr(sender,"sender_type","user") if sender else "user"}},
            "message": {{
                "message_id": message.message_id or "",
                "root_id": message.root_id or "",
                "parent_id": message.parent_id or "",
                "chat_id": message.chat_id or "",
                "chat_type": message.chat_type or "p2p",
                "message_type": message.message_type or "text",
                "content": message.content or "{{}}",
                "mentions": mentions,
            }},
        }}
        print(json.dumps(payload), flush=True)
    except Exception as e:
        print(json.dumps({{"__error__": str(e)}}), flush=True)

handler = EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(_on_message).build()
client = WsClient(app_id={app_id!r}, app_secret={app_secret!r}, event_handler=handler, auto_reconnect=True)
client.start()
"""
            proc = None
            try:
                logger.info(f"[Feishu] Connecting via subprocess (retry={_retry_snapshot})...")
                proc = subprocess.Popen(
                    [sys.executable, "-c", script],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                # 读取子进程 stdout（每行是一个 JSON 事件）
                for line in proc.stdout:
                    line = line.strip()
                    if not line or not line.startswith("{"):
                        continue
                    try:
                        event_dict = _json.loads(line)
                        if "__error__" in event_dict:
                            logger.warning(f"[Feishu] Subprocess event error: {event_dict['__error__']}")
                            continue
                        message_id = event_dict.get("message", {}).get("message_id", "")
                        async def _dispatch(ed=event_dict, mid=message_id):
                            from app.channels.feishu.bot_handler import (
                                _is_duplicate_message,
                                _ensure_bot_handler,
                            )
                            if await _is_duplicate_message(mid):
                                return
                            handler_inst = _ensure_bot_handler()
                            await handler_inst.process_event(ed)
                        asyncio.run_coroutine_threadsafe(_dispatch(), main_loop)
                    except Exception as e:
                        logger.warning(f"[Feishu] Failed to parse event line: {e}")

                proc.wait()
                stderr_out = proc.stderr.read()
                if proc.returncode != 0 and stderr_out:
                    _exc.append(Exception(f"subprocess exit {proc.returncode}: {stderr_out[:200]}"))
                elif proc.returncode == 0:
                    logger.info(f"[Feishu] Subprocess exited cleanly (returncode=0), will reconnect")
            except Exception as e:
                _exc.append(e)
            finally:
                # 确保子进程被回收，防止僵尸进程
                if proc is not None:
                    try:
                        if proc.poll() is None:
                            proc.kill()
                        proc.wait()
                    except Exception:
                        pass
                _done.set()

        t = threading.Thread(target=_run_in_thread, name="feishu-ws", daemon=True)
        t.start()

        try:
            while not _done.is_set():
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            logger.info("[Feishu] Stream task cancelled")
            _release_lock()
            raise

        if _exc:
            retry_count += 1
            delay = min(retry_base * retry_count, 60)
            logger.warning(f"[Feishu] Connection error: {_exc[0]}, retry {retry_count} in {delay}s")
            await asyncio.sleep(delay)
        else:
            # 正常断开（飞书主动关闭），重置重试计数后重连
            logger.info("[Feishu] Connection closed normally, reconnecting...")
            retry_count = 0
            await asyncio.sleep(2)

    _release_lock()
    logger.info("[Feishu] Stream connection loop ended")


# ── 公共 API ────────────────────────────────────────────────────────────────

async def start_feishu_stream() -> bool:
    """
    启动飞书长连接客户端。

    - 检查配置 → 获取文件锁 → 后台 Task 运行
    - 失败不影响主服务，返回 False
    - 多 worker 只有一个实例建立连接
    """
    global _stream_task, _is_primary_worker

    if not settings.feishu_enabled:
        logger.info("[Feishu] Long-connection disabled (FEISHU_ENABLED=false)")
        return False

    app_id = settings.feishu_app_id
    app_secret = settings.feishu_app_secret

    if not app_id or not app_secret:
        logger.warning("[Feishu] FEISHU_APP_ID or FEISHU_APP_SECRET not set")
        return False

    # 检查 lark-oapi 是否安装
    try:
        import lark_oapi  # noqa: F401
    except ImportError:
        logger.error("[Feishu] lark-oapi not installed. Run: pip install lark-oapi")
        return False

    # 容器启动时清理可能残留的过期锁（docker restart 会 SIGKILL 进程，来不及 release）
    # 只在第一个尝试的 worker 到来时做一次清理：检查锁内 PID 是否真实存活
    if os.path.exists(_LOCK_FILE_PATH):
        try:
            with open(_LOCK_FILE_PATH) as _f:
                _content = _f.read().strip()
            _pid_str, _, _start_str = _content.partition(":")
            _stale = True
            if _pid_str.isdigit():
                _pid = int(_pid_str)
                try:
                    os.kill(_pid, 0)
                    _cur_start = _get_proc_start_time(_pid)
                    if _cur_start and _start_str and abs(_cur_start - float(_start_str)) < 1.0:
                        _stale = False
                except OSError:
                    pass
            if _stale:
                os.remove(_LOCK_FILE_PATH)
                logger.info(f"[Feishu] Cleared stale lock on startup (was: {_content!r})")
        except Exception as _e:
            logger.warning(f"[Feishu] Startup lock cleanup error: {_e}")

    # 多 worker 互斥
    if not _try_acquire_lock():
        logger.info(
            f"[Feishu] Worker {os.getpid()} skipping stream init "
            f"(another worker already holds the lock)"
        )
        return False

    _is_primary_worker = True
    logger.info(f"[Feishu] Worker {os.getpid()} acquired lock, starting long-connection...")

    # 确保飞书域名不走代理
    _feishu_domains = ".feishu.cn,.larksuite.com"
    _no_proxy = os.environ.get("NO_PROXY", "")
    if "feishu.cn" not in _no_proxy:
        new_no_proxy = f"{_no_proxy},{_feishu_domains}" if _no_proxy else _feishu_domains
        os.environ["NO_PROXY"] = new_no_proxy
        os.environ["no_proxy"] = new_no_proxy
        logger.info(f"[Feishu] Added feishu domains to NO_PROXY")

    # 初始化 bot_handler 全局单例（同步，不需要线程池）
    try:
        from app.channels.feishu.bot_handler import _ensure_bot_handler
        _ensure_bot_handler()
    except Exception as e:
        logger.warning(f"[Feishu] BotHandler init warning: {e}")

    # 后台启动，不阻塞主服务
    from app.utils.background_task_manager import create_background_task
    _stream_task = create_background_task(
        _run_stream(app_id, app_secret),
        task_name="feishu_stream",
    )

    logger.info("[Feishu] Long-connection task started in background (non-blocking)")
    return True


async def stop_feishu_stream() -> None:
    """停止飞书长连接客户端"""
    global _stream_task, _is_primary_worker

    if _stream_task and not _stream_task.done():
        _stream_task.cancel()
        try:
            await _stream_task
        except asyncio.CancelledError:
            pass
        logger.info("[Feishu] Stream client stopped")

    _stream_task = None

    if _is_primary_worker:
        _release_lock()
        _is_primary_worker = False


def get_stream_status() -> dict:
    """获取长连接状态（供健康检查接口使用）"""
    return {
        "enabled": settings.feishu_enabled,
        "configured": bool(settings.feishu_app_id and settings.feishu_app_secret),
        "running": bool(_stream_task and not _stream_task.done()),
        "is_primary_worker": _is_primary_worker,
        "worker_pid": os.getpid() if _is_primary_worker else None,
        "app_id": (settings.feishu_app_id[:8] + "...") if settings.feishu_app_id else None,
    }
