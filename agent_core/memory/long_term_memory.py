"""
Long-Term Memory (LTM) — 用户画像存储

职责:
- 维护结构化用户画像（多维度）
- 通过 LLM 从对话中自动更新画像维度
- 格式化输出供 system_prompt 注入

存储: 通过 SessionContextDB 的 user_profiles 表操作
"""
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Callable, Awaitable, TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from agent_core.session.context_db import SessionContextDB

# 用户画像维度定义
PROFILE_DIMENSIONS = [
    "risk_appetite",          # 风险偏好: conservative / moderate / aggressive
    "investment_horizon",     # 投资周期: short_term / mid_term / long_term
    "preferred_sectors",      # 偏好行业/领域
    "analysis_style",         # 分析风格偏好: fundamental / technical / quantitative
    "data_preferences",       # 数据偏好: charts / tables / text
    "communication_style",    # 沟通风格: concise / detailed / academic
    "expertise_level",        # 专业水平: beginner / intermediate / expert
    "focus_topics",           # 关注话题
    "geographical_focus",     # 地理关注: domestic / global / specific_regions
    "frequency_pattern",      # 使用频率模式
    "decision_factors",       # 决策要素: valuation / momentum / news / policy
    "portfolio_context",      # 持仓背景（如有）
    "alert_preferences",      # 预警偏好
    "report_format",          # 报告格式偏好
    "language_preference",    # 语言偏好
]

# 维度中文标签（用于 prompt 注入）
DIMENSION_LABELS = {
    "risk_appetite": "风险偏好",
    "investment_horizon": "投资周期",
    "preferred_sectors": "偏好行业",
    "analysis_style": "分析风格",
    "data_preferences": "数据偏好",
    "communication_style": "沟通风格",
    "expertise_level": "专业水平",
    "focus_topics": "关注话题",
    "geographical_focus": "地理关注",
    "frequency_pattern": "使用频率",
    "decision_factors": "决策要素",
    "portfolio_context": "持仓背景",
    "alert_preferences": "预警偏好",
    "report_format": "报告格式",
    "language_preference": "语言偏好",
}


@dataclass
class UserProfile:
    """用户画像数据"""
    user_id: int = 0
    instance_id: str = ""
    dimensions: Dict[str, str] = field(default_factory=dict)
    update_count: int = 0
    last_updated_ts: int = 0

    def to_prompt_text(self, max_tokens: int = 0) -> str:
        """
        格式化为 system_prompt 注入文本

        Args:
            max_tokens: 最大 token 数（0 表示不限制）
        """
        if not self.dimensions:
            return ""

        lines = ["### 用户画像"]
        for key in PROFILE_DIMENSIONS:
            val = self.dimensions.get(key)
            if not val:
                continue
            label = DIMENSION_LABELS.get(key, key)
            if isinstance(val, list):
                val_str = ", ".join(str(v) for v in val)
            else:
                val_str = str(val)
            lines.append(f"- **{label}**: {val_str}")

        if len(lines) <= 1:
            return ""

        text = "\n".join(lines)

        # 粗略截断
        if max_tokens > 0:
            max_chars = max_tokens * 2  # CJK 粗略估算
            if len(text) > max_chars:
                text = text[:max_chars] + "\n...[画像截断]"

        return text


class UserProfileStore:
    """
    用户画像存储管理器

    通过 SessionContextDB 的 user_profiles 表操作。

    Args:
        sqlite_db: SessionContextDB 实例
        user_id: 用户 ID
        instance_id: 实例标识
    """

    def __init__(
        self,
        sqlite_db: "SessionContextDB",
        user_id: int,
        instance_id: str,
    ):
        self._db = sqlite_db
        self._user_id = user_id
        self._instance_id = instance_id

    async def get(self) -> UserProfile:
        """获取用户画像（不存在则返回空画像）"""
        data = await self._db.get_user_profile(
            self._user_id, self._instance_id,
        )
        if data:
            return UserProfile(
                user_id=self._user_id,
                instance_id=self._instance_id,
                dimensions=data.get("dimensions", {}),
                update_count=data.get("update_count", 0),
                last_updated_ts=data.get("updated_at", 0),
            )
        return UserProfile(
            user_id=self._user_id,
            instance_id=self._instance_id,
        )

    async def save(self, profile: UserProfile):
        """保存用户画像"""
        await self._db.save_user_profile(
            self._user_id,
            self._instance_id,
            profile.dimensions,
            profile.update_count,
        )

    async def update_from_conversation(
        self,
        user_msg: str,
        assistant_msg: str,
        update_fn: Callable[
            [str, str, Dict[str, str]], Awaitable[Dict[str, str]]
        ],
    ):
        """
        从对话中更新用户画像

        Args:
            user_msg: 用户消息
            assistant_msg: 助手回复
            update_fn: LLM 更新函数
                签名: (user_msg, assistant_msg, current_dims) → updated_dims
                仅返回需要更新的维度 key-value
        """
        try:
            current = await self.get()

            new_dims = await update_fn(
                user_msg, assistant_msg, current.dimensions,
            )

            if not new_dims or not isinstance(new_dims, dict):
                return

            # 仅合并 PROFILE_DIMENSIONS 内的 key
            merged = dict(current.dimensions)
            changed = False
            for key, val in new_dims.items():
                if key in PROFILE_DIMENSIONS and val:
                    if merged.get(key) != val:
                        merged[key] = val
                        changed = True

            if not changed:
                return

            current.dimensions = merged
            current.update_count += 1
            current.last_updated_ts = int(time.time())
            await self.save(current)

            logger.info(
                f"[LTM] Updated user profile "
                f"(updated keys: {list(new_dims.keys())}, "
                f"total updates: {current.update_count})"
            )
        except Exception as e:
            logger.warning(f"[LTM] Profile update failed: {e}")
