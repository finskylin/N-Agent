"""
Context Window Guard — Token 预算分配器

职责:
1. 解析当前模型的上下文窗口大小（config override → model lookup → 默认 200K）
2. 按比例分配 Token 预算给各组件:
   - session_file: .jsonl 文件预算（替代硬编码 153600 字节阈值）
   - system_prompt: System Prompt 预算（工具描述 + 知识库 + fallback history）
   - experience: 经验知识预算（始终注入）
   - generation_reserve: 为模型生成预留
3. 轻量级 Token 估算（不依赖 tiktoken）

Core 版本：不依赖 V4Config / SessionCompactConfig，
由调用方传入 context_window / budget_ratios 等参数。
"""
import re
from dataclasses import dataclass, field
from typing import Dict, Optional

from loguru import logger


# ── 常量 ──

CONTEXT_WINDOW_HARD_MIN = 16_000
CONTEXT_WINDOW_WARN_BELOW = 32_000
DEFAULT_CONTEXT_WINDOW = 200_000

# Token → 字节近似系数（1 token ≈ 4 bytes UTF-8 平均）
TOKEN_TO_BYTES_RATIO = 4

# 默认预算比例
DEFAULT_BUDGET_RATIOS = {
    "session_file": 0.50,
    "system_prompt": 0.25,
    "memory": 0.0,
    "experience": 0.10,
    "generation_reserve": 0.15,
}

# 默认 memory 子比例
DEFAULT_MEMORY_SUB_RATIOS = {
    "mtm_recall": 0.50,
    "user_profile": 0.15,
    "knowledge": 0.35,
}

# Token 估算正则
_CJK_RANGE = re.compile(
    r'[\u4e00-\u9fff\u3400-\u4dbf\u3000-\u303f\uff00-\uffef'
    r'\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]'
)


@dataclass
class ContextBudget:
    """Token 预算分配结果"""
    total_tokens: int
    session_file_budget: int       # .jsonl 文件预算（字节）
    system_prompt_budget: int      # System Prompt 预算（token）
    memory_budget: int = 0         # 记忆召回预算（token，MemoryOS 扩展时分配）
    experience_budget: int = 0     # 经验知识预算（token）
    generation_reserve: int = 0    # 为模型生成预留（token）
    source: str = "default"        # 预算来源标识（config / model / default）

    @property
    def history_budget(self) -> int:
        """Fallback 路径的 history + summary 预算（token）"""
        return self.system_prompt_budget

    @property
    def compact_trigger_bytes(self) -> int:
        """触发 .jsonl 压缩的字节阈值（替代硬编码 153600）"""
        return self.session_file_budget


class ContextWindowGuard:
    """
    上下文窗口管控器

    统一管控所有进入 LLM 的内容的 Token 预算:
    - .jsonl 文件大小（resume 路径）
    - system_prompt Token（fallback + 始终注入）

    Core 版本：不依赖 V4Config / SessionCompactConfig。
    调用方通过构造参数传入模型名称、context_window 覆盖、budget 比例等。
    """

    # 已知模型的上下文窗口大小
    MODEL_CONTEXT_WINDOWS = {
        "claude-sonnet-4-20250514": 200_000,
        "claude-opus-4-20250514": 200_000,
        "claude-haiku-3-20250307": 200_000,
        "claude-3-5-sonnet-20241022": 200_000,
        "claude-3-5-haiku-20241022": 200_000,
        "claude-3-opus-20240229": 200_000,
    }

    def __init__(
        self,
        model_name: str = "",
        context_window_override: int = 0,
        budget_ratios: Optional[Dict[str, float]] = None,
        memory_sub_ratios: Optional[Dict[str, float]] = None,
    ):
        """
        Args:
            model_name: 模型名称，用于查表获取上下文窗口大小
            context_window_override: > 0 时强制使用该值作为上下文窗口
            budget_ratios: 预算分配比例覆盖（key 同 DEFAULT_BUDGET_RATIOS）
            memory_sub_ratios: memory 内部子比例覆盖
        """
        self._model_name = model_name
        self._context_window_override = context_window_override
        self._budget_ratios = budget_ratios
        self._memory_sub_ratios = memory_sub_ratios

    def resolve_context_window(self) -> int:
        """
        解析当前模型的上下文窗口大小

        优先级:
        1. context_window_override（> 0 时使用）
        2. LLMEndpointProvider 当前活跃 endpoint 声明的 context_window
        3. 按 model 名称查表
        4. DEFAULT_CONTEXT_WINDOW (200K)
        """
        # 1. override
        if self._context_window_override and self._context_window_override > 0:
            override = self._context_window_override
            if override < CONTEXT_WINDOW_HARD_MIN:
                logger.warning(
                    f"[ContextWindowGuard] Override {override} below hard min "
                    f"{CONTEXT_WINDOW_HARD_MIN}, clamping up"
                )
                override = CONTEXT_WINDOW_HARD_MIN
            return override

        # 2. endpoint 声明值
        try:
            from agent_core.agentloop.llm_provider import LLMEndpointProvider
            ep_window = LLMEndpointProvider.get_instance().get_active_context_window()
            if ep_window and ep_window > 0:
                window = max(ep_window, CONTEXT_WINDOW_HARD_MIN)
                logger.debug(
                    f"[ContextWindowGuard] Using endpoint context_window={window}"
                )
                return window
        except Exception as e:
            logger.debug(f"[ContextWindowGuard] Endpoint lookup failed: {e}")

        # 3. model lookup
        model = self._model_name or ""
        for model_prefix, window in self.MODEL_CONTEXT_WINDOWS.items():
            if model.startswith(model_prefix) or model_prefix.startswith(model):
                return window

        # 4. default
        return DEFAULT_CONTEXT_WINDOW

    def allocate_budget(self) -> ContextBudget:
        """
        根据上下文窗口大小分配 Token 预算

        session_file_budget 以字节表示（token * TOKEN_TO_BYTES_RATIO），
        这样可以直接与 SessionCompactor 的文件大小阈值对接。
        """
        total = self.resolve_context_window()
        ratios = self._load_ratios()

        session_file_tokens = int(total * ratios.get("session_file", 0.50))
        session_file_bytes = session_file_tokens * TOKEN_TO_BYTES_RATIO

        system_prompt_tokens = int(total * ratios.get("system_prompt", 0.25))
        memory_tokens = int(total * ratios.get("memory", 0.0))
        experience_tokens = int(total * ratios.get("experience", 0.10))
        generation_tokens = int(total * ratios.get("generation_reserve", 0.15))

        source = "config" if self._has_config_override() else "model"

        budget = ContextBudget(
            total_tokens=total,
            session_file_budget=session_file_bytes,
            system_prompt_budget=system_prompt_tokens,
            memory_budget=memory_tokens,
            experience_budget=experience_tokens,
            generation_reserve=generation_tokens,
            source=source,
        )

        if total < CONTEXT_WINDOW_WARN_BELOW:
            logger.warning(
                f"[ContextWindowGuard] Context window {total} is below "
                f"warning threshold {CONTEXT_WINDOW_WARN_BELOW}"
            )

        logger.debug(
            f"[ContextWindowGuard] Budget allocated: total={total}, "
            f"session_file={session_file_bytes}B, "
            f"system_prompt={system_prompt_tokens}T, "
            f"experience={experience_tokens}T, "
            f"generation={generation_tokens}T, "
            f"source={source}"
        )

        return budget

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """
        估算文本 Token 数量（轻量级，不依赖 tiktoken）

        算法:
        - CJK 字符：每字 ≈ 0.6 token
        - 非 CJK 字符（英文/数字/符号）：按空格分词，每词 ≈ 1.3 token
        - 混合文本：按字符类型分别统计后加总
        """
        if not text:
            return 0

        cjk_chars = len(_CJK_RANGE.findall(text))
        non_cjk_text = _CJK_RANGE.sub('', text)
        # 非 CJK 部分按词估算
        non_cjk_words = len(non_cjk_text.split()) if non_cjk_text.strip() else 0

        cjk_tokens = int(cjk_chars * 0.6)
        non_cjk_tokens = int(non_cjk_words * 1.3)

        return max(cjk_tokens + non_cjk_tokens, 1) if text.strip() else 0

    def allocate_memory_sub_budget(
        self, memory_total: int,
    ) -> Dict[str, int]:
        """
        将 memory 总预算按子比例分配

        Args:
            memory_total: memory 总 token 预算

        Returns:
            {"mtm_recall": int, "user_profile": int, "knowledge": int}
        """
        sub_ratios = self._load_memory_sub_ratios()
        return {
            key: int(memory_total * ratio)
            for key, ratio in sub_ratios.items()
        }

    def _load_ratios(self) -> Dict[str, float]:
        """
        加载预算分配比例

        优先级:
        1. 构造参数 budget_ratios
        2. DEFAULT_BUDGET_RATIOS
        """
        ratios = dict(DEFAULT_BUDGET_RATIOS)
        if self._budget_ratios:
            for key in ratios:
                if key in self._budget_ratios:
                    ratios[key] = float(self._budget_ratios[key])
        return ratios

    def _load_memory_sub_ratios(self) -> Dict[str, float]:
        """
        加载 memory 预算内部分配比例
        """
        sub = dict(DEFAULT_MEMORY_SUB_RATIOS)
        if self._memory_sub_ratios:
            for key in sub:
                if key in self._memory_sub_ratios:
                    sub[key] = float(self._memory_sub_ratios[key])
        return sub

    def _has_config_override(self) -> bool:
        """检查是否有 config 级别的覆盖"""
        if self._context_window_override and self._context_window_override > 0:
            return True
        if self._budget_ratios:
            return True
        return False
