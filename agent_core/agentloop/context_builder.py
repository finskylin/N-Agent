"""
ContextBuilder — LLM 消息数组组装

负责将 system prompt、历史对话、工具调用结果等组装为 OpenAI 格式的 messages 数组，
用于传入 LiteLLMProvider.chat() / chat_stream()。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from loguru import logger

from .message_types import LLMResponse, ToolCallRequest, ToolResult


class ContextBuilder:
    """
    LLM 消息构建器

    维护当前对话的 messages 数组（OpenAI 格式）：
    [
        {"role": "user", "content": "..."},
        {"role": "assistant", "content": [...text/tool_use blocks...]},
        {"role": "user", "content": [...tool_result blocks...]},
        ...
    ]

    系统提示通过 system 参数单独传递（Anthropic API 约定）。
    """

    def __init__(self):
        self._messages: List[Dict[str, Any]] = []

    def reset(self) -> None:
        """清空消息数组（新对话开始时调用）"""
        self._messages = []

    @property
    def messages(self) -> List[Dict[str, Any]]:
        """当前消息数组（只读副本）"""
        return list(self._messages)

    def build_initial_messages(
        self,
        user_message: str,
        history_messages: Optional[List[Dict[str, Any]]] = None,
        attached_files: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """
        构建初始消息数组

        Args:
            user_message: 当前用户消息
            history_messages: 历史对话（已格式化为 OpenAI 格式）
            attached_files: 附件文件列表（由 document_reader skill 处理）

        Returns:
            OpenAI 格式的 messages 数组（不含 system）
        """
        self._messages = []

        # 注入历史消息（来自 ConversationHistory）
        if history_messages:
            for msg in history_messages:
                if msg.get("role") in ("user", "assistant"):
                    self._messages.append(msg)

        # 构建用户消息内容
        user_content = user_message

        # 如果有附件，追加附件描述（由 Phase 2 document_reader 处理）
        if attached_files:
            file_descriptions = []
            for af in attached_files:
                f_type = af.get("type", "file")
                f_name = af.get("name", "")
                parts = [f"type={f_type}"]
                if f_name:
                    parts.append(f"name={f_name}")
                # 传递 dingtalk 附件下载凭证
                dc = af.get("download_code", "")
                rc = af.get("robot_code", "")
                if dc:
                    parts.append(f"dingtalk_download_code={dc}")
                if rc:
                    parts.append(f"robot_code={rc}")
                # 传递直链 URL
                url = af.get("url", "") or af.get("file_url", "")
                if url:
                    parts.append(f"file_url={url}")
                file_descriptions.append("[attached_file|" + "|".join(parts) + "]")
            if file_descriptions:
                user_content = user_content + "\n" + "\n".join(file_descriptions)

        self._messages.append({"role": "user", "content": user_content})
        return self._messages

    def add_llm_response(self, response: LLMResponse) -> None:
        """
        将 LLM 响应追加到消息数组（Anthropic content blocks 格式）
        """
        content_blocks: List[Dict[str, Any]] = []

        # 文本内容
        if response.content:
            content_blocks.append({
                "type": "text",
                "text": response.content,
            })

        # thinking 内容（extended thinking）
        if response.thinking_content:
            # thinking block 必须在 text/tool_use 之前
            # dashscope 要求多轮对话回写时必须携带 signature 字段（即使为空字符串）
            thinking_block: Dict[str, Any] = {
                "type": "thinking",
                "thinking": response.thinking_content,
            }
            sig = getattr(response, "thinking_signature", None)
            if sig is not None:
                thinking_block["signature"] = sig
            else:
                # 若无 signature，写入空字符串以满足 dashscope 格式要求
                thinking_block["signature"] = ""
            content_blocks.insert(0, thinking_block)

        # 工具调用
        for tc in response.tool_calls:
            content_blocks.append({
                "type": "tool_use",
                "id": tc.id,
                "name": tc.name,
                "input": tc.arguments,
            })

        if content_blocks:
            self._messages.append({
                "role": "assistant",
                "content": content_blocks,
            })
        elif response.has_text:
            # 简单文本格式（兼容）
            self._messages.append({
                "role": "assistant",
                "content": response.content,
            })

    def _cap_content(self, content: str, max_chars: int) -> str:
        """
        单条内容硬上限截断（保留头尾各 50%，尾部往往有结论/摘要）。
        max_chars=0 表示不限制。
        """
        if max_chars <= 0 or len(content) <= max_chars:
            return content
        half = max_chars // 2
        return (
            content[:half]
            + f"\n\n... [truncated {len(content) - max_chars} chars] ...\n\n"
            + content[-half:]
        )

    def add_tool_results(self, results: List[ToolResult], max_chars: int = 0) -> None:
        """
        将工具执行结果追加到消息数组（Anthropic tool_result 格式）

        Args:
            results: 工具执行结果列表
            max_chars: 单条内容硬上限（0 = 不限制，默认向后兼容）
        """
        if not results:
            return

        tool_result_blocks = []
        for result in results:
            # 1. 先用 max_chars 硬上限（第二道防线，summarizer 已是第一道）
            content = result.content or ""
            if max_chars > 0:
                content = self._cap_content(content, max_chars)
            elif len(content) > 100000:
                # 向后兼容：旧硬上限 100K
                content = content[:100000] + "\n...[truncated]"

            tool_result_blocks.append({
                "type": "tool_result",
                "tool_use_id": result.tool_call_id,
                "content": content,
                "is_error": result.is_error,
            })

        self._messages.append({
            "role": "user",
            "content": tool_result_blocks,
        })

    def get_message_count(self) -> int:
        """获取当前消息数量"""
        return len(self._messages)

    def get_estimated_tokens(self) -> int:
        """粗略估计 token 数（按字符数 / 4）"""
        total_chars = 0
        for msg in self._messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        total_chars += len(str(block))
        return total_chars // 4

    # ─── 分级优先截断 ──────────────────────────────────────────────────

    def _estimate_chars(self, msg: Dict[str, Any]) -> int:
        """估算单条消息字符数"""
        content = msg.get("content", "")
        if isinstance(content, str):
            return len(content)
        elif isinstance(content, list):
            return sum(len(str(b)) for b in content)
        return 0

    def _get_text_content(self, msg: Dict[str, Any]) -> str:
        """获取消息文本内容（用于优先级判断）"""
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        elif isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    parts.append(block.get("text", "") or block.get("content", "") or "")
            return "".join(parts)
        return ""

    def _has_tool_calls(self, msg: Dict[str, Any]) -> bool:
        """判断 assistant 消息是否含 tool_calls"""
        content = msg.get("content", "")
        if isinstance(content, list):
            return any(
                isinstance(b, dict) and b.get("type") == "tool_use"
                for b in content
            )
        return False

    def _get_tool_call_ids(self, msg: Dict[str, Any]) -> List[str]:
        """从 assistant 消息提取所有 tool_call id"""
        ids = []
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tc_id = block.get("id", "")
                    if tc_id:
                        ids.append(tc_id)
        return ids

    def _get_tool_result_id(self, msg: Dict[str, Any]) -> Optional[str]:
        """从 tool_result 消息（role=user, content=[tool_result块]）提取 tool_use_id"""
        content = msg.get("content", "")
        if isinstance(content, list) and content:
            first = content[0]
            if isinstance(first, dict) and first.get("type") == "tool_result":
                return first.get("tool_use_id", "")
        return None

    def _is_tool_result_msg(self, msg: Dict[str, Any]) -> bool:
        """判断是否是 tool_result 消息（role=user，content 是 tool_result blocks）"""
        if msg.get("role") != "user":
            return False
        content = msg.get("content", "")
        if isinstance(content, list) and content:
            return isinstance(content[0], dict) and content[0].get("type") == "tool_result"
        return False

    def _truncate_msg_to(self, msg: Dict[str, Any], max_chars: int) -> Dict[str, Any]:
        """截断消息内容到 max_chars（保留头部）"""
        content = msg.get("content", "")
        if isinstance(content, str):
            if len(content) > max_chars:
                new_msg = dict(msg)
                new_msg["content"] = content[:max_chars] + "...[truncated]"
                return new_msg
        elif isinstance(content, list):
            total = sum(len(str(b)) for b in content)
            if total > max_chars:
                new_msg = dict(msg)
                # 对 tool_result 块内容截断
                new_blocks = []
                remaining = max_chars
                for block in content:
                    if isinstance(block, dict):
                        block_str = block.get("content", "") or block.get("text", "")
                        if isinstance(block_str, str) and len(block_str) > remaining:
                            new_block = dict(block)
                            new_block["content"] = block_str[:remaining] + "...[truncated]"
                            new_blocks.append(new_block)
                            remaining = 0
                        else:
                            new_blocks.append(block)
                            remaining = max(0, remaining - len(str(block)))
                    else:
                        new_blocks.append(block)
                new_msg["content"] = new_blocks
                return new_msg
        return msg

    def _assign_priority(
        self,
        messages: List[Dict[str, Any]],
        last_assistant_idx: int,
    ) -> List[int]:
        """
        给每条消息打优先级（方案 C，纯消息结构判断）：
          0 CRITICAL: index==0 and role=="user"（原始任务，非 tool_result）
          1 CRITICAL: 最后一条 role=="assistant"
          2 HIGH:     role=="assistant" and 含 tool_calls
          3 HIGH:     role=="tool_result" and (含 "[ERROR]" or 含 "[SUMMARY")
          4 MEDIUM:   role=="tool_result"，其余
          5 LOW:      role=="assistant"，无 tool_calls，非最后一条
          6 NORMAL:   其他（中间 user 普通消息）
        """
        priorities = []
        for i, msg in enumerate(messages):
            role = msg.get("role", "")
            is_tool_result = self._is_tool_result_msg(msg)

            if i == 0 and role == "user" and not is_tool_result:
                p = 0
            elif i == last_assistant_idx and role == "assistant":
                p = 1
            elif role == "assistant" and self._has_tool_calls(msg):
                p = 2
            elif is_tool_result:
                text = self._get_text_content(msg)
                if "[ERROR]" in text or "[SUMMARY" in text:
                    p = 3
                else:
                    p = 4
            elif role == "assistant" and not self._has_tool_calls(msg) and i != last_assistant_idx:
                p = 5
            else:
                p = 6
            priorities.append(p)
        return priorities

    def trim_to_budget(
        self,
        max_tokens: int,
        truncatable_to: int = 300,
        error_truncatable_to: int = 1000,
    ) -> None:
        """
        分级优先截断：保留高优先级消息，tool_call 和 tool_result 成对处理。

        Args:
            max_tokens: token 预算上限（字符数 / 4 估算）
            truncatable_to: MEDIUM 消息超预算时截到此长度（默认 300）
            error_truncatable_to: HIGH 错误消息截到此长度（默认 1000）
        """
        max_chars = max_tokens * 4  # token → 字符估算

        if not self._messages:
            return

        messages = list(self._messages)

        # 找最后一条 assistant 消息的索引
        last_assistant_idx = -1
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "assistant":
                last_assistant_idx = i
                break

        priorities = self._assign_priority(messages, last_assistant_idx)

        # 构建 tool_call_id → assistant_msg_idx 和 tool_result_msg_idx 的映射
        # 用于成对删除
        tc_id_to_assistant: Dict[str, int] = {}
        tc_id_to_result: Dict[str, int] = {}
        for i, msg in enumerate(messages):
            role = msg.get("role", "")
            if role == "assistant" and self._has_tool_calls(msg):
                for tc_id in self._get_tool_call_ids(msg):
                    tc_id_to_assistant[tc_id] = i
            elif self._is_tool_result_msg(msg):
                tc_id = self._get_tool_result_id(msg)
                if tc_id:
                    tc_id_to_result[tc_id] = i

        # 计算各优先级消息总字符数
        def total_chars() -> int:
            return sum(self._estimate_chars(m) for m in messages)

        def prio_chars(target_priorities) -> int:
            return sum(
                self._estimate_chars(messages[i])
                for i, p in enumerate(priorities)
                if p in target_priorities
            )

        essential_priorities = {0, 1, 2, 3}  # priority 0-3
        essential_chars = prio_chars(essential_priorities)

        if total_chars() * 4 // 4 <= max_chars:
            # 预算充足，无需截断
            return

        if essential_chars <= max_chars:
            # === 正常路径：essential 放得下，丢弃 MEDIUM/LOW ===
            new_messages = []
            omitted = 0
            for i, (msg, p) in enumerate(zip(messages, priorities)):
                if p in essential_priorities:
                    new_messages.append(msg)
                elif p == 4:  # MEDIUM: 先尝试截断
                    remaining = max_chars - sum(self._estimate_chars(m) for m in new_messages)
                    if remaining > truncatable_to:
                        truncated = self._truncate_msg_to(msg, truncatable_to)
                        new_messages.append(truncated)
                    else:
                        omitted += 1
                else:
                    omitted += 1

            if omitted > 0:
                # 追加省略提示
                new_messages.append({
                    "role": "user",
                    "content": f"[{omitted} intermediate segments omitted to fit context budget]",
                })
            self._messages = new_messages

        else:
            # === 极端路径：essential 也放不下 ===
            # Pass 1: priority 0+1 全量保留
            p01_chars = prio_chars({0, 1})
            remaining_budget = max_chars - p01_chars

            # Pass 2: priority 2+3（成对）按 remaining // (N+1) 均分截断
            p23_indices = [i for i, p in enumerate(priorities) if p in (2, 3)]
            n_p23 = len(p23_indices)
            per_msg_budget = remaining_budget // max(n_p23 + 1, 1)

            new_messages = []
            for i, (msg, p) in enumerate(zip(messages, priorities)):
                if p == 0 or p == 1:
                    new_messages.append(msg)
                elif p in (2, 3):
                    truncated = self._truncate_msg_to(msg, max(per_msg_budget, 100))
                    new_messages.append(truncated)
                elif p == 5:
                    # Pass 3: priority 5 只保首行前 150 字符
                    truncated = self._truncate_msg_to(msg, 150)
                    new_messages.append(truncated)
                # priority 4, 6 丢弃

            self._messages = new_messages

        logger.debug(
            f"[ContextBuilder] trim_to_budget: {len(messages)} → {len(self._messages)} msgs, "
            f"estimated {self.get_estimated_tokens()} tokens"
        )
