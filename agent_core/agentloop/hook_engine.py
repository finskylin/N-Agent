"""
HookEngine — 开放式 Hook/插件引擎

替换 Claude Agent SDK 的 HookMatcher 体系，提供进程内可编程的 Hook 挂载点。

特性:
- 随时注册/注销 hook（动态可变）
- 按优先级排序执行（数字小优先）
- 支持工具名过滤（tool_filter）
- 支持动态启用/禁用
- 支持插件打包（HookPlugin）
- 链式 context 修改（hook 可修改后续 hook 看到的数据）
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional
from loguru import logger


class HookPoint(str, Enum):
    """Hook 挂载点枚举"""
    PRE_TOOL_USE = "pre_tool_use"        # 工具执行前
    POST_TOOL_USE = "post_tool_use"      # 工具执行后（含完整 raw_data！）
    PRE_LLM_CALL = "pre_llm_call"       # LLM 调用前（可修改 messages/tools）
    POST_LLM_CALL = "post_llm_call"     # LLM 调用后（可处理 response）
    ON_TEXT_DELTA = "on_text_delta"      # 文本流式输出时
    ON_LOOP_START = "on_loop_start"      # 循环开始
    ON_LOOP_END = "on_loop_end"          # 循环结束（替代 Stop hook）
    ON_ERROR = "on_error"                # 错误发生时
    # Phase 4/3/5 扩展点
    ON_PERMISSION_CHECK = "on_permission_check"   # 权限检查（Phase 4）
    ON_CONTEXT_COMPACT = "on_context_compact"      # 上下文压缩（Phase 3）
    ON_OUTPUT_VALIDATE = "on_output_validate"      # 结构化输出校验（Phase 5）
    # SubAgent 生命周期
    ON_SUBAGENT_START = "on_subagent_start"        # 子代理启动（含 agent_id/parent_agent_id/task）
    ON_SUBAGENT_END = "on_subagent_end"            # 子代理结束（含 agent_id/result/tools_used）


@dataclass
class HookRegistration:
    """Hook 注册条目"""
    name: str                           # hook 唯一名称
    hook_point: HookPoint               # 挂载点
    handler: Callable                   # async 函数
    priority: int = 100                 # 优先级（数字小优先执行）
    tool_filter: Optional[str] = None   # 工具名过滤（None = 匹配所有）
    enabled: bool = True                # 动态开关


class HookEngine:
    """
    开放式 Hook/插件引擎

    上下文约定（fire 方法的 context 参数）:
    所有 hook context 都会自动携带 agent_id 字段，用于区分父/子代理。

    - PRE_TOOL_USE:      {agent_id, tool_name, tool_input, request}
    - POST_TOOL_USE:     {agent_id, tool_name, tool_input, tool_result, duration_ms, request}
    - PRE_LLM_CALL:      {agent_id, messages, tools, model}
    - POST_LLM_CALL:     {agent_id, response: LLMResponse, messages}
    - ON_TEXT_DELTA:     {agent_id, delta, accumulated_text}
    - ON_LOOP_START:     {agent_id, request, system_prompt}
    - ON_LOOP_END:       {agent_id, final_content, tools_used, total_iterations, request}
    - ON_ERROR:          {agent_id, error, context}
    - ON_SUBAGENT_START: {agent_id(子), parent_agent_id, task, tools_hint, depth}
    - ON_SUBAGENT_END:   {agent_id(子), parent_agent_id, task, result, tools_used, iterations, depth}
    """

    def __init__(self, agent_id: Optional[str] = None):
        self._hooks: Dict[HookPoint, List[HookRegistration]] = {
            p: [] for p in HookPoint
        }
        # name -> HookRegistration 的快速查找
        self._name_index: Dict[str, HookRegistration] = {}
        # 该 HookEngine 所属的 agent_id（由 AgentLoop 注入）
        self.agent_id: Optional[str] = agent_id

    # ──────────────── 注册管理 ────────────────

    def register(
        self,
        name: str,
        hook_point: HookPoint,
        handler: Callable,
        priority: int = 100,
        tool_filter: Optional[str] = None,
    ) -> None:
        """注册一个 hook"""
        if name in self._name_index:
            logger.warning(f"[HookEngine] Overwriting existing hook: '{name}'")
            self.unregister(name)

        reg = HookRegistration(
            name=name,
            hook_point=hook_point,
            handler=handler,
            priority=priority,
            tool_filter=tool_filter,
        )
        self._hooks[hook_point].append(reg)
        # 按优先级排序（稳定排序保留注册顺序）
        self._hooks[hook_point].sort(key=lambda r: r.priority)
        self._name_index[name] = reg
        logger.debug(
            f"[HookEngine] Registered hook '{name}' @ {hook_point.value} "
            f"(priority={priority}, filter={tool_filter})"
        )

    def unregister(self, name: str) -> None:
        """注销一个 hook"""
        reg = self._name_index.pop(name, None)
        if reg:
            try:
                self._hooks[reg.hook_point].remove(reg)
            except ValueError:
                pass
            logger.debug(f"[HookEngine] Unregistered hook '{name}'")

    def register_plugin(self, plugin: "HookPlugin") -> None:
        """注册插件（一组 hook 的打包）"""
        for reg in plugin.get_hooks():
            self.register(
                name=f"{plugin.name}::{reg.name}",
                hook_point=reg.hook_point,
                handler=reg.handler,
                priority=reg.priority,
                tool_filter=reg.tool_filter,
            )
        logger.info(
            f"[HookEngine] Plugin '{plugin.name}' registered "
            f"({len(plugin.get_hooks())} hooks)"
        )

    def enable(self, name: str) -> None:
        """启用指定 hook"""
        reg = self._name_index.get(name)
        if reg:
            reg.enabled = True

    def disable(self, name: str) -> None:
        """禁用指定 hook"""
        reg = self._name_index.get(name)
        if reg:
            reg.enabled = False

    # ──────────────── 触发 ────────────────

    async def fire(
        self,
        hook_point: HookPoint,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        触发指定挂载点的所有 hook（按优先级顺序）

        - Hook 可修改并返回新 context（链式处理）
        - 单个 hook 失败不影响后续 hook
        - 返回最终 context（可能被 hook 修改过）
        """
        current_context = dict(context)
        tool_name = context.get("tool_name", "")

        for reg in self._hooks[hook_point]:
            if not reg.enabled:
                continue

            # 工具名过滤
            if reg.tool_filter and tool_name and reg.tool_filter != tool_name:
                continue

            try:
                result = await reg.handler(current_context)
                # 如果 handler 返回了 dict，合并到 context 中
                if isinstance(result, dict):
                    current_context.update(result)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    f"[HookEngine] Hook '{reg.name}' @ {hook_point.value} failed: "
                    f"{type(e).__name__}: {e}"
                )

        return current_context

    def list_hooks(self) -> List[Dict[str, Any]]:
        """列出所有已注册 hook（调试用）"""
        result = []
        for point, regs in self._hooks.items():
            for reg in regs:
                result.append({
                    "name": reg.name,
                    "point": point.value,
                    "priority": reg.priority,
                    "enabled": reg.enabled,
                    "tool_filter": reg.tool_filter,
                })
        return result


class HookPlugin:
    """
    插件基类 — 一组相关 hook 的打包

    子类示例:

    class MyPlugin(HookPlugin):
        name = "my_plugin"

        async def on_post_tool_use(self, ctx: dict) -> dict:
            tool_result = ctx.get("tool_result")
            # 访问 tool_result.raw_data 获取完整数据
            return ctx

        def get_hooks(self) -> list:
            return [
                HookRegistration(
                    "post_hook",
                    HookPoint.POST_TOOL_USE,
                    self.on_post_tool_use,
                    priority=50,
                ),
            ]
    """
    name: str = "base_plugin"

    def get_hooks(self) -> List[HookRegistration]:
        raise NotImplementedError


class LegacyHookPlugin(HookPlugin):
    """
    桥接已有 HookManager 的内置 hook 函数

    将 hook_manager._make_pre_tool_hook / _make_post_tool_hook / _make_stop_hook
    封装为 HookPlugin，注册到 HookEngine。

    这样无需修改已有 HookManager，只需用 LegacyHookPlugin 包装一次。
    """
    name = "legacy_hooks"

    def __init__(self, hook_manager, event_bridge, request, **kwargs):
        self._hook_manager = hook_manager
        self._event_bridge = event_bridge
        self._request = request
        self._kwargs = kwargs

        # 提取已有 hook 函数
        mentioned_skills = kwargs.get("mentioned_skills")
        data_collector = kwargs.get("data_collector")
        tracker = kwargs.get("tracker")
        reflection = kwargs.get("reflection")
        accumulated_text_ref = kwargs.get("accumulated_text_ref")
        request_start_time = kwargs.get("request_start_time")

        self._pre_fn = hook_manager._make_pre_tool_hook(
            event_bridge, request, mentioned_skills, data_collector,
            tracker=tracker,
        )
        self._post_fn = hook_manager._make_post_tool_hook(
            event_bridge, request,
            skip_ui_rendering=kwargs.get("skip_ui_rendering", False),
            dingtalk_render=kwargs.get("dingtalk_render", False),
            data_collector=data_collector,
            scene_context=kwargs.get("scene_context"),
            existing_tabs=kwargs.get("existing_tabs", []),
            tracker=tracker,
        )
        self._stop_fn = hook_manager._make_stop_hook(
            event_bridge, request,
            scene_context=kwargs.get("scene_context"),
            existing_tabs=kwargs.get("existing_tabs", []),
            reflection=reflection,
            tracker=tracker,
            accumulated_text_ref=accumulated_text_ref,
            data_collector=data_collector,
            request_start_time=request_start_time,
        )

    async def _pre_tool_handler(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        """PRE_TOOL_USE 桥接：转为旧版 hook 签名"""
        tool_name = ctx.get("tool_name", "")
        tool_input = ctx.get("tool_input", {})
        # 旧版签名: async def pre_tool_use(tool_input, tool_name, hook_context)
        # tool_input 包含 tool_name 和 tool_input 两个字段（按旧版约定）
        wrapped = {"tool_name": tool_name, "tool_input": tool_input}
        try:
            result = await self._pre_fn(wrapped, tool_name, None)
            if isinstance(result, dict):
                ctx["_pre_result"] = result
        except Exception as e:
            logger.warning(f"[LegacyHookPlugin] pre_tool_use failed: {e}")
        return ctx

    async def _post_tool_handler(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        """POST_TOOL_USE 桥接：转为旧版 hook 签名，并注入完整 raw_data"""
        tool_name = ctx.get("tool_name", "")
        tool_result = ctx.get("tool_result")  # ToolResult 对象（含 raw_data！）

        # 构造旧版 tool_output，注入 raw_data
        if tool_result is not None:
            import json
            tool_output = {
                "tool_name": tool_name,
                "tool_response": tool_result.content,
                "content": tool_result.content,
                # 新增：raw_data 直接传递（核心修复！）
                "raw_data": tool_result.raw_data,
                "is_error": tool_result.is_error,
                "duration_ms": tool_result.duration_ms,
            }
        else:
            tool_output = {"tool_name": tool_name, "tool_response": ""}

        try:
            result = await self._post_fn(tool_output, tool_name, None)
            if isinstance(result, dict):
                ctx["_post_result"] = result
        except Exception as e:
            logger.warning(f"[LegacyHookPlugin] post_tool_use failed: {e}")
        return ctx

    async def _loop_end_handler(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        """ON_LOOP_END 桥接：触发旧版 Stop hook，透传 loop_normal_exit"""
        try:
            await self._stop_fn(
                {"loop_normal_exit": ctx.get("loop_normal_exit", True)},
                None, None,
            )
        except Exception as e:
            logger.warning(f"[LegacyHookPlugin] stop hook failed: {e}")
        return ctx

    def get_hooks(self) -> List[HookRegistration]:
        return [
            HookRegistration(
                name="pre_tool_use",
                hook_point=HookPoint.PRE_TOOL_USE,
                handler=self._pre_tool_handler,
                priority=10,
            ),
            HookRegistration(
                name="post_tool_use",
                hook_point=HookPoint.POST_TOOL_USE,
                handler=self._post_tool_handler,
                priority=10,
            ),
            HookRegistration(
                name="loop_end",
                hook_point=HookPoint.ON_LOOP_END,
                handler=self._loop_end_handler,
                priority=10,
            ),
        ]
