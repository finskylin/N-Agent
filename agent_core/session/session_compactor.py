"""
SessionCompactConfig — 从 agent_core/config/session_compact.json 加载 session 压缩参数

供 context_manager、session_rebuilder、context_window_guard、progressive_summarizer 使用。
"""
import json
from pathlib import Path
from typing import List, Dict, Optional, Any

from loguru import logger


class SessionCompactConfig:
    """Session Compact 配置加载器 — 从 agent_core/config/session_compact.json 读取"""

    _instance: Optional["SessionCompactConfig"] = None
    _config: Dict[str, Any] = {}

    def __init__(self, config_path: Optional[str] = None):
        if config_path:
            self._config_path = Path(config_path)
        else:
            # 自动定位 agent_core/config/session_compact.json
            self._config_path = self._find_config_path()
        self._load()

    @classmethod
    def get_instance(cls, config_path: Optional[str] = None) -> "SessionCompactConfig":
        """单例获取"""
        if cls._instance is None:
            cls._instance = cls(config_path)
        return cls._instance

    @classmethod
    def reset(cls):
        """重置单例（测试用）"""
        cls._instance = None
        cls._config = {}

    def _find_config_path(self) -> Path:
        """自动定位配置文件"""
        # 从当前文件位置向上寻找项目根目录
        current = Path(__file__).resolve()
        for parent in current.parents:
            candidate = parent / "config" / "session_compact.json"
            if candidate.exists():
                return candidate
        # 最终回退
        return Path("/app/agent_core/config/session_compact.json")

    def _load(self):
        """加载配置文件"""
        try:
            if self._config_path.exists():
                with open(self._config_path, "r", encoding="utf-8") as f:
                    self._config = json.load(f)
                logger.info(
                    f"[SessionCompactConfig] Loaded from {self._config_path}"
                )
            else:
                logger.warning(
                    f"[SessionCompactConfig] Config not found: {self._config_path}, "
                    f"using empty defaults"
                )
                self._config = {}
        except Exception as e:
            logger.error(f"[SessionCompactConfig] Failed to load config: {e}")
            self._config = {}

    def reload(self):
        """重新加载配置"""
        self._load()

    # --- detection 配置 ---

    @property
    def compaction_block_type(self) -> str:
        return self._config.get("detection", {}).get(
            "compaction_block_type", "compaction"
        )

    @property
    def recent_threshold_messages(self) -> int:
        return self._config.get("detection", {}).get(
            "recent_threshold_messages", 10
        )

    # --- trigger 配置 ---

    @property
    def max_file_size_bytes(self) -> int:
        return self._config.get("trigger", {}).get(
            "max_file_size_bytes", 153600
        )

    @property
    def max_message_count(self) -> int:
        return self._config.get("trigger", {}).get(
            "max_message_count", 100
        )

    # --- compact_strategy 配置 ---

    @property
    def keep_recent_qa_pairs(self) -> int:
        return self._config.get("compact_strategy", {}).get(
            "keep_recent_qa_pairs", 5
        )

    @property
    def summary_max_chars(self) -> int:
        return self._config.get("compact_strategy", {}).get(
            "summary_max_chars", 2000
        )

    @property
    def tool_result_max_chars(self) -> int:
        return self._config.get("compact_strategy", {}).get(
            "tool_result_max_chars", 500
        )

    @property
    def tool_use_keep_fields(self) -> List[str]:
        return self._config.get("compact_strategy", {}).get(
            "tool_use_keep_fields", ["name", "type"]
        )

    # --- token_limit_error_markers ---

    @property
    def token_limit_error_markers(self) -> List[str]:
        return self._config.get("token_limit_error_markers", [])

    def is_token_limit_error(self, text: str) -> bool:
        """检测文本是否包含 token 超限错误标记"""
        return any(marker in text for marker in self.token_limit_error_markers)

    # --- conversation_history 配置 ---

    @property
    def conv_max_raw_messages(self) -> int:
        return self._config.get("conversation_history", {}).get(
            "max_raw_messages", 15
        )

    @property
    def conv_keep_recent(self) -> int:
        return self._config.get("conversation_history", {}).get(
            "keep_recent", 5
        )

    @property
    def conv_max_total(self) -> int:
        return self._config.get("conversation_history", {}).get(
            "max_total", 30
        )

    # --- rebuild 配置 ---

    @property
    def max_rebuild_size_bytes(self) -> int:
        # 优先从 profiles.rebuild 读取
        rebuild_profile = self.profile_config("rebuild")
        if rebuild_profile and "max_rebuild_size_bytes" in rebuild_profile:
            return rebuild_profile["max_rebuild_size_bytes"]
        return self._config.get("rebuild", {}).get(
            "max_rebuild_size_bytes", 153600
        )

    @property
    def rebuild_keep_recent_qa_pairs(self) -> int:
        return self._config.get("rebuild", {}).get(
            "keep_recent_qa_pairs", 5
        )

    @property
    def rebuild_summary_max_chars(self) -> int:
        return self._config.get("rebuild", {}).get(
            "summary_max_chars", 2000
        )

    @property
    def rebuild_tool_result_max_chars(self) -> int:
        return self._config.get("rebuild", {}).get(
            "tool_result_max_chars", 500
        )

    @property
    def rebuild_min_message_threshold(self) -> int:
        """重建时的最小消息数阈值，低于此值继续查询下层数据源"""
        rebuild_profile = self.profile_config("rebuild")
        if rebuild_profile and "min_message_threshold" in rebuild_profile:
            return rebuild_profile["min_message_threshold"]
        return self._config.get("rebuild", {}).get("min_message_threshold", 5)

    # --- 统一压缩内核配置 (MessageCompressEngine) ---

    @property
    def compress_engine_config(self) -> Dict:
        """返回 compress_engine 配置段"""
        return self._config.get("compress_engine", {
            "conclusion_field_names": [
                "summary", "conclusion", "recommendation",
                "总结", "结论", "建议", "操作建议",
            ],
            "tool_use_keep_fields": ["name", "type"],
            "user_message_strategy": "keep_all",
        })

    def profile_config(self, profile_name: str) -> Dict:
        """
        获取指定档位的配置。

        Args:
            profile_name: "pre_request" | "threshold_compact" | "rebuild"

        Returns:
            档位配置 dict
        """
        profiles = self._config.get("profiles", {})
        return profiles.get(profile_name, {})

    @property
    def pre_request_enabled(self) -> bool:
        return self.profile_config("pre_request").get("enabled", True)

    @property
    def pre_request_min_file_size(self) -> int:
        return self.profile_config("pre_request").get(
            "min_file_size_to_trigger", 20480
        )

    # --- storage 配置 ---

    @property
    def storage_backend(self) -> str:
        return self._config.get("storage", {}).get("backend", "sqlite")

    @property
    def sqlite_config(self) -> Dict:
        return self._config.get("storage", {}).get("sqlite", {})

    @property
    def cache_fallback_enabled(self) -> bool:
        return self._config.get("storage", {}).get("cache_fallback", True)

    # --- context_window 配置 ---

    @property
    def context_window_config(self) -> Dict:
        return self._config.get("context_window", {})

    # --- progressive_summarize 配置 ---

    @property
    def progressive_summarize_enabled(self) -> bool:
        return self._config.get("progressive_summarize", {}).get("enabled", True)

    @property
    def progressive_summarize_config(self) -> Dict:
        return self._config.get("progressive_summarize", {})


