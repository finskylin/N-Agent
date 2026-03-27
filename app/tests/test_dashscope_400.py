"""
诊断 dashscope (阿里云百炼) 在 AgentLoop 中返回 400 的原因。

测试策略：
1. 从真实 llm_endpoints.json 读取 dashscope 端点配置
2. 逐步增加请求复杂度，找到触发 400 的确切字段
3. 打印实际请求体，与 curl 成功的请求对比

运行方式:
    cd output_project/sthg_agent_service
    python -m pytest tests/test_dashscope_400.py -v -s
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional

import httpx
import pytest
import pytest_asyncio

# ── 让 agent_core 可 import ──
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# ── 加载 .env（无 dotenv 时用内置解析）──
def _load_dotenv(path: str):
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)

_env_path = os.path.join(BASE_DIR, ".env")
_load_dotenv(_env_path)

# ── 读取 llm_endpoints.json ──
_ENV_VAR_PATTERN = re.compile(r"\$\{(\w+)}")

def _interpolate(val: str) -> str:
    return _ENV_VAR_PATTERN.sub(lambda m: os.getenv(m.group(1), ""), val)

def _load_endpoints():
    json_path = os.path.join(BASE_DIR, "agent_core", "llm_endpoints.json")
    with open(json_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for ep in cfg.get("endpoints", []):
        for key in ("base_url", "api_key", "model", "small_fast_model"):
            if isinstance(ep.get(key), str) and "${" in ep[key]:
                ep[key] = _interpolate(ep[key])
    return cfg

_cfg = _load_endpoints()
_dashscope = next(
    (ep for ep in _cfg["endpoints"] if ep["name"] == "dashscope" and ep.get("enabled", True)),
    None,
)

# ── 工具定义样本（模拟 AgentLoop 实际传入的简化版）──
_SIMPLE_TOOLS = [
    {
        "name": "quick_search",
        "description": "搜索引擎查询",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"}
            },
            "required": ["query"],
        },
    }
]

_MANY_TOOLS = [
    {
        "name": f"tool_{i}",
        "description": f"工具描述 {i}",
        "input_schema": {
            "type": "object",
            "properties": {
                "param": {"type": "string", "description": f"参数 {i}"}
            },
            "required": ["param"],
        },
    }
    for i in range(20)
]

_SYSTEM_PROMPT = "你是一个智能助手，帮助用户分析问题。"

_MESSAGES_SIMPLE = [{"role": "user", "content": "你好"}]

_MESSAGES_WITH_TOOL_RESULT = [
    {"role": "user", "content": "查询苹果公司信息"},
    {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "tool_abc123",
                "name": "quick_search",
                "input": {"query": "苹果公司"},
            }
        ],
    },
    {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "tool_abc123",
                "content": "苹果公司 (Apple Inc.) 是美国跨国科技公司...",
            }
        ],
    },
]


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════

async def _post_to_dashscope(body: dict, stream: bool = False, timeout: float = 30.0):
    """向 dashscope 发送请求，返回 (status_code, response_text)"""
    assert _dashscope, "dashscope 端点未配置"
    url = f"{_dashscope['base_url'].rstrip('/')}/v1/messages"
    headers = {
        "x-api-key": _dashscope["api_key"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    print(f"\n[REQUEST] POST {url}")
    print(f"[REQUEST BODY]\n{json.dumps(body, ensure_ascii=False, indent=2)}")

    async with httpx.AsyncClient(timeout=timeout) as client:
        if stream:
            async with client.stream("POST", url, headers=headers, json=body) as resp:
                body_bytes = await resp.aread()
                text = body_bytes.decode("utf-8", errors="replace")
                print(f"[RESPONSE] status={resp.status_code}")
                print(f"[RESPONSE BODY]\n{text[:1000]}")
                return resp.status_code, text
        else:
            resp = await client.post(url, headers=headers, json=body)
            print(f"[RESPONSE] status={resp.status_code}")
            print(f"[RESPONSE BODY]\n{resp.text[:1000]}")
            return resp.status_code, resp.text


def _make_body(
    messages: list,
    tools: Optional[list] = None,
    system: Optional[str] = None,
    stream: bool = False,
    max_tokens: int = 512,
    model: Optional[str] = None,
    extra: Optional[dict] = None,
) -> dict:
    body: Dict[str, Any] = {
        "model": model or _dashscope["model"],
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        body["system"] = system
    if stream:
        body["stream"] = True
    if tools:
        body["tools"] = tools
    if extra:
        body.update(extra)
    return body


# ════════════════════════════════════════════════════════════════
# 测试用例（按复杂度递增）
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not _dashscope, reason="dashscope 未配置")
class TestDashscopeDiagnosis:
    """逐步增加复杂度，定位触发 400 的原因"""

    @pytest.mark.asyncio
    async def test_01_minimal_no_tools(self):
        """最简单请求：无 tools，无 system，普通 messages"""
        body = _make_body(messages=_MESSAGES_SIMPLE)
        status, text = await _post_to_dashscope(body)
        print(f"\n✅ test_01 status={status}")
        assert status == 200, f"最简请求失败: {text[:500]}"

    @pytest.mark.asyncio
    async def test_02_with_system(self):
        """加 system prompt"""
        body = _make_body(messages=_MESSAGES_SIMPLE, system=_SYSTEM_PROMPT)
        status, text = await _post_to_dashscope(body)
        print(f"\n✅ test_02 status={status}")
        assert status == 200, f"含 system 的请求失败: {text[:500]}"

    @pytest.mark.asyncio
    async def test_03_with_one_tool(self):
        """加一个工具（非流式）"""
        body = _make_body(
            messages=_MESSAGES_SIMPLE,
            system=_SYSTEM_PROMPT,
            tools=_SIMPLE_TOOLS,
        )
        status, text = await _post_to_dashscope(body)
        print(f"\n✅ test_03 status={status}")
        assert status == 200, f"含单工具的请求失败: {text[:500]}"

    @pytest.mark.asyncio
    async def test_04_with_one_tool_stream(self):
        """加一个工具（流式）"""
        body = _make_body(
            messages=_MESSAGES_SIMPLE,
            system=_SYSTEM_PROMPT,
            tools=_SIMPLE_TOOLS,
            stream=True,
        )
        status, text = await _post_to_dashscope(body, stream=True)
        print(f"\n✅ test_04 stream status={status}")
        assert status == 200, f"流式含单工具的请求失败: {text[:500]}"

    @pytest.mark.asyncio
    async def test_05_with_many_tools(self):
        """20个工具（非流式）—— 测试工具数量是否触发限制"""
        body = _make_body(
            messages=_MESSAGES_SIMPLE,
            system=_SYSTEM_PROMPT,
            tools=_MANY_TOOLS,
        )
        status, text = await _post_to_dashscope(body)
        print(f"\n✅ test_05 status={status}")
        assert status == 200, f"含20个工具的请求失败: {text[:500]}"

    @pytest.mark.asyncio
    async def test_06_with_many_tools_stream(self):
        """20个工具（流式）—— 最接近 AgentLoop 实际调用场景"""
        body = _make_body(
            messages=_MESSAGES_SIMPLE,
            system=_SYSTEM_PROMPT,
            tools=_MANY_TOOLS,
            stream=True,
        )
        status, text = await _post_to_dashscope(body, stream=True)
        print(f"\n✅ test_06 stream status={status}")
        assert status == 200, f"流式含20工具的请求失败: {text[:500]}"

    @pytest.mark.asyncio
    async def test_07_with_tool_result_messages(self):
        """含 tool_result 的多轮消息（模拟 AgentLoop 第二轮）"""
        body = _make_body(
            messages=_MESSAGES_WITH_TOOL_RESULT,
            system=_SYSTEM_PROMPT,
            tools=_SIMPLE_TOOLS,
            stream=True,
        )
        status, text = await _post_to_dashscope(body, stream=True)
        print(f"\n✅ test_07 stream status={status}")
        assert status == 200, f"含 tool_result 消息的请求失败: {text[:500]}"

    @pytest.mark.asyncio
    async def test_08_max_tokens_8096(self):
        """AgentLoop 默认 max_tokens=8096"""
        body = _make_body(
            messages=_MESSAGES_SIMPLE,
            system=_SYSTEM_PROMPT,
            tools=_SIMPLE_TOOLS,
            stream=True,
            max_tokens=8096,
        )
        status, text = await _post_to_dashscope(body, stream=True)
        print(f"\n✅ test_08 max_tokens=8096 status={status}")
        assert status == 200, f"max_tokens=8096 触发错误: {text[:500]}"

    @pytest.mark.asyncio
    async def test_09_agentloop_exact_body(self):
        """
        使用 LiteLLMProvider._build_request_body 生成和 AgentLoop 完全相同的请求体。
        这个测试直接重现 AgentLoop 的实际行为。
        """
        from agent_core.agentloop.llm_provider import LiteLLMProvider, _load_json_endpoints

        # 模拟 config 对象（只需要 _build_endpoints 用到的字段）
        class _MockConfig:
            pass

        provider = LiteLLMProvider(_MockConfig())

        # 找到 dashscope endpoint
        ds_ep = next(
            (ep for ep in provider._endpoints if ep.name == "dashscope"),
            None,
        )
        if not ds_ep:
            pytest.skip("dashscope endpoint not loaded by LiteLLMProvider")

        # 用 provider 的 _build_request_body 生成请求体（与真实 AgentLoop 一致）
        # 转换 OpenAI 格式工具为 Anthropic 格式（模拟 prompt_builder 行为）
        openai_format_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["input_schema"],
                },
            }
            for t in _MANY_TOOLS[:5]
        ]
        body = provider._build_request_body(
            messages=_MESSAGES_SIMPLE,
            tools=openai_format_tools,
            model=ds_ep.model,
            max_tokens=8096,
            system=_SYSTEM_PROMPT,
            stream=True,
        )

        print(f"\n[AgentLoop exact body] tools count={len(body.get('tools', []))}")
        status, text = await _post_to_dashscope(body, stream=True)
        print(f"\n✅ test_09 AgentLoop exact body status={status}")
        assert status == 200, f"AgentLoop 精确请求体失败: {text[:500]}"

    @pytest.mark.asyncio
    async def test_10_tool_result_with_is_error_field(self):
        """
        测试 tool_result 中包含 is_error 字段是否触发 400。
        ContextBuilder.add_tool_results() 会加 is_error=False，
        dashscope 可能不支持这个字段。
        """
        messages_with_is_error = [
            {"role": "user", "content": "查询苹果公司"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool_abc123",
                        "name": "quick_search",
                        "input": {"query": "苹果公司"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool_abc123",
                        "content": "苹果公司 (Apple Inc.) 是美国跨国科技公司...",
                        "is_error": False,  # ← 这个字段 dashscope 可能不支持
                    }
                ],
            },
        ]
        body = _make_body(
            messages=messages_with_is_error,
            system=_SYSTEM_PROMPT,
            tools=_SIMPLE_TOOLS,
            stream=True,
            max_tokens=8096,
        )
        status, text = await _post_to_dashscope(body, stream=True)
        print(f"\n✅ test_10 is_error=False status={status}")
        if status != 200:
            print(f"[FOUND] is_error 字段触发 400: {text[:300]}")
        assert status == 200, f"含 is_error=False 的 tool_result 失败: {text[:500]}"

    @pytest.mark.asyncio
    async def test_11_thinking_block_without_signature(self):
        """
        dashscope 返回的 thinking 块包含 signature 字段，
        但 ContextBuilder.add_llm_response 写回时没有 signature。
        测试不带 signature 的 thinking block 是否触发 400。
        """
        messages_with_thinking = [
            {"role": "user", "content": "分析苹果公司"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "用户想了解苹果公司...",
                        # 无 signature 字段 ← 这是关键
                    },
                    {
                        "type": "tool_use",
                        "id": "tool_abc123",
                        "name": "quick_search",
                        "input": {"query": "苹果公司"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool_abc123",
                        "content": "苹果公司 (Apple Inc.) ...",
                    }
                ],
            },
        ]
        body = _make_body(
            messages=messages_with_thinking,
            system=_SYSTEM_PROMPT,
            tools=_SIMPLE_TOOLS,
            stream=True,
            max_tokens=8096,
        )
        status, text = await _post_to_dashscope(body, stream=True)
        print(f"\n✅ test_11 thinking-no-signature status={status}")
        if status != 200:
            print(f"[FOUND] thinking block 无 signature 触发 400: {text[:300]}")
        assert status == 200, f"thinking block 无 signature 失败: {text[:500]}"

    @pytest.mark.asyncio
    async def test_12_thinking_block_with_signature(self):
        """
        测试带 signature 的 thinking block 是否能通过（dashscope 返回格式）。
        """
        messages_with_thinking = [
            {"role": "user", "content": "分析苹果公司"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "用户想了解苹果公司...",
                        "signature": "",  # ← 带 signature（即使为空）
                    },
                    {
                        "type": "tool_use",
                        "id": "tool_abc123",
                        "name": "quick_search",
                        "input": {"query": "苹果公司"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool_abc123",
                        "content": "苹果公司 (Apple Inc.) ...",
                    }
                ],
            },
        ]
        body = _make_body(
            messages=messages_with_thinking,
            system=_SYSTEM_PROMPT,
            tools=_SIMPLE_TOOLS,
            stream=True,
            max_tokens=8096,
        )
        status, text = await _post_to_dashscope(body, stream=True)
        print(f"\n✅ test_12 thinking-with-signature status={status}")
        if status != 200:
            print(f"[FOUND] thinking block 带 signature 仍触发 400: {text[:300]}")
        assert status == 200, f"thinking block 带 signature 失败: {text[:500]}"

    @pytest.mark.asyncio
    async def test_14_real_agentloop_tools_from_skill_registry(self):
        """
        从 skill_registry 加载真实 AgentLoop 工具定义，测试完整场景。
        这是最接近真实 400 复现的测试。
        """
        try:
            from app.agent.v4.skill_registry_v2 import SkillRegistryV2
            from agent_core.skill_discovery import SkillDiscovery

            discovery = SkillDiscovery()
            await discovery.discover()
            skills = discovery.get_all_skills()
            print(f"\n[Skills] Discovered {len(skills)} skills")

            # 构造 Anthropic 格式工具列表（与 AgentLoop 一致）
            anthropic_tools = []
            for skill in skills[:26]:  # 取前26个，模拟实际场景
                anthropic_tools.append({
                    "name": skill.name,
                    "description": skill.description or "",
                    "input_schema": {
                        "type": "object",
                        "properties": skill.input_schema.get("properties", {}) if skill.input_schema else {},
                        "required": skill.input_schema.get("required", []) if skill.input_schema else [],
                    },
                })

            print(f"[Tools] Sending {len(anthropic_tools)} tools to dashscope")
            body = _make_body(
                messages=_MESSAGES_SIMPLE,
                system=_SYSTEM_PROMPT,
                tools=anthropic_tools,
                stream=True,
                max_tokens=8096,
            )
            status, text = await _post_to_dashscope(body, stream=True)
            print(f"\n✅ test_10 real skills status={status}")
            assert status == 200, f"真实 skills 工具列表请求失败: {text[:500]}"
        except ImportError as e:
            pytest.skip(f"无法加载 SkillDiscovery: {e}")
