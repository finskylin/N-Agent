"""
Policy Error Handler — 策略错误检测和消息清理

职责:
1. 检测 Claude API 返回的 Usage Policy 违规错误
2. 从 CLI session .jsonl 文件中精准删除触发错误的消息
3. 保持其余对话历史完整

设计原则:
- 不清除整个 session（保留有价值的上下文）
- 精准定位并删除错误相关的消息链
- 支持异步操作，不阻塞主流程
"""
import asyncio
from typing import Optional, List, Dict, Any, TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from agent_core.session.session_file_ops import SessionFileOps
    from .v4_config import V4Config


class PolicyErrorHandler:
    """策略错误处理器 — 检测并清理违规消息"""

    # 策略错误标记关键词
    POLICY_MARKERS = (
        "Usage Policy",
        "unable to respond",
        "violate",
        "aup",
        "isApiErrorMessage",
        "content_policy",
        "safety",
        "API Error:",
        "BadRequestError",
        "DataInspectionFailed",
        "inappropriate content",
        "InternalError.Algo",
        "litellm.BadRequestError",
        "content_filter",
    )

    # 需要识别的错误类型
    ERROR_TYPES = (
        "invalid_request",
        "content_policy_violation",
        "safety_error",
    )

    def __init__(
        self,
        file_ops: "SessionFileOps",
        config: Optional["V4Config"] = None,
    ):
        """
        初始化策略错误处理器

        Args:
            file_ops: SessionFileOps 实例
            config: V4Config 实例
        """
        self.file_ops = file_ops
        self.enabled = True

        if config:
            self.enabled = config.policy_error_cleanup_enabled

        logger.info(f"[PolicyErrorHandler] Initialized (enabled={self.enabled})")

    @classmethod
    def is_policy_error(cls, message: dict) -> bool:
        """
        检测消息是否为策略错误

        Args:
            message: SDK 消息或 CLI session 消息

        Returns:
            是否为策略错误
        """
        # 方式 1: 检查 isApiErrorMessage 标记
        if message.get("isApiErrorMessage"):
            return True

        # 方式 2: 检查 error 字段
        error = message.get("error", "")
        if error in cls.ERROR_TYPES:
            return True

        # 方式 3: 检查 type 字段
        if message.get("type") == "error":
            error_type = message.get("error_type", "")
            if error_type in cls.ERROR_TYPES:
                return True

        # 方式 4: 检查消息内容
        content = message.get("message", {}).get("content", [])
        if cls._check_content_for_policy_error(content):
            return True

        # 方式 5: 检查 text 字段（某些消息格式）
        text = message.get("text", "")
        if isinstance(text, str) and cls._text_contains_policy_markers(text):
            return True

        return False

    @classmethod
    def is_policy_error_text(cls, text: str) -> bool:
        """
        检测文本是否包含策略错误标记

        Args:
            text: 文本内容

        Returns:
            是否包含策略错误标记
        """
        return cls._text_contains_policy_markers(text)

    @classmethod
    def _check_content_for_policy_error(cls, content: Any) -> bool:
        """检查 content 字段是否包含策略错误"""
        if isinstance(content, str):
            return cls._text_contains_policy_markers(content)

        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block_type = block.get("type")
                    if block_type == "text":
                        text = block.get("text", "")
                        if cls._text_contains_policy_markers(text):
                            return True
                    elif block_type == "error":
                        return True

        return False

    @classmethod
    def _text_contains_policy_markers(cls, text: str) -> bool:
        """检查文本是否包含策略错误标记"""
        if not text:
            return False
        text_lower = text.lower()
        return any(marker.lower() in text_lower for marker in cls.POLICY_MARKERS)

    async def handle_error(
        self,
        cli_session_id: str,
        error_uuid: Optional[str] = None,
        error_response: Optional[str] = None,
    ) -> bool:
        """
        处理策略错误 — 从 session 文件中清理错误消息

        Args:
            cli_session_id: CLI session ID
            error_uuid: 错误消息的 UUID（可选）
            error_response: 错误响应文本（可选，用于日志）

        Returns:
            是否成功清理
        """
        if not self.enabled:
            logger.debug("[PolicyErrorHandler] Cleanup disabled, skipping")
            return False

        if not cli_session_id:
            logger.warning("[PolicyErrorHandler] No cli_session_id provided")
            return False

        logger.info(
            f"[PolicyErrorHandler] Handling policy error for session: {cli_session_id}"
        )

        # 如果没有提供 error_uuid，尝试从 session 文件中查找
        if not error_uuid:
            error_uuid = await self._find_error_uuid(cli_session_id)
            if not error_uuid:
                logger.warning(
                    "[PolicyErrorHandler] Could not find error message UUID, "
                    "attempting to remove last message chain"
                )
                return await self._remove_last_message_chain(cli_session_id)

        # 从 session 文件中删除错误消息
        try:
            success = await asyncio.to_thread(
                self.file_ops.remove_error_messages,
                cli_session_id,
                error_uuid,
            )

            if success:
                logger.info(
                    f"[PolicyErrorHandler] Successfully cleaned up error messages "
                    f"for session: {cli_session_id}"
                )
            else:
                logger.warning(
                    f"[PolicyErrorHandler] Failed to clean up error messages "
                    f"for session: {cli_session_id}"
                )

            return success

        except Exception as e:
            logger.error(f"[PolicyErrorHandler] Error during cleanup: {e}")
            return False

    async def _find_error_uuid(self, cli_session_id: str) -> Optional[str]:
        """
        从 session 文件中查找错误消息的 UUID

        Args:
            cli_session_id: CLI session ID

        Returns:
            错误消息的 UUID，未找到返回 None
        """
        try:
            messages = await asyncio.to_thread(
                self.file_ops.read_session, cli_session_id
            )

            if not messages:
                return None

            # 从后向前查找错误消息
            for msg in reversed(messages):
                if self.is_policy_error(msg):
                    uuid = msg.get("uuid")
                    if uuid:
                        logger.debug(f"[PolicyErrorHandler] Found error UUID: {uuid}")
                        return uuid

            return None

        except Exception as e:
            logger.warning(f"[PolicyErrorHandler] Error finding error UUID: {e}")
            return None

    async def _remove_last_message_chain(self, cli_session_id: str) -> bool:
        """
        移除最后一轮对话（当无法精确定位错误消息时的回退方案）

        策略：删除最后一条 user 消息及其之后的所有消息

        Args:
            cli_session_id: CLI session ID

        Returns:
            是否成功
        """
        try:
            messages = await asyncio.to_thread(
                self.file_ops.read_session, cli_session_id
            )

            if not messages:
                return False

            # 找到最后一条 user 消息的索引
            last_user_idx = None
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("type") == "user":
                    last_user_idx = i
                    break

            if last_user_idx is None:
                logger.warning(
                    "[PolicyErrorHandler] No user message found to remove"
                )
                return False

            # 保留最后一条 user 消息之前的所有消息
            filtered_messages = messages[:last_user_idx]

            # 写回文件
            success = await asyncio.to_thread(
                self.file_ops.write_session,
                cli_session_id,
                filtered_messages,
            )

            if success:
                removed_count = len(messages) - len(filtered_messages)
                logger.info(
                    f"[PolicyErrorHandler] Removed last {removed_count} messages "
                    f"from session: {cli_session_id}"
                )

            return success

        except Exception as e:
            logger.error(
                f"[PolicyErrorHandler] Error removing last message chain: {e}"
            )
            return False

    def extract_error_info(self, response_text: str) -> Dict[str, Any]:
        """
        从响应文本中提取错误信息

        Args:
            response_text: 响应文本

        Returns:
            错误信息字典
        """
        info = {
            "is_policy_error": False,
            "markers_found": [],
            "error_type": None,
        }

        if not response_text:
            return info

        # 检查策略错误标记
        for marker in self.POLICY_MARKERS:
            if marker.lower() in response_text.lower():
                info["is_policy_error"] = True
                info["markers_found"].append(marker)

        # 尝试识别具体错误类型
        if "Usage Policy" in response_text:
            info["error_type"] = "usage_policy_violation"
        elif "unable to respond" in response_text.lower():
            info["error_type"] = "content_refusal"
        elif "safety" in response_text.lower():
            info["error_type"] = "safety_filter"

        return info

    async def pre_check_message(self, message: str) -> Dict[str, Any]:
        """
        预检查用户消息（可选功能，用于提前警告）

        注意：这只是一个简单的关键词检查，不能替代 Claude 的内容审核

        Args:
            message: 用户消息

        Returns:
            检查结果
        """
        # 这里可以添加一些简单的预检查逻辑
        # 但不建议依赖此功能，因为真正的内容审核由 Claude API 完成
        return {
            "passed": True,
            "warnings": [],
        }
