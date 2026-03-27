"""
SkillInvoker — 统一 Skill 执行器

替代 MCP 调用链（MCPToolBuilder → stdio 子进程 → MCP 协议 → execute）。
改为进程内直接调用 V4SkillExecutor，确保 PostToolUse hook 能拿到完整数据。

支持 SKILL.md 定义的所有执行方式:
- Python 脚本 (native/script): V4SkillExecutor 直接执行
- Bash/curl/shell (action): PromptActionExecutor 通过 sandbox 执行
- 远程 toolbox: PromptActionExecutor 通过 HTTP API 执行
- Prompt-only: 不执行，返回说明信息
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional
from loguru import logger

from .message_types import ToolResult


class SkillInvoker:
    """
    统一 Skill 执行入口

    旧链路: MCPToolBuilder → McpStdioServerConfig → stdio 子进程 → MCP 协议 → execute
    新链路: SkillInvoker.invoke() → V4SkillExecutor.execute() → SkillResult (进程内!)
    """

    def __init__(
        self,
        executor,           # V4SkillExecutor
        discovery,          # SkillDiscovery
        action_executor=None,   # PromptActionExecutor（可选）
        sandbox_provider=None,  # SandboxProvider（可选，用于 invoke_bash）
        subagent_executor=None, # SubAgentExecutor（可选，Phase 6）
    ):
        self._executor = executor
        self._discovery = discovery
        self._action_executor = action_executor
        self._sandbox = sandbox_provider
        self._subagent_executor = subagent_executor
        self._subagent_store = None   # 由外部注入（native_agent.py）
        # 缓存工具定义（避免重复计算）
        self._tool_defs_cache: Optional[List[dict]] = None

    # ──────────────────────────── 工具定义 ────────────────────────────

    def get_tool_definitions(self, user_id: str = None) -> List[dict]:
        """
        获取所有可执行 skill 的 OpenAI function calling 格式定义

        Args:
            user_id: 当前用户 ID，传入时额外包含该用户的私有 Skill

        格式:
        [
            {
                "type": "function",
                "function": {
                    "name": "quick_search",
                    "description": "...",
                    "parameters": { JSON Schema }
                }
            },
            ...
        ]
        """
        # 有 user_id 时不使用缓存（不同用户私有 Skill 不同）
        if self._tool_defs_cache is not None and user_id is None:
            return self._tool_defs_cache

        # B 方案：Skills 不再注册为 function calling tools。
        # LLM 通过 system prompt 中的 skills XML 摘要了解有哪些技能，
        # 通过内置 bash 工具（command: string）调用脚本，彻底绕开 JSON Schema 参数限制。
        defs = []

        # 只注册内置工具（bash/read_file/grep/spawn_agent）
        defs.extend(self.get_builtin_tool_definitions())

        self._tool_defs_cache = defs
        logger.info(f"[SkillInvoker] Built {len(defs)} tool definitions (builtins only, skills via bash)")
        return defs

    def invalidate_cache(self) -> None:
        """热加载后调用，清除工具定义缓存"""
        self._tool_defs_cache = None

    def _meta_to_tool_def(self, meta) -> Optional[dict]:
        """将 SkillMetadata 转换为 OpenAI function calling 格式

        只输出 name + description，不包含 parameters/input_schema。
        具体参数说明通过 SKILL.md 预注入机制在首次调用前传递给 LLM。
        """
        try:
            display_name = getattr(meta, "display_name", "") or ""
            description = getattr(meta, "description", "") or ""
            desc = f"[{display_name}] {description}".strip() if display_name else description or meta.name

            return {
                "type": "function",
                "function": {
                    "name": meta.name,
                    "description": desc,
                    # parameters 故意省略：LLM 通过 SKILL.md 预注入了解完整参数结构
                    # 大多数 LLM provider 对缺少 parameters 的工具会接受自由参数
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        except Exception as e:
            logger.warning(f"[SkillInvoker] Failed to build tool def for '{meta.name}': {e}")
            return None

    def get_builtin_tool_definitions(self) -> List[dict]:
        """获取内置工具（Bash/Read/Grep/Glob/SpawnAgent）的定义"""
        defs = [
            {
                "type": "function",
                "function": {
                    "name": "bash",
                    "description": "执行 bash 命令（python3、curl、shell 脚本等）",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "description": "要执行的 bash 命令",
                            },
                            "timeout": {
                                "type": "integer",
                                "description": "超时时间（秒），默认 120",
                                "default": 120,
                            },
                        },
                        "required": ["command"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "读取文件内容",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {
                                "type": "string",
                                "description": "文件路径",
                            },
                        },
                        "required": ["file_path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "grep",
                    "description": "在文件中搜索内容",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pattern": {
                                "type": "string",
                                "description": "搜索模式（正则表达式）",
                            },
                            "path": {
                                "type": "string",
                                "description": "搜索路径",
                            },
                        },
                        "required": ["pattern"],
                    },
                },
            },
        ]
        # Phase 6: spawn_agent + query_subagent（仅在子代理功能启用时注册）
        if self._subagent_executor and getattr(self._subagent_executor, "_enabled", False):
            defs.append({
                "type": "function",
                "function": {
                    "name": "spawn_agent",
                    "description": (
                        "创建子代理执行独立子任务。子代理拥有完整工具集和独立上下文，"
                        "可进行多轮 LLM 推理和工具调用，执行完毕返回文本结果。"
                        "适用于：需要隔离执行的子任务、任务边界清晰且可并行的场景。"
                        "子代理过程事件会实时透传给当前对话流。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task": {
                                "type": "string",
                                "description": (
                                    "子任务的完整描述，必须包含执行所需的所有上下文信息。"
                                    "子代理无法访问父代理的对话历史，请将关键信息直接写入 task。"
                                ),
                            },
                            "role": {
                                "type": "string",
                                "description": (
                                    "子代理角色：stock_expert（股票专家）/ military_expert（军事专家）"
                                    "/ political_expert（政治专家）/ general_expert（通用专家）"
                                    "/ software_expert（高级软件专家）。不填默认 general_expert。"
                                ),
                                "enum": [
                                    "stock_expert",
                                    "military_expert",
                                    "political_expert",
                                    "general_expert",
                                    "software_expert",
                                ],
                            },
                        },
                        "required": ["task"],
                    },
                },
            })
            defs.append({
                "type": "function",
                "function": {
                    "name": "query_subagent",
                    "description": (
                        "查询已执行完毕的子代理上下文，包括状态、工具调用列表、最终结果。"
                        "用于多轮对话中回溯子代理的执行细节。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "sub_agent_id": {
                                "type": "string",
                                "description": "子代理 ID（spawn_agent 返回的 raw_data.sub_agent_id）",
                            },
                            "parent_session_id": {
                                "type": "string",
                                "description": "父 session_id，查询该 session 下的所有子代理记录",
                            },
                        },
                    },
                },
            })
        return defs

    # ──────────────────────────── 执行 ────────────────────────────

    async def invoke(
        self,
        skill_name: str,
        arguments: Dict[str, Any],
        tool_call_id: str = "",
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> ToolResult:
        """
        统一执行入口

        执行策略:
        1. 内置工具（bash/read_file/grep）→ 直接执行
        2. 查找 skill 元数据（SkillDiscovery）
        3. 选择执行器:
           - has_script → V4SkillExecutor.execute()
           - action → PromptActionExecutor.execute()
        4. 返回 ToolResult（content=JSON 字符串 + raw_data=完整数据）
        """
        start_ms = time.monotonic() * 1000

        # DEBUG: 记录 LLM 传入的原始参数
        logger.info(
            f"[SkillInvoker] invoke '{skill_name}' with arguments: "
            f"{json.dumps(arguments, ensure_ascii=False, default=str)[:500]}"
        )

        # Phase 6: spawn_agent 内置工具
        if skill_name == "spawn_agent":
            if self._subagent_executor:
                try:
                    task = arguments.get("task", "")
                    # parent_agent_id / session_id / user_id 由 AgentLoop 注入
                    parent_agent_id = arguments.get("_parent_agent_id", "root")
                    current_depth = arguments.get("_depth", 0)
                    parent_session_id = arguments.get("_parent_session_id", "")
                    req_user_id = int(arguments.get("_user_id", 0))
                    result_text = await self._subagent_executor.execute(
                        task=task,
                        parent_agent_id=parent_agent_id,
                        current_depth=current_depth,
                        parent_session_id=parent_session_id,
                        user_id=req_user_id,
                        role=arguments.get("role", "general_expert"),
                        background=arguments.get("background", False),
                    )
                    # sub_agent_id 从 result_text 里无法提取，但 SubAgentExecutor 会写 DB
                    # 父代理通过 query_subagent(parent_session_id=...) 查询
                    return ToolResult(
                        tool_call_id=tool_call_id,
                        name="spawn_agent",
                        content=result_text,
                        raw_data={
                            "task": task,
                            "result": result_text,
                            "parent_agent_id": parent_agent_id,
                            "parent_session_id": parent_session_id,
                        },
                        duration_ms=time.monotonic() * 1000 - start_ms,
                    )
                except Exception as e:
                    err_content = json.dumps({"error": str(e), "skill": "spawn_agent"})
                    return ToolResult(
                        tool_call_id=tool_call_id,
                        name="spawn_agent",
                        content=err_content,
                        is_error=True,
                        duration_ms=time.monotonic() * 1000 - start_ms,
                    )
            else:
                return ToolResult(
                    tool_call_id=tool_call_id,
                    name="spawn_agent",
                    content=json.dumps({"error": "SubAgent not enabled. Set subagent_enabled=True in config."}),
                    is_error=True,
                    duration_ms=time.monotonic() * 1000 - start_ms,
                )

        # query_subagent 内置工具（readonly，可并行）
        if skill_name == "query_subagent":
            if self._subagent_store:
                try:
                    sub_agent_id = arguments.get("sub_agent_id", "")
                    parent_session_id = arguments.get("parent_session_id", "")
                    if sub_agent_id:
                        record = await self._subagent_store.get_by_id(sub_agent_id)
                        data = record or {"error": f"sub_agent_id={sub_agent_id!r} not found"}
                    elif parent_session_id:
                        records = await self._subagent_store.list_by_parent_session(parent_session_id)
                        data = {"records": records, "count": len(records)}
                    else:
                        data = {"error": "请提供 sub_agent_id 或 parent_session_id"}
                    return ToolResult(
                        tool_call_id=tool_call_id,
                        name="query_subagent",
                        content=json.dumps(data, ensure_ascii=False, default=str),
                        raw_data=data,
                        duration_ms=time.monotonic() * 1000 - start_ms,
                    )
                except Exception as e:
                    return ToolResult(
                        tool_call_id=tool_call_id,
                        name="query_subagent",
                        content=json.dumps({"error": str(e)}),
                        is_error=True,
                        duration_ms=time.monotonic() * 1000 - start_ms,
                    )
            else:
                return ToolResult(
                    tool_call_id=tool_call_id,
                    name="query_subagent",
                    content=json.dumps({"error": "SubAgentStore not configured"}),
                    is_error=True,
                    duration_ms=time.monotonic() * 1000 - start_ms,
                )

        # 内置工具
        if skill_name == "bash":
            result = await self.invoke_bash(
                command=arguments.get("command", ""),
                timeout=arguments.get("timeout", 120),
            )
            result.tool_call_id = tool_call_id
            return result

        if skill_name == "read_file":
            result = await self.invoke_read(arguments.get("file_path", ""))
            result.tool_call_id = tool_call_id
            return result

        if skill_name == "grep":
            result = await self.invoke_grep(
                pattern=arguments.get("pattern", ""),
                path=arguments.get("path", "."),
            )
            result.tool_call_id = tool_call_id
            return result

        # 技能执行
        try:
            raw = await self._executor.execute(
                skill_name=skill_name,
                args=arguments,
                user_id=user_id,
                session_id=session_id,
            )

            duration_ms = time.monotonic() * 1000 - start_ms

            # 提取 raw_data 和 content
            raw_data = None
            content_str = ""
            is_error = False

            if raw is None:
                content_str = json.dumps({"error": f"Skill '{skill_name}' returned None"})
                is_error = True
            elif hasattr(raw, "data") and hasattr(raw, "status"):
                # SkillResult 对象
                raw_data = raw.data
                status_name = getattr(raw.status, "name", str(raw.status)).upper()
                is_error = "ERROR" in status_name or "FAIL" in status_name

                if raw_data is not None:
                    if isinstance(raw_data, (dict, list)):
                        content_str = json.dumps(raw_data, ensure_ascii=False, default=str)
                    else:
                        content_str = str(raw_data)
                else:
                    msg = getattr(raw, "message", "") or getattr(raw, "error", "")
                    content_str = json.dumps({"message": msg or "No data", "skill": skill_name})
            elif isinstance(raw, dict):
                raw_data = raw
                content_str = json.dumps(raw, ensure_ascii=False, default=str)
                is_error = bool(raw.get("error")) or not raw.get("success", True)
            else:
                raw_data = raw
                content_str = str(raw)

            logger.info(
                f"[SkillInvoker] Invoked '{skill_name}' in {duration_ms:.0f}ms, "
                f"data_size={len(content_str)}, error={is_error}"
            )

            return ToolResult(
                tool_call_id=tool_call_id,
                name=skill_name,
                content=content_str,
                raw_data=raw_data,
                is_error=is_error,
                duration_ms=duration_ms,
            )

        except Exception as e:
            duration_ms = time.monotonic() * 1000 - start_ms
            logger.error(f"[SkillInvoker] Invoke '{skill_name}' failed: {type(e).__name__}: {e}")
            err_content = json.dumps({"error": str(e), "skill": skill_name})
            return ToolResult(
                tool_call_id=tool_call_id,
                name=skill_name,
                content=err_content,
                raw_data=None,
                is_error=True,
                duration_ms=duration_ms,
            )

    async def invoke_bash(self, command: str, timeout: int = 120) -> ToolResult:
        """执行 bash 命令（通过 SandboxProvider）"""
        start_ms = time.monotonic() * 1000
        try:
            if self._sandbox:
                result = await self._sandbox.execute(command=command, timeout=timeout)
                output = result.stdout or ""
                if result.stderr:
                    output += f"\n[stderr]\n{result.stderr}"
                is_error = result.exit_code != 0
                raw_data = {
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "exit_code": result.exit_code,
                }
            else:
                # 无 sandbox 时使用 asyncio 子进程
                import asyncio
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=timeout
                    )
                except asyncio.TimeoutError:
                    proc.kill()
                    return ToolResult(
                        tool_call_id="",
                        name="bash",
                        content=json.dumps({"error": f"Command timed out after {timeout}s"}),
                        is_error=True,
                        duration_ms=time.monotonic() * 1000 - start_ms,
                    )
                output = stdout.decode("utf-8", errors="replace")
                err_text = stderr.decode("utf-8", errors="replace")
                if err_text:
                    output += f"\n[stderr]\n{err_text}"
                is_error = proc.returncode != 0
                raw_data = {
                    "stdout": stdout.decode("utf-8", errors="replace"),
                    "stderr": err_text,
                    "exit_code": proc.returncode,
                }

            return ToolResult(
                tool_call_id="",
                name="bash",
                content=output,
                raw_data=raw_data,
                is_error=is_error,
                duration_ms=time.monotonic() * 1000 - start_ms,
            )
        except Exception as e:
            return ToolResult(
                tool_call_id="",
                name="bash",
                content=json.dumps({"error": str(e)}),
                is_error=True,
                duration_ms=time.monotonic() * 1000 - start_ms,
            )

    async def invoke_read(self, file_path: str) -> ToolResult:
        """读取文件"""
        start_ms = time.monotonic() * 1000
        try:
            from pathlib import Path
            p = Path(file_path)
            if not p.exists():
                return ToolResult(
                    tool_call_id="",
                    name="read_file",
                    content=json.dumps({"error": f"File not found: {file_path}"}),
                    is_error=True,
                    duration_ms=time.monotonic() * 1000 - start_ms,
                )
            content = p.read_text(encoding="utf-8", errors="replace")
            return ToolResult(
                tool_call_id="",
                name="read_file",
                content=content,
                raw_data={"file_path": file_path, "content": content},
                duration_ms=time.monotonic() * 1000 - start_ms,
            )
        except Exception as e:
            return ToolResult(
                tool_call_id="",
                name="read_file",
                content=json.dumps({"error": str(e)}),
                is_error=True,
                duration_ms=time.monotonic() * 1000 - start_ms,
            )

    async def invoke_grep(self, pattern: str, path: str = ".") -> ToolResult:
        """搜索文件内容"""
        import subprocess
        start_ms = time.monotonic() * 1000
        try:
            result = subprocess.run(
                ["grep", "-r", "-n", pattern, path],
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = result.stdout or "(no matches)"
            return ToolResult(
                tool_call_id="",
                name="grep",
                content=output,
                raw_data={"matches": result.stdout, "pattern": pattern, "path": path},
                duration_ms=time.monotonic() * 1000 - start_ms,
            )
        except Exception as e:
            return ToolResult(
                tool_call_id="",
                name="grep",
                content=json.dumps({"error": str(e)}),
                is_error=True,
                duration_ms=time.monotonic() * 1000 - start_ms,
            )


def _normalize_schema(schema: Any) -> dict:
    """
    将 skill 的 input_schema 规范化为标准 JSON Schema

    复用 agent_core/mcp/tool_builder.py 的逻辑。
    """
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}

    normalized = dict(schema)
    if "type" not in normalized:
        normalized["type"] = "object"

    if normalized.get("type") == "object" and "properties" not in normalized:
        normalized["properties"] = {}

    return normalized
