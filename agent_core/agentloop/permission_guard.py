"""
PermissionGuard — 工具权限管控系统

在工具执行前检查权限模式，支持四种模式:
- AUTO:    自动执行（默认）— 所有工具自动放行，无额外开销
- CONFIRM: 需用户确认（预留，Phase 4 暂不实现双向通信）
- DENY:    拒绝执行
- HOOK:    通过 hook 决定（调用 ON_PERMISSION_CHECK hook）

当前阶段只实现 AUTO 和 DENY，CONFIRM/HOOK 预留接口。
默认 permission_guard_enabled=False，启用后 AUTO 模式无额外开销。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional
from loguru import logger


class PermissionMode(str, Enum):
    """权限模式枚举"""
    AUTO = "auto"       # 自动执行（默认）
    CONFIRM = "confirm" # 需用户确认（预留）
    DENY = "deny"       # 拒绝执行
    HOOK = "hook"       # 通过 hook 决定


@dataclass
class PermissionDecision:
    """权限检查结果"""
    allowed: bool
    reason: str = ""
    mode: PermissionMode = PermissionMode.AUTO


class PermissionGuard:
    """
    工具权限管控 — opt-in 设计

    用法:
        guard = PermissionGuard(
            default_mode=PermissionMode.AUTO,
            enabled=True,
        )
        # 对特定工具设置拒绝
        guard.set_override("dangerous_tool", PermissionMode.DENY)

        # 在工具执行前检查
        decision = await guard.check(tool_name, tool_input, hook_engine)
        if not decision.allowed:
            return error_result(decision.reason)
    """

    def __init__(
        self,
        default_mode: PermissionMode = PermissionMode.AUTO,
        enabled: bool = False,
    ):
        self._default_mode = default_mode
        self._enabled = enabled
        # 工具名 → 运行时覆盖模式
        self._overrides: Dict[str, PermissionMode] = {}

    def set_override(self, tool_name: str, mode: PermissionMode) -> None:
        """设置特定工具的权限模式覆盖"""
        self._overrides[tool_name] = mode
        logger.info(f"[PermissionGuard] Set override: {tool_name} → {mode.value}")

    def remove_override(self, tool_name: str) -> None:
        """移除特定工具的权限覆盖"""
        self._overrides.pop(tool_name, None)

    def get_mode(
        self,
        tool_name: str,
        skill_meta=None,  # SkillMetadata（可选）
    ) -> PermissionMode:
        """获取工具的有效权限模式"""
        # 运行时覆盖优先
        if tool_name in self._overrides:
            return self._overrides[tool_name]
        # 默认模式
        return self._default_mode

    async def check(
        self,
        tool_name: str,
        tool_input: dict,
        hook_engine=None,   # HookEngine（可选，用于 HOOK 模式）
    ) -> PermissionDecision:
        """
        执行权限检查

        Returns:
            PermissionDecision(allowed, reason)
        """
        if not self._enabled:
            return PermissionDecision(allowed=True, mode=PermissionMode.AUTO)

        mode = self.get_mode(tool_name)

        if mode == PermissionMode.AUTO:
            return PermissionDecision(allowed=True, mode=mode)

        elif mode == PermissionMode.DENY:
            reason = f"Tool '{tool_name}' is denied by permission policy"
            logger.info(f"[PermissionGuard] DENY: {tool_name}")
            return PermissionDecision(allowed=False, reason=reason, mode=mode)

        elif mode == PermissionMode.HOOK:
            # 通过 ON_PERMISSION_CHECK hook 决定
            if hook_engine is not None:
                try:
                    from .hook_engine import HookPoint
                    ctx = await hook_engine.fire(HookPoint.ON_PERMISSION_CHECK, {
                        "tool_name": tool_name,
                        "tool_input": tool_input,
                        "mode": mode.value,
                    })
                    hook_decision = ctx.get("decision", "allow")
                    if hook_decision == "deny":
                        reason = ctx.get("reason", f"Tool '{tool_name}' denied by hook")
                        return PermissionDecision(allowed=False, reason=reason, mode=mode)
                    return PermissionDecision(allowed=True, mode=mode)
                except Exception as e:
                    logger.warning(f"[PermissionGuard] HOOK check failed: {e}, defaulting to ALLOW")
                    return PermissionDecision(allowed=True, mode=mode)
            # 无 hook_engine，降级为 AUTO
            return PermissionDecision(allowed=True, mode=PermissionMode.AUTO)

        elif mode == PermissionMode.CONFIRM:
            # TODO: Phase 4 后续实现双向通信（需要 SSE 反向通道）
            # 当前降级为 AUTO（自动放行）
            logger.debug(
                f"[PermissionGuard] CONFIRM mode for '{tool_name}' not yet implemented, "
                f"falling back to AUTO"
            )
            return PermissionDecision(allowed=True, mode=PermissionMode.AUTO)

        # 未知模式，默认放行
        return PermissionDecision(allowed=True, mode=mode)
