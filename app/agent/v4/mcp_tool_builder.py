"""
MCP Tool Builder -- 从 SkillDiscovery + SkillExecutor 构建 MCP 工具

职责:
1. 从 SkillDiscovery 获取技能元数据
2. 从 SkillExecutor 获取可执行实例
3. 为每个可执行技能创建 MCP tool handler
4. 按 category 分组到不同 MCP server
5. 使用 claude_code_sdk.create_sdk_mcp_server 创建

替代已删除的 tool_adapter.py。
"""
import json
import asyncio
from pathlib import Path
from typing import Dict, Any, List, Optional
from loguru import logger

from agent_core.skill_discovery import SkillDiscovery
from agent_core.skill_executor import V4SkillExecutor
from .v4_config import V4Config


class MCPToolBuilder:
    """
    MCP 工具构建器 -- 从 V4 自有组件构建 MCP Server

    转换链:
        SkillDiscovery (元数据)
            + SkillExecutor (实例)
                → async handler closure
                    → @sdk_tool / dict
                        → MCP Server
    """

    def __init__(
        self,
        config: V4Config,
        discovery: SkillDiscovery,
        executor: V4SkillExecutor,
    ):
        self._config = config
        self._discovery = discovery
        self._executor = executor
        self._cached_mcp_servers: Optional[Dict[str, Any]] = None
        self._cached_sdk_tools: Optional[List[Any]] = None

    def invalidate_cache(self) -> None:
        """热加载后清除缓存"""
        self._cached_mcp_servers = None
        self._cached_sdk_tools = None

    def build_mcp_servers(self, only_skills: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        构建 MCP Servers 字典，供 ClaudeAgentOptions.mcp_servers 使用

        策略: 使用单个 stdio MCP server 加载所有技能。
        CLI 会作为子进程启动 stdio_mcp_server.py，
        通过 stdin/stdout 的 MCP 协议通信。

        Args:
            only_skills: @skill 白名单，非空时只加载这些技能（跳过缓存）

        Returns:
            {"sthg_v4_skills": McpStdioServerConfig}
        """
        if not self._config.mcp_tools_enabled:
            logger.info("[MCPToolBuilder] MCP tool chain disabled, returning no MCP servers")
            return {}

        # @skill 场景不使用缓存（每次请求的白名单可能不同）
        if not only_skills and self._cached_mcp_servers is not None:
            return self._cached_mcp_servers

        # 确认有可执行技能
        executable = list(self._discovery.get_executable_skills())
        if not executable:
            logger.warning("[MCPToolBuilder] No executable skills found")
            return {}

        # 构建 stdio MCP server 配置（使用 agent_core 版本）
        from agent_core.mcp import stdio_server as _stdio_mod
        import inspect
        _agent_core_stdio = str(Path(inspect.getfile(_stdio_mod)).resolve())
        server_script = _agent_core_stdio

        # 在容器中使用绝对路径
        if not Path(server_script).exists():
            server_script = "/app/agent_core/mcp/stdio_server.py"

        McpStdioServerConfig = dict

        # 环境变量透传
        env = dict(self._config.get_env_vars())
        env["PYTHONPATH"] = str(self._config.project_root)

        server_name = "sthg_v4_skills"

        # 构建 args：@skill 场景传入 --only-skills
        script_args = [server_script]
        if only_skills:
            script_args.extend(["--only-skills", ",".join(only_skills)])

        config = McpStdioServerConfig(
            type="stdio",
            command="python3",
            args=script_args,
            env=env,
        )

        mcp_servers = {server_name: config}

        if only_skills:
            logger.info(
                f"[MCPToolBuilder] Created @skill-restricted MCP server '{server_name}' "
                f"with only: {only_skills}"
            )
        else:
            tool_names = [m.name for m in executable]
            logger.info(
                f"[MCPToolBuilder] Created stdio MCP server '{server_name}' "
                f"with {len(executable)} tools: {tool_names[:10]}..."
            )
            self._cached_mcp_servers = mcp_servers

        return mcp_servers

    def _convert_all_tools(self) -> List[Any]:
        """转换所有可执行技能为 SDK 工具"""
        if self._cached_sdk_tools is not None:
            return self._cached_sdk_tools

        sdk_tools = []
        converted_names: set = set()

        for meta in self._discovery.get_executable_skills():
            if meta.name in converted_names:
                continue

            try:
                sdk_tool = self._convert_skill_to_sdk_tool(meta)
                if sdk_tool:
                    sdk_tools.append(sdk_tool)
                    converted_names.add(meta.name)
            except Exception as e:
                logger.error(
                    f"[MCPToolBuilder] Failed to convert skill "
                    f"'{meta.name}': {e}"
                )

        logger.info(f"[MCPToolBuilder] Converted {len(sdk_tools)} tools total")
        self._cached_sdk_tools = sdk_tools
        return sdk_tools

    def _convert_skill_to_sdk_tool(self, meta) -> Optional[Any]:
        """
        将 SkillMetadata 转换为 SDK Tool（subprocess 模式，无需实例）

        Args:
            meta: SkillMetadata
        """
        skill_name = meta.name
        # SDK Tool description 置为 skill_name 引用，不注入实际描述。
        # 完整描述（含真实耗时、display_name、category、intents）由 prompt_builder
        # 的 ## 可用工具列表 统一注入，避免重复。
        skill_desc = f"见可用工具列表中 {skill_name} 的完整说明"

        # 从 meta 获取 input_schema
        raw_schema = getattr(meta, "input_schema", None) or {}
        skill_input_schema = self._normalize_schema(raw_schema)

        # 创建执行闭包
        captured_executor = self._executor

        async def skill_handler(args: dict) -> dict:
            """V4 Skill 执行适配器"""
            try:
                result = await captured_executor.execute(skill_name, args)

                # Legacy SkillResult object check
                if hasattr(result, "status") and not isinstance(result, dict):
                    status_val = getattr(result, "status", None)
                    is_success = str(status_val).upper() not in ("ERROR", "FAILED")
                    if is_success:
                        return {
                            "content": [
                                {
                                    "type": "text",
                                    "text": _serialize_result(result),
                                }
                            ]
                        }
                    else:
                        return {
                            "content": [
                                {
                                    "type": "text",
                                    "text": f"Error: {getattr(result, 'error', None) or getattr(result, 'message', str(result))}",
                                }
                            ],
                            "is_error": True,
                        }

                # dict 兜底
                if isinstance(result, dict):
                    if "error" in result:
                        return {
                            "content": [{"type": "text", "text": result["error"]}],
                            "is_error": True,
                        }
                    return {
                        "content": [
                            {"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}
                        ]
                    }

                return {
                    "content": [{"type": "text", "text": str(result)}]
                }

            except Exception as e:
                logger.error(
                    f"[MCPToolBuilder] Skill '{skill_name}' execution error: {e}"
                )
                return {
                    "content": [
                        {"type": "text", "text": f"Execution error: {str(e)}"}
                    ],
                    "is_error": True,
                }

        return {
            "name": skill_name,
            "description": skill_desc,
            "input_schema": skill_input_schema,
            "handler": skill_handler,
        }

    @staticmethod
    def _create_sdk_mcp_server(name: str, tools: list) -> Optional[Dict[str, Any]]:
        """已废弃 — SDK MCP Server 已替换为 stdio server 模式"""
        return None

    @staticmethod
    def _normalize_schema(raw_schema) -> Dict[str, Any]:
        """将非标准 schema 转为 JSON Schema"""
        if isinstance(raw_schema, dict):
            if "type" in raw_schema and "properties" in raw_schema:
                return raw_schema
            # {key: type} 或 {key: {type: ..., description: ...}} 简写
            properties = {}
            for k, v in raw_schema.items():
                if v is str:
                    properties[k] = {"type": "string"}
                elif v is int:
                    properties[k] = {"type": "integer"}
                elif v is float:
                    properties[k] = {"type": "number"}
                elif v is bool:
                    properties[k] = {"type": "boolean"}
                elif v is list:
                    properties[k] = {"type": "array"}
                elif v is dict:
                    properties[k] = {"type": "object"}
                elif isinstance(v, dict):
                    properties[k] = v
                else:
                    properties[k] = {"type": "string"}
            return {"type": "object", "properties": properties}
        return {"type": "object", "properties": {}}


def _serialize_result(result) -> str:
    """序列化 SkillResult 为文本"""
    if hasattr(result, "data") and result.data:
        try:
            return json.dumps(result.data, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(result.data)
    if hasattr(result, "message"):
        return result.message or "OK"
    return "OK"
