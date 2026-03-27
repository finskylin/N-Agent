"""
Langfuse 容错客户端 — Langfuse 不可用时自动降级，绝不影响业务流程

适配 Langfuse SDK v3 API:
- start_span() 创建根 Span（自动关联 Trace）
- start_generation() / start_observation() 创建子节点
- update_trace() 更新 Trace 级元数据（user_id, session_id 等）
- update() 更新当前节点
- end() 结束节点

三层防护:
1. LangfuseManager._lazy_init(): 延迟初始化 + try/except
2. LangfuseManager.trace(): try/except 返回 NoOpSpan
3. 各注入点外层 try/except

环境变量:
- LANGFUSE_ENABLED=true  开启（默认 false，不开启不消耗任何资源）
- LANGFUSE_PUBLIC_KEY
- LANGFUSE_SECRET_KEY
- LANGFUSE_HOST
- LANGFUSE_INIT_TIMEOUT  初始化超时秒数（默认 5）
"""
import os
import threading
from loguru import logger


class _NoOpSpan:
    """空操作 Span，Langfuse 不可用时的替代品（v3 API 兼容）"""
    id = "noop"
    trace_id = "noop"

    def end(self, **kwargs): return self
    def update(self, **kwargs): return self
    def update_trace(self, **kwargs): return self
    def score(self, **kwargs): pass
    def score_trace(self, **kwargs): pass
    def start_span(self, **kwargs): return _NoOpSpan()
    def start_generation(self, **kwargs): return _NoOpSpan()
    def start_observation(self, **kwargs): return _NoOpSpan()
    def start_as_current_span(self, **kwargs): return _NoOpSpan()
    def start_as_current_generation(self, **kwargs): return _NoOpSpan()
    def start_as_current_observation(self, **kwargs): return _NoOpSpan()
    def create_event(self, **kwargs): pass

    # v2 兼容别名
    def generation(self, **kwargs): return _NoOpSpan()
    def span(self, **kwargs): return _NoOpSpan()


class LangfuseManager:
    """
    Langfuse 容错管理器（适配 SDK v3）

    - Langfuse 服务未启动 → 自动降级，不影响业务
    - SDK 调用异常 → 静默捕获，记录 warning 日志
    - 环境变量 LANGFUSE_ENABLED=false → 完全关闭（零开销）
    """

    def __init__(self):
        self._client = None
        self._enabled = os.getenv("LANGFUSE_ENABLED", "false").lower() == "true"
        self._init_attempted = False

    def _lazy_init(self):
        """延迟初始化：首次调用时才尝试连接 Langfuse"""
        if self._init_attempted:
            return
        self._init_attempted = True

        if not self._enabled:
            logger.info("[Langfuse] Disabled via LANGFUSE_ENABLED=false")
            return

        if self._client is not None:
            return

        async def _do_init():
            try:
                from langfuse import Langfuse
                timeout_sec = int(os.getenv("LANGFUSE_INIT_TIMEOUT", "5"))
                logger.info(f"[Langfuse] Initializing with {timeout_sec}s timeout...")

                def _sync_init():
                    try:
                        self._client = Langfuse(
                            public_key=os.getenv("LANGFUSE_PUBLIC_KEY", "pk-sthg-agent"),
                            secret_key=os.getenv("LANGFUSE_SECRET_KEY", "sk-sthg-agent-secret"),
                            host=os.getenv("LANGFUSE_HOST", "http://langfuse-web:3000"),
                        )
                        logger.info(
                            f"[Langfuse] Initialized: "
                            f"{os.getenv('LANGFUSE_HOST', 'http://langfuse-web:3000')}"
                        )
                    except Exception as e:
                        logger.warning(
                            f"[Langfuse] Init failed (timeout or service down), "
                            f"tracing disabled: {e}"
                        )
                        self._client = None

                init_thread = threading.Thread(target=_sync_init, daemon=True)
                init_thread.start()
                init_thread.join(timeout=timeout_sec)

                if self._client is None:
                    logger.warning(
                        f"[Langfuse] Init timeout after {timeout_sec}s, using noop mode"
                    )
                else:
                    logger.info("[Langfuse] Init completed successfully")

            except ImportError:
                logger.warning("[Langfuse] langfuse package not installed, tracing disabled")
                self._client = None
            except Exception as e:
                logger.warning(
                    f"[Langfuse] Init failed (unexpected error), tracing disabled: {e}"
                )
                self._client = None

        try:
            from agent_core.background_task_manager import create_background_task
            create_background_task(_do_init(), task_name="langfuse_init")
        except Exception:
            import asyncio
            try:
                asyncio.get_event_loop().create_task(_do_init())
            except Exception:
                pass

    def trace(self, **kwargs):
        """
        创建根 Span（v3: start_span），失败时返回 NoOp。

        接受参数:
        - name: str — span 名称
        - input: Any — 输入数据
        - metadata: Any — 元数据
        - user_id / session_id — 设置到 trace 级别
        """
        self._lazy_init()
        if not self._client:
            return _NoOpSpan()
        try:
            span_kwargs = {}
            for k in ("name", "input", "metadata"):
                if k in kwargs:
                    span_kwargs[k] = kwargs[k]
            root_span = self._client.start_span(**span_kwargs)

            trace_kwargs = {}
            for k in ("user_id", "session_id"):
                if k in kwargs:
                    trace_kwargs[k] = kwargs[k]
            if trace_kwargs:
                root_span.update_trace(**trace_kwargs)
            return root_span
        except Exception as e:
            logger.warning(f"[Langfuse] trace creation failed: {e}")
            return _NoOpSpan()

    def flush(self):
        """刷新缓冲区，失败时静默"""
        if self._client:
            try:
                self._client.flush()
            except Exception as e:
                logger.warning(f"[Langfuse] flush failed: {e}")

    @property
    def is_available(self) -> bool:
        self._lazy_init()
        return self._client is not None


# 全局单例
langfuse = LangfuseManager()
