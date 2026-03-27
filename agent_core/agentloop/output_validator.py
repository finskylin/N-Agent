"""
OutputValidator — 结构化输出校验

当 AgentLoop 指定了 output_schema 时，循环结束前校验 LLM 输出
是否符合 JSON Schema。校验失败则追加错误提示重新进入循环，最多重试 max_retries 次。

特性:
- opt-in 设计：schema=None 时不执行任何校验
- 支持从文本中提取 JSON（代码块、裸 JSON）
- 使用 jsonschema 校验（可选依赖，不可用时跳过 schema 校验）
- 最多重试 max_retries 次
"""
from __future__ import annotations

import json
import re
from typing import Optional, Tuple
from loguru import logger


class OutputValidator:
    """
    结构化输出校验器 — opt-in 设计

    用法:
        validator = OutputValidator(
            schema={"type": "object", "properties": {...}},
            max_retries=3,
        )
        is_valid, error = validator.validate(llm_output_text)
        if not is_valid and validator.can_retry:
            retry_msg = validator.build_retry_message(error)
    """

    def __init__(
        self,
        schema: Optional[dict] = None,
        max_retries: int = 3,
    ):
        self._schema = schema
        self._max_retries = max_retries
        self._retry_count = 0

    @property
    def enabled(self) -> bool:
        """是否启用校验（schema 非空时才有意义）"""
        return self._schema is not None

    @property
    def can_retry(self) -> bool:
        """是否还有重试机会"""
        result = self._retry_count < self._max_retries
        if result:
            self._retry_count += 1
        return result

    def reset(self) -> None:
        """重置重试计数（每次新的 AgentLoop.run 调用前）"""
        self._retry_count = 0

    def validate(self, text: str) -> Tuple[bool, str]:
        """
        从文本提取 JSON，用 JSON Schema 校验

        Returns:
            (is_valid, error_message)
        """
        if not self._schema:
            return True, ""

        # 尝试提取 JSON 对象
        obj, extract_error = self._extract_json(text)
        if obj is None:
            return False, f"无法从输出中提取 JSON: {extract_error}"

        # JSON Schema 校验
        try:
            import jsonschema
            jsonschema.validate(instance=obj, schema=self._schema)
            return True, ""
        except ImportError:
            # jsonschema 不可用，只做 JSON 提取检查
            logger.debug("[OutputValidator] jsonschema not available, skipping schema validation")
            return True, ""
        except Exception as e:
            return False, f"JSON Schema 校验失败: {str(e)[:300]}"

    def _extract_json(self, text: str) -> Tuple[Optional[any], str]:
        """
        从文本中提取 JSON

        尝试顺序:
        1. 直接 json.loads(text)
        2. 从 ```json ``` 代码块提取
        3. 找第一个 { ... } 或 [ ... ]
        """
        # 1. 直接解析
        try:
            return json.loads(text.strip()), ""
        except json.JSONDecodeError:
            pass

        # 2. 从代码块提取
        code_block_match = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", text)
        if code_block_match:
            try:
                return json.loads(code_block_match.group(1).strip()), ""
            except json.JSONDecodeError:
                pass

        # 3. 找第一个完整的 JSON 对象或数组
        for pattern in [r"\{[\s\S]*\}", r"\[[\s\S]*\]"]:
            match = re.search(pattern, text)
            if match:
                try:
                    return json.loads(match.group(0)), ""
                except json.JSONDecodeError:
                    pass

        return None, "文本中未找到有效的 JSON 结构"

    def build_retry_message(self, error: str) -> str:
        """构建重试提示（含 schema 和错误信息）"""
        schema_str = json.dumps(self._schema, ensure_ascii=False, indent=2)
        return (
            f"你的输出不符合要求的 JSON 格式。\n\n"
            f"错误信息: {error}\n\n"
            f"请严格按照以下 JSON Schema 重新输出，不要添加任何其他文字：\n"
            f"```json\n{schema_str}\n```\n\n"
            f"请直接输出符合 schema 的 JSON，不要解释。"
        )
