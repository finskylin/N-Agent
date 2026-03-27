"""
AgentLoop — 核心 Agent 执行循环

替换 ClaudeSDKClient 的全部能力:
- LLM 对话循环 (call → tools → call → ... → final)
- 工具执行（进程内，保证 PostToolUse hook 拿到完整数据）
- Hook 触发（开放式，可插拔）
- SSE 事件流（所有事件携带 agent_id，可区分父/子代理）
- Session/记忆/学习集成

主循环流程:
1. session_engine.prepare_session() → 获取历史/经验/知识
2. context_builder.build_initial_messages()
3. hook_engine.fire(ON_LOOP_START)
4. while iteration < max_iterations:
     a. hook_engine.fire(PRE_LLM_CALL)
     b. llm_provider.chat_stream()
     c. hook_engine.fire(POST_LLM_CALL)
     d. if response.has_tool_calls:
          [readonly 组] asyncio.gather 并行执行
          [write 组]    顺序执行（现有逻辑）
     e. 上下文压缩检查
     f. else: yield text_delta; break (含输出校验/重试)
5. hook_engine.fire(ON_LOOP_END)
6. session_engine.finalize()
7. yield done event
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional
from loguru import logger

from .message_types import LLMResponse, SessionContext, ToolCallRequest, ToolResult
from .hook_engine import HookEngine, HookPoint
from .skill_invoker import SkillInvoker
from .llm_provider import LiteLLMProvider
from .context_builder import ContextBuilder
from .session_engine import SessionEngine
from .stream_adapter import (
    make_text_delta, make_thinking, make_tool_call, make_tool_done,
    make_error, make_done, make_report_lang, make_status,
)
from .parallel_executor import _NullParallelExecutor


class AgentLoop:
    """
    核心 Agent 循环

    每个实例拥有唯一 agent_id。所有 hook context 和 SSE 事件均携带
    agent_id，父/子代理产生的事件可通过 agent_id / parent_agent_id 区分。
    """

    def __init__(
        self,
        llm_provider: LiteLLMProvider,
        skill_invoker: SkillInvoker,
        hook_engine: HookEngine,
        session_engine: SessionEngine,
        context_builder: ContextBuilder,
        event_bridge,            # EventBridge 对象（现有实现）
        config,                  # V4Config
        max_iterations: int = 100,
        max_timeout_seconds: int = 1440,
        # === opt-in 组件 ===
        parallel_executor=None,          # Phase 2
        compactor=None,                  # Phase 3
        permission_guard=None,           # Phase 4
        output_validator=None,           # Phase 5
        agent_id: Optional[str] = None,          # 唯一标识（不传则自动生成）
        parent_agent_id: Optional[str] = None,   # 父代理 ID（子代理时传入）
    ):
        self._llm = llm_provider
        self._invoker = skill_invoker
        self._hooks = hook_engine
        self._session = session_engine
        self._builder = context_builder
        self._event_bridge = event_bridge
        self._config = config
        self._max_iterations = max_iterations
        self._max_timeout_seconds = max_timeout_seconds
        # opt-in
        self._parallel_executor = parallel_executor or _NullParallelExecutor()
        self._compactor = compactor
        self._permission_guard = permission_guard
        self._output_validator = output_validator
        # 唯一标识
        self.agent_id: str = agent_id or f"agent-{uuid.uuid4().hex[:8]}"
        self.parent_agent_id: Optional[str] = parent_agent_id
        # 将 agent_id 同步到 HookEngine
        if hook_engine is not None:
            hook_engine.agent_id = self.agent_id

    # ─── 辅助：给所有 hook context 注入 agent_id ───────────────────

    def _ctx(self, base: Dict[str, Any]) -> Dict[str, Any]:
        base["agent_id"] = self.agent_id
        if self.parent_agent_id:
            base["parent_agent_id"] = self.parent_agent_id
        return base

    # ─── 辅助：给 SSE 事件注入 agent_id（子代理事件需区分）─────────

    def _tag(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """仅当存在 parent_agent_id（即子代理）时，才在事件 data 中注入标识"""
        if self.parent_agent_id:
            data = event.get("data", {})
            data["agent_id"] = self.agent_id
            data["parent_agent_id"] = self.parent_agent_id
            event = dict(event)
            event["data"] = data
        return event

    # ─── 主循环 ─────────────────────────────────────────────────────

    async def run(
        self,
        system_prompt: str,
        user_message: str,
        request,             # V4AgentRequest
        data_collector=None, # DataCollector
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        主循环，yield SSE 事件。

        所有事件在子代理模式下（parent_agent_id 非空）会额外携带
        agent_id / parent_agent_id，便于消费方区分来源。
        """
        session_id = getattr(request, "session_id", "default")
        user_id = int(getattr(request, "user_id", 0) or 0)
        report_lang = getattr(request, "report_lang", "zh") or "zh"
        attached_files = getattr(request, "attached_files", None) or []

        logger.info(
            f"[AgentLoop:{self.agent_id}] Starting"
            + (f" (parent={self.parent_agent_id})" if self.parent_agent_id else "")
        )

        # ── 输出报告语种 ──
        yield self._tag(make_report_lang(report_lang))

        # ── 1. 准备 session 上下文 ──
        try:
            session_ctx = await self._session.prepare_session(
                session_id=session_id,
                user_id=user_id,
                query=user_message,
            )
        except Exception as e:
            logger.warning(f"[AgentLoop:{self.agent_id}] prepare_session failed: {e}")
            session_ctx = SessionContext(session_id=session_id, user_id=user_id)

        # ── 2. 构建初始消息数组 ──
        self._builder.reset()
        self._builder.build_initial_messages(
            user_message=user_message,
            history_messages=session_ctx.history_messages,
            attached_files=attached_files,
        )

        # ── 3. 获取工具定义（含当前用户的私有 Skill）──
        tools = self._invoker.get_tool_definitions(user_id=str(user_id) if user_id else None)

        # ── 4. ON_LOOP_START hook ──
        try:
            await self._hooks.fire(HookPoint.ON_LOOP_START, self._ctx({
                "request": request,
                "system_prompt": system_prompt,
            }))
        except Exception as e:
            logger.debug(f"[AgentLoop:{self.agent_id}] ON_LOOP_START hook failed: {e}")

        # ── 5. 启动 episode 追踪 ──
        self._session.start_episode(user_message)

        # ── Langfuse trace（可选）──
        _lf_trace = getattr(request, "langfuse_trace", None)

        # ── 主循环 ──
        accumulated_text: List[str] = []
        _injected_skill_mds: set = set()  # 已注入过 SKILL.md 的 skill 名称集合
        tools_used: List[str] = []
        iteration = 0
        final_response: Optional[LLMResponse] = None
        _loop_start_time = time.time()
        _loop_exit_reason: str = "completed"  # completed | llm_error | truncated | max_iterations | timeout
        _truncation_retry_count: int = 0
        _max_truncation_retry: int = getattr(self._config, "loop_max_truncation_retry", 2)

        while iteration < self._max_iterations:
            # 时间维度检查
            _elapsed = time.time() - _loop_start_time
            if _elapsed > self._max_timeout_seconds:
                logger.warning(
                    f"[AgentLoop:{self.agent_id}] Timeout after {_elapsed:.0f}s "
                    f"(limit={self._max_timeout_seconds}s) at iteration {iteration}"
                )
                _loop_exit_reason = "timeout"
                break

            iteration += 1
            logger.info(
                f"[AgentLoop:{self.agent_id}] Iteration {iteration}/{self._max_iterations} "
                f"(elapsed={_elapsed:.0f}s/{self._max_timeout_seconds}s)"
            )

            # ── a. PRE_LLM_CALL hook ──
            messages = self._builder.messages
            try:
                hook_ctx = await self._hooks.fire(HookPoint.PRE_LLM_CALL, self._ctx({
                    "messages": messages,
                    "tools": tools,
                    "model": getattr(self._config, "anthropic_model", ""),
                }))
                messages = hook_ctx.get("messages", messages)
                tools = hook_ctx.get("tools", tools)
            except Exception as e:
                logger.debug(f"[AgentLoop:{self.agent_id}] PRE_LLM_CALL hook failed: {e}")

            # ── b. 调用 LLM（流式）──
            current_response: Optional[LLMResponse] = None
            pending_tool_calls: List[ToolCallRequest] = []

            # Langfuse generation span（fire-and-forget，失败不影响主流程）
            _lf_gen = None
            try:
                if _lf_trace is not None:
                    _model = getattr(self._config, "anthropic_model", "")
                    _lf_gen = _lf_trace.start_generation(
                        name=f"llm_call_iter_{iteration}",
                        model=_model,
                        input=messages[-1] if messages else {},
                    )
            except Exception:
                _lf_gen = None

            _llm_max_retries = 2  # 最多重试次数（切换 endpoint）
            _llm_attempt = 0
            _llm_failed = False
            # LLM streaming 单轮最长等待时间（防止 stream 中途静默挂死）
            _llm_stream_timeout = getattr(self._config, "llm_stream_timeout_seconds", 180)
            while _llm_attempt <= _llm_max_retries:
                _llm_attempt += 1
                _round_text: List[str] = []
                _round_tool_calls: List[ToolCallRequest] = []
                _round_response: Optional[LLMResponse] = None
                try:
                    async for llm_event in self._llm.chat_stream(
                        messages=messages,
                        tools=tools if tools else None,
                        system=system_prompt,
                        max_tokens=getattr(self._config, "max_tokens_per_turn", 16384),
                        timeout=_llm_stream_timeout,
                    ):
                        event_type = llm_event.get("type", "")

                        if event_type == "text_delta":
                            delta = llm_event["delta"]
                            _round_text.append(delta)
                            accumulated_text.append(delta)
                            yield self._tag(make_text_delta(delta))
                            async for hook_evt in self._drain_event_bridge():
                                yield self._tag(hook_evt)

                        elif event_type == "thinking":
                            yield self._tag(make_thinking(llm_event.get("thinking", "")))

                        elif event_type == "tool_call_complete":
                            _round_tool_calls.append(llm_event["tool_call"])
                            pending_tool_calls.append(llm_event["tool_call"])

                        elif event_type == "llm_response":
                            _round_response = llm_event["response"]
                            current_response = _round_response

                    # 成功，退出重试循环
                    break

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(
                        f"[AgentLoop:{self.agent_id}] LLM stream attempt {_llm_attempt}/{_llm_max_retries+1} "
                        f"failed: {type(e).__name__}: {e}"
                    )
                    try:
                        if _lf_gen is not None:
                            _lf_gen.update(level="ERROR", status_message=str(e)).end()
                            _lf_gen = None
                    except Exception:
                        pass
                    if _llm_attempt > _llm_max_retries:
                        logger.error(
                            f"[AgentLoop:{self.agent_id}] LLM stream all {_llm_attempt} attempts failed, "
                            f"injecting error into context and breaking"
                        )
                        _llm_failed = True
                        break
                    # 重试前清空本轮已累积的内容（下一个 endpoint 会重新请求）
                    for item in _round_text:
                        if item in accumulated_text:
                            accumulated_text.remove(item)
                    pending_tool_calls = [tc for tc in pending_tool_calls if tc not in _round_tool_calls]
                    logger.info(f"[AgentLoop:{self.agent_id}] Retrying LLM call (attempt {_llm_attempt+1})...")

            if _llm_failed:
                # 所有 endpoint 都失败，向 messages 注入错误提示让 loop 继续（不 break）
                _err_hint = (
                    "[系统提示] 由于 LLM 服务暂时不可用，请根据已获取到的信息直接给出当前最佳回答，"
                    "不要再调用任何工具。"
                )
                self._builder._messages.append({"role": "user", "content": _err_hint})
                yield self._tag(make_status("llm_error_recovered", {"message": "LLM call failed, injecting fallback hint"}))
                logger.warning(f"[AgentLoop:{self.agent_id}] Injected fallback hint, continuing loop")
                # current_response 为 None，下面会构建空响应，loop 继续
                # 如果连续失败太多次则最终由 max_iterations 或 timeout 兜底

            # Langfuse: 记录 LLM 响应
            try:
                if _lf_gen is not None and current_response is not None:
                    _usage = getattr(current_response, "usage", {}) or {}
                    _lf_gen.update(
                        output=current_response.content or "",
                        usage={
                            "input": _usage.get("input_tokens", 0),
                            "output": _usage.get("output_tokens", 0),
                        },
                    ).end()
            except Exception:
                pass

            if current_response is None:
                current_response = LLMResponse(
                    content="".join(accumulated_text) if accumulated_text else None,
                    tool_calls=pending_tool_calls,
                    finish_reason="tool_use" if pending_tool_calls else "stop",
                )


            # ── c. POST_LLM_CALL hook ──
            try:
                await self._hooks.fire(HookPoint.POST_LLM_CALL, self._ctx({
                    "response": current_response,
                    "messages": self._builder.messages,
                }))
            except Exception as e:
                logger.debug(f"[AgentLoop:{self.agent_id}] POST_LLM_CALL hook failed: {e}")

            self._builder.add_llm_response(current_response)
            final_response = current_response

            # ── a0. 预检：本轮 LLM 想调用的 skill 是否首次使用 ──
            # 放在 LLM 响应之后、工具执行之前，确保第一轮 tool_call 也能触发注入。
            # 若有 skill 首次被调用（未注入过 SKILL.md），注入完整文档后 continue，
            # 让 LLM 重新阅读文档后生成正确参数再调用。
            if current_response.has_tool_calls:
                from pathlib import Path as _Path
                # 内置工具不走 pre-inject（它们通过 function calling 直接执行，不需要 SKILL.md 注入）
                _BUILTIN_TOOL_NAMES = frozenset({"bash", "read_file", "grep", "spawn_agent", "query_subagent"})
                _new_skills = []
                for _tc in current_response.tool_calls:
                    _sname = _tc.name
                    if _sname in _injected_skill_mds:
                        continue
                    if _sname in _BUILTIN_TOOL_NAMES:
                        continue
                    try:
                        _meta = self._invoker._discovery.get_by_name(_sname)
                        if _meta and getattr(_meta, "skill_dir", ""):
                            _md_path = _Path(_meta.skill_dir) / "SKILL.md"
                            if _md_path.exists():
                                _new_skills.append((_sname, _md_path))
                    except Exception:
                        pass
                if _new_skills:
                    import re as _re
                    _skill_md_parts = []
                    for _sname, _md_path in _new_skills:
                        _md_text = _md_path.read_text(encoding="utf-8")
                        # 只注入 body（剥离 frontmatter），body 里含 bash 调用示例
                        _fm_match = _re.match(r"^---\s*\n.*?\n---\s*\n?(.*)", _md_text, _re.DOTALL)
                        _body = _fm_match.group(1).strip() if _fm_match else _md_text.strip()
                        _skill_md_parts.append(f"## Skill: {_sname}\n\n{_body}")
                        _injected_skill_mds.add(_sname)
                    _inject_content = (
                        "[Skill Reference — 以下是本次调用技能的完整使用说明，请仔细阅读后重新组织参数再调用]\n\n"
                        + "\n\n---\n\n".join(_skill_md_parts)
                    )
                    # 撤销刚追加的 assistant 消息，让 LLM 重新生成带正确参数的调用
                    if (self._builder._messages
                            and self._builder._messages[-1].get("role") == "assistant"):
                        self._builder._messages.pop()
                    self._builder._messages.append({"role": "user", "content": _inject_content})
                    logger.info(
                        f"[AgentLoop:{self.agent_id}] Pre-inject SKILL.md for first-time skills: "
                        f"{[s for s, _ in _new_skills]}, skipping execution this turn"
                    )
                    final_response = None
                    continue

            # ── d. 处理工具调用 ──
            if not current_response.has_tool_calls:
                _stop_reason = (current_response.finish_reason or "").lower()

                # 1. LLM 错误 → 立即终止
                if _stop_reason == "error":
                    logger.error(f"[AgentLoop:{self.agent_id}] LLM returned error, stopping loop")
                    _loop_exit_reason = "llm_error"
                    break

                # 2. 输出截断 → 注入续写提示，重试
                if _stop_reason in ("max_tokens", "length"):
                    if _truncation_retry_count < _max_truncation_retry:
                        _truncation_retry_count += 1
                        logger.warning(
                            f"[AgentLoop:{self.agent_id}] Output truncated, retry "
                            f"{_truncation_retry_count}/{_max_truncation_retry}"
                        )
                        _continuation_hint = {
                            "role": "user",
                            "content": "你的回答被截断了，请继续从截断处接着输出，不要重复已有内容。",
                        }
                        self._builder._messages.append(_continuation_hint)
                        continue
                    else:
                        logger.warning(f"[AgentLoop:{self.agent_id}] Truncation retry exhausted, stopping")
                        _loop_exit_reason = "truncated"
                        break

                # 3. pause_turn → 追加历史，continue（不消耗 iteration）
                if _stop_reason == "pause_turn":
                    logger.info(
                        f"[AgentLoop:{self.agent_id}] pause_turn, continuing without consuming iteration"
                    )
                    iteration -= 1  # 不计入迭代次数
                    continue

                # 4. 正常完成（end_turn / stop / null / 其他）
                # Phase 5: 输出校验（现有逻辑不变）
                if self._output_validator and getattr(self._output_validator, "enabled", False):
                    final_text = "".join(accumulated_text)
                    is_valid, error = self._output_validator.validate(final_text)
                    if not is_valid and self._output_validator.can_retry:
                        retry_msg = self._output_validator.build_retry_message(error)
                        self._builder._messages.append({"role": "user", "content": retry_msg})
                        accumulated_text.clear()
                        yield self._tag(make_status("output_validation_retry", {"error": error}))
                        logger.info(f"[AgentLoop:{self.agent_id}] Output validation retry: {error[:100]}")
                        continue
                _loop_exit_reason = "completed"
                break

            tool_results: List[ToolResult] = []

            # ── d.1 分组 ──
            readonly_group, write_group = self._parallel_executor.partition(
                current_response.tool_calls
            )

            # ── d.2 并行执行 readonly 组 ──
            if readonly_group:
                logger.info(
                    f"[AgentLoop:{self.agent_id}] Parallel: {len(readonly_group)} readonly tools: "
                    f"{[tc.name for tc in readonly_group]}"
                )
                pre_results: Dict[str, Any] = {}
                blocked_ids: set = set()

                for tc in readonly_group:
                    tool_name = tc.name
                    tools_used.append(tool_name)
                    try:
                        pre_ctx = await self._hooks.fire(HookPoint.PRE_TOOL_USE, self._ctx({
                            "tool_name": tool_name,
                            "tool_input": tc.arguments,
                            "request": request,
                        }))
                        tc_args = pre_ctx.get("tool_input", tc.arguments)
                        pre_result = pre_ctx.get("_pre_result", {})
                        if isinstance(pre_result, dict) and pre_result.get("decision") == "block":
                            tool_results.append(ToolResult(
                                tool_call_id=tc.id, name=tool_name,
                                content=json.dumps({"error": pre_result.get("reason", "blocked")}),
                                is_error=True,
                            ))
                            blocked_ids.add(tc.id)
                            continue
                        if isinstance(pre_result, dict) and pre_result.get("decision") == "handled":
                            tool_results.append(ToolResult(
                                tool_call_id=tc.id, name=tool_name,
                                content=json.dumps({"status": "ok", "message": pre_result.get("reason", "handled by hook")}),
                                is_error=False,
                            ))
                            blocked_ids.add(tc.id)
                            continue
                        pre_results[tc.id] = tc_args
                    except Exception as e:
                        logger.debug(f"[AgentLoop:{self.agent_id}] PRE_TOOL_USE hook failed: {e}")
                        pre_results[tc.id] = tc.arguments

                    if self._permission_guard:
                        try:
                            decision = await self._permission_guard.check(
                                tool_name, pre_results.get(tc.id, tc.arguments), self._hooks
                            )
                            if not decision.allowed:
                                tool_results.append(ToolResult(
                                    tool_call_id=tc.id, name=tool_name,
                                    content=json.dumps({"error": decision.reason}),
                                    is_error=True,
                                ))
                                blocked_ids.add(tc.id)
                        except Exception as e:
                            logger.debug(f"[AgentLoop:{self.agent_id}] PermissionGuard failed: {e}")

                    yield self._tag(make_tool_call(tool_name, pre_results.get(tc.id, tc.arguments)))

                parallel_calls = [tc for tc in readonly_group if tc.id not in blocked_ids]
                if parallel_calls:
                    for tc in parallel_calls:
                        if tc.id in pre_results:
                            tc.arguments = pre_results[tc.id]
                        # 为 spawn_agent 注入父代理标识和 session 上下文
                        if tc.name == "spawn_agent":
                            tc.arguments = dict(tc.arguments)
                            tc.arguments["_parent_agent_id"] = self.agent_id
                            tc.arguments["_depth"] = int(bool(self.parent_agent_id))
                            tc.arguments["_parent_session_id"] = getattr(request, "session_id", "")
                            tc.arguments["_user_id"] = getattr(request, "user_id", 0) or 0

                    parallel_results = await self._parallel_executor.execute_parallel(
                        parallel_calls, str(user_id), session_id
                    )

                    for tc, result in zip(parallel_calls, parallel_results):
                        tool_results.append(result)
                        if data_collector is not None:
                            try:
                                data_collector.add_tool_call(tc.name, tc.arguments)
                                data_collector.add_skill_result(tc.name, result.raw_data, result.duration_ms)
                            except Exception:
                                pass
                        self._session.record_tool_execution(
                            skill_name=tc.name, params=tc.arguments,
                            result_summary=result.content[:200], duration_ms=result.duration_ms,
                        )
                        try:
                            await self._hooks.fire(HookPoint.POST_TOOL_USE, self._ctx({
                                "tool_name": tc.name, "tool_input": tc.arguments,
                                "tool_result": result, "duration_ms": result.duration_ms,
                                "request": request,
                            }))
                        except Exception as e:
                            logger.debug(f"[AgentLoop:{self.agent_id}] POST_TOOL_USE hook failed: {e}")
                        async for hook_evt in self._drain_event_bridge():
                            yield self._tag(hook_evt)
                        yield self._tag(make_tool_done(tc.name, result))

            # ── d.3 顺序执行 write 组 ──
            for tc in write_group:
                tool_name = tc.name
                tools_used.append(tool_name)

                try:
                    pre_ctx = await self._hooks.fire(HookPoint.PRE_TOOL_USE, self._ctx({
                        "tool_name": tool_name,
                        "tool_input": tc.arguments,
                        "request": request,
                    }))
                    tc_args = pre_ctx.get("tool_input", tc.arguments)
                    pre_result = pre_ctx.get("_pre_result", {})
                    if isinstance(pre_result, dict) and pre_result.get("decision") == "block":
                        tool_results.append(ToolResult(
                            tool_call_id=tc.id, name=tool_name,
                            content=json.dumps({"error": pre_result.get("reason", "blocked")}),
                            is_error=True,
                        ))
                        continue
                    if isinstance(pre_result, dict) and pre_result.get("decision") == "handled":
                        tool_results.append(ToolResult(
                            tool_call_id=tc.id, name=tool_name,
                            content=json.dumps({"status": "ok", "message": pre_result.get("reason", "handled by hook")}),
                            is_error=False,
                        ))
                        continue
                except Exception as e:
                    logger.debug(f"[AgentLoop:{self.agent_id}] PRE_TOOL_USE hook failed: {e}")
                    tc_args = tc.arguments

                if self._permission_guard:
                    try:
                        decision = await self._permission_guard.check(tool_name, tc_args, self._hooks)
                        if not decision.allowed:
                            tool_results.append(ToolResult(
                                tool_call_id=tc.id, name=tool_name,
                                content=json.dumps({"error": decision.reason}),
                                is_error=True,
                            ))
                            continue
                    except Exception as e:
                        logger.debug(f"[AgentLoop:{self.agent_id}] PermissionGuard failed: {e}")

                yield self._tag(make_tool_call(tool_name, tc_args))

                # Phase 6: 为 spawn_agent 注入父代理标识和 session 上下文
                invoke_args = tc_args
                if tool_name == "spawn_agent":
                    invoke_args = dict(tc_args)
                    invoke_args["_parent_agent_id"] = self.agent_id
                    invoke_args["_depth"] = int(bool(self.parent_agent_id))
                    invoke_args["_parent_session_id"] = getattr(request, "session_id", "")
                    invoke_args["_user_id"] = getattr(request, "user_id", 0) or 0

                # 注入请求上下文参数（dingtalk_* / feishu_*）到技能参数
                # 确保 cron / send_message 等技能能读取渠道信息，不依赖 LLM 手动传递
                _req_params = getattr(request, "params", None)
                if _req_params and isinstance(_req_params, dict):
                    _ctx_keys = [
                        k for k in _req_params
                        if k.startswith("dingtalk_") or k.startswith("feishu_")
                    ]
                    if _ctx_keys:
                        if invoke_args is tc_args:
                            invoke_args = dict(tc_args)
                        for _k in _ctx_keys:
                            if _k not in invoke_args:
                                invoke_args[_k] = _req_params[_k]

                result = await self._invoker.invoke(
                    skill_name=tool_name,
                    arguments=invoke_args,
                    tool_call_id=tc.id,
                    user_id=str(user_id),
                    session_id=session_id,
                )
                tool_results.append(result)

                if data_collector is not None:
                    try:
                        data_collector.add_tool_call(tool_name, tc_args)
                        data_collector.add_skill_result(tool_name, result.raw_data, result.duration_ms)
                    except Exception:
                        pass

                self._session.record_tool_execution(
                    skill_name=tool_name, params=tc_args,
                    result_summary=result.content[:200], duration_ms=result.duration_ms,
                )

                try:
                    await self._hooks.fire(HookPoint.POST_TOOL_USE, self._ctx({
                        "tool_name": tool_name, "tool_input": tc_args,
                        "tool_result": result, "duration_ms": result.duration_ms,
                        "request": request,
                    }))
                except Exception as e:
                    logger.debug(f"[AgentLoop:{self.agent_id}] POST_TOOL_USE hook failed: {e}")

                async for hook_evt in self._drain_event_bridge():
                    yield self._tag(hook_evt)

                yield self._tag(make_tool_done(tool_name, result))

            # 将工具结果追加到消息数组
            self._builder.add_tool_results(tool_results)

            # ── e. Phase 3: 上下文压缩检查 ──
            if self._compactor:
                try:
                    from agent_core.session.context_window_guard import ContextWindowGuard
                    _guard = ContextWindowGuard(
                        model_name=getattr(self._config, "model_name", ""),
                        context_window_override=getattr(self._config, "context_window_override", 0),
                    )
                    # 65% 作为 compactor 压缩阈值（session_file 50% + system_prompt 25% - generation 15% 冗余）
                    token_budget = int(_guard.resolve_context_window() * 0.65)
                    compacted = await self._compactor.maybe_compact(self._builder, token_budget)
                    if compacted:
                        yield self._tag(make_status("context_compacted", {"compaction": True}))
                        logger.info(f"[AgentLoop:{self.agent_id}] Context compacted")
                except Exception as e:
                    logger.debug(f"[AgentLoop:{self.agent_id}] Context compaction failed: {e}")

        else:
            _total_elapsed = time.time() - _loop_start_time
            logger.warning(
                f"[AgentLoop:{self.agent_id}] Loop ended: iterations={iteration}/{self._max_iterations}, "
                f"elapsed={_total_elapsed:.0f}s/{self._max_timeout_seconds}s"
            )
            _loop_exit_reason = "max_iterations"

        # ── 6. ON_LOOP_END hook ──
        logger.info(f"[AgentLoop:{self.agent_id}] exit_reason={_loop_exit_reason} iterations={iteration} elapsed={time.time() - _loop_start_time:.0f}s")
        final_text = "".join(accumulated_text)
        try:
            await self._hooks.fire(HookPoint.ON_LOOP_END, self._ctx({
                "final_content": final_text,
                "tools_used": tools_used,
                "total_iterations": iteration,
                "exit_reason": _loop_exit_reason,
                "loop_normal_exit": _loop_exit_reason == "completed",  # 向后兼容
                "elapsed": time.time() - _loop_start_time,
                "request": request,
            }))
        except Exception as e:
            logger.debug(f"[AgentLoop:{self.agent_id}] ON_LOOP_END hook failed: {e}")

        async for hook_evt in self._drain_event_bridge():
            yield self._tag(hook_evt)

        # ── 7. done 事件（立即发出，不等后处理）──
        yield self._tag(make_done(
            final_text=final_text,
            tools_used=tools_used,
            total_iterations=iteration,
            metadata={"agent_id": self.agent_id},
        ))

        # ── 8. 后台保存历史/经验/知识（不阻塞响应流）──
        asyncio.create_task(self._safe_finalize_bg(
            session_id=session_id,
            user_id=user_id,
            final_text=final_text,
            user_message=user_message,
            tools_used=tools_used,
        ))

    async def _safe_finalize_bg(
        self,
        session_id: str,
        user_id: int,
        final_text: str,
        user_message: str,
        tools_used: List[str],
    ) -> None:
        """后台执行 session finalize，异常不影响用户响应"""
        try:
            await self._session.finalize(
                session_id=session_id,
                user_id=user_id,
                assistant_text=final_text,
                request_message=user_message,
                tool_calls_summary=tools_used,
            )
        except Exception as e:
            logger.warning(f"[AgentLoop:{self.agent_id}] bg finalize failed: {e}")

    async def _drain_event_bridge(self) -> AsyncIterator[Dict[str, Any]]:
        """排空 event_bridge 队列，yield 所有待推送的 hook 事件"""
        if self._event_bridge is None:
            return
        try:
            event_queue = self._event_bridge.get_event_queue()
            while not event_queue.empty():
                try:
                    event = event_queue.get_nowait()
                    yield event
                except asyncio.QueueEmpty:
                    break
        except Exception as e:
            logger.debug(f"[AgentLoop:{self.agent_id}] drain_event_bridge error: {e}")
