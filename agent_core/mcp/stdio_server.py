"""
Stdio MCP Server for V4 Agent Skills

独立进程运行，通过 stdin/stdout 与 Claude CLI 通信。
由 MCPToolBuilder 通过 McpStdioServerConfig 启动。

用法:
    python -m agent_core.mcp.stdio_server [--category CATEGORY]

如果指定 --category，仅加载该分类的技能；否则加载所有技能。

AgentCore 版本:
- 从 agent_core 内部导入 V4Config, SkillDiscovery, V4SkillExecutor
- 无 FastAPI/Redis/MySQL 依赖
"""
import sys
import os
import json
import asyncio
import argparse
from pathlib import Path

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


async def main(category_filter: str = None, only_skills: list = None):
    """启动 stdio MCP Server

    Args:
        category_filter: 只加载指定分类的技能
        only_skills: 技能白名单列表，如果非空则只加载这些技能（用于 @skill 场景）
    """
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool

    from agent_core.config import V4Config
    from agent_core.skill_discovery import SkillDiscovery
    from agent_core.skill_executor import V4SkillExecutor

    # 初始化 V4 组件
    config = V4Config.from_env()
    discovery = SkillDiscovery(config.skills_dir)
    discovery.scan()

    executor = V4SkillExecutor(discovery)
    executor.load_all()

    # 收集可执行技能（subprocess 模式，无需实例）
    skill_metas = []
    for meta in discovery.get_executable_skills():
        # @skill 白名单过滤（最高优先级）
        if only_skills and meta.name not in only_skills:
            continue
        skill_metas.append(meta)

    server_name = f"sthg_v4_{category_filter}" if category_filter else "sthg_v4_all"
    server = Server(server_name)

    # 构建工具映射
    tool_map = {}
    for meta in skill_metas:
        tool_map[meta.name] = meta

    @server.list_tools()
    async def list_tools() -> list:
        tools = []
        for name, meta in tool_map.items():
            raw_schema = getattr(meta, "input_schema", None) or {}
            schema = _normalize_schema(raw_schema)
            desc = meta.description or f"执行 {name}"
            tools.append(Tool(
                name=name,
                description=desc,
                inputSchema=schema,
            ))
        return tools

    @server.call_tool()
    async def call_tool(tool_name: str, arguments: dict) -> list:
        if tool_name not in tool_map:
            return [TextContent(type="text", text=f"Error: tool '{tool_name}' not found")]

        try:
            result = await executor.execute(tool_name, arguments)

            original_data = None  # 用于 budget trimming 的原始数据

            # SkillResult (legacy) or dict
            if hasattr(result, "status") and not isinstance(result, dict):
                # Legacy SkillResult object
                status_val = getattr(result, "status", None)
                is_success = str(status_val).upper() not in ("ERROR", "FAILED")
                if is_success:
                    data = getattr(result, "data", None)
                    if data:
                        original_data = data
                        try:
                            text = json.dumps(data, ensure_ascii=False, default=str)
                        except (TypeError, ValueError):
                            text = str(data)
                    else:
                        text = getattr(result, "message", None) or "OK"
                else:
                    text = f"Error: {getattr(result, 'error', None) or getattr(result, 'message', str(result))}"
            elif isinstance(result, dict):
                if "error" in result:
                    text = f"Error: {result['error']}"
                elif "for_llm" in result:
                    # 新格式：分离 for_llm 和 for_ui
                    # 只把 for_llm 部分返回给 Claude，for_ui 由前端单独处理
                    original_data = result["for_llm"]
                    text = json.dumps(result["for_llm"], ensure_ascii=False, default=str)

                    # 始终写 sidecar（保存完整 result 含 for_ui），确保 hook 能恢复 UI 组件
                    if "for_ui" in result:
                        try:
                            sidecar_text = json.dumps(result, ensure_ascii=False, default=str)
                            sidecar_path = _write_sidecar(tool_name, sidecar_text)
                            # 嵌入 sidecar 路径到 LLM 数据中
                            if isinstance(original_data, dict):
                                llm_with_path = json.loads(text)
                                llm_with_path["_full_data_path"] = sidecar_path
                                text = json.dumps(llm_with_path, ensure_ascii=False, default=str)
                        except Exception as sc_err:
                            print(f"[stdio_mcp_server] Sidecar write failed for {tool_name}: {sc_err}", file=sys.stderr)
                else:
                    original_data = result
                    text = json.dumps(result, ensure_ascii=False, default=str)
            else:
                text = str(result)

            # LLM 数据预算裁剪: 当 text 超过阈值时裁剪，防止 CLI token 上限截断
            if (config.llm_budget_enabled
                    and original_data is not None
                    and len(text) > config.llm_budget_max_chars):
                try:
                    from agent_core.llm_data_budget import budget_for_llm, BudgetConfig

                    budget_cfg = BudgetConfig.from_v4_config(config)
                    skill_strip = discovery.get_llm_strip_fields(tool_name)

                    # 写 sidecar 保存全量数据（for_llm/for_ui 格式已在上面写过，跳过）
                    if not (isinstance(result, dict) and "for_ui" in result):
                        sidecar_path = _write_sidecar(tool_name, text)
                    else:
                        # 复用已写的 sidecar 路径
                        try:
                            _tmp = json.loads(text)
                            sidecar_path = _tmp.get("_full_data_path", "")
                        except Exception:
                            sidecar_path = _write_sidecar(tool_name, text)

                    # 裁剪
                    budgeted = budget_for_llm(tool_name, original_data, budget_cfg, skill_strip)
                    if isinstance(budgeted, dict):
                        budgeted["_full_data_path"] = sidecar_path
                    text = json.dumps(budgeted, ensure_ascii=False, default=str)

                    print(
                        f"[stdio_mcp_server] Budget trimmed {tool_name}: "
                        f"{len(json.dumps(original_data, ensure_ascii=False, default=str))} -> {len(text)} chars",
                        file=sys.stderr,
                    )
                except Exception as budget_err:
                    print(f"[stdio_mcp_server] Budget trimming failed for {tool_name}: {budget_err}", file=sys.stderr)

            return [TextContent(type="text", text=text)]

        except Exception as e:
            return [TextContent(type="text", text=f"Execution error: {str(e)}")]

    # 运行 stdio server
    async with stdio_server() as (read_stream, write_stream):
        from mcp.server import NotificationOptions
        from mcp.server.models import InitializationOptions

        init_options = InitializationOptions(
            server_name=server_name,
            server_version="1.0.0",
            capabilities=server.get_capabilities(
                notification_options=NotificationOptions(),
                experimental_capabilities={},
            ),
        )

        # 日志输出到 stderr（不能用 stdout，那是 MCP 协议通道）
        print(
            f"[stdio_mcp_server] Starting: {server_name}, "
            f"tools: {list(tool_map.keys())}",
            file=sys.stderr,
        )

        await server.run(
            read_stream,
            write_stream,
            init_options,
        )


def _write_sidecar(tool_name: str, json_text: str) -> str:
    """将全量数据写到临时文件，供 PostToolUse hook 读取用于 UI 渲染"""
    import time
    path = f"/tmp/skill_sidecar_{tool_name}_{int(time.time() * 1000)}.json"
    with open(path, 'w', encoding='utf-8') as f:
        f.write(json_text)
    return path


def _normalize_schema(raw_schema) -> dict:
    """将非标准 schema 转为 JSON Schema

    处理 skill 自定义的 input_schema 格式，将 property 内部的
    required/default 等非标准字段提取到顶层，确保输出符合 JSON Schema 规范。
    """
    if isinstance(raw_schema, dict):
        if "type" in raw_schema and "properties" in raw_schema:
            return raw_schema
        properties = {}
        required = []
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
                prop = dict(v)
                # 提取 required 到顶层数组（JSON Schema 标准格式）
                if prop.pop("required", False):
                    required.append(k)
                properties[k] = prop
            else:
                properties[k] = {"type": "string"}
        schema = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        return schema
    return {"type": "object", "properties": {}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--category", default=None, help="只加载指定分类的技能")
    parser.add_argument("--only-skills", default=None, help="逗号分隔的技能白名单，只加载这些技能")
    args = parser.parse_args()

    only_skills = None
    if args.only_skills:
        only_skills = [s.strip() for s in args.only_skills.split(",") if s.strip()]

    asyncio.run(main(category_filter=args.category, only_skills=only_skills))
