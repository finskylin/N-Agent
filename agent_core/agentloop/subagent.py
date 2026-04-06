"""
SubAgent — 子代理系统

注册 spawn_agent 为内置工具。LLM 调用时创建独立 AgentLoop 实例执行子任务。

v3 变更：
1. 独立 HookEngine：子代理拥有完全独立的 Hook 链路，不共享也不污染父级
2. SSE 事件透传：子代理的所有事件通过 parent_event_bridge 推送给父级，
   事件 data 携带 agent_id / parent_agent_id，消费方可区分来源
3. HookPoint 生命周期：ON_SUBAGENT_START / ON_SUBAGENT_END 在父级 HookEngine 触发
4. 全量工具集：子代理默认使用与父代理完全相同的工具集
5. 持久化上下文：子代理历史落库（ConversationHistory），供 query_subagent 查询
6. 轻量 finalize：只保存对话历史，跳过 Episode 蒸馏和 Memory 更新（用完即弃）
7. data2ui 复用：每个子代理 new 独立 LegacyHookPlugin（同款 event_bridge），
   触发 component_for_render 事件，前端通过 agent_id 区分父子来源
8. 并发安全：多个并行子代理各自持有独立 plugin 实例，无共享可变状态
"""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Any, Callable, Dict, List, Optional
from loguru import logger


class SubAgentExecutor:
    """
    子代理执行器

    调用链：
        父 AgentLoop.run()
          → LLM 调用 spawn_agent
          → SkillInvoker.invoke("spawn_agent")
          → SubAgentExecutor.execute()
              → 计算 sub_session_id，写 SubAgentRecord（running）
              → session_engine.prepare_session（历史+记忆注入）
              → 创建独立 HookEngine + 独立 LegacyHookPlugin（含 data2ui）
              → 创建独立 AgentLoop（agent_id 新生成，parent_agent_id=父id）
              → 触发父级 ON_SUBAGENT_START hook
              → sub_loop.run() 完整多轮循环
                  → 所有 SSE 事件 push 到 parent_event_bridge（带 agent_id 标记）
                  → 子代理工具 POST_TOOL_USE → component_for_render → 前端渲染
              → session_engine.finalize_lightweight（仅历史落库）
              → 更新 SubAgentRecord（completed）
              → 触发父级 ON_SUBAGENT_END hook
              → 返回最终文本给父级 ToolResult
    """

    def __init__(
        self,
        llm_provider,               # LiteLLMProvider
        skill_invoker,              # SkillInvoker（全量，子代理默认不受限）
        parent_hook_engine,         # 父级 HookEngine（用于触发生命周期 hook）
        parent_event_bridge,        # 父级 EventBridge（SSE 透传目标 + data2ui）
        config,                     # V4Config
        max_depth: int = 3,
        enabled: bool = False,
        session_engine=None,        # 真实 SessionEngine（可选，None 则用轻量版）
        hook_plugin_factory: Optional[Callable] = None,  # () -> HookPlugin，每次调用 new 一个
        subagent_store=None,        # SubAgentStore（可选，None 则不持久化元数据）
    ):
        self._llm = llm_provider
        self._invoker = skill_invoker
        self._parent_hooks = parent_hook_engine
        self._parent_event_bridge = parent_event_bridge
        self._config = config
        self._max_depth = max_depth
        self._enabled = enabled
        self._session_engine = session_engine
        self._hook_plugin_factory = hook_plugin_factory
        self._subagent_store = subagent_store

    async def execute(
        self,
        task: str,
        parent_agent_id: str,
        current_depth: int = 0,
        parent_session_id: str = "",
        user_id: int = 0,
        system_prompt: Optional[str] = None,
        role: str = "general_expert",
        background: bool = False,
    ) -> str:
        """
        创建隔离 AgentLoop 执行子任务，将过程事件透传给父级。

        Args:
            task:               子任务描述（包含所有必要上下文）
            parent_agent_id:    父代理 agent_id
            current_depth:      当前嵌套深度
            parent_session_id:  父代理 session_id（用于关联查询）
            user_id:            用户 ID（继承自父代理请求）
            system_prompt:      覆盖 system prompt（None 使用默认）
            background:         True=后台异步执行，父代理立即返回 task_id

        Returns:
            子代理的最终文本输出（background=True 时返回 JSON 启动确认）
        """
        if not self._enabled:
            return "[SubAgent] 子代理功能未启用 (subagent_enabled=False)"

        if current_depth >= self._max_depth:
            logger.warning(f"[SubAgent] Max depth reached ({self._max_depth})")
            return f"[SubAgent] 达到最大嵌套深度 ({self._max_depth})，无法继续委托"

        # ── 后台异步模式：立即返回，子代理在后台运行 ──
        if background:
            return await self._execute_background(
                task=task,
                parent_agent_id=parent_agent_id,
                current_depth=current_depth,
                parent_session_id=parent_session_id,
                user_id=user_id,
                role=role,
            )

        # ── 子代理 ID 和 session_id ──────────────────────────────────────────
        sub_agent_id = f"sub-{uuid.uuid4().hex[:8]}"
        task_hash = hashlib.sha256(task.encode()).hexdigest()[:8]
        sub_session_id = f"sub_{parent_session_id}_{task_hash}" if parent_session_id else f"sub_{sub_agent_id}"

        logger.info(
            f"[SubAgent] Spawning {sub_agent_id} "
            f"(parent={parent_agent_id}, depth={current_depth + 1}): "
            f"task={task[:100]!r}"
        )

        # ── 写入 SubAgentRecord（running）───────────────────────────────────
        if self._subagent_store:
            await self._subagent_store.insert(
                sub_agent_id=sub_agent_id,
                parent_agent_id=parent_agent_id,
                parent_session_id=parent_session_id,
                sub_session_id=sub_session_id,
                user_id=user_id,
                task=task,
                depth=current_depth + 1,
            )

        # ── 触发父级 ON_SUBAGENT_START hook ──────────────────────────────────
        if self._parent_hooks:
            try:
                from .hook_engine import HookPoint
                await self._parent_hooks.fire(HookPoint.ON_SUBAGENT_START, {
                    "agent_id": sub_agent_id,
                    "parent_agent_id": parent_agent_id,
                    "task": task,
                    "depth": current_depth + 1,
                })
            except Exception as e:
                logger.debug(f"[SubAgent] ON_SUBAGENT_START hook failed: {e}")

        # ── 创建子代理专属 HookEngine ─────────────────────────────────────────
        from .hook_engine import HookEngine
        sub_hook_engine = HookEngine(agent_id=sub_agent_id)

        # 为子代理 new 独立 LegacyHookPlugin（每个子代理独立实例，并发安全）
        # plugin_factory 由 native_agent.py 注入，内部 new LegacyHookPlugin(event_bridge=父级)
        if self._hook_plugin_factory:
            try:
                sub_plugin = self._hook_plugin_factory()
                sub_hook_engine.register_plugin(sub_plugin)
            except Exception as e:
                logger.debug(f"[SubAgent] hook plugin registration failed: {e}")

        # ── 准备 session（历史 + 记忆注入）──────────────────────────────────
        session_engine = self._session_engine
        if session_engine:
            try:
                await session_engine.prepare_session(
                    session_id=sub_session_id,
                    user_id=user_id,
                    query=task,
                )
            except Exception as e:
                logger.warning(f"[SubAgent] prepare_session failed, using minimal: {e}")
                session_engine = None

        sub_session = _SubAgentSessionEngine(session_engine, sub_session_id) if session_engine else _MinimalSessionEngine()

        # ── 创建独立 ContextBuilder ───────────────────────────────────────────
        try:
            from .context_builder import ContextBuilder
            sub_builder = ContextBuilder()
        except Exception as e:
            logger.error(f"[SubAgent] ContextBuilder init failed: {e}")
            _result = f"[SubAgent] 初始化失败: {e}"
            if self._subagent_store:
                await self._subagent_store.complete(sub_agent_id, _result, [], "failed")
            return _result

        # ── 创建子 AgentLoop（共享 LLMProvider，独立其他一切）────────────────
        try:
            from .loop import AgentLoop
            sub_loop = AgentLoop(
                llm_provider=self._llm,
                skill_invoker=self._invoker,   # 全量工具，无限制
                hook_engine=sub_hook_engine,
                session_engine=sub_session,
                context_builder=sub_builder,
                event_bridge=None,             # SSE 由 _push_to_parent 透传
                config=self._config,
                agent_id=sub_agent_id,
                parent_agent_id=parent_agent_id,
            )
        except Exception as e:
            logger.error(f"[SubAgent] AgentLoop init failed: {e}")
            _result = f"[SubAgent] 循环初始化失败: {e}"
            if self._subagent_store:
                await self._subagent_store.complete(sub_agent_id, _result, [], "failed")
            return _result

        sub_request = _SubAgentRequest(
            session_id=sub_session_id,
            user_id=user_id,
        )

        # 1. 加载角色模板
        from agent_core.prompts.loader import load_prompt as _load_prompt
        _template_name = f"subagent_role_{role}"
        try:
            _role_template = _load_prompt(_template_name)
            if not _role_template:
                raise ValueError(f"empty template for role: {role}")
        except Exception:
            _role_template = (
                f"你是一个专注的子代理（ID: {sub_agent_id}），负责完成以下具体任务。"
                f"直接执行任务并返回结果，不要询问确认。"
                f"当前嵌套深度: {current_depth + 1}/{self._max_depth}"
            )

        # 2. 动态生成 skill 列表
        _skill_lines: List[str] = []
        try:
            for _meta in self._invoker._discovery.get_all():
                _name = getattr(_meta, "name", "")
                _desc = (getattr(_meta, "description", "") or "").split("\n")[0].strip()
                _paths = getattr(_meta, "script_paths", [])
                if _name and _paths:
                    _p = _paths[0] if _paths[0].startswith("/") else "/app/" + _paths[0]
                    _skill_lines.append(f"- **{_name}** (`{_p}`): {_desc}")
        except Exception:
            pass

        _skill_section = (
            "\n\n## 可用 Skill（通过 bash stdin JSON 调用）\n\n"
            "调用格式：\n```bash\npython3 {script_path} <<'EOF'\n{\"param\":\"value\"}\nEOF\n```\n\n"
            + "\n".join(_skill_lines)
        ) if _skill_lines else ""

        # 3. 组装最终 system prompt
        sub_system = system_prompt or _role_template.replace("{skill_list_section}", _skill_section)

        # ── 执行子代理循环，透传 SSE 事件到父级 event_bridge ─────────────────
        accumulated: List[str] = []
        tools_used: List[str] = []
        final_status = "completed"

        try:
            async for event in sub_loop.run(
                system_prompt=sub_system,
                user_message=task,
                request=sub_request,
            ):
                event_type = event.get("event", "") or event.get("type", "")

                # 收集最终文本
                if event_type == "text_delta":
                    accumulated.append(event.get("data", {}).get("delta", ""))
                elif event_type == "done":
                    final = event.get("data", {}).get("final_text", "")
                    if final and not accumulated:
                        accumulated.append(final)
                    tools_used = event.get("data", {}).get("tools_used", [])

                # 透传所有事件到父级 event_bridge（含 agent_id 标记）
                self._push_to_parent(event)

        except asyncio.CancelledError:
            # 被外部 cancel（如 asyncio.wait_for 超时），标记为 cancelled 后继续清理
            logger.warning(f"[SubAgent:{sub_agent_id}] Execution cancelled")
            final_status = "cancelled"
            accumulated = accumulated or ["[SubAgent] 执行被取消"]
            raise  # 让 CancelledError 继续传播，不吞掉

        except Exception as e:
            logger.error(f"[SubAgent:{sub_agent_id}] Execution failed: {e}")
            final_status = "failed"
            accumulated = [f"[SubAgent] 执行失败: {e}"]

        finally:
            # ── 无论正常/异常/cancel，都更新 SubAgentRecord 和 finalize ────
            result = "".join(accumulated) or "[SubAgent] 未返回任何内容"

            # 轻量 finalize：仅保存对话历史，跳过 Episode 蒸馏和 Memory 更新
            if session_engine:
                try:
                    await session_engine.finalize_lightweight(
                        session_id=sub_session_id,
                        user_id=user_id,
                        request_message=task,
                        assistant_text=result,
                    )
                except Exception as e:
                    try:
                        await session_engine.finalize(
                            session_id=sub_session_id,
                            user_id=user_id,
                            assistant_text=result,
                            request_message=task,
                        )
                    except Exception as e2:
                        logger.debug(f"[SubAgent] finalize failed: {e2}")

            # 更新 SubAgentRecord
            if self._subagent_store:
                try:
                    await self._subagent_store.complete(
                        sub_agent_id=sub_agent_id,
                        result=result,
                        tools_used=tools_used,
                        status=final_status,
                    )
                except Exception as e:
                    logger.debug(f"[SubAgent] store.complete failed: {e}")

        # ── 触发父级 ON_SUBAGENT_END hook ────────────────────────────────────
        if self._parent_hooks:
            try:
                from .hook_engine import HookPoint
                await self._parent_hooks.fire(HookPoint.ON_SUBAGENT_END, {
                    "agent_id": sub_agent_id,
                    "parent_agent_id": parent_agent_id,
                    "sub_session_id": sub_session_id,
                    "task": task,
                    "result": result,
                    "tools_used": tools_used,
                    "depth": current_depth + 1,
                })
            except Exception as e:
                logger.debug(f"[SubAgent] ON_SUBAGENT_END hook failed: {e}")

        logger.info(
            f"[SubAgent:{sub_agent_id}] Completed ({final_status}): "
            f"output_len={len(result)}, tools={tools_used}"
        )
        return result

    # ── 后台异步执行 ──────────────────────────────────────────────────────

    async def _execute_background(
        self,
        task: str,
        parent_agent_id: str,
        current_depth: int,
        parent_session_id: str,
        user_id: int,
        role: str,
        recovered_sub_agent_id: str = None,
        recovered_sub_session_id: str = None,
    ) -> str:
        """后台启动子代理，立即返回 task_id"""
        import json
        import os

        # ── 代码层硬约束（恢复模式跳过，系统主动恢复不受限）──────────────────
        if not recovered_sub_agent_id and self._subagent_store and parent_session_id:
            # 提前计算 sub_session_id，供检查1使用
            _task_hash = hashlib.sha256(task.encode()).hexdigest()[:8]
            _check_sub_session_id = f"sub_{parent_session_id}_{_task_hash}" if parent_session_id else ""

            # 检查1：同 sub_session_id 去重（同一任务只能有一个运行中）
            if _check_sub_session_id and await self._subagent_store.is_session_running(_check_sub_session_id):
                msg = (
                    f"[SubAgent] 该任务已在后台运行中，禁止重复创建。"
                    f"请通过 query_subagent 查看任务状态。"
                )
                logger.warning(
                    f"[SubAgent:bg] Rejected duplicate: sub_session={_check_sub_session_id} already running"
                )
                return msg

            # 检查2：session 并发上限
            max_concurrent = getattr(self._config, "subagent_max_concurrent_background", 10)
            active_count = await self._subagent_store.count_active_background(parent_session_id)
            if active_count >= max_concurrent:
                msg = (
                    f"[SubAgent] 当前 session 已有 {active_count} 个后台任务未完成"
                    f"（上限 {max_concurrent}），禁止重复创建。"
                    f"请等待现有任务完成后再试，或通过 query_subagent 查看任务状态。"
                )
                logger.warning(
                    f"[SubAgent:bg] Rejected: session={parent_session_id}, active={active_count}, max={max_concurrent}"
                )
                return msg

        sub_agent_id = recovered_sub_agent_id or f"bg_sub-{uuid.uuid4().hex[:8]}"
        task_id = f"bg_agent_{sub_agent_id}"
        log_path = f"/tmp/{task_id}.log"

        logger.info(
            f"[SubAgent:bg] Launching background agent {sub_agent_id} "
            f"(parent={parent_agent_id}): task={task[:100]!r}"
        )

        # 注册后台任务到 TaskService（复用 docker_operator 的注册机制）
        try:
            import urllib.request
            _agent_url = os.getenv("AGENT_SERVICE_URL", "http://localhost:8000")
            _payload = json.dumps({
                "task_id": task_id,
                "name": f"bg_agent: {task[:60]}",
                "log_path": log_path,
                "session_id": parent_session_id,
                "user_id": str(user_id),
                "channel": "dingtalk",
            }).encode("utf-8")
            _req = urllib.request.Request(
                f"{_agent_url}/api/v1/chat/v4/tasks",
                data=_payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(_req, timeout=5)
        except Exception as e:
            logger.warning(f"[SubAgent:bg] Task registration failed (non-fatal): {e}")

        # bg 模式 session_id：恢复时复用原 sub_session_id，新启动时基于 task_hash 生成
        if recovered_sub_session_id:
            bg_sub_session_id = recovered_sub_session_id
        else:
            task_hash = hashlib.sha256(task.encode()).hexdigest()[:8]
            bg_sub_session_id = f"sub_{parent_session_id}_{task_hash}" if parent_session_id else f"sub_{sub_agent_id}"

        # 写入 SubAgentRecord（恢复模式下跳过 insert，记录已存在）
        if self._subagent_store and not recovered_sub_agent_id:
            await self._subagent_store.insert(
                sub_agent_id=sub_agent_id,
                parent_agent_id=parent_agent_id,
                parent_session_id=parent_session_id,
                sub_session_id=bg_sub_session_id,
                user_id=user_id,
                task=task,
                depth=current_depth + 1,
            )

        # 启动后台 asyncio.create_task（不等待）
        asyncio.create_task(
            self._run_background_agent(
                sub_agent_id=sub_agent_id,
                bg_sub_session_id=bg_sub_session_id,
                task=task,
                parent_agent_id=parent_agent_id,
                current_depth=current_depth,
                parent_session_id=parent_session_id,
                user_id=user_id,
                role=role,
                task_id=task_id,
                log_path=log_path,
            )
        )

        # 立即返回（父代理不等待）
        return json.dumps({
            "for_llm": {
                "status": "background_started",
                "task_id": task_id,
                "sub_agent_id": sub_agent_id,
                "next_action": (
                    "后台开发任务已启动，请立即通知用户任务已在后台执行，"
                    "然后结束本轮回答。不要轮询进度，系统会在任务完成后自动通知用户。"
                ),
            }
        }, ensure_ascii=False)

    async def recover_interrupted(self, store: "SubAgentStore") -> int:
        """
        服务启动时调用：扫描并恢复被中断的后台 subagent 任务。

        1. list_interrupted(min_age_seconds=0) 查询所有 status='running' 记录（服务重启，所有 running 均视为中断）
        2. created_at 超过 24h（86400s）的标为 failed（过期，不恢复）
        3. 24h 内的：mark_interrupted → 重新 _execute_background（复用 sub_session_id 和 task）
        4. 返回恢复数量
        """
        import time as _time

        try:
            # claim_interrupted 是原子操作，多进程下只有一个 worker 能 claim 到记录
            interrupted = await store.claim_interrupted(min_age_seconds=0)
        except Exception as e:
            logger.warning(f"[SubAgent:recover] list_interrupted failed: {e}")
            return 0

        if not interrupted:
            logger.info("[SubAgent:recover] No interrupted subagents found")
            return 0

        now = _time.time()
        expired_ids: List[str] = []
        to_recover: List[Dict[str, Any]] = []

        for rec in interrupted:
            age = now - rec.get("created_at", now)
            if age > 86400:
                expired_ids.append(rec["sub_agent_id"])
            else:
                to_recover.append(rec)

        # 过期记录标为 failed
        if expired_ids:
            try:
                for eid in expired_ids:
                    await store.complete(eid, "[recover] expired after 24h", [], "failed")
                logger.info(f"[SubAgent:recover] Marked {len(expired_ids)} expired subagent(s) as failed")
            except Exception as e:
                logger.warning(f"[SubAgent:recover] Failed to mark expired records: {e}")

        if not to_recover:
            logger.info(f"[SubAgent:recover] recovered 0 interrupted subagents (all expired)")
            return 0

        # ── session 分组去重 ──────────────────────────────────────────────────
        import json as _json
        max_concurrent = getattr(self._config, "subagent_max_concurrent_background", 10)
        cancel_ids: List[str] = []
        deduped: List[Dict[str, Any]] = []

        # 按 parent_session_id 分组
        session_groups: Dict[str, List[Dict[str, Any]]] = {}
        for rec in to_recover:
            sid = rec.get("parent_session_id", "")
            session_groups.setdefault(sid, []).append(rec)

        for sid, group in session_groups.items():
            # 步骤1：同 sub_session_id 去重，保留 tools_used 条数最多的
            by_sub_session: Dict[str, List[Dict[str, Any]]] = {}
            for rec in group:
                key = rec.get("sub_session_id", rec["sub_agent_id"])
                by_sub_session.setdefault(key, []).append(rec)

            deduped_group: List[Dict[str, Any]] = []
            for key, recs in by_sub_session.items():
                if len(recs) == 1:
                    deduped_group.append(recs[0])
                else:
                    # 保留 tools_used 条数最多的（执行进度最深）
                    best = max(
                        recs,
                        key=lambda r: len(_json.loads(r.get("tools_used") or "[]")),
                    )
                    deduped_group.append(best)
                    cancel_ids.extend(r["sub_agent_id"] for r in recs if r is not best)

            # 步骤2：超出 session 并发上限，按 tools_used 条数降序取前 N
            if len(deduped_group) > max_concurrent:
                deduped_group.sort(
                    key=lambda r: len(_json.loads(r.get("tools_used") or "[]")),
                    reverse=True,
                )
                cancel_ids.extend(r["sub_agent_id"] for r in deduped_group[max_concurrent:])
                deduped_group = deduped_group[:max_concurrent]

            deduped.extend(deduped_group)

        # 批量取消淘汰记录
        if cancel_ids:
            try:
                await store.cancel_by_ids(cancel_ids)
                logger.info(f"[SubAgent:recover] Cancelled {len(cancel_ids)} duplicate/excess subagent(s): {cancel_ids}")
            except Exception as e:
                logger.warning(f"[SubAgent:recover] cancel_by_ids failed: {e}")

        # 逐条重新启动后台 AgentLoop（claim_interrupted 已原子性标记为 recovering）
        recovered = 0
        for rec in deduped:
            try:
                await self._execute_background(
                    task=rec["task"],
                    parent_agent_id=rec.get("parent_agent_id", "recovered"),
                    current_depth=max(0, rec.get("depth", 1) - 1),
                    parent_session_id=rec.get("parent_session_id", ""),
                    user_id=rec.get("user_id", 0),
                    role="general_expert",
                    recovered_sub_agent_id=rec["sub_agent_id"],
                    recovered_sub_session_id=rec.get("sub_session_id", ""),
                )
                recovered += 1
                logger.info(
                    f"[SubAgent:recover] Resumed {rec['sub_agent_id']} "
                    f"(session={rec.get('sub_session_id')}, task={rec['task'][:60]!r})"
                )
            except Exception as e:
                logger.warning(f"[SubAgent:recover] Failed to resume {rec['sub_agent_id']}: {e}")

        logger.info(f"[SubAgent:recover] recovered {recovered} interrupted subagent(s)")
        return recovered

    async def recover_by_session(
        self,
        parent_session_id: str,
        store: "SubAgentStore",
    ) -> tuple:
        """
        问答时调用：找到该 session 下所有 interrupted 的后台 subagent，重新启动。
        返回 (恢复数量, 当前活跃任务列表)，活跃任务列表供 native_agent 注入 system prompt。
        """
        try:
            records = await store.list_interrupted_by_session(parent_session_id)
        except Exception as e:
            logger.warning(f"[SubAgent:recover_by_session] list failed: {e}")
            records = []

        recovered = 0
        for rec in records:
            try:
                await self._execute_background(
                    task=rec["task"],
                    parent_agent_id=rec.get("parent_agent_id", "recovered"),
                    current_depth=max(0, rec.get("depth", 1) - 1),
                    parent_session_id=rec.get("parent_session_id", ""),
                    user_id=rec.get("user_id", 0),
                    role="general_expert",
                    recovered_sub_agent_id=rec["sub_agent_id"],
                    recovered_sub_session_id=rec.get("sub_session_id", ""),
                )
                recovered += 1
                logger.info(
                    f"[SubAgent:recover_by_session] Resumed {rec['sub_agent_id']} "
                    f"for session={parent_session_id} task={rec['task'][:60]!r}"
                )
            except Exception as e:
                logger.warning(f"[SubAgent:recover_by_session] Failed to resume {rec['sub_agent_id']}: {e}")

        if recovered:
            logger.info(f"[SubAgent:recover_by_session] Recovered {recovered} subagent(s) for session={parent_session_id}")

        # 查询当前所有活跃后台任务（含刚恢复的）
        try:
            active_tasks = await store.list_active_background(parent_session_id)
        except Exception as e:
            logger.warning(f"[SubAgent:recover_by_session] list_active_background failed: {e}")
            active_tasks = []

        return recovered, active_tasks

    async def _run_background_agent(
        self,
        sub_agent_id: str,
        bg_sub_session_id: str,
        task: str,
        parent_agent_id: str,
        current_depth: int,
        parent_session_id: str,
        user_id: int,
        role: str,
        task_id: str,
        log_path: str,
    ) -> None:
        """在后台独立运行子代理 AgentLoop（通过 asyncio.create_task 启动）"""
        import json

        sub_session_id = bg_sub_session_id
        accumulated: List[str] = []
        tools_used: List[str] = []
        final_status = "completed"
        exit_reason = ""
        exit_code = 0

        try:
            logger.info(f"[SubAgent:bg] {sub_agent_id} starting background AgentLoop")

            # 构建子代理（与同步模式基本相同）
            from .hook_engine import HookEngine
            from .context_builder import ContextBuilder
            from .loop import AgentLoop

            sub_hook_engine = HookEngine(agent_id=sub_agent_id)
            if self._hook_plugin_factory:
                try:
                    sub_plugin = self._hook_plugin_factory()
                    sub_hook_engine.register_plugin(sub_plugin)
                except Exception:
                    pass

            # Session 准备
            session_engine = self._session_engine
            if session_engine:
                try:
                    await session_engine.prepare_session(
                        session_id=sub_session_id,
                        user_id=user_id,
                        query=task,
                    )
                except Exception as e:
                    logger.warning(f"[SubAgent:bg] prepare_session failed: {e}")
                    session_engine = None

            sub_session = _SubAgentSessionEngine(session_engine, sub_session_id) if session_engine else _MinimalSessionEngine()
            sub_builder = ContextBuilder()

            sub_loop = AgentLoop(
                llm_provider=self._llm,
                skill_invoker=self._invoker,
                hook_engine=sub_hook_engine,
                session_engine=sub_session,
                context_builder=sub_builder,
                event_bridge=None,
                config=self._config,
                agent_id=sub_agent_id,
                parent_agent_id=parent_agent_id,
            )

            sub_request = _SubAgentRequest(
                session_id=sub_session_id,
                user_id=user_id,
            )

            # 加载角色模板
            from agent_core.prompts.loader import load_prompt as _load_prompt
            _template_name = f"subagent_role_{role}"
            try:
                _role_template = _load_prompt(_template_name)
                if not _role_template:
                    raise ValueError(f"empty template for role: {role}")
            except Exception:
                _role_template = (
                    f"你是一个后台执行的子代理（ID: {sub_agent_id}），负责独立完成开发任务。"
                    f"直接执行任务并输出结果，不要询问确认。"
                    f"所有编码、脚本执行必须在 docker_operator (toolbox) 中完成。"
                )

            # 动态生成 skill 列表
            _skill_lines: List[str] = []
            try:
                for _meta in self._invoker._discovery.get_all():
                    _name = getattr(_meta, "name", "")
                    _desc = (getattr(_meta, "description", "") or "").split("\n")[0].strip()
                    _paths = getattr(_meta, "script_paths", [])
                    if _name and _paths:
                        _p = _paths[0] if _paths[0].startswith("/") else "/app/" + _paths[0]
                        _skill_lines.append(f"- **{_name}** (`{_p}`): {_desc}")
            except Exception:
                pass

            _skill_section = (
                "\n\n## 可用 Skill（通过 bash stdin JSON 调用）\n\n"
                "调用格式：\n```bash\npython3 {script_path} <<'EOF'\n{\"param\":\"value\"}\nEOF\n```\n\n"
                + "\n".join(_skill_lines)
            ) if _skill_lines else ""

            sub_system = _role_template.replace("{skill_list_section}", _skill_section)

            # 执行 AgentLoop
            async for event in sub_loop.run(
                system_prompt=sub_system,
                user_message=task,
                request=sub_request,
            ):
                event_type = event.get("event", "") or event.get("type", "")
                if event_type == "text_delta":
                    accumulated.append(event.get("data", {}).get("delta", ""))
                elif event_type == "done":
                    final = event.get("data", {}).get("final_text", "")
                    if final and not accumulated:
                        accumulated.append(final)
                    tools_used = event.get("data", {}).get("tools_used", [])
                    exit_reason = event.get("data", {}).get("exit_reason", "")

        except asyncio.CancelledError:
            logger.warning(f"[SubAgent:bg] {sub_agent_id} cancelled")
            final_status = "cancelled"
            exit_code = 2
        except Exception as e:
            logger.error(f"[SubAgent:bg] {sub_agent_id} failed: {e}")
            final_status = "failed"
            exit_code = 1
            accumulated = [f"[SubAgent:bg] 执行失败: {e}"]
        finally:
            result = "".join(accumulated) or "[SubAgent:bg] 未返回任何内容"

            # 保存对话历史
            if session_engine:
                try:
                    await session_engine.finalize_lightweight(
                        session_id=sub_session_id,
                        user_id=user_id,
                        request_message=task,
                        assistant_text=result,
                    )
                except Exception:
                    pass

            # 更新 SubAgentRecord
            if self._subagent_store:
                try:
                    await self._subagent_store.complete(
                        sub_agent_id=sub_agent_id,
                        result=result,
                        tools_used=tools_used,
                        status=final_status,
                    )
                except Exception:
                    pass

            # 写 .done 标记文件（触发 TaskMonitor 回调）
            try:
                with open(f"{log_path}.done", "w") as f:
                    f.write(str(exit_code))
                # 写结果日志（TaskMonitor 回调时会读取）
                with open(log_path, "w") as f:
                    f.write(result[:5000])
                logger.info(
                    f"[SubAgent:bg] {sub_agent_id} finished ({final_status}), "
                    f".done written, exit_code={exit_code}, output_len={len(result)}"
                )
            except Exception as e:
                logger.error(f"[SubAgent:bg] Failed to write .done file: {e}")

    def _push_to_parent(self, event: Dict[str, Any]) -> None:
        """
        将子代理的 SSE 事件推入父级 event_bridge 队列。

        父级 AgentLoop._drain_event_bridge() 会在工具执行后排空队列，
        子代理过程事件就会出现在父级 SSE 流中，带有 agent_id 标记。
        """
        if self._parent_event_bridge is None:
            return
        try:
            queue = self._parent_event_bridge.get_event_queue()
            queue.put_nowait(event)
        except Exception:
            pass


class _SubAgentSessionEngine:
    """
    包装真实 SessionEngine，仅暴露子代理需要的接口。
    finalize_lightweight 只保存历史，不触发 LLM 蒸馏。
    """

    def __init__(self, real_engine, sub_session_id: str):
        self._engine = real_engine
        self._sub_session_id = sub_session_id

    async def prepare_session(self, **kwargs):
        return await self._engine.prepare_session(**kwargs)

    def start_episode(self, query: str):
        if hasattr(self._engine, "start_episode"):
            self._engine.start_episode(query)

    def record_tool_execution(self, **kwargs):
        if hasattr(self._engine, "record_tool_execution"):
            self._engine.record_tool_execution(**kwargs)

    async def finalize(self, **kwargs):
        """重定向到 finalize_lightweight"""
        await self.finalize_lightweight(**kwargs)

    async def finalize_lightweight(
        self,
        session_id: str = "",
        user_id: int = 0,
        request_message: str = "",
        assistant_text: str = "",
        **kwargs,
    ) -> None:
        """只保存对话历史，跳过 Episode 蒸馏 + Memory 更新"""
        if not self._engine:
            return
        history = getattr(self._engine, "_history", None)
        if history:
            try:
                await history.append_text_exchange(
                    session_id=session_id or self._sub_session_id,
                    user_text=request_message,
                    assistant_text=assistant_text,
                )
            except Exception as e:
                logger.debug(f"[SubAgent] history save failed: {e}")


class _SubAgentRequest:
    """最小化请求对象"""
    def __init__(self, session_id: str = "subagent", user_id: int = 0):
        self.session_id = session_id
        self.user_id = user_id
        self.report_lang = "zh"
        self.attached_files = []
        self.channel = "api"
        self.scene_type = None
        self.render_mode = "text_only"


class _MinimalSessionEngine:
    """最小化 SessionEngine，无持久化（session_engine=None 时的 fallback）"""

    async def prepare_session(self, **kwargs):
        from .message_types import SessionContext
        return SessionContext(
            session_id=kwargs.get("session_id", "subagent"),
            user_id=kwargs.get("user_id", 0),
        )

    def start_episode(self, query: str):
        pass

    def record_tool_execution(self, **kwargs):
        pass

    async def finalize(self, **kwargs):
        pass

    async def finalize_lightweight(self, **kwargs):
        pass
