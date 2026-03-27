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

    def add_tool_results(self, results: List[ToolResult]) -> None:
        """
        将工具执行结果追加到消息数组（Anthropic tool_result 格式）
        """
        if not results:
            return

        tool_result_blocks = []
        for result in results:
            # 截断过长内容（避免超出 token 预算）
            content = result.content
            if len(content) > 100000:
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

    def trim_to_budget(self, max_tokens: int) -> None:
        """
        裁剪消息以适应 token 预算（保留最新的消息）

        策略: 从最老的对话轮次开始删除，保留用户的第一条消息和最新消息。
        """
        while (
            self.get_estimated_tokens() > max_tokens
            and len(self._messages) > 2
        ):
            # 删除第二条消息（保留第一条 user 消息和最后的消息）
            self._messages.pop(1)
            logger.debug(
                f"[ContextBuilder] Trimmed messages to fit budget "
                f"({self.get_estimated_tokens()} tokens estimated)"
            )
