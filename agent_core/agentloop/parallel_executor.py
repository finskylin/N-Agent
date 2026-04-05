"""
ParallelToolExecutor — 并行工具执行器

将 tool_calls 按 readonly 属性分为两组:
- readonly 组: asyncio.gather() 并行执行，受 Semaphore 限流
- write  组: 顺序执行（保留原有行为）

用法:
    executor = ParallelToolExecutor(
        skill_invoker=skill_invoker,
        discovery=discovery,
        enabled=True,
        max_concurrent=8,
    )
    readonly_group, write_group = executor.partition(tool_calls)
    results = await executor.execute_parallel(readonly_group, user_id, session_id)
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple
from loguru import logger

from .message_types import ToolCallRequest, ToolResult

# 内置只读工具硬编码兜底名单（注册表优先，此处作为后备）
_BUILTIN_READONLY: frozenset = frozenset({"read_file", "grep", "glob", "spawn_agent"})


class ParallelToolExecutor:
    """
    并行工具执行器 — opt-in 设计，默认不影响现有行为。

    判断只读逻辑优先级:
    1. 内置名单 (_BUILTIN_READONLY) 中的工具 → readonly
    2. SkillDiscovery 中 readonly=True 的 skill → readonly
    3. 其他 → 顺序执行（write 组）
    """

    def __init__(
        self,
        skill_invoker,        # SkillInvoker
        discovery,            # SkillDiscovery
        enabled: bool = True,
        max_concurrent: int = 8,
        per_tool_timeout: float = 60.0,
    ):
        self._invoker = skill_invoker
        self._discovery = discovery
        self._enabled = enabled
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._per_tool_timeout = per_tool_timeout

    def is_readonly(self, tool_name: str) -> bool:
        """判断工具是否只读（注册表 > SkillDiscovery > 硬编码名单）"""
        # 优先从 BuiltinToolRegistry 查询（注册表 readonly 属性权威）
        try:
            registry = getattr(self._invoker, "_tool_registry", None)
            if registry is not None:
                builtin = registry.get(tool_name)
                if builtin is not None:
                    return builtin.readonly
        except Exception:
            pass
        # 硬编码兜底
        if tool_name in _BUILTIN_READONLY:
            return True
        # 查 SkillDiscovery（支持 get / get_by_name 两种接口）
        try:
            get_fn = getattr(self._discovery, "get_by_name", None) or getattr(self._discovery, "get", None)
            if get_fn:
                meta = get_fn(tool_name)
                if meta is not None:
                    return getattr(meta, "readonly", False)
        except Exception:
            pass
        return False

    def partition(
        self,
        tool_calls: List[ToolCallRequest],
    ) -> Tuple[List[ToolCallRequest], List[ToolCallRequest]]:
        """
        将 tool_calls 分为只读组和写入组

        Returns:
            (readonly_group, write_group)
        """
        if not self._enabled:
            return [], list(tool_calls)

        readonly_group: List[ToolCallRequest] = []
        write_group: List[ToolCallRequest] = []

        for tc in tool_calls:
            if self.is_readonly(tc.name):
                readonly_group.append(tc)
            else:
                write_group.append(tc)

        if readonly_group:
            logger.info(
                f"[ParallelExecutor] Partitioned: "
                f"{len(readonly_group)} readonly, {len(write_group)} write"
            )

        return readonly_group, write_group

    async def execute_parallel(
        self,
        tool_calls: List[ToolCallRequest],
        user_id: str,
        session_id: str,
    ) -> List[ToolResult]:
        """
        asyncio.gather 并行执行 readonly 组

        - Semaphore 限流（max_concurrent）
        - 单个工具异常转为 ToolResult(is_error=True)，不影响其他工具
        """
        if not tool_calls:
            return []

        tasks = [
            self._execute_one(tc, user_id, session_id)
            for tc in tool_calls
        ]

        results: List[ToolResult] = await asyncio.gather(*tasks, return_exceptions=False)
        return results

    async def _execute_one(
        self,
        tc: ToolCallRequest,
        user_id: str,
        session_id: str,
    ) -> ToolResult:
        """执行单个工具，受 Semaphore 限流 + 单工具超时

        spawn_agent 不受 per_tool_timeout 限制（子代理需要多轮工具调用，耗时不可控）。
        """
        async with self._semaphore:
            try:
                # spawn_agent 子代理运行时间不可控，跳过 wait_for 超时限制
                coro = self._invoker.invoke(
                    skill_name=tc.name,
                    arguments=tc.arguments,
                    tool_call_id=tc.id,
                    user_id=user_id,
                    session_id=session_id,
                )
                if tc.name == "spawn_agent":
                    return await coro
                return await asyncio.wait_for(coro, timeout=self._per_tool_timeout)
            except asyncio.TimeoutError:
                import json
                logger.warning(
                    f"[ParallelExecutor] Tool '{tc.name}' timed out after "
                    f"{self._per_tool_timeout}s"
                )
                return ToolResult(
                    tool_call_id=tc.id,
                    name=tc.name,
                    content=json.dumps({
                        "error": f"Tool '{tc.name}' timed out after {self._per_tool_timeout}s",
                        "skill": tc.name,
                    }),
                    is_error=True,
                )
            except Exception as e:
                import json
                logger.error(
                    f"[ParallelExecutor] Tool '{tc.name}' failed: "
                    f"{type(e).__name__}: {e}"
                )
                return ToolResult(
                    tool_call_id=tc.id,
                    name=tc.name,
                    content=json.dumps({"error": str(e), "skill": tc.name}),
                    is_error=True,
                )


class _NullParallelExecutor:
    """
    空实现 — 未注入 ParallelToolExecutor 时使用。

    partition() 始终返回 ([], all_calls)，全部走顺序执行路径。
    """

    def partition(
        self,
        tool_calls: List[ToolCallRequest],
    ) -> Tuple[List[ToolCallRequest], List[ToolCallRequest]]:
        return [], list(tool_calls)

    async def execute_parallel(
        self,
        tool_calls: List[ToolCallRequest],
        user_id: str,
        session_id: str,
    ) -> List[ToolResult]:
        return []
