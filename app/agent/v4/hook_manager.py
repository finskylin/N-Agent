"""
Hook Manager -- 管理 SDK Hooks，注入自定义逻辑

职责:
1. 构建 PreToolUse / PostToolUse / Stop Hooks 配置
2. PreToolUse: 推送 tool_call 事件，参数增强
3. PostToolUse: 推送 tool_done 事件，UI 组件渲染决策（受 render_mode 控制），结果缓存
4. Stop: 会话持久化准备

V4 解耦:
- UI 组件选择使用 V4UISelector（不依赖 V3 的 UIResourceBinder）
- 结果缓存使用简单的 dict+timestamp（不依赖 V3 的 SessionCacheItem/ContextReuseStrategy）
- 显示信息从 SkillDiscovery 获取（不依赖 V3 的 ConfigLoader）
"""
import os
import time
import asyncio
from typing import Dict, Any, Optional, List
from loguru import logger

from .ui_selector import V4UISelector
from agent_core.skill_discovery import SkillDiscovery


# V4 简单缓存条目
class _CacheEntry:
    __slots__ = ("skill_name", "data", "timestamp", "ts_code", "ttl")

    def __init__(self, skill_name: str, data: Any, ts_code: str = "", ttl: int = 300):
        self.skill_name = skill_name
        self.data = data
        self.timestamp = time.time()
        self.ts_code = ts_code
        self.ttl = ttl

    def is_valid(self) -> bool:
        return (time.time() - self.timestamp) < self.ttl


# 默认 TTL 配置（秒）
_DEFAULT_TTL_CONFIG = {
    "realtime_quote": 60,
    "stock_query": 300,
    "historical_data": 3600,
    "financial_report": 86400,
    "rating": 3600,
    "prediction": 3600,
    "valuation_analysis": 3600,
    "sentiment_analysis": 1800,
    "money_flow": 300,
    "technical_indicators": 600,
    "backtest": 7200,
}


class HookManager:
    """
    管理 Claude Agent SDK 的 Hooks 配置

    Hook 注入点:
    - PreToolUse:  工具执行前 -> 推送 tool_call、参数增强
    - PostToolUse: 工具执行后 -> 推送 tool_done、UI 组件渲染、结果缓存
    - Stop:        执行停止时 -> 会话持久化准备
    """

    def __init__(self, ui_selector: V4UISelector, discovery: SkillDiscovery, persistence=None):
        self._ui_selector = ui_selector
        self._discovery = discovery
        self._persistence = persistence  # V4PersistenceLayer 实例
        # V4 自建缓存
        self._cache: Dict[str, _CacheEntry] = {}

        # Knowledge Engine (agent_core) 轨迹采集
        self._knowledge_tracker = None
        self._knowledge_store = None
        self._knowledge_guard = None

        # 预测提取器（Stop Hook 异步触发）
        self._prediction_extractor = None

        # CLAW 扩展: 新增 3 个 hook 列表
        from typing import Callable
        self._session_start_hooks: List[Callable] = []
        self._session_end_hooks: List[Callable] = []
        self._message_sending_hooks: List[Callable] = []

    def set_knowledge_tracker(self, tracker, store=None, guard=None):
        """设置 Knowledge Engine 轨迹采集器引用"""
        self._knowledge_tracker = tracker
        self._knowledge_store = store
        self._knowledge_guard = guard

    def set_prediction_extractor(self, extractor):
        """设置预测提取器引用（Stop Hook 异步触发）"""
        self._prediction_extractor = extractor

    def build_hooks(
        self, event_bridge, request, mentioned_skills=None, data_collector=None,
        scene_info: Optional[Dict[str, Any]] = None,
        # Agentic Loop 增强参数（默认 None，向后兼容）
        reflection=None,
        tracker=None,
        accumulated_text_ref=None,
        request_start_time=None,
    ) -> Dict[str, list]:
        """
        构建 Hooks 字典供 ClaudeAgentOptions 使用

        Args:
            event_bridge: EventBridge 实例
            request: V4AgentRequest 实例
            mentioned_skills: @skill 指定的技能名列表（用于工具门控）
            data_collector: DataCollector 实例（用于收集置信度评估数据）
            reflection: AgenticReflection 实例（可选，反思评估器）
            tracker: ToolExecutionTracker 实例（可选，工具执行跟踪器）
            accumulated_text_ref: list 引用（可选，累积文本用于反思）
            request_start_time: float（可选，请求开始时间用于总耗时熔断）

        Returns:
            {"PreToolUse": [...], "PostToolUse": [...], "Stop": [...]}
        """
        # 尽早注入用户 query 到 knowledge_tracker（确保 finalize 时 query 有值）
        if self._knowledge_tracker:
            _msg = getattr(request, "message", "") or ""
            if _msg:
                self._knowledge_tracker.set_query(_msg)

        render_mode = getattr(request, "render_mode", "auto")
        channel = getattr(request, "channel", "web")
        # 钉钉渠道：不跳过 UI 决策，而是改为 component_for_render 事件
        is_dingtalk = channel == "dingtalk"
        skip_ui = (render_mode == "text_only") and (not is_dingtalk)

        # 场景上下文（Scene Tab 系统）
        scene_context = None
        existing_tabs: List[Dict[str, Any]] = []
        try:
            from .scene_context import SceneContext
            user_scene = getattr(request, "scene_type", None)
            scene_context = SceneContext(user_specified_scene=user_scene)

            # Phase 0 场景预设（消除独立 LLM 调用）
            if scene_info and scene_info.get("scene_type") and not user_scene:
                scene_context.preset_from_phase0(scene_info)

            # 加载 session 已有 Tab
            existing_tabs = self._load_session_tabs(getattr(request, "session_id", ""))
        except Exception as sc_err:
            logger.debug(f"[HookManager] SceneContext init skipped: {sc_err}")

        # 共享列表：收集所有 PostToolUse 产生的后台渲染任务，Stop hook 等待完成
        _bg_render_tasks: List = []

        return {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        self._make_pre_tool_hook(
                            event_bridge, request, mentioned_skills, data_collector,
                            tracker=tracker,
                        )
                    ],
                    "timeout": 30.0,
                }
            ],
            "PostToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        self._make_post_tool_hook(
                            event_bridge,
                            request,
                            skip_ui_rendering=skip_ui,
                            dingtalk_render=is_dingtalk,
                            data_collector=data_collector,
                            scene_context=scene_context,
                            existing_tabs=existing_tabs,
                            tracker=tracker,
                            bg_render_tasks=_bg_render_tasks,
                        )
                    ],
                    "timeout": 60.0,
                }
            ],
            "Stop": [
                {
                    "matcher": None,
                    "hooks": [
                        self._make_stop_hook(
                            event_bridge, request,
                            scene_context=scene_context,
                            existing_tabs=existing_tabs,
                            reflection=reflection,
                            tracker=tracker,
                            accumulated_text_ref=accumulated_text_ref,
                            data_collector=data_collector,
                            request_start_time=request_start_time,
                            bg_render_tasks=_bg_render_tasks,
                        )
                    ],
                    "timeout": 60.0,
                }
            ],
        }

    def _make_pre_tool_hook(self, event_bridge, request, mentioned_skills=None, data_collector=None,
                            tracker=None):
        """PreToolUse Hook 工厂"""

        # 兜底确认消息：LLM 未调用 send_message 时，第一个实质工具触发前自动注入
        _ack_sent = [False]  # 用列表包装，供闭包修改

        async def pre_tool_use(
            tool_input: Dict[str, Any],
            tool_name: Optional[str],
            hook_context,
        ) -> Dict[str, Any]:
            # 从 tool_input 中提取实际工具名
            # SDK hook 的 tool_name 参数是 tool_use_id (call_xxx)
            # 实际工具名在 tool_input["tool_name"] 中，格式可能是:
            #   - 内置工具: "WebSearch", "Read" 等
            #   - MCP 工具: "mcp__server_name__tool_name"
            raw_tool_name = tool_input.get("tool_name", tool_name)
            # 提取 MCP 工具的短名称
            if raw_tool_name and raw_tool_name.startswith("mcp__"):
                parts = raw_tool_name.split("__", 2)
                actual_tool_name = parts[2] if len(parts) >= 3 else raw_tool_name
            else:
                actual_tool_name = raw_tool_name

            # [Agentic Loop] 记录工具开始执行事实
            if tracker:
                tracker.on_tool_start(actual_tool_name, tool_input.get("tool_input", tool_input))

            # 提取工具输入参数用于日志
            tool_params = tool_input.get("tool_input", tool_input)
            # 对于 web_search，特别记录 mode/task_type/intelligent 参数
            mode_info = ""
            if actual_tool_name == "web_search" and isinstance(tool_params, dict):
                task_type = tool_params.get("task_type", "")
                intelligent = tool_params.get("intelligent", False)
                query = tool_params.get("query", "")
                mode_info = f", task_type={task_type}, intelligent={intelligent}, query={query[:50]}"

                # V4 Fallback: 记录 web_search 模式信息到 data_collector
                if data_collector:
                    data_collector.last_web_search_mode = task_type or "research"
                    data_collector.last_web_search_query = query
                    if intelligent:
                        data_collector.used_intelligent_mode = True

            logger.info(
                f"[Hook:PreToolUse] tool_use_id={tool_name}, "
                f"actual_tool={actual_tool_name}, raw={raw_tool_name}{mode_info}"
            )

            # 0. @skill 工具门控: 阻止非指定工具
            if mentioned_skills and actual_tool_name not in mentioned_skills:
                # 允许 native 基础工具通过（Bash/Skill 是 native skill 的执行载体）
                if actual_tool_name not in ("Bash", "Skill"):
                    logger.warning(
                        f"[Hook:PreToolUse] BLOCKED tool '{actual_tool_name}' — "
                        f"@skill only allows: {mentioned_skills}"
                    )
                    return {
                        "decision": "block",
                        "reason": f"用户通过 @mention 只允许调用 {mentioned_skills}，"
                                  f"请直接调用 {mentioned_skills[0]} 工具。",
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": f"用户通过 @mention 只允许调用 {mentioned_skills}，"
                                                        f"请直接调用 {mentioned_skills[0]} 工具。",
                        },
                    }

            # 0.5 bash 调用 send_message 拦截: LLM 通过 bash 调用 send_message.py 时，
            #     脚本本身只做参数验证，实际发送需要推 EventBridge
            if actual_tool_name == "bash":
                _bash_cmd = ""
                _inner = tool_input.get("tool_input", tool_input)
                if isinstance(_inner, dict):
                    _bash_cmd = _inner.get("command", "")
                elif isinstance(_inner, str):
                    _bash_cmd = _inner
                if "send_message" in _bash_cmd and "send_message.py" in _bash_cmd:
                    import re as _re_sm
                    # 从 bash 命令中提取 JSON 参数（echo '...' | python3 send_message.py）
                    _json_match = _re_sm.search(r"echo\s+'(\{[^']+\})'", _bash_cmd)
                    if not _json_match:
                        _json_match = _re_sm.search(r'echo\s+"(\{[^"]+\})"', _bash_cmd)
                    if _json_match:
                        import json as _json_sm
                        try:
                            _sm_params = _json_sm.loads(_json_match.group(1))
                            _sm_content = _sm_params.get("content", "").strip()
                            _sm_type = _sm_params.get("msg_type", "text")
                            _sm_title = _sm_params.get("title", "分析进展")
                            if _sm_content:
                                _params = request.params if hasattr(request, "params") and isinstance(request.params, dict) else {}
                                event_bridge.push_event({
                                    "event": "send_message",
                                    "data": {
                                        "content": _sm_content,
                                        "msg_type": _sm_type,
                                        "title": _sm_title,
                                        "channel": getattr(request, "channel", "web"),
                                        "conversation_id": _params.get("dingtalk_conversation_id", ""),
                                        "conversation_type": _params.get("dingtalk_conversation_type", ""),
                                        "sender_nick": _params.get("dingtalk_sender", ""),
                                        "sender_staff_id": _params.get("dingtalk_staff_id", "") or _params.get("dingtalk_sender_staff_id", ""),
                                    },
                                })
                                logger.info(
                                    f"[Hook:PreToolUse] send_message via bash: EventBridge pushed, "
                                    f"type={_sm_type}, len={len(_sm_content)}"
                                )
                        except Exception as _sm_err:
                            logger.warning(f"[Hook:PreToolUse] send_message bash parse failed: {_sm_err}")

            # 1. 推送 tool_call 事件
            event_bridge.push_event(
                {"event": "tool_call", "data": {"name": actual_tool_name, "input": tool_input.get("tool_input", tool_input)}}
            )

            # 2. 参数增强: 注入上下文中的 ts_code
            enhanced_input = dict(tool_input)
            req_ts_code = getattr(request, "ts_code", None)
            if req_ts_code and "ts_code" not in enhanced_input:
                enhanced_input["ts_code"] = req_ts_code

            # 3. geo_search 参数增强: 注入用户原始查询（Phase0 改写后的完整查询）
            #    LLM 拆分多步调用时会丢失军事/基础设施等语义关键词,
            #    geo_search 需要原始查询来推断 nearby_type
            if actual_tool_name == "geo_search":
                inner = enhanced_input.get("tool_input", {})
                logger.info(f"[Hook:PreToolUse] geo_search params: {inner}")
                if isinstance(inner, dict) and "_original_query" not in inner:
                    user_msg = getattr(request, "message", None)
                    if user_msg:
                        inner = dict(inner)
                        inner["_original_query"] = user_msg
                        enhanced_input["tool_input"] = inner
                        logger.info(
                            f"[Hook:PreToolUse] Injected _original_query for geo_search: "
                            f"'{user_msg[:60]}'"
                        )

            # 4. Quality-driven 参数增强
            quality_focus = getattr(data_collector, "quality_focus", None) if data_collector else None
            if quality_focus and isinstance(tool_params, dict):
                inner = enhanced_input.get("tool_input", enhanced_input)
                if isinstance(inner, dict):
                    modified = dict(inner)
                    changed = False

                    if actual_tool_name == "web_search":
                        # timeliness >= 0.8 → 注入时间偏好
                        if quality_focus.get("timeliness", 0) >= 0.8:
                            if modified.get("task_type") in (None, "", "research"):
                                modified["task_type"] = "news"
                                changed = True
                        # coverage >= 0.8 → 增加搜索数量
                        if quality_focus.get("coverage", 0) >= 0.8:
                            modified["max_results"] = max(modified.get("max_results", 5), 10)
                            changed = True

                    # 通用: 注入 _quality_focus 元数据（让 Skill 内部自行决策）
                    modified["_quality_focus"] = quality_focus
                    changed = True

                    # Sidecar 文件透传 search_time_range（MCP 子进程拿不到 tool_input 中的额外字段）
                    try:
                        _str = quality_focus.get("search_time_range", "none")
                        _sidecar_path = "/tmp/_search_time_range.txt"
                        if _str and _str != "none":
                            with open(_sidecar_path, "w") as _f:
                                _f.write(_str)
                            logger.info(f"[Hook:PreToolUse] Written search_time_range={_str}")
                        else:
                            # 清理旧文件，避免下次误读
                            if os.path.exists(_sidecar_path):
                                os.remove(_sidecar_path)
                    except Exception:
                        pass

                    if changed:
                        enhanced_input["tool_input"] = modified
                        logger.info(
                            f"[Hook:PreToolUse] Quality-driven enhancement for {actual_tool_name}: "
                            f"qf={quality_focus}"
                        )

            if enhanced_input != tool_input:
                # updatedInput 只传实际的工具参数（不带外层包装）
                actual_params = enhanced_input.get("tool_input", enhanced_input)
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                        "updatedInput": actual_params,
                    },
                }
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                },
            }

        return pre_tool_use

    def _make_post_tool_hook(
        self, event_bridge, request, skip_ui_rendering: bool = False,
        dingtalk_render: bool = False, data_collector=None,
        scene_context=None, existing_tabs: Optional[List[Dict[str, Any]]] = None,
        tracker=None, bg_render_tasks: Optional[List] = None,
    ):
        """PostToolUse Hook 工厂"""

        async def post_tool_use(
            tool_output: Dict[str, Any],
            tool_name: Optional[str],
            hook_context,
        ) -> Dict[str, Any]:
            # 提取实际工具名（与 pre_tool_use 一致）
            raw_tool_name = tool_output.get("tool_name", tool_name)
            if raw_tool_name and raw_tool_name.startswith("mcp__"):
                parts = raw_tool_name.split("__", 2)
                actual_tool_name = parts[2] if len(parts) >= 3 else raw_tool_name
            else:
                actual_tool_name = raw_tool_name

            # [Agentic Loop] 记录工具执行完成事实
            if tracker:
                tool_response = tool_output.get("tool_response", tool_output.get("content", ""))
                tracker.on_tool_end(actual_tool_name, tool_response)

            # [Knowledge Engine] 记录 Skill 执行轨迹
            if self._knowledge_tracker:
                try:
                    tool_input = tool_output.get("tool_input", {})
                    tool_resp = tool_output.get("tool_response", tool_output.get("content", ""))
                    # 优先用 ToolResult.is_error（skill_invoker 已准确算好）
                    tool_result_obj = tool_output.get("tool_result")
                    if tool_result_obj is not None and hasattr(tool_result_obj, "is_error"):
                        success = not tool_result_obj.is_error
                    elif isinstance(tool_resp, dict) and "success" in tool_resp:
                        success = bool(tool_resp["success"])
                    else:
                        # fallback: 字符串匹配（仅兜底）
                        success = "error" not in str(tool_resp)[:200].lower()
                    self._knowledge_tracker.record_skill_execution(
                        skill_name=actual_tool_name,
                        params=tool_input,
                        result=tool_resp,
                        duration_ms=0,
                        success=success,
                    )
                except Exception:
                    pass  # 不影响主流程

            logger.info(
                f"[Hook:PostToolUse] tool={actual_tool_name}, "
                f"skip_ui={skip_ui_rendering}, dingtalk_render={dingtalk_render}"
            )

            # [Quality-Driven] 质量缺口检测（轻量级规则，不调用 LLM）
            quality_focus = getattr(data_collector, "quality_focus", None) if data_collector else None
            if quality_focus and data_collector:
                tool_response_str = str(
                    tool_output.get("tool_response", tool_output.get("content", ""))
                )[:500]
                gaps = []

                # timeliness 检测: 关键工具结果数据量过少
                if quality_focus.get("timeliness", 0) >= 0.7:
                    if actual_tool_name in ("web_search", "stock_query", "realtime_quote"):
                        if len(tool_response_str) < 100:
                            gaps.append("timeliness:数据量过少，可能未获取到最新信息")

                # correctness 检测: 关键工具返回空或错误
                if quality_focus.get("correctness", 0) >= 0.7:
                    if len(tool_response_str) < 50 and actual_tool_name not in ("stock_resolver",):
                        gaps.append(f"correctness:{actual_tool_name}返回数据不足")

                if gaps:
                    data_collector.quality_gaps.extend(gaps)
                    logger.info(f"[Hook:PostToolUse] Quality gaps: {gaps}")

            # 1. 推送 tool_done 事件
            event_bridge.push_event(
                {"event": "tool_done", "data": {"name": actual_tool_name}}
            )

            # 2. UI 组件渲染决策（fire-and-forget，不阻塞工具调用链）
            # _decide_ui_component 含 LLM 调用(~15s)，改为后台任务，tool_done 已推送后继续下一工具
            if not skip_ui_rendering:
                async def _render_and_push(
                    _tool_name=actual_tool_name,
                    _tool_output=tool_output,
                    _scene_ctx=scene_context,
                    _existing_tabs=existing_tabs,
                    _dingtalk=dingtalk_render,
                ):
                    try:
                        import uuid as _uuid
                        component_result = await self._decide_ui_component(
                            _tool_name, _tool_output, scene_context=_scene_ctx
                        )
                        if not component_result:
                            return
                        _bg_list: List[Dict[str, Any]] = (
                            component_result if isinstance(component_result, list) else [component_result]
                        )

                        # ---- 场景 Tab 管理 (搬运到 bg task 内部) ----
                        _active_tab_id_bg = None
                        _active_scene_role_bg = None
                        if _scene_ctx and _existing_tabs is not None:
                            try:
                                if not _scene_ctx._inferred:
                                    try:
                                        query = getattr(request, "message", "") or getattr(request, "query", "")
                                        await _scene_ctx.infer_scene(
                                            query=query,
                                            skill_names=[_tool_name],
                                            existing_tabs=_existing_tabs,
                                        )
                                    except Exception:
                                        _scene_ctx.scene_type = "default"
                                        _scene_ctx._inferred = True

                                if _scene_ctx.scene_type and _scene_ctx.scene_type != "default":
                                    tab, is_new = _scene_ctx.find_or_create_tab(
                                        _existing_tabs,
                                        title="",
                                        reuse_tab_id=_scene_ctx.reuse_tab_id,
                                    )
                                    _active_tab_id_bg = tab["tab_id"]
                                    if is_new:
                                        event_bridge.push_event({
                                            "event": "scene_tab",
                                            "data": {
                                                "tab_id": tab["tab_id"],
                                                "scene_type": _scene_ctx.scene_type,
                                                "title": tab.get("title", ""),
                                                "is_new": True,
                                                "canvas_type": _scene_ctx.canvas_type,
                                                "canvas_config": _scene_ctx.canvas_config,
                                            },
                                        })
                                    if self._persistence and _existing_tabs:
                                        tabs_snapshot = [
                                            {
                                                "tab_id": t.get("tab_id"),
                                                "scene_type": t.get("scene_type"),
                                                "title": t.get("title"),
                                                "canvas_type": t.get("canvas_type"),
                                            }
                                            for t in _existing_tabs
                                        ]
                                        _sid = getattr(request, "session_id", "default")
                                        _uid = getattr(request, "user_id", None)
                                        if _sid and _sid != "default":
                                            from app.utils.background_task_manager import create_background_task
                                            create_background_task(
                                                self._persistence.update_session_metadata(
                                                    _sid, {"scene_tabs": tabs_snapshot}, user_id=_uid
                                                ),
                                                task_name="scene_tabs_save"
                                            )

                                    skill_meta = self._ui_selector.get_skill_metadata(_tool_name)
                                    for evt in _bg_list:
                                        comp_name = evt.get("data", {}).get("component", "")
                                        comp_data = evt.get("data", {}).get("data", {})
                                        layout_hint = _scene_ctx.classify_component_layout(
                                            comp_name,
                                            skill_metadata=skill_meta,
                                            data=comp_data if isinstance(comp_data, dict) else None,
                                        )
                                        if layout_hint and layout_hint.get("role") == "standalone" and _scene_ctx.scene_type == "map":
                                            if isinstance(comp_data, dict):
                                                if _scene_ctx._has_layer_data(comp_data):
                                                    layout_hint = {"role": "layer", "position": "canvas", "z_index": 0}
                                                elif _scene_ctx._has_poi_data(comp_data):
                                                    layout_hint = {"role": "poi", "position": "canvas", "z_index": 100}
                                                else:
                                                    layout_hint = {"role": "overlay", "position": "canvas", "z_index": 1001}
                                            else:
                                                layout_hint = {"role": "overlay", "position": "canvas", "z_index": 1001}
                                        if layout_hint:
                                            layout_hint["tab_id"] = tab["tab_id"]
                                            if layout_hint.get("role") == "standalone" and _scene_ctx.scene_type == "map":
                                                layout_hint["role"] = "overlay"
                                                layout_hint["position"] = "canvas"
                                                layout_hint["z_index"] = 1001
                                            evt["data"]["layout_hint"] = layout_hint
                                            _active_scene_role_bg = layout_hint.get("role")

                                    # 多组件协调
                                    has_map_view = any(
                                        e.get("data", {}).get("component") == "map_bindary_view"
                                        for e in _bg_list
                                    )
                                    if has_map_view:
                                        card_count = 0
                                        for evt in _bg_list:
                                            comp_name_c = evt.get("data", {}).get("component", "")
                                            hint_c = evt.get("data", {}).get("layout_hint", {})
                                            if not hint_c or comp_name_c == "map_bindary_view":
                                                continue
                                            if hint_c.get("role") in ("poi", "layer"):
                                                hint_c["role"] = "overlay"
                                                hint_c["z_index"] = 1001
                                                card_count += 1
                                        if card_count > 5:
                                            from collections import Counter
                                            name_counts = Counter(
                                                e.get("data", {}).get("component", "")
                                                for e in _bg_list
                                                if e.get("data", {}).get("layout_hint", {}).get("role") == "overlay"
                                            )
                                            for comp_name_dup, cnt in name_counts.items():
                                                if cnt <= 5:
                                                    continue
                                                kept = []
                                                dup_count = 0
                                                for evt in _bg_list:
                                                    cn = evt.get("data", {}).get("component", "")
                                                    if cn == comp_name_dup and evt.get("data", {}).get("layout_hint", {}).get("role") == "overlay":
                                                        dup_count += 1
                                                        if dup_count == 1:
                                                            kept.append(evt)
                                                    else:
                                                        kept.append(evt)
                                                _bg_list = kept

                                    # 规则 3: 自动拆分
                                    if _scene_ctx.scene_type == "map":
                                        split_events = []
                                        for evt in _bg_list:
                                            hint = evt.get("data", {}).get("layout_hint", {})
                                            if not hint:
                                                continue
                                            role = hint.get("role", "")
                                            comp_data_s = evt.get("data", {}).get("data", {})
                                            comp_name_s = evt.get("data", {}).get("component", "")
                                            if not isinstance(comp_data_s, dict):
                                                continue
                                            if role == "poi":
                                                layer_data = self._extract_layer_data(comp_data_s)
                                                if layer_data:
                                                    split_events.append({"event": "component", "data": {"component": "map_bindary_view", "data": layer_data, "title": evt["data"].get("title", ""), "layout_hint": {"role": "layer", "position": "canvas", "z_index": 0, "tab_id": hint.get("tab_id")}}})
                                            elif role == "overlay":
                                                geo_data = self._extract_geo_from_data(comp_name_s, comp_data_s)
                                                if geo_data:
                                                    split_events.append({"event": "component", "data": {"component": "map_bindary_view", "data": geo_data, "title": evt["data"].get("title", ""), "layout_hint": {"role": "poi", "position": "canvas", "z_index": 100, "tab_id": hint.get("tab_id")}}})
                                            elif role == "layer":
                                                if _scene_ctx._has_poi_data(comp_data_s):
                                                    geo_data = self._extract_geo_from_data(comp_name_s, comp_data_s)
                                                    if geo_data:
                                                        split_events.append({"event": "component", "data": {"component": "map_bindary_view", "data": geo_data, "title": evt["data"].get("title", ""), "layout_hint": {"role": "poi", "position": "canvas", "z_index": 100, "tab_id": hint.get("tab_id")}}})
                                        _bg_list.extend(split_events)

                                        # 过滤 trivial overlay
                                        filtered = []
                                        for evt in _bg_list:
                                            hint_f = evt.get("data", {}).get("layout_hint", {})
                                            if hint_f.get("role") == "overlay":
                                                if self._is_trivial_overlay(evt.get("data", {}).get("data", {})):
                                                    continue
                                            filtered.append(evt)
                                        _bg_list = filtered

                            except Exception as scene_err:
                                logger.debug(f"[Hook:PostToolUse:bg] Scene processing failed: {scene_err}")

                        # 通用过滤: 移除空组件
                        _bg_list = [
                            evt for evt in _bg_list
                            if not self._is_trivial_overlay(evt.get("data", {}).get("data", {}))
                        ]

                        # 推送 component 事件
                        for evt in _bg_list:
                            if _dingtalk:
                                evt["event"] = "component_for_render"
                            if isinstance(evt.get("data"), dict):
                                _comp_name = evt["data"].get("component", "unknown")
                                evt["data"]["id"] = f"{_tool_name}_{_comp_name}_{_uuid.uuid4().hex[:8]}"
                            event_bridge.push_event(evt)

                        # 缓存
                        self._cache_tool_result(
                            _tool_name, _tool_output, request, _bg_list,
                            tab_id=_active_tab_id_bg, scene_role=_active_scene_role_bg,
                        )
                        logger.debug(
                            f"[Hook:PostToolUse:bg] Render complete: "
                            f"{_tool_name} → {len(_bg_list)} component(s)"
                        )
                    except Exception as bg_err:
                        logger.warning(f"[Hook:PostToolUse:bg] Async render failed for '{_tool_name}': {bg_err}")

                _render_task = asyncio.create_task(_render_and_push())
                if bg_render_tasks is not None:
                    bg_render_tasks.append(_render_task)
            else:
                logger.debug(
                    f"[Hook:PostToolUse] UI rendering skipped for "
                    f"'{actual_tool_name}' (text_only mode)"
                )

            # 3. 场景 Tab 管理、UI 组件过滤和推送均已移入 _render_and_push bg task（非阻塞）

            # 4. Langfuse: 记录 Skill Span（失败不影响业务）
            try:
                lf_trace = getattr(request, 'langfuse_trace', None)
                if lf_trace:
                    tool_params = tool_output.get("tool_input", {})
                    tool_result = tool_output.get("tool_response", {})
                    span = lf_trace.start_span(
                        name=f"skill:{actual_tool_name}",
                        input={"params_keys": list(tool_params.keys()) if isinstance(tool_params, dict) else []},
                    )
                    span.update(
                        output={
                            "status": "completed",
                            "result_type": type(tool_result).__name__,
                        },
                    )
                    span.end()
            except Exception as lf_err:
                logger.debug(f"[Langfuse] skill span failed: {lf_err}")

            # 6. 收集数据用于置信度评估
            if data_collector:
                try:
                    # Claude SDK 的 PostToolUse hook 中，实际工具输出在 tool_response 字段
                    tool_result = tool_output.get("tool_response", tool_output)

                    # 特殊处理：如果 tool_response 是一个错误消息（结果太大被保存到文件）
                    # 尝试从文件中读取实际结果
                    # 工具名可能是 "web_search", "Skill", "mcp__sthg_v4_skills__web_search" 等
                    raw_tool_name = tool_output.get("tool_name", "")
                    is_web_search = "web_search" in raw_tool_name.lower() or "web_search" in actual_tool_name.lower()
                    logger.debug(f"[Hook:PostToolUse] raw_tool_name={raw_tool_name}, is_web_search={is_web_search}")

                    # 检查是否有结果太大保存到文件的情况（对所有工具都检查）
                    if isinstance(tool_result, str) and "exceeds maximum allowed tokens" in tool_result and "saved to" in tool_result:
                        # 提取文件路径
                        import re
                        file_match = re.search(r'saved to ([^\s]+)', tool_result)
                        if file_match:
                            file_path = file_match.group(1).rstrip('.')
                            logger.info(f"[Hook:PostToolUse] Result saved to file: {file_path}")
                            try:
                                import os
                                if os.path.exists(file_path):
                                    with open(file_path, 'r', encoding='utf-8') as f:
                                        # 只读取前100KB（避免内存问题和内容泄露）
                                        file_content = f.read(100000)
                                        tool_result = file_content
                                        logger.info(f"[Hook:PostToolUse] Read {len(file_content)} chars from file")
                            except Exception as e:
                                logger.warning(f"[Hook:PostToolUse] Failed to read file: {e}")

                    data_collector.add_skill_result(actual_tool_name, tool_result)

                    # 从 tool_result 中提取 URL（记录数据来源）
                    extracted_urls = self._extract_urls_from_result(tool_result)
                    if extracted_urls:
                        for url_info in extracted_urls[:10]:
                            data_collector.add_data_source(
                                name=url_info.get("title", actual_tool_name),
                                url=url_info.get("url"),
                                source_type="skill",
                                skill_name=actual_tool_name,
                            )
                    else:
                        data_collector.add_data_source(
                            name=actual_tool_name,
                            source_type="skill",
                            skill_name=actual_tool_name,
                        )
                    logger.debug(f"[Hook:PostToolUse] Data collected: {actual_tool_name}")
                except Exception as e:
                    logger.warning(f"[Hook:PostToolUse] Failed to collect data: {e}")

            return {}

        return post_tool_use

    def _make_stop_hook(
        self, event_bridge, request, scene_context=None,
        existing_tabs: Optional[List[Dict[str, Any]]] = None,
        # Agentic Loop 参数
        reflection=None,
        tracker=None,
        accumulated_text_ref=None,
        data_collector=None,
        request_start_time=None,
        bg_render_tasks: Optional[List] = None,
    ):
        """Stop Hook 工厂"""
        _request_start = request_start_time

        async def stop_hook(
            result_data: Dict[str, Any],
            tool_name: Optional[str],
            hook_context,
        ) -> Dict[str, Any]:
            logger.info("[Hook:Stop] Execution stopped")

            # 等待所有后台渲染任务完成（最多 5s），确保 component 事件全部推送
            if bg_render_tasks:
                pending = [t for t in bg_render_tasks if not t.done()]
                if pending:
                    logger.info(f"[Hook:Stop] Waiting for {len(pending)} bg render task(s)...")
                    try:
                        done, still_pending = await asyncio.wait(pending, timeout=5.0)
                        for t in still_pending:
                            t.cancel()
                        if still_pending:
                            logger.warning(
                                f"[Hook:Stop] {len(still_pending)} render task(s) cancelled (timeout 5s)"
                            )
                    except Exception as wait_err:
                        logger.warning(f"[Hook:Stop] Render wait failed: {wait_err}")

            # --- 原有逻辑: Scene Tab 持久化（完全不变）---
            try:
                session_id = getattr(request, "session_id", "default")
                if session_id and session_id != "default":
                    logger.info(
                        f"[Hook:Stop] Session {session_id} ready for persistence"
                    )

                    # 场景 Tab 持久化（只要有 tabs 就保存，不依赖 active_tab_id）
                    if existing_tabs and self._persistence:
                        try:
                            # 序列化 tabs（移除大数据，只保留结构）
                            tabs_to_save = []
                            for tab in existing_tabs:
                                tabs_to_save.append({
                                    "tab_id": tab.get("tab_id"),
                                    "scene_type": tab.get("scene_type"),
                                    "title": tab.get("title"),
                                    "created_at": tab.get("created_at"),
                                    "template": tab.get("template"),
                                    "canvas_type": tab.get("canvas_type"),
                                })
                            user_id = getattr(request, "user_id", None)
                            await self._persistence.update_session_metadata(
                                session_id, {"scene_tabs": tabs_to_save}, user_id=user_id
                            )
                            logger.info(f"[Hook:Stop] Scene tabs persisted: {len(tabs_to_save)} tabs")
                        except Exception as tab_err:
                            logger.debug(f"[Hook:Stop] Scene tab persistence failed: {tab_err}")

            except Exception as e:
                logger.warning(f"[Hook:Stop] Session persistence prep failed: {e}")

            # --- Agentic Reflection ---
            if reflection and tracker:
                # 跳过 Phase 0: 没有工具调用且没有输出文本，说明是意图分析阶段
                _total_tools = len(data_collector.tool_calls) if data_collector else 0
                _has_output = bool(accumulated_text_ref and "".join(accumulated_text_ref).strip())
                if _total_tools == 0 and not _has_output:
                    logger.info("[Hook:Stop] Phase 0 detected (no tools, no output), skipping reflection")
                    return {}

                # 防护: token limit 检测（避免与 compact+retry 交叉重入）
                if accumulated_text_ref:
                    _recent = accumulated_text_ref[-3:] if len(accumulated_text_ref) >= 3 else accumulated_text_ref
                    if any("token limit" in t.lower() for t in _recent if isinstance(t, str)):
                        logger.info("[Hook:Stop] Token limit detected, skipping reflection")
                        return {}

                try:
                    tool_summary = tracker.get_summary()
                    current_output = "".join(accumulated_text_ref) if accumulated_text_ref else ""
                    user_query = getattr(request, "message", "")

                    # 计算熔断参数
                    _total_tool_calls = len(data_collector.tool_calls) if data_collector else 0
                    _elapsed = time.monotonic() - _request_start if _request_start else 0.0

                    # 构建 quality_gaps 摘要
                    _quality_gaps_text = ""
                    if data_collector and hasattr(data_collector, 'quality_gaps') and data_collector.quality_gaps:
                        _quality_gaps_text = "; ".join(data_collector.quality_gaps[-10:])

                    eval_result = await reflection.evaluate(
                        user_query=user_query,
                        tool_summary=tool_summary,
                        current_output=current_output,
                        total_tool_calls=_total_tool_calls,
                        elapsed_seconds=_elapsed,
                        quality_gaps=_quality_gaps_text,
                    )

                    if eval_result.get("continue_"):
                        logger.info(
                            f"[Hook:Stop] Reflection: INSUFFICIENT "
                            f"(round={eval_result['round']}, "
                            f"reason={eval_result['reason']})"
                        )
                        # 重置 tracker 为下一轮准备
                        tracker.reset()
                        return {
                            "continue_": True,
                            "systemMessage": eval_result.get("system_message", ""),
                        }
                    else:
                        logger.info(
                            f"[Hook:Stop] Reflection: SUFFICIENT "
                            f"(round={eval_result['round']}, "
                            f"reason={eval_result['reason']})"
                        )
                except Exception as ref_err:
                    logger.warning(f"[Hook:Stop] Reflection failed (non-fatal): {ref_err}")

            # --- Knowledge Engine: finalize episode ---
            if self._knowledge_tracker and self._knowledge_store:
                try:
                    user_id = getattr(request, "user_id", 0)
                    instance_id = getattr(request, "instance_id", "")
                    session_id_str = str(getattr(request, "session_id", ""))

                    # 注入查询和对话上下文（用于思维链提取）
                    _user_query = getattr(request, "message", "")
                    if _user_query:
                        self._knowledge_tracker.set_query(_user_query)
                    _assistant_text = "".join(accumulated_text_ref) if accumulated_text_ref else ""
                    if _assistant_text or _user_query:
                        self._knowledge_tracker.record_conversation_context(
                            assistant_response=_assistant_text[:2000],
                            conversation_context=f"用户: {_user_query}\n助手: {_assistant_text[:1000]}",
                        )
                    _loop_normal_exit = result_data.get("loop_normal_exit", True)
                    if self._knowledge_guard:
                        asyncio.create_task(
                            self._knowledge_guard.safe_finalize(
                                self._knowledge_tracker, self._knowledge_store,
                                user_id=user_id, instance_id=instance_id,
                                session_id=session_id_str,
                                loop_normal_exit=_loop_normal_exit,
                            )
                        )
                    else:
                        asyncio.create_task(
                            self._knowledge_tracker.finalize(
                                self._knowledge_store,
                                user_id=user_id, instance_id=instance_id,
                                session_id=session_id_str,
                                loop_normal_exit=_loop_normal_exit,
                            )
                        )
                    logger.info("[Hook:Stop] Knowledge engine finalize scheduled (async)")
                except Exception as ke_err:
                    logger.debug(f"[Hook:Stop] Knowledge engine finalize failed: {ke_err}")

            # --- 预测提取（异步，完全不阻塞响应流）---
            if self._prediction_extractor:
                try:
                    _user_q = getattr(request, "message", "")
                    _asst_text = "".join(accumulated_text_ref) if accumulated_text_ref else ""
                    _uid = getattr(request, "user_id", 0)
                    _iid = getattr(request, "instance_id", "")
                    _sid = str(getattr(request, "session_id", ""))
                    if _asst_text and _uid:
                        asyncio.create_task(
                            self._prediction_extractor.extract_and_save(
                                user_id=_uid,
                                instance_id=_iid,
                                session_id=_sid,
                                user_query=_user_q,
                                assistant_text=_asst_text,
                            )
                        )
                except Exception as pred_err:
                    logger.debug(f"[Hook:Stop] Prediction extractor task failed: {pred_err}")

            return {}

        return stop_hook

    def _load_session_tabs(self, session_id: str) -> List[Dict[str, Any]]:
        """从 persistence 同步加载 session 已有的场景 Tab 列表（SQLite 暂不支持同步读，返回空列表）"""
        return []

    def _get_tool_display_info(self, tool_name: str) -> Dict[str, str]:
        """获取工具的显示名称和描述（从 SkillDiscovery）"""
        if not tool_name:
            return {"display_name": "unknown", "description": ""}

        return self._ui_selector.get_display_info(tool_name)

    # 纯执行类工具，不参与 UI 组件渲染（LLM 自行决定后续展示）
    _EXECUTION_ONLY_TOOLS = {"Read", "Glob", "Grep", "Write", "Edit"}

    async def _decide_ui_component(
        self, tool_name: str, tool_output: Dict[str, Any],
        scene_context=None,
    ) -> Optional[Dict[str, Any]]:
        """
        UI 组件渲染决策（使用 V4UISelector）

        保证：每个工具调用都产生一个 component 事件（至少是 dynamic_card）。
        这确保钉钉渠道的全技能页面能展示所有工具的数据。

        策略：始终使用 dynamic_card 展示完整的工具返回数据（所有字段），
        避免专用组件（如 stock_info_card）只渲染部分字段导致数据丢失。
        """
        # 纯执行类工具跳过 UI 渲染，LLM 自行决定后续展示
        if tool_name in self._EXECUTION_ONLY_TOOLS:
            logger.info(f"[Hook:PostToolUse] {tool_name} is execution-only, skip UI component")
            return None

        actual_data = None
        try:
            # 从 tool_output 中提取实际数据
            actual_data = self._extract_tool_data(tool_name, tool_output)
            logger.info(f"[Hook:PostToolUse] _decide_ui_component: tool={tool_name}, actual_data type={type(actual_data).__name__}")
            if isinstance(actual_data, dict):
                logger.info(f"[Hook:PostToolUse] _decide_ui_component: actual_data keys={list(actual_data.keys())[:15]}")

            # 提前过滤：提取的数据为空时，不创建任何组件
            if self._is_empty_tool_data(actual_data):
                logger.info(f"[Hook:PostToolUse] {tool_name} returned empty data, skip UI component")
                return None

            # 优先尝试从数据中提取已有的 UI 组件配置（如 web_search 的 for_ui.components）
            # 支持多组件返回（如 flight_tracker_card + map_binary_view + insight_highlights）
            all_instructions = self._ui_selector._extract_all_ui_components_from_data(tool_name, actual_data)
            if all_instructions:
                # Layer 1 enhancement: merge parent-level map_data/street_view/primary_location
                # into each component's data so overlay components carry geo info to frontend
                # Unwrap list-wrapped data for parent geo extraction
                _parent = actual_data
                if isinstance(_parent, list) and len(_parent) == 1 and isinstance(_parent[0], dict):
                    _parent = _parent[0]
                if isinstance(_parent, dict):
                    parent_geo_keys = ("map_data", "street_view_base64", "primary_location")
                    parent_geo = {k: _parent[k] for k in parent_geo_keys if k in _parent}
                    if parent_geo:
                        for ri in all_instructions:
                            if isinstance(ri.data, dict):
                                for gk, gv in parent_geo.items():
                                    if gk not in ri.data:
                                        ri.data[gk] = gv
                                logger.debug(
                                    f"[Hook:PostToolUse] Merged parent geo keys {list(parent_geo.keys())} "
                                    f"into component {ri.component}"
                                )

                if len(all_instructions) == 1:
                    ri = all_instructions[0]
                    logger.info(f"[Hook:PostToolUse] _decide_ui_component: Using embedded component: {ri.component}")
                    return {
                        "event": "component",
                        "data": {
                            "component": ri.component,
                            "data": ri.data,
                            "title": ri.title,
                        },
                    }
                else:
                    # 多组件：返回列表，调用方需遍历发送多个 component 事件
                    logger.info(f"[Hook:PostToolUse] _decide_ui_component: Returning {len(all_instructions)} embedded components: "
                                f"{[ri.component for ri in all_instructions]}")
                    return [
                        {
                            "event": "component",
                            "data": {
                                "component": ri.component,
                                "data": ri.data,
                                "title": ri.title,
                            },
                        }
                        for ri in all_instructions
                    ]

            # Layer 2: SKILL.md data_hints 匹配（通用多组件匹配）
            if actual_data and isinstance(actual_data, dict):
                all_matched = self._ui_selector.select_all_matching_components(tool_name, actual_data)
                if all_matched:
                    # 前端映射到 DynamicCard.vue 的组件名集合（需要 fields 格式转换）
                    _DYNAMIC_CARD_ALIASES = {
                        "dynamic_card", "consensus_chart", "dividend_chart",
                        "insider_chart", "market_overview_chart", "image_gallery",
                        "summary_card", "bar_chart",
                        "quick_search", "url_fetch",
                    }
                    for m in all_matched:
                        if m.component in _DYNAMIC_CARD_ALIASES and isinstance(m.data, dict) and "fields" not in m.data:
                            m.data = self._build_full_dynamic_card_data(m.data)

                    if len(all_matched) == 1:
                        ri = all_matched[0]
                        logger.info(
                            f"[Hook:PostToolUse] Layer 2 matched: {ri.component} "
                            f"(conf={ri.confidence})"
                        )
                        return {
                            "event": "component",
                            "data": {
                                "component": ri.component,
                                "data": ri.data,
                                "title": ri.title,
                            },
                        }
                    else:
                        logger.info(
                            f"[Hook:PostToolUse] Layer 2 multi-matched: "
                            f"{[m.component for m in all_matched]}"
                        )
                        return [
                            {
                                "event": "component",
                                "data": {
                                    "component": m.component,
                                    "data": m.data,
                                    "title": m.title,
                                },
                            }
                            for m in all_matched
                        ]

            # Layer 3: LLM 生成 HTML（frontend_builder，带场景上下文）
            if actual_data and isinstance(actual_data, dict):
                try:
                    html_result = await self._try_llm_html_generation(tool_name, actual_data, scene_context=scene_context)
                    if html_result:
                        return html_result
                except Exception as layer3_err:
                    logger.debug(f"[Hook:PostToolUse] Layer 3 LLM generation failed: {layer3_err}")

        except Exception as e:
            import traceback
            logger.error(
                f"[Hook:PostToolUse] UI selection failed for '{tool_name}': {e}\n{traceback.format_exc()}"
            )

        # Layer 4 fallback: 使用 dynamic_card 展示完整数据，确保所有字段可见
        display_info = self._ui_selector.get_display_info(tool_name)
        card_title = display_info.get("display_name", tool_name)

        if actual_data is None:
            actual_data = tool_output.get("tool_response", tool_output)

        # 二次校验：fallback 数据也可能为空（如 tool_output 本身为空或仅含元信息）
        if self._is_empty_tool_data(actual_data):
            logger.info(f"[Hook:PostToolUse] {tool_name} fallback data also empty, skip UI component")
            return None

        card_data = self._build_full_dynamic_card_data(actual_data)

        # 构建后校验：fields 全为空值的 dynamic_card 不应渲染
        if self._is_trivial_overlay(card_data):
            logger.info(f"[Hook:PostToolUse] {tool_name} dynamic_card has no meaningful fields, skip")
            return None

        logger.info(
            f"[Hook:PostToolUse] _decide_ui_component: Using dynamic_card for {tool_name} "
            f"(fields={len(card_data.get('fields', []))})"
        )

        return {
            "event": "component",
            "data": {
                "component": "dynamic_card",
                "data": card_data,
                "title": card_title,
            },
        }

    async def _try_llm_html_generation(
        self, tool_name: str, data: Dict[str, Any],
        scene_context=None,
    ) -> Optional[Dict[str, Any]]:
        """
        Layer 3: 调用 LLM 生成 HTML 可视化

        超时 15s，失败静默降级到 Layer 4 (dynamic_card)。
        """
        import asyncio
        import os
        import httpx

        api_key = os.getenv("ANTHROPIC_AUTH_TOKEN", "")
        base_url = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        model = os.getenv("ANTHROPIC_SMALL_FAST_MODEL") or os.getenv("ANTHROPIC_MODEL", "")

        if not api_key or not model:
            return None

        # Load prompt from config
        try:
            from app.prompts.loader import load_prompt
            from pathlib import Path
            import json as _json

            # Load JS libraries text
            js_libs_text = "ECharts 5, D3.js 7, Chart.js 4, Leaflet 1.9.4"
            js_config_path = Path(__file__).resolve().parents[3] / "app" / "config" / "ui" / "js_preloads.json"
            if js_config_path.exists():
                js_config = _json.loads(js_config_path.read_text(encoding="utf-8"))
                js_libs_text = "\n".join(
                    f"- {lib['name']} ({lib.get('version', '')}) — {lib.get('usage', '')}"
                    for lib in js_config.get("libraries", [])
                )

            system_prompt = load_prompt("frontend_builder_system", js_libraries=js_libs_text)
            if not system_prompt:
                return None
        except Exception:
            return None

        # Build data summary
        data_summary = {}
        for k, v in list(data.items())[:20]:
            if isinstance(v, list):
                data_summary[k] = f"list[{len(v)}]"
            elif isinstance(v, dict):
                data_summary[k] = f"dict keys={list(v.keys())[:8]}"
            else:
                data_summary[k] = type(v).__name__

        import json as _json
        user_prompt = (
            f"工具名: {tool_name}\n"
            f"数据结构: {_json.dumps(data_summary, ensure_ascii=False)}\n"
            f"完整数据: {_json.dumps(data, ensure_ascii=False, default=str)[:8000]}"
        )

        # 当 map 场景激活时，附加场景上下文让 LLM 判断是否输出 layout_hint
        if scene_context and getattr(scene_context, 'scene_type', None) == "map":
            user_prompt += (
                "\n\n## 当前场景: map（地图画布）\n"
                "请判断此数据是否包含可地图化的地理信息（如国家、城市、坐标、区域）。\n"
                "如果包含地理信息，在 HTML 末尾追加一个隐藏的 JSON 块：\n"
                '<script type="application/json" id="layout_hint">'
                '{"role": "overlay", "has_geo": true, "geo_entities": ["实体1", "实体2"]}'
                '</script>\n'
                "如果不包含地理信息，不添加此 JSON 块。\n"
                "注意: 生成的 HTML 应该是紧凑型卡片布局（适合浮窗展示），而非全宽页面。"
            )

        try:
            url = f"{base_url.rstrip('/')}/v1/messages"
            headers = {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            body = {
                "model": model,
                "max_tokens": 4096,
                "temperature": 0.2,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            }

            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, json=body, headers=headers)
                resp.raise_for_status()
                result = resp.json()
                content = result.get("content", [])
                if content and isinstance(content[0], dict):
                    text = content[0].get("text", "")
                    html_code = self._extract_html_from_llm(text)
                    if html_code:
                        logger.info(f"[Hook:PostToolUse] Layer 3 LLM generated HTML for {tool_name} ({len(html_code)} chars)")
                        result_event = {
                            "event": "component",
                            "data": {
                                "component": "html_render",
                                "data": {
                                    "html": html_code,
                                    "title": tool_name,
                                },
                                "title": tool_name,
                            },
                        }
                        # 提取 LLM 内联的 layout_hint（map 场景下）
                        if scene_context and getattr(scene_context, 'scene_type', None) == "map":
                            layout_hint = self._extract_layout_hint_from_html(html_code)
                            if layout_hint and layout_hint.get("has_geo"):
                                result_event["data"]["layout_hint"] = {
                                    "role": "overlay",
                                    "position": "canvas",
                                    "z_index": 10,
                                }
                                logger.info(f"[Hook:PostToolUse] Layer 3 detected geo content → overlay for {tool_name}")
                        return result_event
        except asyncio.TimeoutError:
            logger.debug(f"[Hook:PostToolUse] Layer 3 LLM timeout for {tool_name}")
        except Exception as e:
            logger.debug(f"[Hook:PostToolUse] Layer 3 LLM error for {tool_name}: {e}")

        return None

    @staticmethod
    def _extract_html_from_llm(text: str) -> Optional[str]:
        """Extract HTML from LLM response"""
        import re
        text = text.strip()
        match = re.search(r'```(?:html)?\s*(<!DOCTYPE.*?</html>)\s*```', text, re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1).strip()
        if text.lower().startswith('<!doctype') or text.lower().startswith('<html'):
            return text
        return None

    @staticmethod
    def _extract_layout_hint_from_html(html: str) -> Optional[Dict]:
        """从 LLM 生成的 HTML 中提取 layout_hint JSON"""
        import re
        import json
        match = re.search(
            r'<script\s+type="application/json"\s+id="layout_hint">(.*?)</script>',
            html, re.DOTALL
        )
        if match:
            try:
                return json.loads(match.group(1))
            except (json.JSONDecodeError, TypeError):
                pass
        return None

    @staticmethod
    def _is_empty_tool_data(data) -> bool:
        """
        判断工具返回的数据是否为空/无实质内容，用于跳过空组件的创建。

        过滤条件（满足任一即为空）:
        1. data 为 None / 空字符串 / 空字典 / 空列表
        2. data 为 dict 但所有值为空
        3. data 为仅包含元信息字段的 dict（如 query/status/error）
        """
        if data is None:
            return True
        if isinstance(data, str):
            return not data.strip()
        if isinstance(data, (list, dict)) and len(data) == 0:
            return True

        if isinstance(data, dict):
            # 元信息字段：这些字段单独存在时不构成有意义的数据
            meta_keys = {
                "query", "query_type", "status", "message", "error",
                "task_type", "mode", "search_engine", "skill_name",
                "tool_name", "tracking_type", "search_mode",
                "execution_time_ms",
            }
            meaningful_keys = set(data.keys()) - meta_keys
            if not meaningful_keys:
                return True

            # 所有有意义字段的值为空
            empty_markers = (None, "", "N/A", "-", "无数据", "（无数据）", [], {}, 0)
            if all(data.get(k) in empty_markers for k in meaningful_keys):
                return True

        return False

    @staticmethod
    def _is_trivial_overlay(data) -> bool:
        """
        判断 overlay 组件数据是否"无实质内容"，用于 map 场景中过滤噪音 overlay。

        过滤条件（满足任一即为 trivial）:
        1. data 为空（None / {} / 空字符串）
        2. data 仅含元信息字段（query/query_type/status/message 等），无实质数据
        3. data 中 results/items/insights 列表全部为空 + total_count=0
        4. data 含 fields 列表但字段值全为空/无数据标记
        """
        if not data:
            return True
        if not isinstance(data, dict):
            return False

        # 元信息字段：这些字段单独存在时不构成有意义的 overlay 内容
        meta_keys = {
            "query", "query_type", "status", "message", "error",
            "task_type", "mode", "total_results", "search_engine",
            "execution_time_ms", "skill_name", "tool_name",
            "tracking_type", "search_mode", "total_count",
        }
        meaningful_keys = set(data.keys()) - meta_keys
        if not meaningful_keys:
            return True

        # 快速检查: total_count=0 + results/components 为空列表 → trivial
        total_count = data.get("total_count")
        if total_count == 0 or total_count == "0":
            results = data.get("results", data.get("items", data.get("insights")))
            if results is not None and (results == [] or results == "（无数据）"):
                return True

        # 检查有意义字段是否全为空值
        empty_markers = (None, "", "N/A", "-", "无数据", "（无数据）", [], {}, 0)
        all_empty = True
        for key in meaningful_keys:
            val = data[key]
            if val in empty_markers:
                continue
            # fields 列表（dynamic_card 格式）中所有字段值为空
            if key == "fields" and isinstance(val, list):
                has_content = any(
                    f.get("value") not in empty_markers
                    for f in val if isinstance(f, dict)
                )
                if not has_content:
                    continue
            # nested / sections 可能只含空列表
            if key in ("nested", "sections") and isinstance(val, (dict, list)):
                if not val:
                    continue
                # sections 全是空 data → trivial
                if isinstance(val, list) and all(
                    isinstance(s, dict) and not s.get("data")
                    for s in val
                ):
                    continue
            all_empty = False
            break

        return all_empty

    def _extract_geo_from_data(self, comp_name: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        从组件数据中提取地理信息，返回 map layer 数据格式。

        提取优先级:
        1. data.map_data 含 markers/center → 直接返回
        2. data.locations 数组 → 转为 markers
        3. data.lat + data.lng/lon 单点 → 转为单点 marker
        4. data.markers 顶层 → 直接返回
        5. 均无 → 返回 None

        Marker type 推断: 根据组件名自动附加 type 字段
        """
        if not data or not isinstance(data, dict):
            return None

        # 组件名 → marker type 映射
        _COMP_TYPE_MAP = {
            "flight_tracker_card": "aircraft",
            "ship_tracker_card": "ship",
            "geo_search_card": "location",
            "weather_realtime_card": "weather",
            "weather_forecast_card": "weather",
            "military_balance_card": "base_army",
        }
        marker_type = _COMP_TYPE_MAP.get(comp_name, "default")

        # 1. data.map_data
        md = data.get("map_data")
        if isinstance(md, dict) and (md.get("markers") or md.get("center")):
            result = dict(md)
            # 为 markers 附加 type
            if result.get("markers") and isinstance(result["markers"], list):
                for m in result["markers"]:
                    if isinstance(m, dict) and not m.get("type"):
                        m["type"] = marker_type
            if data.get("street_view_base64"):
                result["street_view_base64"] = data["street_view_base64"]
            if data.get("primary_location"):
                result["primary_location"] = data["primary_location"]
            return result

        # 2. data.locations 数组
        locs = data.get("locations")
        if isinstance(locs, list) and locs:
            markers = []
            for loc in locs:
                if not isinstance(loc, dict):
                    continue
                lat = loc.get("lat")
                lng = loc.get("lng") or loc.get("lon")
                if lat and lng:
                    markers.append({
                        "coordinates": [lng, lat],  # [lng, lat] 前端标准格式
                        "name": loc.get("name") or loc.get("display_name", ""),
                        "lat": lat,
                        "lng": lng,
                        "description": loc.get("address") or loc.get("display_name", ""),
                        "type": loc.get("type") or marker_type,
                    })
            if markers:
                center = [markers[0]["lng"], markers[0]["lat"]]
                result = {"markers": markers, "center": center, "zoom": 12}
                if data.get("primary_location"):
                    result["primary_location"] = data["primary_location"]
                if data.get("street_view_base64"):
                    result["street_view_base64"] = data["street_view_base64"]
                return result

        # 3. 单点 lat + lng/lon
        lat = data.get("lat")
        lng = data.get("lng") or data.get("lon")
        if lat and lng:
            result = {
                "markers": [{
                    "coordinates": [lng, lat],  # [lng, lat] 前端标准格式
                    "name": data.get("name", ""),
                    "lat": lat,
                    "lng": lng,
                    "type": marker_type,
                }],
                "center": [lng, lat],
                "zoom": 14,
            }
            if data.get("street_view_base64"):
                result["street_view_base64"] = data["street_view_base64"]
            if data.get("primary_location"):
                result["primary_location"] = data["primary_location"]
            return result

        # 4. 顶层 markers
        top_markers = data.get("markers")
        if isinstance(top_markers, list) and top_markers:
            for m in top_markers:
                if isinstance(m, dict):
                    if not m.get("type"):
                        m["type"] = marker_type
                    # 确保 coordinates 格式统一
                    if not m.get("coordinates"):
                        m_lng = m.get("lng") or m.get("lon")
                        m_lat = m.get("lat")
                        if m_lat and m_lng:
                            m["coordinates"] = [m_lng, m_lat]
                    if not m.get("name") and m.get("title"):
                        m["name"] = m["title"]
            first = top_markers[0] if isinstance(top_markers[0], dict) else {}
            center_lng = first.get("lng") or first.get("lon")
            center_lat = first.get("lat")
            if not center_lng and first.get("coordinates"):
                center_lng = first["coordinates"][0]
                center_lat = first["coordinates"][1]
            result = {"markers": top_markers}
            if center_lat and center_lng:
                result["center"] = [center_lng, center_lat]
                result["zoom"] = 12
            if data.get("street_view_base64"):
                result["street_view_base64"] = data["street_view_base64"]
            if data.get("primary_location"):
                result["primary_location"] = data["primary_location"]
            return result

        # 5. 深度扫描: 遍历一层子字典，查找含 position/lat/lng 的嵌套结构
        # 典型场景: flight_tracker_card → data.flight_info.position.{lat, lon}
        for key, val in data.items():
            if not isinstance(val, dict):
                continue
            # 子字典直接含 lat + lng/lon
            sub_lat = val.get("lat")
            sub_lng = val.get("lng") or val.get("lon")
            if sub_lat and sub_lng:
                return {
                    "markers": [{
                        "lat": sub_lat,
                        "lng": sub_lng,
                        "title": val.get("name") or val.get("callsign") or val.get("title", ""),
                        "type": val.get("type") or marker_type,
                    }],
                    "center": [sub_lng, sub_lat],
                    "zoom": 14,
                }
            # 子字典含 position 对象 (e.g. flight_info.position)
            pos = val.get("position")
            if isinstance(pos, dict):
                pos_lat = pos.get("lat")
                pos_lng = pos.get("lng") or pos.get("lon")
                if pos_lat and pos_lng:
                    return {
                        "markers": [{
                            "lat": pos_lat,
                            "lng": pos_lng,
                            "title": val.get("callsign") or val.get("name") or val.get("title", ""),
                            "description": val.get("aircraft") or val.get("description", ""),
                            "type": val.get("type") or marker_type,
                            "heading": val.get("heading"),
                        }],
                        "center": [pos_lng, pos_lat],
                        "zoom": 14,
                    }

        return None

    def _extract_layer_data(self, data: dict) -> Optional[Dict[str, Any]]:
        """
        从组件数据中提取纯图层数据（geojson/polyline/areas/circles/tracks/tile_url）。
        用于 Rule 3a: 当 poi 组件同时携带面/线/区域数据时拆分出独立 layer event。
        """
        if not data or not isinstance(data, dict):
            return None

        result = {}

        # geojson / boundary_geojson
        if data.get("geojson"):
            result["geojson"] = data["geojson"]
        elif data.get("boundary_geojson"):
            result["geojson"] = data["boundary_geojson"]

        # 嵌套 map_data 中的 markers/geojson/areas/circles/tracks
        md = data.get("map_data")
        if isinstance(md, dict):
            if md.get("markers") and isinstance(md["markers"], list):
                result["markers"] = md["markers"]
            if md.get("geojson") and "geojson" not in result:
                result["geojson"] = md["geojson"]
            if md.get("areas"):
                result["areas"] = md["areas"]
            if md.get("circles"):
                result["circles"] = md["circles"]
            if md.get("tracks"):
                result["tracks"] = md["tracks"]
            if md.get("center"):
                result["center"] = md["center"]
            if md.get("zoom"):
                result["zoom"] = md["zoom"]

        # trace 轨迹点列表 → 转为 polyline（如航班历史轨迹）
        trace = data.get("trace")
        if isinstance(trace, list) and len(trace) >= 2:
            polyline = []
            for pt in trace:
                if isinstance(pt, dict) and pt.get("lat") and (pt.get("lon") or pt.get("lng")):
                    polyline.append([pt["lat"], pt.get("lon") or pt["lng"]])
                elif isinstance(pt, (list, tuple)) and len(pt) >= 2:
                    polyline.append([pt[0], pt[1]])
            if polyline and "polyline" not in result:
                result["polyline"] = polyline

        # 轨迹 tracks
        if data.get("tracks"):
            result["tracks"] = data["tracks"]

        # 区域 areas / circles (顶层)
        if data.get("areas") and "areas" not in result:
            result["areas"] = data["areas"]
        if data.get("circles") and "circles" not in result:
            result["circles"] = data["circles"]

        # polyline
        if data.get("polyline") and "polyline" not in result:
            result["polyline"] = data["polyline"]
        ri = data.get("route_info")
        if isinstance(ri, dict) and ri.get("polyline") and "polyline" not in result:
            result["polyline"] = ri["polyline"]

        # tile_url / tiles
        if data.get("tile_url"):
            result["tile_url"] = data["tile_url"]
        if data.get("tiles"):
            result["tiles"] = data["tiles"]

        # geo_distribution FeatureCollection
        gd = data.get("geo_distribution")
        if isinstance(gd, dict) and gd.get("type") == "FeatureCollection":
            result["geojson"] = gd

        # 深度扫描: 子字典中的 trace → polyline
        # 典型: flight_tracker_card → data.flight_info.trace
        if "polyline" not in result:
            for key, val in data.items():
                if not isinstance(val, dict):
                    continue
                sub_trace = val.get("trace")
                if isinstance(sub_trace, list) and len(sub_trace) >= 2:
                    polyline = []
                    for pt in sub_trace:
                        if isinstance(pt, dict) and pt.get("lat") and (pt.get("lon") or pt.get("lng")):
                            polyline.append([pt["lat"], pt.get("lon") or pt["lng"]])
                        elif isinstance(pt, (list, tuple)) and len(pt) >= 2:
                            polyline.append([pt[0], pt[1]])
                    if polyline:
                        result["polyline"] = polyline
                        break

        # 保留 center/zoom
        if "center" not in result and data.get("center"):
            result["center"] = data["center"]
        if "zoom" not in result and data.get("zoom"):
            result["zoom"] = data["zoom"]

        # locations → markers（geo_search 等技能返回 locations 数组）
        locs = data.get("locations")
        if isinstance(locs, list) and locs and "markers" not in result:
            markers = []
            for loc in locs:
                if not isinstance(loc, dict):
                    continue
                lat = loc.get("lat")
                lng = loc.get("lng") or loc.get("lon")
                if lat and lng:
                    markers.append({
                        "coordinates": [lng, lat],
                        "name": loc.get("name") or loc.get("display_name", ""),
                        "lat": lat,
                        "lng": lng,
                        "description": loc.get("address") or loc.get("display_name", ""),
                        "type": loc.get("type", "poi"),
                    })
            if markers:
                result["markers"] = markers
                if "center" not in result:
                    result["center"] = [markers[0]["lng"], markers[0]["lat"]]
                    result["zoom"] = 14

        # street_view_base64
        if data.get("street_view_base64") and "street_view_base64" not in result:
            result["street_view_base64"] = data["street_view_base64"]

        # primary_location
        if data.get("primary_location") and "primary_location" not in result:
            result["primary_location"] = data["primary_location"]

        return result if result else None

    def _build_full_dynamic_card_data(self, data: Any) -> Dict[str, Any]:
        """
        将工具返回的数据转换为 dynamic_card 的完整 fields 格式，
        确保所有字段都能被渲染展示。

        Returns:
            {"fields": [...], "nested": {...}} 格式的数据
        """
        if data is None:
            return {"fields": []}

        if not isinstance(data, dict):
            return {"raw_data": data}

        # 分离简单值和复杂结构
        simple_kvs = {}
        complex_parts = {}
        for k, v in data.items():
            if isinstance(v, (str, int, float, bool, type(None))):
                simple_kvs[k] = v
            elif isinstance(v, list) and len(v) == 0:
                # 空列表当简单值处理
                simple_kvs[k] = "（无数据）"
            else:
                complex_parts[k] = v

        # 格式化所有简单键值对
        fields = self._ui_selector._format_key_value_pairs(simple_kvs)

        result = {"fields": fields}
        if complex_parts:
            result["nested"] = complex_parts

        # 智能分区（sections）— 前端向后兼容，与 fields/nested 并存
        sections = []
        # metrics: 数值型字段
        metrics = {k: v for k, v in simple_kvs.items() if isinstance(v, (int, float))}
        if metrics:
            sections.append({"type": "metrics", "data": metrics})
        # info: 文本型字段
        info = {k: v for k, v in simple_kvs.items() if isinstance(v, str)}
        if info:
            sections.append({"type": "info", "data": info})
        # table / nested: 复杂结构
        for k, v in complex_parts.items():
            if isinstance(v, list) and v:
                sections.append({"type": "table", "key": k, "data": v})
            else:
                sections.append({"type": "nested", "key": k, "data": v})
        if sections:
            result["sections"] = sections

        return result

    def _cache_tool_result(
        self, tool_name: str, tool_output: Dict[str, Any], request,
        component_events: Optional[list] = None,
        tab_id: Optional[str] = None,
        scene_role: Optional[str] = None,
    ):
        """缓存工具结果（V4 自建缓存 + MySQL 持久化）"""
        try:
            ts_code = getattr(request, "ts_code", "") or ""
            ttl = _DEFAULT_TTL_CONFIG.get(tool_name, 300)

            cache_key = f"{tool_name}:{ts_code}" if ts_code else tool_name
            self._cache[cache_key] = _CacheEntry(
                skill_name=tool_name,
                data=tool_output,
                ts_code=ts_code,
                ttl=ttl,
            )

            logger.debug(
                f"[Hook:PostToolUse] Cached result for '{tool_name}', TTL={ttl}s"
            )

            # MySQL 持久化（异步后台任务）
            if self._persistence:
                import asyncio
                import uuid
                import json

                session_id = getattr(request, "session_id", "")
                user_id = getattr(request, "user_id", 1)
                query = getattr(request, "query", "") or getattr(request, "message", "")
                execution_id = str(uuid.uuid4())[:12]

                # 提取实际数据
                actual_data = self._extract_tool_data(tool_name, tool_output)

                # 计算数据大小和条数
                raw_data_json = json.dumps(actual_data, ensure_ascii=False, default=str) if actual_data else None
                raw_data_size = len(raw_data_json) if raw_data_json else 0
                items_count = 0
                if isinstance(actual_data, dict):
                    # 尝试从常见的列表字段计算条数
                    for key in ["results", "insights", "items", "data", "collected_data"]:
                        if key in actual_data and isinstance(actual_data[key], list):
                            items_count = len(actual_data[key])
                            break
                elif isinstance(actual_data, list):
                    items_count = len(actual_data)

                # 构建 UI 组件 JSON（用于历史会话恢复）— 存储为 JSON 数组
                ui_components_json = None
                if component_events:
                    ui_comps_list = []
                    for evt in component_events:
                        if isinstance(evt, dict) and evt.get("event") == "component":
                            comp_data = evt.get("data", {})
                            ui_comps_list.append({
                                "type": comp_data.get("component"),
                                "data": comp_data.get("data"),
                                "title": comp_data.get("title", ""),
                                "layout_hint": comp_data.get("layout_hint"),
                            })
                    if ui_comps_list:
                        ui_components_json = json.dumps(ui_comps_list, ensure_ascii=False, default=str)

                async def save_to_db():
                    try:
                        await self._persistence.save_skill_output(
                            execution_id=execution_id,
                            session_id=session_id,
                            skill_name=tool_name,
                            query=query,
                            raw_data_json=raw_data_json if raw_data_size < 65536 else None,  # < 64KB 内联存储
                            raw_data_size=raw_data_size,
                            items_count=items_count,
                            ui_components_json=ui_components_json,
                            success=True,
                            user_id=user_id,
                            tab_id=tab_id,
                            scene_role=scene_role,
                        )
                        logger.info(f"[Hook:PostToolUse] Saved skill output: {tool_name}, size={raw_data_size}, items={items_count}, has_ui={ui_components_json is not None}")
                    except Exception as e:
                        logger.warning(f"[Hook:PostToolUse] Failed to save skill output: {e}")

                # 在后台运行
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(save_to_db())
                except RuntimeError:
                    # 没有运行中的事件循环
                    pass

        except Exception as e:
            logger.debug(
                f"[Hook:PostToolUse] Cache failed for '{tool_name}': {e}"
            )

    def get_cached_result(self, tool_name: str, ts_code: str = "") -> Optional[Any]:
        """获取缓存的工具结果"""
        cache_key = f"{tool_name}:{ts_code}" if ts_code else tool_name
        entry = self._cache.get(cache_key)
        if entry and entry.is_valid():
            return entry.data
        return None

    def _extract_urls_from_result(self, tool_result: Any) -> list:
        """
        从工具返回结果中提取 URL 信息

        用于置信度评估时的 URL 打标。web_search 返回的结果格式可能是:
        1. 普通格式: {"_raw_results": [...], "results": [...]}
        2. intelligent模式: {"intelligent_data": {"collected_data": [{"source": "url", ...}]}}
        3. 文件保存格式: [{"type": "text", "text": "[{'title': '...', 'intelligent_data': {...}}]"}]

        Returns:
            URL 信息列表: [{"url": "...", "title": "..."}, ...]
        """
        import json
        import re
        import ast

        urls = []
        try:
            parsed = None

            # 如果是字符串，尝试多种解析方式
            if isinstance(tool_result, str):
                # 1. 先尝试解析为 JSON
                try:
                    parsed = json.loads(tool_result)
                except (json.JSONDecodeError, ValueError):
                    pass

                # 2. 如果是 Python 字典字符串，尝试 ast.literal_eval
                if parsed is None and tool_result.strip().startswith(('[', '{')):
                    try:
                        parsed = ast.literal_eval(tool_result)
                    except (ValueError, SyntaxError):
                        pass

                # 3. 都失败了，使用正则提取 URL
                if parsed is None:
                    url_pattern = r'https?://[^\s<>"\'}\])，,]+'
                    found_urls = re.findall(url_pattern, tool_result)
                    for url in found_urls[:20]:
                        # 清理 URL 末尾可能的特殊字符
                        url = url.rstrip(".,;:'\"]}")
                        if url and len(url) > 10:
                            urls.append({"url": url, "title": ""})
                    logger.debug(f"[_extract_urls] Regex extracted {len(urls)} URLs")
                    return urls

            elif isinstance(tool_result, dict):
                parsed = tool_result
            elif isinstance(tool_result, list):
                parsed = tool_result
            else:
                return urls

            # 处理 Claude SDK 文件保存格式: [{"type": "text", "text": "..."}]
            if isinstance(parsed, list) and len(parsed) > 0:
                first_item = parsed[0]
                if isinstance(first_item, dict) and first_item.get("type") == "text":
                    text_content = first_item.get("text", "")
                    logger.debug(f"[_extract_urls] Found text block, length={len(text_content)}")
                    # 递归解析 text 内容
                    if text_content:
                        # 尝试解析嵌套的 Python dict 字符串
                        try:
                            inner_parsed = ast.literal_eval(text_content)
                            parsed = inner_parsed
                            logger.debug(f"[_extract_urls] Parsed inner content as Python dict/list")
                        except (ValueError, SyntaxError):
                            # 尝试 JSON
                            try:
                                inner_parsed = json.loads(text_content)
                                parsed = inner_parsed
                            except:
                                # 使用正则从 text 内容中提取
                                url_pattern = r'https?://[^\s<>"\'}\])，,]+'
                                found_urls = re.findall(url_pattern, text_content)
                                for url in found_urls[:20]:
                                    url = url.rstrip(".,;:'\"]}")
                                    if url and len(url) > 10:
                                        urls.append({"url": url, "title": ""})
                                logger.debug(f"[_extract_urls] Regex from text block: {len(urls)} URLs")
                                return urls

            # 如果 parsed 是列表，处理第一个元素
            if isinstance(parsed, list) and len(parsed) > 0:
                first = parsed[0]
                if isinstance(first, dict):
                    parsed = first

            if not isinstance(parsed, dict):
                return urls

            # === 提取 URL 的多种格式 ===

            # 1. intelligent_data.collected_data[].source 格式 (intelligent 模式)
            intelligent_data = parsed.get("intelligent_data", {})
            if intelligent_data:
                collected_data = intelligent_data.get("collected_data", [])
                for item in collected_data:
                    if isinstance(item, dict):
                        url = item.get("source") or item.get("url") or item.get("link")
                        title = item.get("title", "")
                        if url and url.startswith("http"):
                            urls.append({"url": url, "title": title})
                logger.debug(f"[_extract_urls] intelligent_data: {len(urls)} URLs")

            # 2. _raw_results 格式
            raw_results = parsed.get("_raw_results", [])
            if raw_results:
                for r in raw_results:
                    if isinstance(r, dict):
                        url = r.get("link") or r.get("url") or r.get("href")
                        title = r.get("title", "")
                        if url:
                            urls.append({"url": url, "title": title})

            # 3. components 格式
            components = parsed.get("components", [])
            for comp in components:
                if isinstance(comp, dict) and comp.get("component") == "search_results":
                    for item in comp.get("data", {}).get("items", []):
                        url = item.get("link") or item.get("url")
                        title = item.get("title", "")
                        if url:
                            urls.append({"url": url, "title": title})

            # 4. results 格式
            results = parsed.get("results", [])
            for r in results:
                if isinstance(r, dict):
                    url = r.get("link") or r.get("url")
                    title = r.get("title", "")
                    if url:
                        urls.append({"url": url, "title": title})

            # 5. search_results 格式
            search_results = parsed.get("search_results", [])
            for r in search_results:
                if isinstance(r, dict):
                    url = r.get("link") or r.get("url")
                    title = r.get("title", "")
                    if url:
                        urls.append({"url": url, "title": title})

            # 6. 如果以上都没提取到，尝试从整个 dict 的字符串表示中正则提取
            if not urls:
                dict_str = str(parsed)
                url_pattern = r'https?://[^\s<>"\'}\])，,]+'
                found_urls = re.findall(url_pattern, dict_str)
                for url in found_urls[:20]:
                    url = url.rstrip(".,;:'\"]}")
                    if url and len(url) > 10:
                        urls.append({"url": url, "title": ""})
                logger.debug(f"[_extract_urls] Fallback regex: {len(urls)} URLs")

        except Exception as e:
            logger.warning(f"[Hook:PostToolUse] Failed to extract URLs: {e}")
            import traceback
            logger.debug(f"[_extract_urls] Traceback: {traceback.format_exc()}")

        # 去重
        seen = set()
        unique_urls = []
        for u in urls:
            if u["url"] not in seen:
                seen.add(u["url"])
                unique_urls.append(u)

        logger.info(f"[_extract_urls] Final extracted {len(unique_urls)} unique URLs")
        return unique_urls

    def _extract_tool_data(self, tool_name: str, tool_output: Dict[str, Any]) -> Any:
        """
        从原始 tool_output 中提取实际的工具返回数据

        Claude SDK hook 的 tool_output 结构为:
        {
            "tool_name": "mcp__sthg_v4_skills__web_search",
            "tool_input": {...},
            "tool_response": <实际数据>
        }

        本方法提取 tool_response 并进行解析:
        - 如果是 JSON 字符串，解析为字典
        - 如果是列表且第一个元素有 "type": "text"，提取 text 内容并解析
        - 否则直接返回原始数据

        Args:
            tool_name: 工具名称
            tool_output: 原始 hook 输出

        Returns:
            解析后的实际数据
        """
        import json
        import ast

        # 1. 从 tool_output 中提取 tool_response
        tool_response = tool_output.get("tool_response", tool_output)

        # 2. 如果 tool_response 本身就是原始 tool_output（没有 tool_response 字段），直接返回
        if tool_response is tool_output and "tool_response" not in tool_output:
            logger.debug(f"[_extract_tool_data] {tool_name}: No tool_response field, using raw output")
            return tool_output

        logger.debug(f"[_extract_tool_data] {tool_name}: tool_response type={type(tool_response).__name__}")

        # 2.5 检查 sidecar 文件路径（LLM 数据预算写入的全量数据）
        _sidecar_path = self._find_sidecar_path(tool_response)
        if _sidecar_path:
            sidecar_data = self._read_sidecar_file(_sidecar_path)
            if sidecar_data is not None:
                logger.info(f"[_extract_tool_data] {tool_name}: Restored full data from sidecar file")
                return sidecar_data

        # 3. 处理 Claude SDK 文件保存格式: [{"type": "text", "text": "..."}]
        if isinstance(tool_response, list) and len(tool_response) > 0:
            first_item = tool_response[0]
            if isinstance(first_item, dict) and first_item.get("type") == "text":
                text_content = first_item.get("text", "")
                logger.debug(f"[_extract_tool_data] {tool_name}: Found text block, length={len(text_content)}")

                # 尝试解析嵌套的数据
                parsed = self._parse_text_content(text_content)
                if parsed is not None:
                    return parsed
                # 解析失败，返回原始 text 内容
                return text_content

        # 4. 如果是字符串，先检查是否是"结果太大保存到文件"的情况
        if isinstance(tool_response, str):
            # 检查是否有结果保存到文件的情况
            if "exceeds maximum allowed tokens" in tool_response and "saved to" in tool_response:
                file_data = self._read_result_from_file(tool_response)
                if file_data is not None:
                    logger.info(f"[_extract_tool_data] {tool_name}: Read result from file successfully")
                    # 文件内容可能是 [{"type": "text", "text": "...JSON..."}] 格式，需要深入解析
                    if isinstance(file_data, list) and len(file_data) > 0:
                        first = file_data[0]
                        if isinstance(first, dict) and first.get("type") == "text":
                            text_content = first.get("text", "")
                            parsed = self._parse_text_content(text_content)
                            if parsed is not None:
                                logger.info(f"[_extract_tool_data] {tool_name}: Parsed text block from file data")
                                # 4.1 检查解析结果中是否有 sidecar 路径
                                if isinstance(parsed, dict) and "_full_data_path" in parsed:
                                    sidecar_data = self._read_sidecar_file(parsed["_full_data_path"])
                                    if sidecar_data is not None:
                                        logger.info(f"[_extract_tool_data] {tool_name}: Restored from sidecar via CLI tmp file")
                                        return sidecar_data
                                return parsed
                    # 4.2 检查 file_data 自身是否有 sidecar 路径
                    if isinstance(file_data, dict) and "_full_data_path" in file_data:
                        sidecar_data = self._read_sidecar_file(file_data["_full_data_path"])
                        if sidecar_data is not None:
                            logger.info(f"[_extract_tool_data] {tool_name}: Restored from sidecar via CLI tmp file (dict)")
                            return sidecar_data
                    return file_data
                # 如果读取文件失败，继续尝试解析字符串本身

            # 尝试解析字符串
            parsed = self._parse_text_content(tool_response)
            if parsed is not None:
                return parsed

        # 5. 直接返回 tool_response
        return tool_response

    def _parse_text_content(self, text_content: str) -> Any:
        """
        解析文本内容，支持 JSON 和 Python literal

        Returns:
            解析后的数据，如果解析失败返回 None
        """
        import json
        import ast

        if not text_content or not isinstance(text_content, str):
            return None

        text_content = text_content.strip()

        # 1. 尝试 JSON 解析
        if text_content.startswith(('[', '{')):
            try:
                return json.loads(text_content)
            except (json.JSONDecodeError, ValueError):
                pass

            # 2. 尝试 Python literal (如 ast.literal_eval)
            try:
                return ast.literal_eval(text_content)
            except (ValueError, SyntaxError):
                pass

        return None

    def _read_result_from_file(self, message: str) -> Any:
        """
        从文件中读取保存的工具结果

        当结果太大时，SDK会将结果保存到文件，返回类似:
        "Result exceeds maximum allowed tokens (XXXX > YYYY). Full result saved to /path/to/file."

        Args:
            message: 包含文件路径的消息

        Returns:
            解析后的文件内容，如果失败返回 None
        """
        import re
        import os
        import json
        import ast

        try:
            # 提取文件路径
            file_match = re.search(r'saved to ([^\s]+)', message)
            if not file_match:
                logger.debug(f"[_read_result_from_file] No file path found in message")
                return None

            file_path = file_match.group(1).rstrip('.')
            logger.info(f"[_read_result_from_file] Attempting to read from: {file_path}")

            if not os.path.exists(file_path):
                logger.warning(f"[_read_result_from_file] File not found: {file_path}")
                return None

            # 获取文件大小
            file_size = os.path.getsize(file_path)
            logger.info(f"[_read_result_from_file] File size: {file_size} bytes")

            # 读取完整文件内容（geo_search 含 base64 街景可达 4MB+）
            with open(file_path, 'r', encoding='utf-8') as f:
                file_content = f.read(10_000_000)  # 最多读取 10MB

            logger.info(f"[_read_result_from_file] Read {len(file_content)} chars from file")

            # 尝试解析文件内容
            if file_content.strip().startswith(('[', '{')):
                # 尝试 JSON 解析
                try:
                    return json.loads(file_content)
                except (json.JSONDecodeError, ValueError):
                    pass

                # 尝试 Python literal
                try:
                    return ast.literal_eval(file_content)
                except (ValueError, SyntaxError):
                    pass

            # 解析失败，返回原始字符串（这样 UI 选择器可以尝试正则提取）
            return file_content

        except Exception as e:
            logger.warning(f"[_read_result_from_file] Failed to read file: {e}")
            import traceback
            logger.debug(f"[_read_result_from_file] Traceback: {traceback.format_exc()}")
            return None

    def _find_sidecar_path(self, tool_response: Any) -> Optional[str]:
        """
        从 tool_response 中检测 _full_data_path（sidecar 文件路径）

        支持 3 种 tool_response 格式:
        - str: JSON 字符串
        - dict: 直接 dict
        - list: [{"type": "text", "text": "...JSON..."}]
        """
        import json as _json

        if isinstance(tool_response, str):
            try:
                parsed = _json.loads(tool_response)
                if isinstance(parsed, dict) and "_full_data_path" in parsed:
                    return parsed["_full_data_path"]
            except (ValueError, TypeError):
                pass
            return None

        if isinstance(tool_response, dict) and "_full_data_path" in tool_response:
            return tool_response["_full_data_path"]

        if isinstance(tool_response, list) and len(tool_response) > 0:
            first = tool_response[0]
            if isinstance(first, dict) and first.get("type") == "text":
                try:
                    parsed = _json.loads(first.get("text", ""))
                    if isinstance(parsed, dict) and "_full_data_path" in parsed:
                        return parsed["_full_data_path"]
                except (ValueError, TypeError):
                    pass

        return None

    def _read_sidecar_file(self, file_path: str) -> Any:
        """
        读取 sidecar 文件（由 stdio_mcp_server 写入的全量数据）

        与 _read_result_from_file 类似，但路径已知（不需要从消息中提取）。
        """
        import os
        import json

        try:
            if not file_path or not os.path.exists(file_path):
                logger.debug(f"[_read_sidecar_file] File not found: {file_path}")
                return None

            file_size = os.path.getsize(file_path)
            logger.info(f"[_read_sidecar_file] Reading sidecar: {file_path} ({file_size} bytes)")

            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read(10_000_000)

            if content.strip().startswith(('[', '{')):
                try:
                    return json.loads(content)
                except (json.JSONDecodeError, ValueError):
                    pass

            return content

        except Exception as e:
            logger.warning(f"[_read_sidecar_file] Failed to read: {e}")
            return None

    # ── CLAW 扩展: Session 生命周期 + MessageSending Hook ──

    def register_session_start_hook(self, hook):
        """注册 SessionStart hook（Void 类型，不修改数据）"""
        self._session_start_hooks.append(hook)

    def register_session_end_hook(self, hook):
        """注册 SessionEnd hook（Void 类型，不修改数据）"""
        self._session_end_hooks.append(hook)

    def register_message_sending_hook(self, hook):
        """注册 MessageSending hook（Modifying 类型，可修改消息内容）"""
        self._message_sending_hooks.append(hook)

    async def fire_session_start(
        self, session_id: str, user_id: Any, metadata: Optional[Dict] = None,
    ) -> None:
        """
        触发 SessionStart hooks（Void 类型，并行触发）

        Args:
            session_id: 会话 ID
            user_id: 用户 ID
            metadata: 附加元数据
        """
        if not self._session_start_hooks:
            return

        tasks = []
        for hook in self._session_start_hooks:
            tasks.append(self._safe_call_hook(hook, session_id, user_id, metadata))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            logger.debug(
                f"[HookManager] SessionStart fired: "
                f"{len(self._session_start_hooks)} hooks"
            )

    async def fire_session_end(
        self, session_id: str, user_id: Any, stats: Optional[Dict] = None,
    ) -> None:
        """
        触发 SessionEnd hooks（Void 类型，并行触发）

        Args:
            session_id: 会话 ID
            user_id: 用户 ID
            stats: 会话统计数据
        """
        if not self._session_end_hooks:
            return

        tasks = []
        for hook in self._session_end_hooks:
            tasks.append(self._safe_call_hook(hook, session_id, user_id, stats))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            logger.debug(
                f"[HookManager] SessionEnd fired: "
                f"{len(self._session_end_hooks)} hooks"
            )

    async def fire_message_sending(
        self, session_id: str, message: str,
    ) -> Optional[str]:
        """
        触发 MessageSending hooks（Modifying 类型，串行触发，可修改消息内容）

        Args:
            session_id: 会话 ID
            message: 原始消息内容

        Returns:
            修改后的消息内容（如果有修改），否则返回原始消息
        """
        if not self._message_sending_hooks:
            return message

        current_message = message
        for hook in self._message_sending_hooks:
            try:
                if asyncio.iscoroutinefunction(hook):
                    result = await hook(session_id, current_message)
                else:
                    result = hook(session_id, current_message)
                if result is not None and isinstance(result, str):
                    current_message = result
            except Exception as e:
                logger.warning(f"[HookManager] MessageSending hook error: {e}")
                # 错误隔离: 继续执行下一个 hook

        return current_message

    @staticmethod
    async def _safe_call_hook(hook, *args):
        """安全调用 hook（错误隔离）"""
        try:
            if asyncio.iscoroutinefunction(hook):
                await hook(*args)
            else:
                hook(*args)
        except Exception as e:
            logger.warning(f"[HookManager] Hook error: {e}")
