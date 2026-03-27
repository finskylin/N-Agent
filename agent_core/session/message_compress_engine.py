"""
MessageCompressEngine — 统一消息压缩内核

所有压缩场景（预压缩、阈值压缩、重建压缩）共享此引擎。
通过传入不同的 CompressProfile 控制松紧度。

三种档位:
- pre_request: 每次请求前预压缩，最温和
- threshold_compact: 文件过大时的阈值压缩，中等力度
- rebuild: 从 MySQL 重建 session，最激进

所有参数从 agent_core/config/session_compact.json 读取，禁止硬编码。
"""
import json
import uuid as uuid_lib
from typing import List, Dict, Tuple, Optional, Any

from loguru import logger


class CompressProfile:
    """压缩档位 — 从配置中加载一组 tier 规则"""

    def __init__(self, profile_config: Dict):
        self.tool_result_tiers = profile_config.get("tool_result_tiers", [])
        self.assistant_text_tiers = profile_config.get("assistant_text_tiers", [])
        self.keep_recent_qa_pairs = profile_config.get("keep_recent_qa_pairs", 0)

    def get_tool_result_max_chars(self, distance: int) -> int:
        """查找给定 distance 的 ToolResult 压缩上限。-1 表示不压缩。"""
        for tier in self.tool_result_tiers:
            if distance <= tier.get("rounds_from_latest", 999):
                return tier.get("max_chars", -1)
        return -1

    def get_assistant_text_max_chars(self, distance: int) -> int:
        """查找给定 distance 的 Assistant Text 压缩上限。-1 表示不压缩。"""
        for tier in self.assistant_text_tiers:
            if distance <= tier.get("rounds_from_latest", 999):
                return tier.get("max_chars", -1)
        return -1


class MessageCompressEngine:
    """
    统一消息压缩内核

    所有压缩场景共享此引擎，通过传入不同的 CompressProfile 控制松紧度。
    """

    def __init__(self, engine_config: Dict):
        """
        Args:
            engine_config: agent_core/config/session_compact.json 中 compress_engine 段
        """
        self.conclusion_fields = engine_config.get("conclusion_field_names", [
            "summary", "conclusion", "recommendation",
            "总结", "结论", "建议", "操作建议",
        ])
        self.tool_use_keep_fields = engine_config.get(
            "tool_use_keep_fields", ["name", "type"]
        )

    # ----------------------------------------------------------------
    # 核心方法
    # ----------------------------------------------------------------

    async def compress_rounds(
        self,
        qa_rounds: List[Dict],
        profile: CompressProfile,
    ) -> Tuple[List[Dict], bool]:
        """
        按档位压缩 Q&A 轮次列表。

        Args:
            qa_rounds: Q&A 轮次列表（由 identify_qa_rounds 生成）
            profile: 压缩档位

        Returns:
            (压缩后的消息列表, 是否发生了压缩)
        """
        total_rounds = len(qa_rounds)
        compressed_body = []
        modified = False

        for idx, rnd in enumerate(qa_rounds):
            distance = total_rounds - idx  # 距最新轮的距离（1 = 最新）

            # user 消息: 纯文本保留，但 tool_result 按 profile 压缩
            if rnd.get("user_msg"):
                compressed_user, user_modified = self._compress_user_message(
                    rnd["user_msg"], distance, profile
                )
                compressed_body.append(compressed_user)
                if user_modified:
                    modified = True

            # assistant 消息: 按 profile 的 tier 压缩
            for msg in rnd.get("assistant_msgs", []):
                compressed_msg, was_modified = await self._compress_message(
                    msg, distance, profile
                )
                compressed_body.append(compressed_msg)
                if was_modified:
                    modified = True

        return compressed_body, modified

    def _compress_user_message(
        self,
        msg: Dict,
        distance: int,
        profile: CompressProfile,
    ) -> Tuple[Dict, bool]:
        """
        压缩 user 消息中的 tool_result 块。

        CLI session 中 tool_result 以 role=user 发送（符合 Claude API 规范）。
        用户的纯文本 content 完整保留，tool_result 按 profile tier 压缩。
        """
        content = msg.get("message", {}).get("content", [])
        if not isinstance(content, list):
            return msg, False

        modified = False
        compressed_content = []

        for block in content:
            if not isinstance(block, dict):
                compressed_content.append(block)
                continue

            block_type = block.get("type", "")

            if block_type == "tool_result":
                max_chars = profile.get_tool_result_max_chars(distance)
                if max_chars == -1:
                    compressed_content.append(block)
                else:
                    compressed_block = self._compress_tool_result_smart(
                        block, max_chars
                    )
                    if compressed_block is not block:
                        modified = True
                    compressed_content.append(compressed_block)
            else:
                # text, image 等保留
                compressed_content.append(block)

        if not modified:
            return msg, False

        compressed_msg = dict(msg)
        compressed_msg["message"] = dict(msg.get("message", {}))
        compressed_msg["message"]["content"] = compressed_content
        # CLI session 的 toolUseResult 字段是 tool_result.content 的冗余副本，同步清理
        if "toolUseResult" in compressed_msg:
            del compressed_msg["toolUseResult"]
        return compressed_msg, True

    async def _compress_message(
        self,
        msg: Dict,
        distance: int,
        profile: CompressProfile,
    ) -> Tuple[Dict, bool]:
        """
        按距离 + 档位压缩单条 assistant 消息。

        遍历 content 数组中的每个 block，按类型分派:
        - tool_result → _compress_tool_result_smart()
        - text → small 模型摘要
        - tool_use → 保留指定字段
        """
        content = msg.get("message", {}).get("content", [])
        if not isinstance(content, list):
            return msg, False

        modified = False
        compressed_content = []

        for block in content:
            if not isinstance(block, dict):
                compressed_content.append(block)
                continue

            block_type = block.get("type", "")

            if block_type == "tool_result":
                max_chars = profile.get_tool_result_max_chars(distance)
                if max_chars == -1:
                    compressed_content.append(block)
                else:
                    compressed_block = self._compress_tool_result_smart(
                        block, max_chars
                    )
                    if compressed_block is not block:
                        modified = True
                    compressed_content.append(compressed_block)

            elif block_type == "text":
                max_chars = profile.get_assistant_text_max_chars(distance)
                text = block.get("text", "")
                if max_chars == -1 or len(text) <= max_chars:
                    compressed_content.append(block)
                else:
                    compressed_content.append({
                        "type": "text",
                        "text": await self._compress_assistant_text(text, max_chars),
                    })
                    modified = True

            elif block_type == "tool_use":
                compressed_content.append(
                    self._compress_tool_use_block(block)
                )

            elif block_type == "compaction":
                # compaction blocks 在 SessionCompactor._strip_old_compaction_blocks
                # 中已统一清除。此处作为兜底：跳过残留的 compaction blocks，
                # 避免多次 compact 后无限累积。
                modified = True
                continue

            else:
                # 其他类型保留
                compressed_content.append(block)

        if not modified:
            return msg, False

        compressed_msg = dict(msg)
        compressed_msg["message"] = dict(msg.get("message", {}))
        compressed_msg["message"]["content"] = compressed_content
        return compressed_msg, True

    # ----------------------------------------------------------------
    # Assistant Text 智能压缩（small 模型摘要）
    # ----------------------------------------------------------------

    async def _compress_assistant_text(self, text: str, max_chars: int) -> str:
        """
        用 small 模型对 assistant 文本生成摘要，替代粗暴截断。

        策略：
        1. 先提取所有 URL（报告链接，必须保留）
        2. 调用 small 模型生成 <1000 字的语义摘要（保留关键结论）
        3. 摘要末尾附加所有 URL
        4. LLM 调用失败时 fallback 到截断

        目标摘要长度：< 1000 字
        """
        import re

        # 1. 提取所有 URL（报告链接最关键，必须保留）
        urls = re.findall(r'https?://[^\s\)\]"\']+', text)
        seen: set = set()
        unique_urls = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                unique_urls.append(u)

        # 2. 调用 small 模型生成摘要
        summary = await self._call_llm_summarize(text)

        # 3. 拼接摘要 + URL
        parts = [summary]
        if unique_urls:
            parts.append("\n[链接] " + " | ".join(unique_urls[:6]))

        return "".join(parts)

    async def _call_llm_summarize(self, text: str) -> str:
        """
        调用 small 模型生成摘要，失败时 fallback 到截断。

        摘要要求：
        - 删除工具调用/执行过程的描述
        - 保留关键结论、评级、建议、数据指标
        - 不超过 1000 字
        - 用中文输出
        """
        prompt = (
            "请对以下 AI 助手的回复内容进行压缩摘要。\n\n"
            "要求：\n"
            "1. 删除工具调用和执行过程的描述（如[正在查询...]、[调用了...工具]等）\n"
            "2. 保留所有关键结论、评级、投资建议、重要数据指标\n"
            "3. 保留所有已完成的分析结果\n"
            "4. 输出不超过 1000 字\n"
            "5. 用中文输出，不需要任何解释\n\n"
            "原文内容：\n"
            "---\n"
            f"{text[:6000]}\n"
            "---\n\n"
            "直接输出摘要，不要任何前缀："
        )
        try:
            from agent_core.agentloop.llm_provider import call_llm
            from .policy_error_handler import PolicyErrorHandler
            result = await call_llm(
                prompt,
                use_small_fast=True,
                max_tokens=1200,
                timeout=30.0,
            )
            if result and result.strip():
                # policy error 兜底：模型拒绝时返回错误文本，直接 fallback
                if PolicyErrorHandler.is_policy_error_text(result):
                    logger.warning(
                        "[CompressEngine] LLM returned policy error, fallback to truncation"
                    )
                else:
                    return result.strip()
        except Exception as e:
            logger.warning(f"[CompressEngine] LLM summarize failed, fallback: {e}")

        # fallback：截断（仅在 LLM 不可用或 policy error 时使用）
        return text[:1000] + "...[已压缩]"

    # ----------------------------------------------------------------
    # ToolResult 智能压缩
    # ----------------------------------------------------------------

    def _compress_tool_result_smart(self, block: Dict, max_chars: int) -> Dict:
        """
        智能压缩 ToolResultBlock — 提取 JSON 结构概览 + 结论字段。

        替代粗暴截断（content[:500]）。
        """
        content = block.get("content", "")

        # 提取原始文本
        if isinstance(content, list):
            text_parts = []
            for sub in content:
                if isinstance(sub, dict) and sub.get("type") == "text":
                    text_parts.append(sub.get("text", ""))
            raw_text = "\n".join(text_parts)
        elif isinstance(content, str):
            raw_text = content
        else:
            return block

        if len(raw_text) <= max_chars:
            return block  # 已经够短

        # 智能摘要
        compressed_text = self._extract_json_summary(raw_text, max_chars)

        # 重建 block
        compressed = dict(block)
        if isinstance(content, list):
            compressed["content"] = [{"type": "text", "text": compressed_text}]
        else:
            compressed["content"] = compressed_text
        return compressed

    def _extract_json_summary(self, raw_text: str, max_chars: int) -> str:
        """
        从文本提取结构摘要 + 结论。

        优先 JSON 解析提取语义，失败时 fallback 到纯文本截断。
        """
        try:
            data = json.loads(raw_text)
        except (json.JSONDecodeError, ValueError):
            return raw_text[:max_chars] + "...[已压缩]"

        if not isinstance(data, dict):
            return raw_text[:max_chars] + "...[已压缩]"

        # 提取顶层 key 列表
        keys = list(data.keys())
        keys_str = ", ".join(keys[:15])
        if len(keys) > 15:
            keys_str += f" ...共{len(keys)}个字段"

        # 递归搜索结论字段（最多3层深度）
        conclusions = []
        conclusion_fields_lower = [f.lower() for f in self.conclusion_fields]

        def _search(obj: Any, depth: int = 0):
            if depth > 3:
                return
            if isinstance(obj, dict):
                for key, val in obj.items():
                    if key.lower() in conclusion_fields_lower:
                        if isinstance(val, str) and val.strip():
                            conclusions.append(val.strip())
                    elif isinstance(val, (dict, list)):
                        _search(val, depth + 1)
            elif isinstance(obj, list):
                for item in obj:
                    if isinstance(item, dict):
                        _search(item, depth + 1)

        _search(data)

        # 拼接摘要
        parts = [f"[Skill结果概要] Keys: [{keys_str}]"]
        if conclusions:
            parts.append(f"结论: {' | '.join(conclusions)}")

        result = ". ".join(parts)
        if len(result) > max_chars:
            result = result[:max_chars] + "...[已压缩]"

        return result

    # ----------------------------------------------------------------
    # ToolUse 压缩
    # ----------------------------------------------------------------

    def _compress_tool_use_block(self, block: Dict) -> Dict:
        """压缩 ToolUseBlock: 只保留 id + keep_fields"""
        compressed = {"type": "tool_use"}
        if "id" in block:
            compressed["id"] = block["id"]
        for field in self.tool_use_keep_fields:
            if field in block:
                compressed[field] = block[field]
        return compressed

    # ----------------------------------------------------------------
    # 公共辅助方法（三个场景共用）
    # ----------------------------------------------------------------

    @staticmethod
    def split_header_body(
        messages: List[Dict],
    ) -> Tuple[List[Dict], List[Dict]]:
        """分离 queue-operation 头和消息体"""
        header, body = [], []
        for msg in messages:
            if msg.get("type") == "queue-operation":
                header.append(msg)
            else:
                body.append(msg)
        return header, body

    @staticmethod
    def identify_qa_rounds(messages: List[Dict]) -> List[Dict]:
        """
        识别 Q&A 轮次。

        一个 Q&A 轮次 = 一个 user 消息 + 后续所有 assistant 消息
        """
        rounds: List[Dict] = []
        current_round: Optional[Dict] = None

        for msg in messages:
            msg_role = msg.get("message", {}).get("role", "")
            msg_type = msg.get("type", "")

            if msg_role == "user" or msg_type == "user":
                if current_round:
                    rounds.append(current_round)
                current_round = {
                    "user_msg": msg,
                    "assistant_msgs": [],
                    "messages": [msg],
                }
            elif current_round is not None:
                current_round["assistant_msgs"].append(msg)
                current_round["messages"].append(msg)
            else:
                rounds.append({
                    "user_msg": None,
                    "assistant_msgs": [msg],
                    "messages": [msg],
                })

        if current_round:
            rounds.append(current_round)
        return rounds

    @staticmethod
    def fix_parent_chain(messages: List[Dict]):
        """修复消息链的 parentUuid"""
        prev_uuid = None
        for msg in messages:
            if msg.get("type") == "queue-operation":
                continue
            if not msg.get("uuid"):
                msg["uuid"] = str(uuid_lib.uuid4())
            msg["parentUuid"] = prev_uuid
            prev_uuid = msg["uuid"]
