"""
GAIA 评测分发器

通过 httpx.AsyncClient 向 /api/v1/chat/v4/send 发送请求，
使用 asyncio.Semaphore 控制并发。
"""
import asyncio
import hashlib
import time
from typing import Optional

import httpx
from loguru import logger

from app.benchmark.models import GaiaQuestion


class GaiaDispatcher:
    """向 Agent 服务发送 GAIA 题目"""

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        chat_endpoint: str = "/api/v1/chat/v4/send",
        max_concurrency: int = 3,
        timeout: int = 300,
        retry_count: int = 1,
        retry_delay: float = 5.0,
        benchmark_user_id: int = 99999,
    ):
        self.base_url = base_url.rstrip("/")
        self.chat_endpoint = chat_endpoint
        self.max_concurrency = max_concurrency
        self.timeout = timeout
        self.retry_count = retry_count
        self.retry_delay = retry_delay
        self.benchmark_user_id = benchmark_user_id
        self._semaphore: Optional[asyncio.Semaphore] = None

    @property
    def send_url(self) -> str:
        return f"{self.base_url}{self.chat_endpoint}"

    def _make_session_id(self, task_id: str) -> int:
        """每题生成唯一 session_id，防止上下文泄露"""
        h = hashlib.md5(task_id.encode()).hexdigest()
        return int(h[:8], 16)

    async def dispatch(
        self,
        question: GaiaQuestion,
        formatted_message: str,
    ) -> dict:
        """
        发送单道题给 Agent 服务。

        Args:
            question: GAIA 题目
            formatted_message: 已格式化的消息（含指令 + 题目）

        Returns:
            dict with keys: text, latency_ms, error
        """
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrency)

        async with self._semaphore:
            return await self._dispatch_with_retry(question, formatted_message)

    async def _dispatch_with_retry(
        self,
        question: GaiaQuestion,
        formatted_message: str,
    ) -> dict:
        """带重试的分发"""
        last_error = None

        for attempt in range(1 + self.retry_count):
            if attempt > 0:
                logger.info(
                    f"[GAIA Dispatcher] Retry {attempt}/{self.retry_count} "
                    f"for task {question.task_id}"
                )
                await asyncio.sleep(self.retry_delay)

            try:
                return await self._do_dispatch(question, formatted_message)
            except Exception as e:
                last_error = str(e)
                logger.warning(
                    f"[GAIA Dispatcher] Attempt {attempt + 1} failed "
                    f"for task {question.task_id}: {e}"
                )

        return {
            "text": None,
            "latency_ms": 0.0,
            "error": f"All {1 + self.retry_count} attempts failed: {last_error}",
        }

    async def _do_dispatch(
        self,
        question: GaiaQuestion,
        formatted_message: str,
    ) -> dict:
        """执行单次请求"""
        payload = {
            "message": formatted_message,
            "session_id": self._make_session_id(question.task_id),
            "user_id": self.benchmark_user_id,
            "render_mode": "text_only",
            "auto_approve_plan": True,
        }

        start = time.monotonic()
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout)) as client:
            resp = await client.post(self.send_url, json=payload)
            resp.raise_for_status()

        elapsed_ms = (time.monotonic() - start) * 1000
        data = resp.json()

        return {
            "text": data.get("text", ""),
            "latency_ms": elapsed_ms,
            "error": None,
        }
