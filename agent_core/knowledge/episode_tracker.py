"""
Episode Tracker — 执行轨迹采集

在请求生命周期中收集 Skill 执行数据、置信度评估、用户反馈。
通过 PostToolUse Hook 挂载，不修改核心流程。
"""
import asyncio
import copy
import re
import time
from typing import List, Dict, Optional, Any

from loguru import logger

from .models import Episode, SkillExecution

# 敏感字段正则
_SENSITIVE_KEYS = re.compile(
    r"(api_key|password|token|secret|credential|auth)", re.IGNORECASE
)


class EpisodeTracker:
    """执行轨迹采集器 — 请求级别实例"""

    def __init__(self, config: dict):
        self._config = config.get("episode_tracker", {})
        self._skill_executions: List[SkillExecution] = []
        self._feedback: Optional[Dict] = None
        self._start_time: float = time.time()
        self._query: str = ""

        # 下游模块（由外部注入，可选）
        self._distiller = None
        self._reflection = None
        self._temporal_manager = None

    def set_downstream(self, distiller=None, reflection=None, temporal_manager=None):
        """注入蒸馏器、反思引擎、时序管理器"""
        self._distiller = distiller
        self._reflection = reflection
        self._temporal_manager = temporal_manager

    def set_query(self, query: str):
        """设置用户查询"""
        self._query = query

    def record_skill_execution(
        self, skill_name: str, params: Any, result: Any,
        duration_ms: float, success: bool,
    ):
        """
        记录单次 Skill 执行。
        参数脱敏 + 结果截断。
        """
        # 参数脱敏
        safe_params = self._sanitize_params(params) if isinstance(params, dict) else {}
        params_max = self._config.get("params_max_chars", 500)
        params_str = str(safe_params)
        if len(params_str) > params_max:
            params_str = params_str[:params_max] + "..."

        # 结果截断
        result_max = self._config.get("result_summary_max_chars", 200)
        result_summary = self._truncate(str(result), result_max)

        execution = SkillExecution(
            skill_name=skill_name,
            params=safe_params,
            result_summary=result_summary,
            duration_ms=duration_ms,
            success=success,
        )
        self._skill_executions.append(execution)
        logger.debug(
            f"[EpisodeTracker] Recorded: {skill_name}, "
            f"success={success}, duration={duration_ms:.0f}ms"
        )

    def record_feedback(self, feedback_type: str, feedback_data: dict):
        """记录用户反馈"""
        self._feedback = {"type": feedback_type, "data": feedback_data}

    def record_conversation_context(
        self, assistant_response: str = "", conversation_context: str = "",
    ):
        """记录对话上下文（用于思维链提取，不持久化到 Episode 表）"""
        self._assistant_response = assistant_response
        self._conversation_context = conversation_context

    async def finalize(
        self, store, user_id: int, instance_id: str, session_id: str = "",
        loop_normal_exit: bool = True,
    ) -> Episode:
        """
        收尾: 构建 Episode，写入 SQLite，更新 SkillProfile。

        success 判断（方案D）：
          - loop 正常结束（非超时/异常中断）
          - 且 assistant_response 长度 >= success_min_response_chars（默认 500）
        工具调用的单次成功率继续由 SkillProfile 维护，与 episode success 解耦。
        """
        total_duration = (time.time() - self._start_time) * 1000
        min_chars = self._config.get("success_min_response_chars", 500)
        assistant_response = getattr(self, "_assistant_response", "")
        success = loop_normal_exit and len(assistant_response) >= min_chars

        episode = Episode(
            user_id=user_id,
            instance_id=instance_id,
            session_id=session_id,
            query=self._query,
            skill_executions=list(self._skill_executions),
            feedback=self._feedback,
            total_duration_ms=total_duration,
            success=success,
            assistant_response=getattr(self, "_assistant_response", ""),
            conversation_context=getattr(self, "_conversation_context", ""),
        )

        # 写入 Episode
        await store.save_episode(episode)

        # 更新 Skill Profiles
        for se in self._skill_executions:
            await store.update_skill_profile(
                skill_name=se.skill_name,
                user_id=user_id,
                instance_id=instance_id,
                duration_ms=se.duration_ms,
                success=se.success,
                confidence=0.0,
            )

        logger.info(
            f"[EpisodeTracker] Finalized: episode={episode.episode_id}, "
            f"skills={len(self._skill_executions)}, success={success}"
        )

        # ── 下游触发：蒸馏 + 反思（失败不影响主流程） ──

        # 1. 知识蒸馏
        if self._distiller and self._skill_executions:
            try:
                await self._distiller.distill(
                    episode=episode,
                    user_id=user_id,
                    instance_id=instance_id,
                    temporal_manager=self._temporal_manager,
                )
                logger.info(f"[EpisodeTracker] Distill completed for {episode.episode_id}")
            except Exception as e:
                logger.warning(f"[EpisodeTracker] Distill failed (non-fatal): {e}")

        # 2. 记录成功/失败 + 触发反思
        if self._reflection:
            try:
                if success:
                    self._reflection.record_success()
                else:
                    self._reflection.record_failure()
                result = await self._reflection.maybe_reflect(
                    user_id=user_id,
                    instance_id=instance_id,
                )
                if result:
                    logger.info(f"[EpisodeTracker] Reflection produced {len(result)} knowledge units")
            except Exception as e:
                logger.warning(f"[EpisodeTracker] Reflection failed (non-fatal): {e}")

        return episode

    def reset(self):
        """重置状态，用于下次请求"""
        self._skill_executions = []
        self._feedback = None
        self._start_time = time.time()
        self._query = ""
        self._assistant_response = ""
        self._conversation_context = ""

    @property
    def skill_count(self) -> int:
        return len(self._skill_executions)

    @property
    def has_failures(self) -> bool:
        return any(not se.success for se in self._skill_executions)

    @staticmethod
    def _sanitize_params(params: dict) -> dict:
        """深拷贝并掩码敏感字段"""
        safe = copy.deepcopy(params)
        for key in list(safe.keys()):
            if _SENSITIVE_KEYS.search(key):
                safe[key] = "***"
            elif isinstance(safe[key], dict):
                safe[key] = EpisodeTracker._sanitize_params(safe[key])
        return safe

    @staticmethod
    def _truncate(text: str, max_chars: int) -> str:
        """截断文本"""
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "..."
