"""
V4 Config -- Docker 模式 wrapper

继承 agent_core.config.V4Config，增加 from_settings() 自动注入 app.config.settings。
App 层负责将 Settings 对象展开为 dict，调用 AgentCore 的 from_dict()。
"""
import os
from pathlib import Path
from agent_core.config import V4Config as _CoreV4Config, _to_bool  # noqa: F401


class V4Config(_CoreV4Config):
    """V4 Agent 配置（Docker 模式 — 自动读取 app.config.settings）"""

    @classmethod
    def from_settings(cls, settings=None) -> "V4Config":
        """从 app.config.settings 构建 V4Config

        如果 settings 未传入，自动从 app.config 导入。
        """
        if settings is None:
            from app.config import settings  # noqa: PLC0415

        project_root = str(Path(__file__).parent.parent.parent.parent.absolute())
        skills_dir = str(Path(project_root) / ".claude" / "skills")

        d = {
            "project_root": project_root,
            "skills_dir": skills_dir,
            # Anthropic
            "anthropic_auth_token": getattr(settings, "anthropic_auth_token", "") or "",
            "anthropic_base_url": getattr(settings, "anthropic_base_url", "") or "",
            "anthropic_model": getattr(settings, "anthropic_model", "") or "",
            "anthropic_small_fast_model": getattr(settings, "anthropic_small_fast_model", "") or "",
            "anthropic_auth_token_backup": getattr(settings, "anthropic_auth_token_backup", "") or "",
            "anthropic_base_url_backup": getattr(settings, "anthropic_base_url_backup", "") or "",
            "anthropic_model_backup": getattr(settings, "anthropic_model_backup", "") or "",
            "anthropic_auth_token_c": getattr(settings, "anthropic_auth_token_c", "") or "",
            "anthropic_base_url_c": getattr(settings, "anthropic_base_url_c", "") or "",
            "anthropic_model_c": getattr(settings, "anthropic_model_c", "") or "",
            "anthropic_small_fast_model_c": getattr(settings, "anthropic_small_fast_model_c", "") or "",
            # Experience
            "experience_ttl": getattr(settings, "v4_experience_ttl", 3600 * 24 * 30),
            "conversation_history_ttl": getattr(settings, "v4_conversation_history_ttl", 3600 * 24 * 7),
            "conversation_summary_ttl": getattr(settings, "v4_conversation_summary_ttl", 3600 * 24 * 7),
            "experience_min_user_msg_length": getattr(settings, "v4_experience_min_user_msg_length", 10),
            "experience_min_assistant_msg_length": getattr(settings, "v4_experience_min_assistant_msg_length", 50),
            "experience_max_preferences": getattr(settings, "v4_experience_max_preferences", 15),
            "experience_max_insights": getattr(settings, "v4_experience_max_insights", 30),
            "experience_max_patterns": getattr(settings, "v4_experience_max_patterns", 15),
            "experience_max_corrections": getattr(settings, "v4_experience_max_corrections", 15),
            # General
            "setting_sources": getattr(settings, "v4_setting_sources", "project") or "project",
            "session_fallback_enabled": getattr(settings, "v4_session_fallback_enabled", True),
            "default_user_id": getattr(settings, "v4_default_user_id", 1),
            # MemoryOS
            "memory_engine_enabled": getattr(settings, "v4_memory_engine_enabled",
                _to_bool(os.environ.get("MEMORY_ENGINE_ENABLED", "false"))),
            # AgentLoop
            "max_iterations": int(os.environ.get("V4_MAX_ITERATIONS", getattr(settings, "v4_max_iterations", 100))),
            "max_timeout_seconds": int(os.environ.get("V4_MAX_TIMEOUT_SECONDS", getattr(settings, "v4_max_timeout_seconds", 3600))),
            # Parallel
            "parallel_tool_execution": getattr(settings, "v4_parallel_tool_execution", True),
            "parallel_max_concurrent": getattr(settings, "v4_parallel_max_concurrent", 8),
            # Compaction
            "context_compaction_enabled": getattr(settings, "v4_context_compaction_enabled", True),
            "context_compaction_threshold": getattr(settings, "v4_context_compaction_threshold", 0.70),
            "context_compaction_keep_recent": getattr(settings, "v4_context_compaction_keep_recent", 6),
            # Permission
            "permission_guard_enabled": getattr(settings, "v4_permission_guard_enabled", False),
            # SubAgent
            "subagent_enabled": getattr(settings, "v4_subagent_enabled", True),
            "subagent_max_depth": getattr(settings, "v4_subagent_max_depth", 3),
            "subagent_max_iterations": int(os.environ.get("V4_SUBAGENT_MAX_ITERATIONS", getattr(settings, "v4_subagent_max_iterations", 100))),
            "bg_subagent_max_iterations": int(os.environ.get("V4_BG_SUBAGENT_MAX_ITERATIONS", 0)),
            "bg_subagent_max_timeout_seconds": int(os.environ.get("V4_BG_SUBAGENT_MAX_TIMEOUT_SECONDS", 18000)),
            # MCP / Sandbox
            "mcp_tools_enabled": getattr(settings, "v4_mcp_tools_enabled", False),
            "sandbox_provider": getattr(settings, "v4_sandbox_provider", "auto") or "auto",
            "sandbox_runtime_bin": getattr(settings, "v4_sandbox_runtime_bin", "docker") or "docker",
            "sandbox_image": getattr(settings, "v4_sandbox_image", "") or "",
            "sandbox_work_dir": getattr(settings, "v4_sandbox_work_dir", "") or "",
            "sandbox_project_mount_readonly": getattr(settings, "v4_sandbox_project_mount_readonly", True),
            "sandbox_network_mode": getattr(settings, "v4_sandbox_network_mode", "bridge") or "bridge",
            "sandbox_container_project_root": getattr(settings, "v4_sandbox_container_project_root", "/workspace/project") or "/workspace/project",
            "sandbox_container_workdir": getattr(settings, "v4_sandbox_container_workdir", "/workspace/workdir") or "/workspace/workdir",
            "sandbox_settings_path": getattr(settings, "v4_sandbox_settings_path", "") or "",
            "sandbox_check_runtime": getattr(settings, "v4_sandbox_check_runtime", True),
            # Ring 2: Skill 进化 (DGM Patch)
            "skill_evolution_enabled": _to_bool(os.environ.get("SKILL_EVOLUTION_ENABLED", "false")),
            "skill_evolution_error_threshold": float(os.environ.get("SKILL_EVOLUTION_ERROR_THRESHOLD", "0.3")),
            "skill_evolution_min_calls": int(os.environ.get("SKILL_EVOLUTION_MIN_CALLS", "5")),
            "skill_evolution_window_days": int(os.environ.get("SKILL_EVOLUTION_WINDOW_DAYS", "7")),
            "skill_evolution_patch_per_day": int(os.environ.get("SKILL_EVOLUTION_PATCH_PER_DAY", "1")),
            # Ring 3: 能力盲区检测
            "capability_gap_detection_enabled": _to_bool(os.environ.get("CAPABILITY_GAP_DETECTION_ENABLED", "true")),
            "capability_gap_trigger_threshold": int(os.environ.get("CAPABILITY_GAP_TRIGGER_THRESHOLD", "3")),
            "capability_gap_cooldown_hours": int(os.environ.get("CAPABILITY_GAP_COOLDOWN_HOURS", "24")),
        }

        return cls.from_dict(d)
