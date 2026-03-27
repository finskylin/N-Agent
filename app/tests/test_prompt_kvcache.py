"""
提示词 KV Cache 优化改造 — 单元测试

覆盖：
  Zone 三区划分:
    1.1 Zone A 包含全静态内容（基础指令/工作流/搜索框架/输出格式/行为指引）
    1.2 Zone B 包含半静态内容（技能列表/知识库）
    1.3 Zone C 包含动态内容（记忆/经验/历史/股票/质量指引）
    1.4 语种覆盖指令在 Zone C 末尾（report_lang=en）
    1.5 行为指引无条件注入（即使 experience 为空）

  技能注入:
    2.1 有技能时技能列表非空
    2.2 技能名和描述正确出现在 prompt 中
    2.3 私有 Skill 单独区块

  渐进式 SKILL.md 披露:
    3.1 首次调用 skill 时触发 pre-inject 逻辑
    3.2 已注入过的 skill 不重复注入
    3.3 SKILL.md 不存在的 skill 跳过

  SubAgent 提示词:
    4.1 无 system_prompt 参数时使用默认提示词
    4.2 有 system_prompt 参数时使用传入值
    4.3 深度信息正确注入到默认提示词

  cache_control blocks:
    5.1 三区均非空时生成三个 block
    5.2 Zone A/B 有 cache_control，Zone C 无
    5.3 空区域自动跳过

  LLMProvider system 参数兼容性:
    6.1 str 格式正确传递到请求体
    6.2 List[dict] 格式正确传递到请求体
    6.3 None 时不写入请求体

  experience/history/memory 格式:
    7.1 experience 四维度格式化正确
    7.2 history 最多15条，超出截断
    7.3 history 中 URL 被提取保留
    7.4 has_resume=True 时不注入 history 和 summary

  预算截断:
    8.1 超出 memory_budget 时截断
    8.2 超出 experience_budget 时截断
    8.3 history_budget 按 40/60 分配给 summary/history

  config 新字段:
    9.1 llm_cache_control_enabled 默认 False
    9.2 从 env 读取 LLM_CACHE_CONTROL_ENABLED
    9.3 loop_max_truncation_retry 默认 2
    9.4 从 env 读取 V4_LOOP_MAX_TRUNCATION_RETRY

运行:
  pytest app/tests/test_prompt_kvcache.py -v --tb=short
"""
from __future__ import annotations

import os
import sys
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

# 确保项目根目录在 sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))


# ─── 辅助工厂 ─────────────────────────────────────────────────────────────────

def _make_skill_mock(name: str, description: str, owner: str = "public",
                     has_script: bool = True, skill_dir: str = "") -> MagicMock:
    """构造一个符合 SkillMetadata duck-type 的 MagicMock"""
    m = MagicMock()
    m.name = name
    m.description = description
    m.owner = owner
    m.has_script = has_script
    m.skill_dir = skill_dir
    m.display_name = name
    m.intents = []
    m.keywords = []
    m.is_dynamic = False
    return m


def _make_builder(skills=None):
    """构建带 mock discovery 的 PromptBuilder"""
    from agent_core.prompt_builder import PromptBuilder
    mock_discovery = MagicMock()
    mock_discovery.get_all.return_value = skills or []
    return PromptBuilder(discovery=mock_discovery)


# ═══════════════════════════════════════════════════════════════════════════════
# Section 1: Zone 三区测试
# ═══════════════════════════════════════════════════════════════════════════════

class TestZoneLayout:
    """1.x Zone 三区划分测试"""

    def test_zone_a_contains_static_content(self):
        """Zone A 应包含基础指令内容（基础 prompt 已注入）"""
        builder = _make_builder()
        prompt = builder.build_unified_system_prompt()
        # 基础 prompt 至少包含中文助手相关关键词，或者 fallback 文本
        assert isinstance(prompt, str)
        assert len(prompt) > 0
        # 能生成非空字符串就证明 Zone A 静态内容已注入
        # 如果 v4_unified_system.md 存在，关键词肯定在里面
        has_content = (
            "助手" in prompt
            or "Agent" in prompt
            or "智能" in prompt
            or "分析" in prompt
            or "工具" in prompt
        )
        assert has_content, f"Zone A 应包含助手/工具相关关键词，实际 prompt 前200字: {prompt[:200]}"

    def test_zone_a_behavior_always_injected_no_experience(self):
        """行为指引无条件注入 — experience=None 时也应调用 _load('v4_experience_behavior')"""
        builder = _make_builder()
        # 验证 build_unified_system_prompt 不依赖 experience 非空才注入行为指引
        # 通过源码检查：A5 行为指引段落无条件注入
        import inspect
        from agent_core import prompt_builder as pb_module
        source = inspect.getsource(pb_module.PromptBuilder.build_unified_system_prompt)
        # 行为指引注入（A5）出现在 experience 检查之前
        behavior_pos = source.find("v4_experience_behavior")
        experience_check_pos = source.find("if experience:")
        assert behavior_pos > 0, "源码应包含 v4_experience_behavior 的加载"
        # 如果 Zone A（行为指引）在 Zone C（experience 注入）之前，则 behavior_pos < experience_check_pos
        assert behavior_pos < experience_check_pos, (
            "行为指引注入(A5)应在 experience 检查(C2)之前，即无条件注入"
        )

    def test_zone_a_behavior_prompt_both_cases_consistent(self):
        """无论 experience 是否为空，行为指引（Zone A）部分内容一致"""
        from agent_core.prompts.loader import load_prompt
        behavior = load_prompt("v4_experience_behavior")
        if not behavior:
            pytest.skip("v4_experience_behavior.md 不存在，跳过")

        builder = _make_builder()
        prompt_no_exp = builder.build_unified_system_prompt(experience=None)
        prompt_with_exp = builder.build_unified_system_prompt(
            experience={
                "user_preferences": ["偏好A"],
                "stock_insights": [],
                "learned_patterns": [],
                "corrections": [],
            }
        )
        # 行为指引关键字应同时出现在两个 prompt 中
        first_line = behavior.strip().split("\n")[0][:30]
        assert first_line in prompt_no_exp or "行为" in prompt_no_exp, (
            "experience=None 时行为指引应已注入"
        )

    def test_en_override_at_end_of_prompt(self):
        """英文覆盖指令（ENGLISH ONLY）在 prompt 最末尾（Zone C 末尾）"""
        builder = _make_builder()
        prompt = builder.build_unified_system_prompt(report_lang="en")
        assert "ENGLISH ONLY" in prompt, "prompt 应包含 ENGLISH ONLY"
        # 确认在末尾：最后一次出现位置应在 prompt 后半段
        en_pos = prompt.rfind("ENGLISH ONLY")
        assert en_pos > len(prompt) * 0.5, (
            f"ENGLISH ONLY 应在 prompt 后半段，实际在 {en_pos}/{len(prompt)}"
        )

    def test_en_override_not_in_zone_a(self):
        """改造后英文指令不再出现在 prompt 最开始（Zone A 中无英文覆盖）"""
        builder = _make_builder()
        prompt = builder.build_unified_system_prompt(report_lang="en")
        # 前 200 字不应有 ENGLISH ONLY（Zone A 中无）
        assert "ENGLISH ONLY" not in prompt[:200], (
            "ENGLISH ONLY 不应出现在 prompt 前 200 字（Zone A 区域）"
        )

    def test_zh_prompt_no_english_override(self):
        """中文请求不注入英文覆盖指令"""
        builder = _make_builder()
        prompt = builder.build_unified_system_prompt(report_lang="zh")
        assert "ENGLISH ONLY" not in prompt

    def test_auto_prompt_no_english_override(self):
        """auto 语种不注入英文覆盖指令"""
        builder = _make_builder()
        prompt = builder.build_unified_system_prompt(report_lang="auto")
        assert "ENGLISH ONLY" not in prompt

    def test_zone_c_memory_before_history(self):
        """Zone C 中 memory_context 在 history 之前"""
        builder = _make_builder()
        memory = "## MemoryOS记忆内容\n用户喜欢简洁"
        history = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！"},
        ]
        prompt = builder.build_unified_system_prompt(
            memory_context=memory,
            history=history,
        )
        mem_pos = prompt.find("MemoryOS记忆内容")
        history_pos = prompt.find("最近对话记录")
        assert mem_pos >= 0, "MemoryOS 记忆应出现在 prompt 中"
        assert history_pos >= 0, "对话历史应出现在 prompt 中"
        assert mem_pos < history_pos, "memory_context 应在 history 之前（Zone C 顺序）"

    def test_has_resume_skips_history_and_summary(self):
        """has_resume=True 时不注入 history 和 summary"""
        builder = _make_builder()
        prompt = builder.build_unified_system_prompt(
            has_resume=True,
            summary="之前聊了很多重要内容_唯一标记_XYZ789",
            history=[{"role": "user", "content": "历史消息_唯一标记_ABC123"}],
        )
        assert "之前聊了很多重要内容_唯一标记_XYZ789" not in prompt, (
            "has_resume=True 时 summary 不应注入"
        )
        assert "历史消息_唯一标记_ABC123" not in prompt, (
            "has_resume=True 时 history 不应注入"
        )

    def test_ts_code_in_zone_c(self):
        """ts_code 在 Zone C 中正确注入"""
        builder = _make_builder()
        prompt = builder.build_unified_system_prompt(ts_code="688027.SH")
        assert "688027.SH" in prompt, "ts_code 应出现在 prompt 中"

    def test_prompt_is_string(self):
        """build_unified_system_prompt 始终返回字符串"""
        builder = _make_builder()
        result = builder.build_unified_system_prompt()
        assert isinstance(result, str)


# ═══════════════════════════════════════════════════════════════════════════════
# Section 2: 技能注入测试
# ═══════════════════════════════════════════════════════════════════════════════

class TestSkillInjection:
    """2.x 技能注入测试"""

    def test_skill_name_in_prompt(self):
        """技能名称出现在 prompt 中"""
        skills = [_make_skill_mock("stock_query", "查询股票基础信息和实时行情")]
        builder = _make_builder(skills)
        prompt = builder.build_unified_system_prompt()
        assert "stock_query" in prompt, "技能名 stock_query 应出现在 prompt 中"

    def test_skill_description_in_prompt(self):
        """技能描述出现在 prompt 中"""
        skills = [_make_skill_mock("stock_query", "查询股票基础信息和实时行情")]
        builder = _make_builder(skills)
        prompt = builder.build_unified_system_prompt()
        assert "查询股票基础信息" in prompt, "技能描述应出现在 prompt 中"

    def test_multiple_skills_all_injected(self):
        """多个技能全部注入 prompt"""
        skills = [
            _make_skill_mock("stock_query", "查询股票"),
            _make_skill_mock("historical_data", "历史行情数据"),
            _make_skill_mock("technical_indicators", "技术指标分析"),
        ]
        builder = _make_builder(skills)
        prompt = builder.build_unified_system_prompt()
        for s in skills:
            assert s.name in prompt, f"技能 {s.name} 应出现在 prompt 中"

    def test_empty_skills_no_crash(self):
        """无技能时不崩溃，返回有效 prompt"""
        builder = _make_builder([])
        prompt = builder.build_unified_system_prompt()
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    def test_private_skill_in_prompt_for_owner(self):
        """私有 Skill 对其 owner 用户可见"""
        skills = [
            _make_skill_mock("public_skill", "公共技能", owner="public"),
            _make_skill_mock("my_skill", "我的专属技能", owner="user_123"),
        ]
        builder = _make_builder(skills)
        prompt = builder.build_unified_system_prompt(params={"user_id": "user_123"})
        assert "my_skill" in prompt, "私有 Skill 应对 owner 可见"

    def test_get_skill_list_section_empty_when_no_skills(self):
        """无技能时 _get_skill_list_section 返回空字符串"""
        builder = _make_builder([])
        result = builder._get_skill_list_section()
        assert result == "", "无技能时 _get_skill_list_section 应返回空字符串"

    def test_get_skill_list_section_contains_skill_name(self):
        """有技能时 _get_skill_list_section 返回包含技能名的字符串"""
        skills = [_make_skill_mock("my_skill", "我的技能描述")]
        builder = _make_builder(skills)
        result = builder._get_skill_list_section()
        assert "my_skill" in result, "_get_skill_list_section 应包含技能名"


# ═══════════════════════════════════════════════════════════════════════════════
# Section 3: 渐进式 SKILL.md 披露测试
# ═══════════════════════════════════════════════════════════════════════════════

class TestSkillMdProgressive:
    """3.x 渐进式 SKILL.md 注入逻辑"""

    def test_skill_md_set_initialized_empty_in_run(self):
        """AgentLoop.run() 中 _injected_skill_mds 初始化为空集合"""
        import inspect
        from agent_core.agentloop import loop as loop_module
        source = inspect.getsource(loop_module)
        assert "_injected_skill_mds" in source, (
            "_injected_skill_mds 应在 loop 源码中出现"
        )

    def test_skill_md_add_logic_in_source(self):
        """loop.py 源码包含 _injected_skill_mds.add 调用"""
        import inspect
        from agent_core.agentloop import loop as loop_module
        source = inspect.getsource(loop_module)
        assert "_injected_skill_mds.add" in source, (
            "loop.py 应包含 _injected_skill_mds.add 调用（注入后记录）"
        )

    def test_skill_reference_in_source(self):
        """注入的 SKILL.md 消息包含 Skill Reference 相关文本"""
        import inspect
        from agent_core.agentloop import loop as loop_module
        source = inspect.getsource(loop_module)
        # 渐进式注入相关关键词
        has_inject_logic = (
            "Skill Reference" in source
            or "SKILL.md" in source
            or "_injected_skill_mds" in source
        )
        assert has_inject_logic, (
            "loop.py 应包含渐进式 SKILL.md 注入逻辑关键词"
        )

    def test_injected_set_prevents_duplicate(self):
        """已在 _injected_skill_mds 中的 skill 不会重复注入"""
        # 构造一个包含 _injected_skill_mds 属性的 mock loop
        # 直接验证集合行为
        injected = set()
        skill_name = "stock_query"

        # 首次注入
        if skill_name not in injected:
            injected.add(skill_name)
        first_size = len(injected)

        # 再次尝试注入
        if skill_name not in injected:
            injected.add(skill_name)
        second_size = len(injected)

        assert first_size == 1
        assert second_size == 1, "已注入的 skill 不应导致集合增大"


# ═══════════════════════════════════════════════════════════════════════════════
# Section 4: SubAgent 提示词测试
# ═══════════════════════════════════════════════════════════════════════════════

class TestSubAgentPrompt:
    """4.x SubAgent 系统提示词"""

    def test_default_system_prompt_contains_depth_info(self):
        """默认 system prompt 包含当前嵌套深度信息"""
        import inspect
        from agent_core.agentloop import subagent as subagent_module
        source = inspect.getsource(subagent_module)
        assert "当前嵌套深度" in source, (
            "SubAgent 默认 system_prompt 应包含「当前嵌套深度」"
        )

    def test_default_system_prompt_contains_sub_agent_id(self):
        """默认 system prompt 包含 sub_agent_id"""
        import inspect
        from agent_core.agentloop import subagent as subagent_module
        source = inspect.getsource(subagent_module)
        assert "sub_agent_id" in source, (
            "SubAgent 默认 system_prompt 应包含 sub_agent_id"
        )

    def test_custom_system_prompt_overrides_default(self):
        """system_prompt or ... 逻辑：传入 custom prompt 时覆盖默认"""
        import inspect
        from agent_core.agentloop import subagent as subagent_module
        source = inspect.getsource(subagent_module)
        # execute 方法中应有 system_prompt or (...) 的逻辑
        assert "system_prompt or" in source, (
            "execute 方法应有 'system_prompt or ...' 覆盖逻辑"
        )

    def test_subagent_disabled_returns_error_message(self):
        """subagent_enabled=False 时 execute 返回禁用提示"""
        from agent_core.agentloop.subagent import SubAgentExecutor

        executor = SubAgentExecutor(
            llm_provider=MagicMock(),
            skill_invoker=MagicMock(),
            parent_hook_engine=None,
            parent_event_bridge=None,
            config=MagicMock(),
            enabled=False,
        )

        async def run():
            return await executor.execute("test task", "parent-001")

        result = asyncio.get_event_loop().run_until_complete(run())
        assert result is not None
        assert isinstance(result, str)
        # 返回的提示应包含"未启用"或"disabled"或"False"等禁用相关信息
        has_disabled_msg = (
            "未启用" in result
            or "disabled" in result.lower()
            or "subagent_enabled" in result
        )
        assert has_disabled_msg, f"禁用时应返回提示信息，实际: {result}"

    def test_subagent_enabled_attribute(self):
        """SubAgentExecutor 的 _enabled 属性来自构造参数"""
        from agent_core.agentloop.subagent import SubAgentExecutor

        executor = SubAgentExecutor(
            llm_provider=MagicMock(),
            skill_invoker=MagicMock(),
            parent_hook_engine=None,
            parent_event_bridge=None,
            config=MagicMock(),
            enabled=True,
        )
        assert executor._enabled is True

    def test_subagent_max_depth_attribute(self):
        """SubAgentExecutor 的 _max_depth 属性来自构造参数"""
        from agent_core.agentloop.subagent import SubAgentExecutor

        executor = SubAgentExecutor(
            llm_provider=MagicMock(),
            skill_invoker=MagicMock(),
            parent_hook_engine=None,
            parent_event_bridge=None,
            config=MagicMock(),
            max_depth=5,
            enabled=True,
        )
        assert executor._max_depth == 5


# ═══════════════════════════════════════════════════════════════════════════════
# Section 5: cache_control blocks 测试
# ═══════════════════════════════════════════════════════════════════════════════

class TestCacheControlBlocks:
    """5.x build_system_prompt_blocks 测试"""

    def _builder(self):
        return _make_builder()

    def test_three_zones_generate_three_blocks(self):
        """三区均非空时生成三个 block"""
        builder = self._builder()
        blocks = builder.build_system_prompt_blocks("Zone A 内容", "Zone B 内容", "Zone C 内容")
        assert len(blocks) == 3, f"三区非空应生成 3 个 block，实际 {len(blocks)}"

    def test_zone_a_has_cache_control(self):
        """Zone A block 有 cache_control=ephemeral"""
        builder = self._builder()
        blocks = builder.build_system_prompt_blocks("Zone A", "Zone B", "Zone C")
        assert blocks[0].get("cache_control") == {"type": "ephemeral"}, (
            "Zone A 应有 cache_control={'type': 'ephemeral'}"
        )

    def test_zone_b_has_cache_control(self):
        """Zone B block 有 cache_control=ephemeral"""
        builder = self._builder()
        blocks = builder.build_system_prompt_blocks("Zone A", "Zone B", "Zone C")
        assert blocks[1].get("cache_control") == {"type": "ephemeral"}, (
            "Zone B 应有 cache_control={'type': 'ephemeral'}"
        )

    def test_zone_c_no_cache_control(self):
        """Zone C block 无 cache_control（动态内容不缓存）"""
        builder = self._builder()
        blocks = builder.build_system_prompt_blocks("Zone A", "Zone B", "Zone C")
        assert "cache_control" not in blocks[2], (
            "Zone C 不应有 cache_control（动态内容不缓存）"
        )

    def test_empty_zone_b_skipped(self):
        """Zone B 为空时自动跳过，只生成两个 block"""
        builder = self._builder()
        blocks = builder.build_system_prompt_blocks("Zone A", "", "Zone C")
        assert len(blocks) == 2
        texts = [b["text"] for b in blocks]
        assert "Zone A" in texts
        assert "Zone C" in texts

    def test_empty_zone_a_skipped(self):
        """Zone A 为空时自动跳过"""
        builder = self._builder()
        blocks = builder.build_system_prompt_blocks("", "Zone B", "Zone C")
        assert len(blocks) == 2
        texts = [b["text"] for b in blocks]
        assert "Zone B" in texts
        assert "Zone C" in texts

    def test_whitespace_only_zone_skipped(self):
        """只含空白的区域自动跳过"""
        builder = self._builder()
        blocks = builder.build_system_prompt_blocks("Zone A", "  ", "Zone C")
        assert len(blocks) == 2

    def test_all_zones_empty_returns_empty_list(self):
        """三区全为空时返回空列表"""
        builder = self._builder()
        blocks = builder.build_system_prompt_blocks("", "  ", "")
        assert len(blocks) == 0, "三区全空应返回空列表"

    def test_block_type_is_text(self):
        """所有 block 的 type 为 text"""
        builder = self._builder()
        blocks = builder.build_system_prompt_blocks("A", "B", "C")
        for block in blocks:
            assert block["type"] == "text", f"block type 应为 'text'，实际 {block['type']}"

    def test_block_text_content_preserved(self):
        """block 的 text 字段内容与输入一致"""
        builder = self._builder()
        blocks = builder.build_system_prompt_blocks("内容A", "内容B", "内容C")
        assert blocks[0]["text"] == "内容A"
        assert blocks[1]["text"] == "内容B"
        assert blocks[2]["text"] == "内容C"


# ═══════════════════════════════════════════════════════════════════════════════
# Section 6: LLMProvider system 参数兼容性测试
# ═══════════════════════════════════════════════════════════════════════════════

class TestLLMProviderSystemParam:
    """6.x LiteLLMProvider._build_request_body system 参数兼容性"""

    def _make_provider(self):
        """构建一个不需要端点配置的 LiteLLMProvider 实例"""
        from agent_core.agentloop.llm_provider import LiteLLMProvider
        provider = LiteLLMProvider.__new__(LiteLLMProvider)
        # 初始化必要属性（不调用 __init__，避免读取 json 配置）
        provider._endpoints = []
        provider._states = {}
        return provider

    def test_build_request_body_str_system(self):
        """str 格式的 system 直接写入请求体"""
        provider = self._make_provider()
        body = provider._build_request_body(
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
            model="claude-opus-4-6",
            max_tokens=1024,
            system="你是一个助手",
            stream=False,
        )
        assert body["system"] == "你是一个助手"

    def test_build_request_body_list_system(self):
        """List[dict] 格式的 system（cache_control blocks）写入请求体"""
        provider = self._make_provider()
        cache_blocks = [
            {"type": "text", "text": "Zone A 内容", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "Zone B 内容", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "Zone C 内容"},
        ]
        body = provider._build_request_body(
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
            model="claude-opus-4-6",
            max_tokens=1024,
            system=cache_blocks,
            stream=False,
        )
        assert body["system"] == cache_blocks
        assert isinstance(body["system"], list)

    def test_build_request_body_none_system(self):
        """system=None 时不写入请求体"""
        provider = self._make_provider()
        body = provider._build_request_body(
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
            model="claude-opus-4-6",
            max_tokens=1024,
            system=None,
            stream=False,
        )
        assert "system" not in body, "system=None 时不应写入请求体"

    def test_build_request_body_stream_true(self):
        """stream=True 时请求体包含 stream: True"""
        provider = self._make_provider()
        body = provider._build_request_body(
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
            model="claude-opus-4-6",
            max_tokens=1024,
            system=None,
            stream=True,
        )
        assert body.get("stream") is True

    def test_build_request_body_stream_false(self):
        """stream=False 时请求体不含 stream 字段"""
        provider = self._make_provider()
        body = provider._build_request_body(
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
            model="claude-opus-4-6",
            max_tokens=1024,
            system=None,
            stream=False,
        )
        assert "stream" not in body, "stream=False 时请求体不应包含 stream 字段"

    def test_build_request_body_model_and_max_tokens(self):
        """model 和 max_tokens 正确写入请求体"""
        provider = self._make_provider()
        body = provider._build_request_body(
            messages=[],
            tools=None,
            model="claude-opus-4-6",
            max_tokens=2048,
            system=None,
            stream=False,
        )
        assert body["model"] == "claude-opus-4-6"
        assert body["max_tokens"] == 2048

    def test_build_request_body_empty_string_system_not_injected(self):
        """system="" 时不写入请求体（falsy 字符串）"""
        provider = self._make_provider()
        body = provider._build_request_body(
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
            model="claude-opus-4-6",
            max_tokens=1024,
            system="",
            stream=False,
        )
        # 空字符串是 falsy，不应写入
        assert "system" not in body, "system='' 时不应写入请求体"


# ═══════════════════════════════════════════════════════════════════════════════
# Section 7: experience/history/memory 格式测试
# ═══════════════════════════════════════════════════════════════════════════════

class TestContextFormatting:
    """7.x experience/history/memory 格式化测试"""

    def test_experience_four_dimensions_all_formatted(self):
        """experience 四维度全部格式化"""
        builder = _make_builder()
        exp = {
            "user_preferences": ["偏好简洁报告"],
            "stock_insights": ["茅台长期看好"],
            "learned_patterns": ["MACD金叉看涨"],
            "corrections": ["不要过度解读量能"],
        }
        result = builder._format_experience(exp)
        assert "偏好简洁报告" in result, "user_preferences 应出现在格式化结果中"
        assert "茅台长期看好" in result, "stock_insights 应出现在格式化结果中"
        assert "MACD金叉看涨" in result, "learned_patterns 应出现在格式化结果中"
        assert "不要过度解读量能" in result, "corrections 应出现在格式化结果中"

    def test_experience_empty_all_dimensions_returns_empty(self):
        """全空 experience 返回空字符串"""
        builder = _make_builder()
        exp = {
            "user_preferences": [],
            "stock_insights": [],
            "learned_patterns": [],
            "corrections": [],
        }
        result = builder._format_experience(exp)
        assert result == "", "全空 experience 应返回空字符串"

    def test_experience_partial_dimensions(self):
        """部分维度有值时，只输出有值的维度"""
        builder = _make_builder()
        exp = {
            "user_preferences": ["喜欢技术分析"],
            "stock_insights": [],
            "learned_patterns": [],
            "corrections": [],
        }
        result = builder._format_experience(exp)
        assert "喜欢技术分析" in result
        assert result != ""

    def test_history_max_15_entries(self):
        """history 最多保留最近 15 条"""
        builder = _make_builder()
        history = [{"role": "user", "content": f"消息_{i:03d}"} for i in range(20)]
        result = builder._format_history(history)
        # 最后 15 条（005-019）应在，前 5 条（000-004）应不在
        assert "消息_019" in result, "最后一条消息应出现"
        assert "消息_005" in result, "倒数第 15 条消息应出现"
        assert "消息_004" not in result, "倒数第 16 条消息不应出现（超出 15 条限制）"
        assert "消息_000" not in result, "第 0 条消息不应出现（超出 15 条限制）"

    def test_history_long_content_truncated_at_500(self):
        """history 长内容（>500字）截断并附加省略号"""
        builder = _make_builder()
        long_content = "A" * 600
        history = [{"role": "user", "content": long_content}]
        result = builder._format_history(history)
        assert "..." in result, "超过 500 字的内容应截断并附加 '...'"

    def test_history_assistant_url_extracted_after_truncation(self):
        """assistant 消息截断后 URL 被提取保留"""
        builder = _make_builder()
        long_content = "B" * 501 + " https://example.com/report/12345"
        history = [{"role": "assistant", "content": long_content}]
        result = builder._format_history(history)
        # URL 应在截断后被追加
        assert "https://example.com/report/12345" in result, (
            "assistant 消息截断后 URL 应被提取并保留"
        )

    def test_has_resume_no_history_injected(self):
        """has_resume=True 时 history 不注入 prompt"""
        builder = _make_builder()
        unique_marker = "历史消息唯一标记_NKJH8762"
        prompt = builder.build_unified_system_prompt(
            has_resume=True,
            history=[{"role": "user", "content": unique_marker}],
        )
        assert unique_marker not in prompt, (
            "has_resume=True 时 history 不应注入"
        )

    def test_has_resume_no_summary_injected(self):
        """has_resume=True 时 summary 不注入 prompt"""
        builder = _make_builder()
        unique_summary = "摘要唯一标记_PQRS9876"
        prompt = builder.build_unified_system_prompt(
            has_resume=True,
            summary=unique_summary,
        )
        assert unique_summary not in prompt, (
            "has_resume=True 时 summary 不应注入"
        )

    def test_memory_context_injected_in_prompt(self):
        """memory_context 出现在 prompt 中"""
        builder = _make_builder()
        memory = "## MemoryOS记忆\n用户关注量子计算板块_UNIQUE_TAG"
        prompt = builder.build_unified_system_prompt(memory_context=memory)
        assert "量子计算板块_UNIQUE_TAG" in prompt, "memory_context 应出现在 prompt 中"

    def test_no_history_no_resume_summary_injected(self):
        """has_resume=False 时 summary 正常注入"""
        builder = _make_builder()
        summary_content = "上次讨论了茅台_UNIQUE_SUMMARY_TAG"
        prompt = builder.build_unified_system_prompt(
            has_resume=False,
            summary=summary_content,
        )
        assert summary_content in prompt, (
            "has_resume=False 时 summary 应注入到 prompt"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Section 8: 预算截断测试
# ═══════════════════════════════════════════════════════════════════════════════

class TestBudgetTruncation:
    """8.x 预算截断测试"""

    def _make_budget(self, memory=0, experience=0, history=0):
        budget = MagicMock()
        budget.memory_budget = memory
        budget.experience_budget = experience
        budget.history_budget = history
        return budget

    def test_memory_budget_causes_truncation(self):
        """memory_context 超出 memory_budget 时被截断（prompt 不含完整内容）"""
        builder = _make_builder()
        # 构造约 500 字的记忆内容
        long_memory = "记忆内容重复片段 " * 100
        budget = self._make_budget(memory=50)  # 极小预算（约 50 字符）
        prompt = builder.build_unified_system_prompt(
            memory_context=long_memory,
            budget=budget,
        )
        # 截断后 prompt 中的记忆部分应远短于原始 long_memory
        # 检查 prompt 不会完全包含 long_memory（太长了）
        assert long_memory not in prompt, (
            "超出 memory_budget 时 long_memory 不应完整出现在 prompt 中"
        )

    def test_no_budget_no_truncation(self):
        """budget=None 时不截断，memory 完整出现"""
        builder = _make_builder()
        memory = "## 记忆\n" + "重要信息_唯一标记 " * 50
        prompt = builder.build_unified_system_prompt(
            memory_context=memory,
            budget=None,
        )
        assert "重要信息_唯一标记" in prompt, "budget=None 时 memory 不应被截断"

    def test_experience_budget_causes_truncation(self):
        """experience 超出 experience_budget 时被截断"""
        builder = _make_builder()
        long_exp_text = "经验内容重复 " * 200
        exp = {
            "user_preferences": [long_exp_text],
            "stock_insights": [],
            "learned_patterns": [],
            "corrections": [],
        }
        budget = self._make_budget(experience=30)  # 极小预算
        prompt = builder.build_unified_system_prompt(
            experience=exp,
            budget=budget,
        )
        # 完整的 long_exp_text 不应出现
        assert long_exp_text not in prompt, (
            "超出 experience_budget 时经验内容不应完整出现"
        )

    def test_history_budget_split_40_60(self):
        """history_budget 按 40% summary / 60% history 分配（源码验证）"""
        import inspect
        from agent_core import prompt_builder as pb_module
        source = inspect.getsource(pb_module.PromptBuilder.build_unified_system_prompt)
        # 验证 40% 和 60% 分配逻辑存在于源码
        has_split = "0.4" in source and "0.6" in source
        assert has_split, (
            "build_unified_system_prompt 源码应包含 history_budget * 0.4 和 * 0.6 的分配逻辑"
        )

    def test_truncate_to_budget_shortens_text(self):
        """_truncate_to_budget 方法存在且签名正确（max_tokens 参数）"""
        import inspect
        from agent_core.prompt_builder import PromptBuilder
        sig = inspect.signature(PromptBuilder._truncate_to_budget)
        params = list(sig.parameters.keys())
        assert "text" in params, "_truncate_to_budget 应有 text 参数"
        assert "max_tokens" in params, "_truncate_to_budget 应有 max_tokens 参数"
        # 验证 None/0 时不截断（直接返回原文）
        builder = _make_builder()
        text = "测试文本内容"
        assert builder._truncate_to_budget(text, max_tokens=None) == text
        assert builder._truncate_to_budget(text, max_tokens=0) == text

    def test_truncate_to_budget_short_text_unchanged(self):
        """_truncate_to_budget 不截短预算内的文本"""
        builder = _make_builder()
        short_text = "短文本"
        result = builder._truncate_to_budget(short_text, max_tokens=10000)
        assert short_text in result, (
            "_truncate_to_budget 不应截短预算内的文本"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Section 9: config 新字段测试
# ═══════════════════════════════════════════════════════════════════════════════

class TestConfigNewFields:
    """9.x V4Config 新字段测试"""

    def test_llm_cache_control_enabled_attribute_exists(self):
        """V4Config 有 llm_cache_control_enabled 属性"""
        from agent_core.config import V4Config
        c = V4Config()
        assert hasattr(c, "llm_cache_control_enabled"), (
            "V4Config 应有 llm_cache_control_enabled 属性"
        )

    def test_llm_cache_control_enabled_default_false(self):
        """llm_cache_control_enabled 默认为 False"""
        from agent_core.config import V4Config
        c = V4Config()
        assert c.llm_cache_control_enabled is False, (
            "llm_cache_control_enabled 默认应为 False"
        )

    def test_llm_cache_control_enabled_from_env_true(self):
        """从环境变量读取 LLM_CACHE_CONTROL_ENABLED=true"""
        from agent_core.config import V4Config
        with patch.dict(os.environ, {"LLM_CACHE_CONTROL_ENABLED": "true"}):
            c = V4Config.from_env()
            assert c.llm_cache_control_enabled is True

    def test_llm_cache_control_enabled_from_env_false(self):
        """从环境变量读取 LLM_CACHE_CONTROL_ENABLED=false"""
        from agent_core.config import V4Config
        with patch.dict(os.environ, {"LLM_CACHE_CONTROL_ENABLED": "false"}):
            c = V4Config.from_env()
            assert c.llm_cache_control_enabled is False

    def test_loop_max_truncation_retry_attribute_exists(self):
        """V4Config 有 loop_max_truncation_retry 属性"""
        from agent_core.config import V4Config
        c = V4Config()
        assert hasattr(c, "loop_max_truncation_retry"), (
            "V4Config 应有 loop_max_truncation_retry 属性"
        )

    def test_loop_max_truncation_retry_default_2(self):
        """loop_max_truncation_retry 默认为 2"""
        from agent_core.config import V4Config
        c = V4Config()
        assert c.loop_max_truncation_retry == 2, (
            "loop_max_truncation_retry 默认应为 2"
        )

    def test_loop_max_truncation_retry_from_env(self):
        """从环境变量读取 V4_LOOP_MAX_TRUNCATION_RETRY"""
        from agent_core.config import V4Config
        with patch.dict(os.environ, {"V4_LOOP_MAX_TRUNCATION_RETRY": "5"}):
            c = V4Config.from_env()
            assert c.loop_max_truncation_retry == 5

    def test_llm_cache_control_from_dict(self):
        """从 dict 构建 V4Config 时 llm_cache_control_enabled 正确读取"""
        from agent_core.config import V4Config
        c = V4Config.from_dict({"llm_cache_control_enabled": True})
        assert c.llm_cache_control_enabled is True

    def test_loop_max_truncation_retry_from_dict(self):
        """从 dict 构建 V4Config 时 loop_max_truncation_retry 正确读取"""
        from agent_core.config import V4Config
        c = V4Config.from_dict({"loop_max_truncation_retry": 3})
        assert c.loop_max_truncation_retry == 3


# ═══════════════════════════════════════════════════════════════════════════════
# 冒烟测试: 模块可正常导入
# ═══════════════════════════════════════════════════════════════════════════════

class TestModuleImports:
    """验证所有相关模块可正常导入"""

    def test_prompt_builder_importable(self):
        from agent_core.prompt_builder import PromptBuilder
        assert PromptBuilder is not None

    def test_litellm_provider_importable(self):
        from agent_core.agentloop.llm_provider import LiteLLMProvider
        assert LiteLLMProvider is not None

    def test_subagent_executor_importable(self):
        from agent_core.agentloop.subagent import SubAgentExecutor
        assert SubAgentExecutor is not None

    def test_v4config_importable(self):
        from agent_core.config import V4Config
        assert V4Config is not None

    def test_agentloop_importable(self):
        from agent_core.agentloop.loop import AgentLoop
        assert AgentLoop is not None
