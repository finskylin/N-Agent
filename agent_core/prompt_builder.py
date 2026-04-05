"""
Prompt Builder -- V4 System Prompt 构建器

职责:
1. 加载基础 system prompt (agent_core/prompts/v4_unified_system.md)
2. 注入技能说明（从 SkillDiscovery 动态获取 SKILL.md 内容）
3. 注入对话历史和摘要
4. 注入经验知识（用户偏好、洞察、规律、纠正记录）
5. 控制输出格式（markdown / mermaid）
6. 控制渲染模式（auto / text_only）

所有提示词模板均从 agent_core/prompts/ 目录加载，不再硬编码。

V4 解耦: 不导入 skills.registry / app.agent.resource_manager。
迁移: 从 app/agent/v4/prompt_builder.py 迁移至 agent_core/prompt_builder.py。
"""
from typing import Dict, Any, List, Optional, Union, TYPE_CHECKING
from datetime import datetime
from pathlib import Path
from loguru import logger

from agent_core.skill_discovery import SkillDiscovery

if TYPE_CHECKING:
    from agent_core.session.context_window_guard import ContextBudget


def _load(name: str, **kwargs) -> str:
    """从 app/prompts/ 加载提示词，加载失败返回空字符串"""
    try:
        from agent_core.prompts.loader import load_prompt
        return load_prompt(name, **kwargs)
    except Exception as e:
        logger.debug(f"[PromptBuilder] loader unavailable, fallback: {e}")
        # fallback: 直接读文件
        try:
            from pathlib import Path
            p = Path(__file__).parent / "prompts" / f"{name}.md"
            if p.exists():
                text = p.read_text(encoding="utf-8")
                if kwargs:
                    try:
                        return text.format(**kwargs)
                    except KeyError:
                        return text
                return text
        except Exception:
            pass
    return ""


class PromptBuilder:
    """
    V4 System Prompt 构建器

    数据来源:
    - agent_core/prompts/v4_unified_system.md      -- 基础指令
    - agent_core/prompts/v4_output_markdown.md     -- Markdown 输出格式
    - agent_core/prompts/v4_output_mermaid.md      -- Mermaid 输出格式
    - agent_core/prompts/v4_text_only.md           -- 纯文本模式指令
    - agent_core/prompts/v4_experience_behavior.md -- 经验行为指引
    - agent_core/prompts/v4_experience_sections.md -- 经验段落模板
    - agent_core/prompts/v4_skill_mention.md       -- @skill 提示模板
    - SkillDiscovery                           -- 技能说明（从 SKILL.md）

    知识库摘要通过 set_knowledge_provider() 由 app 层注入，不直接 import app 代码。
    """

    # app 层注入的知识库摘要获取函数：() -> Dict[str, str]
    _knowledge_provider: Optional[Any] = None

    @classmethod
    def set_knowledge_provider(cls, provider) -> None:
        """
        注入知识库目录摘要获取函数，由 app/main.py 启动时调用。
        provider 签名：() -> Dict[str, str]，返回 {category: summary_text}
        """
        cls._knowledge_provider = provider
        logger.info("[PromptBuilder] knowledge_provider injected")

    def __init__(self, discovery: SkillDiscovery, prompt_budget_guard=None):
        self._discovery = discovery
        self._prompt_budget_guard = prompt_budget_guard  # PromptBudgetGuard 实例（可选）
        self._base_prompt: Optional[str] = None

    def build(
        self,
        history: List[Dict[str, str]] = None,
        summary: Optional[str] = None,
        experience: Optional[Dict[str, List[str]]] = None,
        ts_code: Optional[str] = None,
        params: Dict[str, Any] = None,
        skill_exec_times: Dict[str, float] = None,
        output_format: Optional[str] = "markdown",
        render_mode: str = "auto",
        has_resume: bool = False,
        budget: Optional["ContextBudget"] = None,
        memory_context: str = "",
        report_lang: str = "zh",
    ) -> str:
        """
        构建完整的 system prompt

        Args:
            has_resume: 是否有 SDK session resume。
                        为 True 时跳过 history/summary 注入（SDK 已自动恢复完整对话），
                        仅注入 ExperienceStore（结构化经验是 SDK 不具备的长期记忆）。
            skill_exec_times: 技能实际平均耗时（秒），从 Redis 预取。
            memory_context: MemoryOS 记忆上下文（始终注入，不受 has_resume 影响）
            report_lang: 报告语种（zh|en|auto）
        """
        parts = []

        # 0. 语种指令（最高优先级 — 放在整个 prompt 的第一段）
        if report_lang == "en":
            parts.append(
                "# ⚠️ CRITICAL INSTRUCTION: ENGLISH ONLY\n"
                "**The user asked in English. You MUST write your ENTIRE response in English.**\n"
                "This instruction has the HIGHEST priority and overrides ALL other language rules below.\n"
            )

        # 1. 基础指令（身份 + 核心原则）
        parts.append(self._get_base_prompt(report_lang=report_lang))

        # 2. 技能说明 — 由 Claude Agent SDK 自动从 .claude/skills/ 加载，不再手动注入
        #    SDK 在 preset="claude_code" 模式下会自动扫描 SKILL.md 并注入 tool schema

        # 2.5 知识库上下文（从配置文件动态加载）
        knowledge_section = self._get_knowledge_context()
        if knowledge_section:
            parts.append(knowledge_section)

        # 2.6 记忆上下文（始终注入，不受 has_resume 影响）
        if memory_context:
            if budget and budget.memory_budget > 0:
                memory_context = self._truncate_to_budget(
                    memory_context, budget.memory_budget
                )
            parts.append(memory_context)

        # 3. 经验注入（始终注入，不受 resume 影响 — 结构化经验是 SDK 不具备的）
        if experience:
            experience_section = self._format_experience(experience)
            if experience_section:
                # Token 预算感知截断
                if budget and budget.experience_budget > 0:
                    experience_section = self._truncate_to_budget(
                        experience_section, budget.experience_budget
                    )
                parts.append(experience_section)

        # 4. 对话摘要（仅在无 resume 时注入 — SDK resume 已自动恢复完整对话）
        if not has_resume and summary:
            summary_text = f"\n## 之前对话总结\n{summary}"
            if budget and budget.history_budget > 0:
                summary_text = self._truncate_to_budget(
                    summary_text, int(budget.history_budget * 0.4)
                )
            parts.append(summary_text)

        # 5. 最近对话历史（仅在无 resume 时注入 — SDK resume 已包含完整历史含工具调用）
        if not has_resume and history:
            history_section = self._format_history(history)
            if history_section:
                if budget and budget.history_budget > 0:
                    history_section = self._truncate_to_budget(
                        history_section, int(budget.history_budget * 0.6)
                    )
                parts.append(f"\n## 最近对话记录\n{history_section}")

        # 6. 上下文信息
        if ts_code:
            parts.append(f"\n## 当前关注股票: {ts_code}")

        # 6.5 钉钉渠道上下文
        self._inject_dingtalk_context(parts, params)

        # 7. 输出格式 + 渲染模式控制
        format_section = self._format_output_instructions(output_format, render_mode)
        if format_section:
            parts.append(format_section)

        # 7.5 语种指令尾部封口
        if report_lang == "en":
            parts.append(
                "\n# ⚠️ FINAL REMINDER: ENGLISH ONLY\n"
                "Everything you write MUST be in English. Do NOT use Chinese."
            )

        # 8. 行为指引（仅在有经验时注入）
        if experience and any(experience.values()):
            behavior = _load("v4_experience_behavior")
            if behavior:
                parts.append(f"\n{behavior}")

        return "\n".join(parts)

    def build_system_prompt_blocks(
        self,
        zone_a: str,
        zone_b: str,
        zone_c: str,
    ) -> List[dict]:
        """
        将三区 system prompt 构建为带 cache_control 的 blocks 列表。

        Anthropic cache_control 格式：
          [
            {"type": "text", "text": "<Zone A>", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "<Zone B>", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "<Zone C>"},
          ]

        Zone A 和 Zone B 设置断点，Zone C 不设置（动态内容不缓存）。
        空字符串的区域跳过（避免发送空 block）。
        """
        blocks = []
        if zone_a.strip():
            blocks.append({
                "type": "text",
                "text": zone_a,
                "cache_control": {"type": "ephemeral"},
            })
        if zone_b.strip():
            blocks.append({
                "type": "text",
                "text": zone_b,
                "cache_control": {"type": "ephemeral"},
            })
        if zone_c.strip():
            blocks.append({
                "type": "text",
                "text": zone_c,
            })
        return blocks

    @staticmethod
    def build_runtime_context_prefix(params: Dict[str, Any] = None) -> str:
        """
        构建运行时上下文元数据块，注入为用户消息前缀而非 system prompt。

        包含：
        - 当前日期时间（每次请求动态生成，不污染 system prompt prefix cache）
        - 钉钉会话上下文（仅钉钉渠道有值时）

        Returns:
            格式化的元数据字符串，空时返回 ""
        """
        lines = []

        # 当前时间
        now = datetime.now()
        weekday_names = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        weekday = weekday_names[now.weekday()]
        lines.append(
            f"[系统元数据 — 仅供参考，不需要在回答中重复]\n"
            f"- 当前日期: {now.strftime('%Y年%m月%d日')} {weekday}\n"
            f"- 当前时间: {now.strftime('%H:%M:%S')}\n"
            f"- 时区: Asia/Shanghai (北京时间)"
        )
        lines.append(
            "**重要**：你生成的所有报告、分析文档中的「报告日期」必须使用上述当前日期，禁止使用任何其他日期。"
        )

        # 钉钉上下文
        if params:
            _dt_keys = [
                "dingtalk_conversation_id",
                "dingtalk_conversation_type",
                "dingtalk_robot_code",
                "dingtalk_sender_id",
                "dingtalk_staff_id",
                "dingtalk_sender",
            ]
            dt_lines = [f"- {k}: {params[k]}" for k in _dt_keys if params.get(k)]
            conv_type = params.get("dingtalk_conversation_type", "")
            if conv_type == "2":
                dt_lines.append("- 当前聊天环境: 群聊（group chat）")
            elif conv_type == "1":
                dt_lines.append("- 当前聊天环境: 单聊（private chat）")
            if dt_lines:
                lines.append("钉钉会话上下文:\n" + "\n".join(dt_lines))
                logger.info(f"[PromptBuilder] Runtime prefix: DingTalk context injected")

        return "\n".join(lines) if lines else ""

    @staticmethod
    def _inject_dingtalk_context(parts: list, params: Dict[str, Any] = None):
        """
        [已迁移] 钉钉上下文现通过 build_runtime_context_prefix() 注入用户消息前缀。
        此方法保留仅作兼容占位，不再向 system prompt 注入任何内容。
        """
        pass

    def _get_base_prompt(self, report_lang: str = "zh") -> str:
        """获取基础 prompt（从配置文件加载，动态注入当前日期时间和语种规则）"""
        # 加载模板（缓存原始模板，每次替换动态变量）
        if not self._base_prompt:
            self._base_prompt = _load("v4_unified_system")
            if not self._base_prompt:
                # 最终兜底
                self._base_prompt = (
                    "你是一个强大的多领域智能分析助手。\n"
                    "你能够根据用户的问题，自动调配合适的工具和技能来提供专业、客观的分析结论。\n"
                )

        # 动态替换语种规则
        if report_lang == "en":
            lang_rule = "**English response**: You MUST write ALL responses in English. This overrides any other language instructions."
        elif report_lang == "auto":
            lang_rule = (
                "**语言自动适配**：根据用户消息的语言自动选择回复语言。"
                "用户用英文提问则全程英文回复，用中文提问则中文回复。"
                "股票/公司专有名词可保留原文。"
            )
        else:
            lang_rule = "**中文回复**：始终使用中文与用户交流"

        result = self._base_prompt.replace("{response_language_rule}", lang_rule)
        return result

    def _get_knowledge_context(self) -> str:
        """从 agent_core/config/knowledge/knowledge.json 读取配置，扫描知识库目录，注入提示词"""
        try:
            config_path = Path(__file__).parent / "config" / "knowledge" / "knowledge.json"
            if not config_path.exists():
                return ""

            import json
            config = json.loads(config_path.read_text(encoding="utf-8"))
            if not config.get("enabled", False):
                return ""

            base_dir = Path(__file__).parent.parent / config.get("base_dir", "app/knowledge")
            if not base_dir.exists():
                return ""

            # 扫描目录结构
            tree_lines = []
            rules = config.get("inject_rules", {})
            max_depth = rules.get("max_tree_depth", 3)
            max_files = rules.get("max_files_shown", 50)
            extensions = set(rules.get("file_extensions", [".md", ".txt"]))

            file_count = 0
            for cat in config.get("categories", []):
                if not cat.get("auto_inject", True):
                    continue
                cat_path = base_dir / cat["path"]
                if not cat_path.exists():
                    continue
                tree_lines.append(f"- `app/knowledge/{cat['path']}/` — {cat['description']}")
                for item in sorted(cat_path.rglob("*")):
                    if file_count >= max_files:
                        tree_lines.append("  ... (更多文件)")
                        break
                    rel = item.relative_to(base_dir)
                    depth = len(rel.parts) - 1
                    if depth > max_depth:
                        continue
                    if item.is_file() and item.suffix in extensions:
                        indent = "  " * depth
                        tree_lines.append(f"  {indent}- `{rel}`")
                        file_count += 1

            if not tree_lines:
                return ""

            knowledge_tree = "\n".join(tree_lines)

            # 从注入的 provider 读取各目录摘要（app 层启动时通过 set_knowledge_provider 注入）
            knowledge_summaries = ""
            try:
                if PromptBuilder._knowledge_provider:
                    summaries = PromptBuilder._knowledge_provider()
                    if summaries:
                        knowledge_summaries = "\n\n".join(
                            f"#### {cat}\n{text}" for cat, text in summaries.items() if text
                        )
            except Exception as e:
                logger.debug(f"[PromptBuilder] Knowledge summaries load failed: {e}")

            return _load(
                "v4_knowledge_context",
                knowledge_tree=knowledge_tree,
                knowledge_summaries=knowledge_summaries or "（知识库暂无内容摘要）",
            )
        except Exception as e:
            logger.debug(f"[PromptBuilder] Knowledge context injection failed: {e}")
            return ""

    @staticmethod
    def _get_current_datetime_section() -> str:
        """生成当前日期时间段落，每次构建 prompt 时动态获取"""
        now = datetime.now()
        weekday_names = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        weekday = weekday_names[now.weekday()]
        return (
            f"\n## 当前时间\n"
            f"- 当前日期: {now.strftime('%Y年%m月%d日')} {weekday}\n"
            f"- 当前时间: {now.strftime('%H:%M:%S')}\n"
            f"- 时区: Asia/Shanghai (北京时间)\n"
        )

    def _get_skills_instructions(
        self,
        user_id: str = None,
        skill_exec_times: Dict[str, float] = None,
        phase0_intents: Optional[List[str]] = None,
        phase0_topics: Optional[List[str]] = None,
    ) -> str:
        """
        从 SkillDiscovery 获取所有技能的说明（含分类、适用场景、可调用标记）

        注意：append 模式下 SDK 会自动注入 MCP Tool Schema（name+description+input_schema），
        此处的 description 与 SDK 注入的内容基本一致，但包含 SDK 不提供的差异化信息：
        - display_name（中文名）
        - category、intents（分类、适用场景）
        - 真实耗时（从 Redis 预取替换静态描述）
        - [可调用]/[仅参考知识]/[动态技能] 标记
        因此仍需完整注入。

        Args:
            user_id: 用户 ID，用于过滤动态技能
            skill_exec_times: 技能实际平均耗时（秒），从 Redis 预取。
                              有值时替换 SKILL.md 中的静态【耗时】描述。
            phase0_intents: Phase 0 推荐的工具名列表（用于预算保护）
            phase0_topics: Phase 0 识别的主题列表（用于预算保护）

        Returns:
            技能说明文本
        """
        import re as _re
        instructions = []

        all_skills = self._discovery.get_all(user_id=user_id)

        # 预算控制: 如果有 budget_guard，先筛选
        if self._prompt_budget_guard and all_skills:
            allocation = self._prompt_budget_guard.select_skills_for_prompt(
                all_skills, phase0_intents, phase0_topics
            )
            selected_skills = allocation.selected_skills
        else:
            selected_skills = all_skills

        for meta in selected_skills:
            parts = [f"- **{meta.display_name or meta.name}** (`{meta.name}`)"]

            desc = meta.description or ""

            # 预算截断: 单 Skill 描述超限时截断
            if self._prompt_budget_guard:
                desc = self._prompt_budget_guard.truncate_skill_description(meta)

            # 如果有真实耗时数据，替换描述中的静态【耗时】
            if skill_exec_times and meta.name in skill_exec_times:
                avg_sec = skill_exec_times[meta.name]
                if avg_sec < 60:
                    time_str = f"~{avg_sec:.0f}秒"
                else:
                    time_str = f"~{avg_sec / 60:.1f}分钟"
                real_time_tag = f"【耗时】{time_str}（基于历史平均）"
                # 替换已有的【耗时】标签
                if "【耗时】" in desc:
                    desc = _re.sub(r"【耗时】[^\n【]*", real_time_tag, desc)
                else:
                    desc = desc.rstrip() + f"\n{real_time_tag}"

            if desc:
                parts.append(f"  {desc}")

            intents = getattr(meta, "intents", None)
            if intents:
                parts.append(f"  适用场景: {', '.join(intents[:5])}")

            if meta.has_script:
                parts.append("  [可调用]")
            else:
                parts.append("  [仅参考知识]")

            # 动态技能标记
            if getattr(meta, "is_dynamic", False):
                parts.append("  [动态技能]")

            instructions.append("\n".join(parts))

        return "\n\n".join(instructions) if instructions else ""

    def _format_history(self, history: List[Dict[str, str]]) -> str:
        """格式化对话历史（最近15条，含时间戳）"""
        from datetime import datetime
        lines = []
        for h in history[-15:]:
            role = h.get("role", "")
            content = h.get("content", "")
            if role in ("user", "assistant", "ai") and content:
                role_label = "Assistant" if role in ("ai", "assistant") else "User"
                if len(content) <= 500:
                    display_content = content
                else:
                    display_content = content[:500] + "..."
                    # assistant 消息截断后，额外附加原文中的 URL（报告链接可能在末尾）
                    if role in ("ai", "assistant"):
                        import re as _re
                        urls = _re.findall(r'https?://\S+', content)
                        if urls:
                            display_content += "\n[链接] " + " | ".join(urls[:5])
                # 附加时间戳（如果有），帮助 LLM 建立时序感知
                ts = h.get("created_at")
                if ts:
                    try:
                        time_str = datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M")
                        lines.append(f"[{time_str}] {role_label}: {display_content}")
                    except (ValueError, OSError):
                        lines.append(f"{role_label}: {display_content}")
                else:
                    lines.append(f"{role_label}: {display_content}")
        return "\n".join(lines)

    def _format_experience(self, experience: dict) -> str:
        """
        格式化经验知识为 system prompt 段落

        兼容新格式（dict with text/score）和旧格式（纯字符串）。
        """
        def _extract_text(item) -> str:
            """从经验条目中提取文本，兼容新旧格式"""
            if isinstance(item, dict):
                return item.get("text", "")
            return str(item) if item else ""

        def _bullet_list(items: list) -> str:
            texts = [_extract_text(item) for item in items]
            texts = [t for t in texts if t.strip()]
            return "\n".join(f"- {t}" for t in texts) if texts else ""

        user_prefs = _bullet_list(experience.get("user_preferences", []))
        insights = _bullet_list(experience.get("stock_insights", []))
        patterns = _bullet_list(experience.get("learned_patterns", []))
        corrections = _bullet_list(experience.get("corrections", []))

        if not any([user_prefs, insights, patterns, corrections]):
            return ""

        # 尝试从配置文件加载模板
        template = _load("v4_experience_sections")
        if template:
            try:
                return "\n" + template.format(
                    user_preferences=user_prefs or "(无)",
                    stock_insights=insights or "(无)",
                    learned_patterns=patterns or "(无)",
                    corrections=corrections or "(无)",
                )
            except KeyError:
                pass

        # fallback: 直接拼接
        sections = ["\n## 用户画像和历史经验"]
        if user_prefs:
            sections.append(f"### 用户偏好\n{user_prefs}")
        if insights:
            sections.append(f"### 已有分析结论\n{insights}")
        if patterns:
            sections.append(f"### 用户认可的分析模式\n{patterns}")
        if corrections:
            sections.append(f"### 纠正记录（务必避免重复犯错）\n{corrections}")
        return "\n".join(sections)

    def build_clarity_check_split(
        self,
        query: str,
        history: List[Dict[str, str]] = None,
        include_tools: bool = False,
        existing_tabs: List[Dict] = None,
    ) -> tuple:
        """
        构建 Phase 0 清晰度评估 — system/user 分离版

        将 v4_clarity_check.md 模板拆分为:
        - system: 角色定义 + 规则 + 输出格式（不含动态上下文）
        - user:   对话历史 + 可用工具 + 当前用户消息

        这种分离让 LLM 在 system 中接收指令，在 user 中看到上下文和问题，
        避免巨大的指令模板稀释对话历史的注意力。

        Returns:
            (system_text, user_text) 二元组
        """
        # 构建历史、工具、Tab 等动态内容
        conversation_history, available_tools, existing_tabs_section = \
            self._build_clarity_dynamic_parts(query, history, include_tools, existing_tabs)

        from datetime import datetime
        current_datetime = datetime.now().strftime("%Y年%m月%d日 %H:%M (%A)")

        template = _load("v4_clarity_check")
        if not template:
            # fallback: 全部塞进 user
            return ("", self.build_clarity_check_prompt(
                query, history, include_tools, existing_tabs,
            ))

        # --- 拆分模板 ---
        # 模板结构:
        #   角色定义 + 核心原则               → system
        #   {conversation_history}              → user
        #   ## 当前用户消息\n{user_query}       → user
        #   ## 任务 ... (规则/格式)             → system

        # 用标记切分: 以 "## 任务" 为界（兼容 "## 你的任务"）
        marker = "## 任务"
        marker_pos = template.find(marker)
        if marker_pos < 0:
            marker = "## 你的任务"
            marker_pos = template.find(marker)
        if marker_pos < 0:
            return ("", self.build_clarity_check_prompt(
                query, history, include_tools, existing_tabs,
            ))

        # system = 角色定义 + 规则部分(marker 之后到末尾)
        ch_marker = "{conversation_history}"
        ch_pos = template.find(ch_marker)
        if ch_pos > 0:
            role_part = template[:ch_pos].rstrip()
        else:
            role_part = ""
        rules_part = template[marker_pos:]

        system_text = role_part + "\n\n" + rules_part
        try:
            system_text = system_text.format(
                current_datetime=current_datetime,
                # 提供空默认值防 KeyError
                conversation_history="",
                available_tools="",
                existing_tabs_section="",
                user_query="",
            )
        except KeyError:
            pass

        # user = 对话历史 + 工具列表 + 当前问题
        user_parts = []
        if conversation_history:
            user_parts.append(conversation_history)
        if available_tools:
            user_parts.append(available_tools)
        user_parts.append(f"## 当前用户消息\n\n{query}")
        user_text = "\n\n".join(user_parts)

        return (system_text, user_text)

    def _build_clarity_dynamic_parts(
        self,
        query: str,
        history: List[Dict[str, str]] = None,
        include_tools: bool = True,
        existing_tabs: List[Dict] = None,
    ) -> tuple:
        """构建 Phase 0 的动态部分: conversation_history, available_tools, existing_tabs_section"""
        # 构建对话历史文本（由近到远排列）
        conversation_history = ""
        if history and len(history) > 0:
            rounds = []
            current_user = None
            for h in history:
                role = h.get("role", "")
                content = h.get("content", "")
                if not content:
                    continue
                if role == "user":
                    if current_user is not None:
                        rounds.append({"user": current_user, "assistant": ""})
                    current_user = content
                elif role in ("assistant", "ai"):
                    if current_user is not None:
                        rounds.append({"user": current_user, "assistant": content})
                        current_user = None
                    elif rounds:
                        rounds[-1]["assistant"] = content
            if current_user is not None:
                rounds.append({"user": current_user, "assistant": ""})

            recent_rounds = list(reversed(rounds[-5:]))
            if recent_rounds:
                history_lines = []
                for i, rnd in enumerate(recent_rounds):
                    label = f"[最近第{i+1}轮]"
                    user_display = rnd["user"] if len(rnd["user"]) <= 200 else rnd["user"][:200] + "..."
                    asst_display = rnd["assistant"] if len(rnd["assistant"]) <= 300 else rnd["assistant"][:300] + "..."
                    history_lines.append(f"{label} 用户: {user_display}")
                    if asst_display:
                        history_lines.append(f"{label} 助手: {asst_display}")
                conversation_history = "## 最近对话历史（由近到远）\n\n" + "\n\n".join(history_lines)

        # 工具列表
        available_tools = ""
        if include_tools:
            tools_section = self._get_skills_brief_list()
            if tools_section:
                available_tools = f"## 可用工具列表\n\n{tools_section}"

        # Tab 信息
        existing_tabs_section = ""
        if existing_tabs:
            tab_lines = ["已有场景 Tab:"]
            for t in existing_tabs:
                tab_lines.append(
                    f'  - {t["tab_id"]}: {t.get("scene_type", "default")} 场景, '
                    f'标题 "{t.get("title", "")}"'
                )
            tab_lines.append("")
            tab_lines.append("判断新问题是否属于某个已有 Tab 的主题/区域：")
            tab_lines.append("- 同一地理区域 → reuse_tab_id = 该 Tab ID")
            tab_lines.append("- 完全不同主题 → reuse_tab_id = null")
            existing_tabs_section = "\n".join(tab_lines)

        return conversation_history, available_tools, existing_tabs_section

    def build_clarity_check_prompt(
        self,
        query: str,
        history: List[Dict[str, str]] = None,
        include_tools: bool = False,
        existing_tabs: List[Dict] = None,
    ) -> str:
        """
        构建 Phase 0 清晰度评估 prompt（单条消息版，兼容旧调用方）

        从 app/prompts/v4_clarity_check.md 加载模板，
        注入用户 query、对话历史和可用工具列表，由 LLM 动态判断意图清晰度。

        Args:
            query: 当前用户消息
            history: 对话历史列表，用于理解追问类请求的上下文
            include_tools: 是否注入可用工具列表（帮助 LLM 匹配工具）
        """
        conversation_history, available_tools, existing_tabs_section = \
            self._build_clarity_dynamic_parts(query, history, include_tools, existing_tabs)
        # 旧方法中 available_tools 带前缀换行
        if available_tools:
            available_tools = f"\n{available_tools}\n"

        template = _load("v4_clarity_check")
        if template:
            try:
                from datetime import datetime
                current_datetime = datetime.now().strftime("%Y年%m月%d日 %H:%M (%A)")

                # 在模板中注入工具列表
                full_template = template
                if available_tools and "{available_tools}" not in template:
                    # 模板没有工具占位符，在用户消息前插入
                    full_template = template.replace(
                        "## 当前用户消息",
                        f"{available_tools}\n## 当前用户消息"
                    )
                return full_template.format(
                    user_query=query,
                    conversation_history=conversation_history,
                    available_tools=available_tools,
                    current_datetime=current_datetime,
                    existing_tabs_section=existing_tabs_section,
                )
            except KeyError as e:
                logger.warning(f"[PromptBuilder] Clarity check prompt template error: {e}")

        # fallback: 内联兜底（包含历史和工具列表）
        fallback_history = f"{conversation_history}\n\n" if conversation_history else ""
        return (
            f"分析以下用户请求，判断是否足够清晰可以执行:\n\n"
            f"{fallback_history}"
            f"{available_tools}"
            f"当前用户请求: {query}\n\n"
            f"如果清晰且需要工具，输出 JSON: {{\"clarity\": \"clear\", \"proceed\": true, \"acknowledgment\": \"好的，我现在开始通过【工具名】帮你【任务】，预计需要【时间】。\", \"matched_tools\": [\"工具1\"]}}\n"
            f"如果模糊，输出 JSON: {{\"clarity\": \"ambiguous\", \"proceed\": false, \"response\": \"澄清问题\"}}"
        )

    def _get_skills_brief_list(self) -> str:
        """获取精简版技能列表（用于 Phase 0 工具匹配）"""
        skills = self._discovery.get_all()
        if not skills:
            return ""

        lines = []
        for skill in skills:
            # SkillMetadata 是 dataclass，使用属性访问而非 dict.get()
            name = getattr(skill, "name", "") if not isinstance(skill, dict) else skill.get("name", "")
            desc = getattr(skill, "description", "") if not isinstance(skill, dict) else skill.get("description", "")
            keywords = getattr(skill, "keywords", []) if not isinstance(skill, dict) else skill.get("keywords", [])

            if not name:
                continue

            # 精简描述
            short_desc = desc[:100] + "..." if len(desc) > 100 else desc

            # 关键词
            kw_str = ""
            if keywords:
                kw_str = f" [关键词: {', '.join(keywords[:5])}]"

            lines.append(f"- **{name}**: {short_desc}{kw_str}")

        return "\n".join(lines)

    def _get_skill_list_section(
        self,
        user_id: str = None,
        phase0_intents: Optional[List[str]] = None,
        phase0_topics: Optional[List[str]] = None,
    ) -> str:
        """
        生成注入系统提示词的 skill 列表段落。
        从 SKILL.md 动态读取 name + description，通过 v4_skill_list.md 模板渲染。
        """
        all_skills = self._discovery.get_all(user_id=user_id)
        if not all_skills:
            return ""

        # 预算筛选
        if self._prompt_budget_guard and (phase0_intents or phase0_topics):
            try:
                allocation = self._prompt_budget_guard.select_skills_for_prompt(
                    all_skills, phase0_intents, phase0_topics
                )
                selected = allocation.selected_skills
            except Exception:
                selected = all_skills
        else:
            selected = all_skills

        # 分离系统 Skill 和私有 Skill
        public_skills = [s for s in selected if s.owner in ("public", "", None)]
        private_skills = [s for s in selected if s.owner not in ("public", "", None)]

        lines = []
        for meta in public_skills:
            name = meta.name
            desc = (meta.description or "").split("\n")[0].strip()
            if not name:
                continue
            # 含 script_path（容器内绝对路径），供 LLM 用 bash 调用
            script_path = meta.script_paths[0] if meta.has_script and meta.script_paths else ""
            if script_path:
                # 将相对路径转为容器内绝对路径 /app/...
                import os as _os
                if not script_path.startswith("/"):
                    script_path = "/app/" + script_path
                lines.append(f"- **{name}** (`{script_path}`): {desc}")
            else:
                lines.append(f"- **{name}**: {desc}")

        if not lines:
            return ""

        skill_list_text = "\n".join(lines)
        result = _load("v4_skill_list", skill_list=skill_list_text)

        # 注入私有 Skill 区块（仅当用户有私有 Skill 时）
        if private_skills:
            private_lines = []
            for meta in private_skills:
                name = meta.name
                desc = (meta.description or "").split("\n")[0].strip()
                script_path = meta.script_paths[0] if meta.has_script and meta.script_paths else ""
                if name:
                    if script_path:
                        import os as _os
                        if not script_path.startswith("/"):
                            script_path = "/app/" + script_path
                        private_lines.append(f"- **{name}** (`{script_path}`): {desc}")
                    else:
                        private_lines.append(f"- **{name}**: {desc}")
            private_block = (
                "\n## 我的专属技能（私有）\n"
                "以下是您创建的专属技能，只有您可以使用，优先级高于同名系统技能：\n"
                + "\n".join(private_lines)
                + "\n触发方式：直接说「用我的 XX 技能」或按技能名称调用。\n"
            )
            result = result + private_block

        return result

    # 优化 8: Token 计数缓存 — 按文本 hash 缓存估算结果
    _token_count_cache: Dict[int, int] = {}
    _token_cache_max_size: int = 200

    @classmethod
    def _estimate_tokens_cached(cls, text: str) -> int:
        """带缓存的 token 估算（优化 8）"""
        from agent_core.session.context_window_guard import ContextWindowGuard

        # 用文本 hash + 长度作为缓存 key（避免对大文本做完整 hash）
        text_key = hash((text[:200], len(text)))
        cached = cls._token_count_cache.get(text_key)
        if cached is not None:
            return cached

        count = ContextWindowGuard.estimate_tokens(text)

        # 缓存容量限制
        if len(cls._token_count_cache) >= cls._token_cache_max_size:
            # 简单清空（因为 prompt 模板在进程周期内变化不大）
            cls._token_count_cache.clear()

        cls._token_count_cache[text_key] = count
        return count

    @staticmethod
    def _truncate_to_budget(text: str, max_tokens: Optional[int]) -> str:
        """
        按 Token 预算截断文本（优化 8: 带缓存的 token 计数）

        使用 ContextWindowGuard.estimate_tokens 估算，超出时截断。
        max_tokens 为 None 或 0 时不截断。
        """
        if not max_tokens or max_tokens <= 0 or not text:
            return text

        try:
            current_tokens = PromptBuilder._estimate_tokens_cached(text)
            if current_tokens <= max_tokens:
                return text

            # 按比例截断（粗略但高效）
            ratio = max_tokens / max(current_tokens, 1)
            max_chars = int(len(text) * ratio * 0.95)  # 留 5% 余量
            if max_chars < len(text):
                return text[:max_chars] + "\n...[因 Token 预算限制已截断]"
        except Exception:
            pass
        return text

    @staticmethod
    def _build_quality_focus_guidance(quality_focus: Dict[str, float]) -> str:
        """根据 quality_focus 权重生成质量行为指引，注入 system prompt"""
        if not quality_focus:
            return ""

        # 维度描述映射
        dim_descriptions = {
            "timeliness": (
                "时效性",
                "优先获取最新数据。优先使用 `quick_search`、`url_fetch` 或实时行情类工具，"
                "调用行情类工具时确保获取实时/最新数据而非历史数据。"
            ),
            "correctness": (
                "正确性",
                "关键数据必须有多源交叉验证。引用具体数字时标明数据源和时间节点，"
                "避免使用模糊表述。优先引用权威机构和官方数据。"
            ),
            "coverage": (
                "全面性",
                "确保覆盖用户查询涉及的所有关键维度。多角度分析，"
                "不遗漏重要子话题。搜索时使用多个关键词组合覆盖不同方面。"
            ),
            "validity": (
                "有效性",
                "分析结论必须有数据支撑，避免主观臆断。"
                "引用有署名作者、有机构背景的内容，确保分析的客观性和可验证性。"
            ),
        }

        # 筛选权重 >= 0.7 的维度（跳过非数值字段如 search_time_range）
        high_dims = []
        for dim, weight in quality_focus.items():
            if not isinstance(weight, (int, float)):
                continue
            if weight >= 0.7 and dim in dim_descriptions:
                label, desc = dim_descriptions[dim]
                high_dims.append(f"- **{label}（权重 {weight:.1f}）**: {desc}")

        if not high_dims:
            return ""

        quality_dimensions = "\n".join(high_dims)

        # 加载模板
        guidance = _load("v4_quality_focus_guidance", quality_dimensions=quality_dimensions)
        if guidance:
            return f"\n{guidance}"

        # fallback: 直接拼接
        return f"\n## 本次查询的质量需求重点\n\n{quality_dimensions}"

    def _format_output_instructions(
        self,
        output_format: Optional[str],
        render_mode: str = "auto"
    ) -> str:
        """输出格式指令 -- 从配置文件加载"""
        sections = []

        if output_format == "mermaid":
            text = _load("v4_output_mermaid")
            if text:
                sections.append(f"\n{text}")
        elif output_format == "markdown":
            text = _load("v4_output_markdown")
            if text:
                sections.append(f"\n{text}")

        if render_mode == "text_only":
            text = _load("v4_text_only")
            if text:
                sections.append(f"\n{text}")

        return "\n".join(sections) if sections else ""

    @staticmethod
    def _build_lang_instruction(report_lang: str) -> str:
        """构建语种输出指令（仅非默认语种时调用）"""
        if report_lang == "en":
            return (
                "\n## ⚠️ MANDATORY Language Requirement — English Only\n"
                "**YOU MUST write the ENTIRE response in English.** This is a hard requirement, not a suggestion.\n\n"
                "- ALL analysis text, conclusions, reasoning, and section headers → English\n"
                "- ALL data table column headers → English\n"
                "- ALL planning, step descriptions, and tool call purposes → English\n"
                "- Stock/company proper nouns may keep original form (e.g., 贵州茅台)\n"
                "- Data values keep original form (numbers, dates, codes)\n"
                "- **Do NOT mix Chinese into any part of the output.** If you find yourself writing Chinese, STOP and rewrite in English."
            )
        return ""

    def build_unified_system_prompt(
        self,
        history: List[Dict[str, str]] = None,
        summary: Optional[str] = None,
        experience: Optional[Dict[str, List[str]]] = None,
        ts_code: Optional[str] = None,
        params: Dict[str, Any] = None,
        skill_exec_times: Dict[str, float] = None,
        output_format: Optional[str] = "markdown",
        render_mode: str = "auto",
        has_resume: bool = False,
        budget: Optional["ContextBudget"] = None,
        memory_context: str = "",
        phase0_intents: Optional[List[str]] = None,
        phase0_topics: Optional[List[str]] = None,
        quality_focus: Optional[Dict[str, float]] = None,
        report_lang: str = "zh",
        as_blocks: bool = False,
    ):
        """
        构建单 Client 架构的统一 System Prompt

        LLM 在同一个 session 中完成所有阶段：
        - 阶段 0：意图理解（输出 [INTENT_RESULT]）
        - 阶段 1：规划（输出 [PLAN]，仅当需要工具时）
        - 阶段 2：执行（调用工具，整合结果）

        report_lang: zh=中文, en=英文, auto=根据用户语言自适应
        as_blocks: True 时返回带 cache_control 的 List[dict]，False 时返回 str
        """
        zone_a_parts = []
        zone_b_parts = []
        zone_c_parts = []

        # ─────────────────────────────────────────────────────────────────
        # Zone A: 全静态区 — 跨所有请求完全一致，KV Cache 高命中
        # ─────────────────────────────────────────────────────────────────

        # A1. 基础指令（身份 + 核心原则；{response_language_rule} 已按 lang 替换）
        zone_a_parts.append(self._get_base_prompt(report_lang=report_lang))

        # A2. 问答工作流规则（附件处理、输出标记、可信度评估）
        workflow = _load("v4_agent_workflow")
        if workflow:
            zone_a_parts.append(workflow)

        # A3. 搜索质量评估框架（永久注入，指导 quick_search/url_fetch 迭代决策）
        search_quality = _load("v4_search_quality_framework")
        if search_quality:
            zone_a_parts.append(f"\n{search_quality}")

        # A4. 输出格式 + 渲染模式控制
        # 7.1 即时确认回复已迁移到两阶段架构（Phase 1 _phase1_quick_respond）
        # v4_smart_ack 不再注入 system prompt
        format_section = self._format_output_instructions(output_format, render_mode)
        if format_section:
            zone_a_parts.append(format_section)

        # A5. 行为指引（无条件注入，不再检查 experience）
        behavior = _load("v4_experience_behavior")
        if behavior:
            zone_a_parts.append(f"\n{behavior}")

        # ─────────────────────────────────────────────────────────────────
        # Zone B: 半静态区 — 技能列表/知识库，天级更新
        # ─────────────────────────────────────────────────────────────────

        # B1. 技能列表注入 — 从 SKILL.md 动态读取 name + description，注入系统提示词
        skill_list_section = self._get_skill_list_section(
            user_id=params.get("user_id") if params else None,
            phase0_intents=phase0_intents,
            phase0_topics=phase0_topics,
        )
        if skill_list_section:
            zone_b_parts.append(skill_list_section)

        # B2. 知识库上下文（从配置文件动态加载）
        knowledge_section = self._get_knowledge_context()
        if knowledge_section:
            zone_b_parts.append(knowledge_section)

        # ─────────────────────────────────────────────────────────────────
        # Zone C: 动态区 — 记忆/历史/覆盖指令，每次请求变化
        # ─────────────────────────────────────────────────────────────────

        # C1. 记忆上下文（始终注入，不受 has_resume 影响）
        if memory_context:
            if budget and budget.memory_budget > 0:
                memory_context = self._truncate_to_budget(
                    memory_context, budget.memory_budget
                )
            zone_c_parts.append(memory_context)

        # C2. 经验注入（始终注入）
        if experience:
            experience_section = self._format_experience(experience)
            if experience_section:
                if budget and budget.experience_budget > 0:
                    experience_section = self._truncate_to_budget(
                        experience_section, budget.experience_budget
                    )
                zone_c_parts.append(experience_section)

        # C3. 对话摘要（仅在无 resume 时注入）
        if not has_resume and summary:
            summary_text = f"\n## 之前对话总结\n{summary}"
            if budget and budget.history_budget > 0:
                summary_text = self._truncate_to_budget(
                    summary_text, int(budget.history_budget * 0.4)
                )
            zone_c_parts.append(summary_text)

        # C4. 最近对话历史（仅在无 resume 时注入）
        if not has_resume and history:
            history_section = self._format_history(history)
            if history_section:
                if budget and budget.history_budget > 0:
                    history_section = self._truncate_to_budget(
                        history_section, int(budget.history_budget * 0.6)
                    )
                zone_c_parts.append(f"\n## 最近对话记录\n{history_section}")

        # C5. 上下文信息
        if ts_code:
            zone_c_parts.append(f"\n## 当前关注股票: {ts_code}")

        # C6. 钉钉渠道上下文（创建定时任务时需要这些值填入 callback JSON）
        self._inject_dingtalk_context(zone_c_parts, params)

        # C7. 质量需求指引（quality_focus 驱动）
        if quality_focus:
            qf_guidance = self._build_quality_focus_guidance(quality_focus)
            if qf_guidance:
                zone_c_parts.append(qf_guidance)

        # C8. 条件覆盖指令（语种）— 放在整个 prompt 最末尾，首尾夹击改为单段末尾覆盖
        #     原头部 + 尾部两段合并为一段，确保 LLM 在读完全部上下文后仍记住语种要求
        if report_lang == "en":
            zone_c_parts.append(
                "# ⚠️ CRITICAL INSTRUCTION: ENGLISH ONLY\n"
                "**The user asked in English. You MUST write your ENTIRE response in English.**\n"
                "This instruction has the HIGHEST priority and overrides ALL other language rules above.\n"
                "Do NOT write Chinese in any part of your output — not in analysis, not in conclusions, "
                "not in section headers, not in table headers, not in planning JSON values.\n"
                "The only exceptions: stock/company proper nouns (e.g. 贵州茅台) and raw data values.\n"
                "\n# ⚠️ FINAL REMINDER: ENGLISH ONLY\n"
                "Everything you write — analysis, report, conclusions, section headers, "
                "table headers — MUST be in English. Do NOT use Chinese."
            )

        if as_blocks:
            return self.build_system_prompt_blocks(
                zone_a="\n".join(zone_a_parts),
                zone_b="\n".join(zone_b_parts),
                zone_c="\n".join(zone_c_parts),
            )

        return "\n".join(zone_a_parts + zone_b_parts + zone_c_parts)
