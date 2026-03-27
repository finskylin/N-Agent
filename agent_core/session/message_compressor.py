"""
Message Compressor — 智能压缩对话消息以节省存储

压缩策略（按设计文档 session_manage_2.md）:
- 用户消息: 原样保留（截断超长） — 压缩比 1:1
- 工具输入: 只保留关键参数 — 压缩比 ~10:1
- 工具输出: 规则提取摘要（条数、关键词） — 压缩比 ~50:1
- 助手回复: 保留前 500 字 + 表格/列表结构 — 压缩比 ~5:1
- 执行计划: 只保留步骤名称列表 — 压缩比 ~10:1

用途:
1. 保存到 MySQL 持久化层前压缩消息
2. 重建 session 时从压缩数据恢复
"""
import json
import re
from typing import Dict, List, Optional, Any, TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from .v4_config import V4Config


class MessageCompressor:
    """消息压缩器 — 智能压缩对话消息以节省存储"""

    # 默认关键参数（Provider 查不到时的 fallback）
    _DEFAULT_KEY_PARAMS = ["query", "ts_code", "code", "url", "action", "topic"]

    # 结构化元素的正则模式（用于保留文本结构）
    STRUCTURE_PATTERNS = [
        r'^#{1,6}\s',      # Markdown 标题
        r'^\|',            # 表格行
        r'^[-*+]\s',       # 无序列表
        r'^\d+\.\s',       # 有序列表
        r'^```',           # 代码块
        r'^>\s',           # 引用块
    ]

    def __init__(self, config: Optional["V4Config"] = None):
        """
        初始化压缩器

        Args:
            config: V4Config 实例，用于读取压缩参数
        """
        if config:
            self.max_user_msg_length = config.compress_user_msg_max_length
            self.max_assistant_text_length = config.compress_assistant_text_max_length
            self.max_tool_input_length = config.compress_tool_input_max_length
            self.max_tool_output_length = config.compress_tool_output_max_length
        else:
            # 默认值
            self.max_user_msg_length = 2000
            self.max_assistant_text_length = 500
            self.max_tool_input_length = 200
            self.max_tool_output_length = 500

    def compress_message(self, message: dict) -> Optional[dict]:
        """
        压缩单条消息

        Args:
            message: 原始消息（CLI session .jsonl 行）

        Returns:
            压缩后的消息，progress 类型消息返回 None（可跳过）
        """
        msg_type = message.get("type")

        if msg_type == "user":
            return self._compress_user_message(message)
        elif msg_type == "assistant":
            return self._compress_assistant_message(message)
        elif msg_type == "tool_result":
            return self._compress_tool_result(message)
        elif msg_type == "progress":
            # progress 消息可以完全跳过（重建时不需要）
            return None
        elif msg_type == "queue-operation":
            # 队列操作消息保留元数据
            return self._compress_metadata_only(message)
        else:
            # 其他类型保留元数据，清空详情
            return self._compress_metadata_only(message)

    def compress_messages(self, messages: List[dict]) -> List[dict]:
        """
        批量压缩消息列表

        Args:
            messages: 原始消息列表

        Returns:
            压缩后的消息列表（已过滤 None）
        """
        compressed = []
        for msg in messages:
            result = self.compress_message(msg)
            if result is not None:
                compressed.append(result)
        return compressed

    def _compress_user_message(self, message: dict) -> dict:
        """压缩用户消息 — 原样保留（截断超长）"""
        compressed = message.copy()
        msg_content = compressed.get("message", {})

        # 处理 content 字段
        if isinstance(msg_content, dict):
            content = msg_content.get("content", "")
            if isinstance(content, str) and len(content) > self.max_user_msg_length:
                msg_content = msg_content.copy()
                msg_content["content"] = content[:self.max_user_msg_length] + "...[truncated]"
                compressed["message"] = msg_content
            elif isinstance(content, list):
                # content 是 block 数组的情况
                new_content = self._compress_content_blocks(content, self.max_user_msg_length)
                msg_content = msg_content.copy()
                msg_content["content"] = new_content
                compressed["message"] = msg_content

        return compressed

    def _compress_assistant_message(self, message: dict) -> dict:
        """
        压缩助手消息
        - text: 保留前 N 字 + 结构信息
        - tool_use: 只保留关键参数
        """
        compressed = message.copy()
        msg_data = compressed.get("message", {})

        if not isinstance(msg_data, dict):
            return compressed

        msg_content = msg_data.get("content", [])

        if not isinstance(msg_content, list):
            return compressed

        new_content = []
        for block in msg_content:
            if not isinstance(block, dict):
                new_content.append(block)
                continue

            block_type = block.get("type")

            if block_type == "text":
                # 压缩文本：保留前 N 字 + 结构摘要
                text = block.get("text", "")
                compressed_text = self._compress_text_with_structure(
                    text, self.max_assistant_text_length
                )
                new_content.append({"type": "text", "text": compressed_text})

            elif block_type == "tool_use":
                # 压缩工具输入：只保留关键参数
                tool_name = block.get("name", "")
                tool_input = block.get("input", {})
                compressed_input = self._compress_tool_input(tool_name, tool_input)
                new_content.append({
                    "type": "tool_use",
                    "id": block.get("id"),
                    "name": tool_name,
                    "input": compressed_input,
                    "_compressed": True,
                })
            else:
                new_content.append(block)

        compressed["message"] = msg_data.copy()
        compressed["message"]["content"] = new_content
        return compressed

    def _compress_tool_result(self, message: dict) -> dict:
        """
        压缩工具结果
        - 提取摘要信息（条数、关键词）
        - 限制总长度
        """
        compressed = message.copy()
        msg_data = compressed.get("message", {})

        if not isinstance(msg_data, dict):
            return compressed

        msg_content = msg_data.get("content", [])

        if not isinstance(msg_content, list):
            return compressed

        new_content = []
        for block in msg_content:
            if not isinstance(block, dict):
                new_content.append(block)
                continue

            if block.get("type") == "tool_result":
                content = block.get("content", "")
                summary = self._summarize_tool_output(content)
                new_content.append({
                    "type": "tool_result",
                    "tool_use_id": block.get("tool_use_id"),
                    "content": summary,
                    "_compressed": True,
                })
            else:
                new_content.append(block)

        compressed["message"] = msg_data.copy()
        compressed["message"]["content"] = new_content
        return compressed

    def _compress_content_blocks(self, blocks: list, max_length: int) -> list:
        """压缩 content block 数组"""
        new_blocks = []
        total_length = 0

        for block in blocks:
            if not isinstance(block, dict):
                new_blocks.append(block)
                continue

            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text", "")
                remaining = max_length - total_length
                if remaining <= 0:
                    break
                if len(text) > remaining:
                    text = text[:remaining] + "...[truncated]"
                new_blocks.append({"type": "text", "text": text})
                total_length += len(text)
            else:
                new_blocks.append(block)

        return new_blocks

    def _compress_text_with_structure(self, text: str, max_length: int) -> str:
        """
        压缩文本并保留结构信息
        - 保留 Markdown 标题
        - 保留表格结构
        - 保留列表前缀
        """
        if len(text) <= max_length:
            return text

        lines = text.split('\n')
        result_lines = []
        current_length = 0

        for line in lines:
            # 检查是否为结构化元素
            is_structure = any(
                re.match(p, line)
                for p in self.STRUCTURE_PATTERNS
            )

            # 优先保留结构元素，或者还没超过长度限制
            if is_structure or current_length < max_length:
                result_lines.append(line)
                current_length += len(line) + 1

            # 允许 20% 超出以保留完整的结构
            if current_length >= max_length * 1.2:
                break

        result = '\n'.join(result_lines)
        if len(text) > len(result):
            result += "\n...[content truncated]"

        return result

    def _compress_tool_input(self, tool_name: str, tool_input: Any) -> dict:
        """压缩工具输入 — 只保留关键参数（从 SkillMetadataProvider 查询）"""
        if not isinstance(tool_input, dict):
            return {"_raw": str(tool_input)[:self.max_tool_input_length]}

        # 从 Provider 获取关键参数
        try:
            from agent_core.skill_metadata_provider import get_skill_metadata_provider
            provider = get_skill_metadata_provider()
            key_params = provider.get_key_params(tool_name)
        except Exception:
            key_params = self._DEFAULT_KEY_PARAMS

        compressed = {}
        for key in key_params:
            if key in tool_input:
                value = tool_input[key]
                # 截断过长的字符串值
                if isinstance(value, str) and len(value) > 200:
                    value = value[:200] + "..."
                compressed[key] = value

        # 添加参数数量信息
        compressed["_param_count"] = len(tool_input)
        compressed["_other_params"] = [
            k for k in tool_input.keys()
            if k not in key_params
        ][:5]  # 最多记录 5 个其他参数名

        return compressed

    def _summarize_tool_output(self, content: str) -> str:
        """
        提取工具输出摘要
        - JSON 数组：提取条数和结构
        - 纯文本：截断
        """
        if not content:
            return ""

        # 尝试解析 JSON
        try:
            data = json.loads(content)

            if isinstance(data, list):
                # 数组：提取统计信息
                count = len(data)
                sample = data[0] if data else {}
                keys = list(sample.keys())[:5] if isinstance(sample, dict) else []
                return json.dumps({
                    "_type": "array",
                    "_count": count,
                    "_sample_keys": keys,
                    "_first_item": self._truncate_dict(sample, 200) if sample else None,
                }, ensure_ascii=False)

            elif isinstance(data, dict):
                # 对象：保留关键字段
                return json.dumps(
                    self._truncate_dict(data, self.max_tool_output_length),
                    ensure_ascii=False
                )
            else:
                return str(data)[:self.max_tool_output_length]

        except json.JSONDecodeError:
            # 非 JSON：直接截断
            if len(content) > self.max_tool_output_length:
                return content[:self.max_tool_output_length] + "...[truncated]"
            return content

    def _truncate_dict(self, d: Any, max_length: int) -> Any:
        """递归截断字典中的长字符串"""
        if not isinstance(d, dict):
            if isinstance(d, str) and len(d) > 100:
                return d[:100] + "..."
            return d

        result = {}
        current_length = 0

        for key, value in d.items():
            if current_length >= max_length:
                result["_truncated"] = True
                break

            if isinstance(value, str):
                if len(value) > 100:
                    value = value[:100] + "..."
            elif isinstance(value, dict):
                value = self._truncate_dict(value, 200)
            elif isinstance(value, list):
                value = f"[array of {len(value)} items]"

            result[key] = value
            current_length += len(str(value))

        return result

    def _compress_metadata_only(self, message: dict) -> dict:
        """只保留元数据"""
        return {
            "type": message.get("type"),
            "uuid": message.get("uuid"),
            "parentUuid": message.get("parentUuid"),
            "timestamp": message.get("timestamp"),
            "sessionId": message.get("sessionId"),
            "_metadata_only": True,
        }

    def estimate_size(self, message: dict) -> int:
        """估算消息的 JSON 序列化大小（字节）"""
        try:
            return len(json.dumps(message, ensure_ascii=False).encode('utf-8'))
        except Exception:
            return 0

    def compress_plan(self, plan: dict) -> dict:
        """
        压缩执行计划 — 只保留步骤名称列表

        Args:
            plan: 原始执行计划 JSON

        Returns:
            压缩后的计划
        """
        if not isinstance(plan, dict):
            return {"_invalid": True}

        compressed = {
            "_type": "plan",
            "_compressed": True,
        }

        # 保留意图分析摘要
        intent = plan.get("intent_analysis", {})
        if intent:
            compressed["intent"] = {
                "type": intent.get("type"),
                "complexity": intent.get("complexity"),
            }

        # 只保留步骤名称和工具
        steps = plan.get("steps", [])
        if steps:
            compressed["steps"] = [
                {
                    "name": s.get("name", ""),
                    "tool": s.get("tool", ""),
                }
                for s in steps
                if isinstance(s, dict)
            ]

        return compressed
