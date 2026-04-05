"""
V4 Config -- AgentCore 核心配置

从 app/agent/v4/v4_config.py 迁移核心配置。
支持两种初始化方式:
1. from_env() -- CLI 模式，纯 os.getenv + dotenv
2. from_settings(settings) -- Docker 模式，接收上层传入的 settings 对象
"""
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _to_bool(val) -> bool:
    """将字符串/布尔值转为 bool（兼容环境变量 'true'/'false' 字符串）"""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes")
    return bool(val)


@dataclass
class V4Config:
    """V4 Agent 配置"""

    # 技能目录（SKILL.md 所在根目录）
    skills_dir: str = ""

    # 热加载检测间隔（秒）
    reload_interval: float = 30.0

    # SDK 最大执行轮次
    max_turns: int = 20

    # 默认渲染模式: auto | text_only
    default_render_mode: str = "auto"

    # MCP Server 名称前缀
    mcp_server_prefix: str = "sthg_v4"

    # Anthropic API 配置
    anthropic_api_key: str = ""
    anthropic_base_url: str = ""
    anthropic_model: str = ""
    anthropic_small_fast_model: str = ""

    # Backup Anthropic API 配置（容灾切换）
    anthropic_api_key_backup: str = ""
    anthropic_base_url_backup: str = ""
    anthropic_model_backup: str = ""

    # C 端点 Anthropic API 配置（第三优先级端点）
    anthropic_api_key_c: str = ""
    anthropic_base_url_c: str = ""
    anthropic_model_c: str = ""
    anthropic_small_fast_model_c: str = ""

    # 项目根目录
    project_root: str = ""

    # === 经验/历史保存策略配置 ===
    experience_ttl: int = 3600 * 24 * 30
    conversation_history_ttl: int = 3600 * 24 * 7
    conversation_summary_ttl: int = 3600 * 24 * 7
    experience_min_user_msg_length: int = 10
    experience_min_assistant_msg_length: int = 50
    experience_max_preferences: int = 15
    experience_max_insights: int = 30
    experience_max_patterns: int = 15
    experience_max_corrections: int = 15

    # === 知识库 / CLAUDE.md 加载配置 ===
    setting_sources: str = "project"
    system_prompt_mode: str = "append"

    # === Session 管理配置 ===
    session_fallback_enabled: bool = True

    # === Persistence 配置 ===
    default_user_id: int = 1

    # === Session 管理 v2 配置 ===
    instance_id: str = ""
    claude_dir: str = "/root/.claude"
    session_rebuild_max_size: int = 180 * 1024
    policy_error_cleanup_enabled: bool = True
    session_auto_rebuild_enabled: bool = True

    # === 消息压缩配置 ===
    compress_user_msg_max_length: int = 2000
    compress_assistant_text_max_length: int = 500
    compress_tool_input_max_length: int = 200
    compress_tool_output_max_length: int = 500

    # === 智能搜索配置 ===
    intel_search_max_steps: int = 30
    intel_search_max_time: float = 600.0
    intel_search_max_depth: int = 5
    intel_search_max_downloads: int = 10
    intel_search_max_image_analyses: int = 15
    intel_search_zai_mcp_enabled: bool = True

    # === Session 上下文存储层配置 ===
    session_storage_backend: str = "sqlite"
    sqlite_db_path_template: str = "app/data/sessions/{instance_id}/memory.db"
    sqlite_wal_mode: bool = True
    sqlite_busy_timeout_ms: int = 30000     # 30秒，支持高并发写入场景

    # === 上下文窗口管控配置 ===
    context_window_override: int = 0
    context_budget_session_file: float = 0.0
    context_budget_system_prompt: float = 0.0
    context_budget_experience: float = 0.0
    context_budget_generation: float = 0.0

    # === 渐进式摘要配置 ===
    progressive_summarize_enabled: bool = True
    summarize_identity_preservation: bool = True

    # === MemoryOS 三层记忆配置 ===
    memory_engine_enabled: bool = True
    mtm_max_pages: int = 200
    mtm_heat_alpha: float = 1.0
    mtm_heat_beta: float = 0.5
    mtm_heat_gamma: float = 2.0
    mtm_heat_tau: float = 168.0
    mtm_max_age_days: int = 90
    ltm_promotion_threshold: float = 5.0
    user_profile_enabled: bool = True
    memory_budget_ratio: float = 0.10
    memory_cleanup_interval_hours: int = 24

    # === LLM 数据预算配置 ===
    llm_budget_enabled: bool = True
    llm_budget_max_chars: int = 150_000
    llm_budget_min_full_articles: int = 5
    llm_budget_max_full_articles: int = 15
    llm_budget_high_score_threshold: float = 60.0
    llm_budget_summary_max_chars: int = 800
    llm_budget_timeseries_max_records: int = 30
    llm_budget_strip_fields: str = "street_view_base64,boundary_geojson,geojson,screenshot_base64,base64,trace,tracks,polyline,areas,circles,tile_url,tiles,route_info"

    # === Knowledge Engine 配置 ===
    knowledge_engine_enabled: bool = True
    knowledge_engine_config_path: str = "agent_core/knowledge/knowledge_engine.json"
    knowledge_engine_retrieve_timeout_ms: int = 50

    # === 知识图谱配置 ===
    knowledge_graph_enabled: bool = True          # 是否启用图谱蒸馏和检索
    knowledge_graph_max_hops: int = 2             # BFS 最大跳数
    knowledge_graph_max_edges_in_prompt: int = 15 # 注入 prompt 的最大边数

    # === 预测验证配置 ===
    prediction_extraction_enabled: bool = True   # Stop Hook 中提取预测
    prediction_staleness_days: int = 30          # 图谱边陈旧阈值（天）
    prediction_verify_timeout: int = 30          # 验证超时（秒）


    # === AgentLoop 停止条件 ===
    max_iterations: int = 100              # 最大循环次数
    max_timeout_seconds: int = 3600        # 最大执行时间（秒），默认 60 分钟
    max_tokens_per_turn: int = 16384       # 每轮 LLM 最大输出 token 数
    loop_max_truncation_retry: int = 2     # max_tokens 截断续写上限
    loop_treat_unknown_stop_as_complete: bool = True  # 未知 stop_reason 按完成处理

    # === Phase 2: 并行工具执行配置 ===
    parallel_tool_execution: bool = True   # 是否启用并行工具执行
    parallel_max_concurrent: int = 8       # 最大并行数
    parallel_per_tool_timeout: float = 120.0  # 单工具并行超时（秒），应大于 skill 内部超时

    # === Phase 3: 上下文压缩配置 ===
    context_compaction_enabled: bool = True
    context_compaction_threshold: float = 0.70   # 达到预算 70% 时触发
    context_compaction_keep_recent: int = 6      # 保留最近 6 条不压缩

    # === Phase 4: 权限管控配置 ===
    permission_guard_enabled: bool = False   # 默认禁用（AUTO 模式无额外开销）

    # === Phase 6: SubAgent 子代理配置 ===
    subagent_enabled: bool = True
    subagent_max_depth: int = 3
    subagent_max_iterations: int = 100
    bg_subagent_max_iterations: int = 0              # 0 = 不限制迭代次数
    bg_subagent_max_timeout_seconds: int = 18000   # 异步后台子代理超时，默认 5 小时

    # === AutoDream: 周期性深度记忆整合配置 ===
    dream_enabled: bool = True                    # 是否启用 DreamConsolidator
    dream_interval_hours: int = 24               # 定时触发间隔（小时）
    dream_session_threshold: int = 50            # 会话数触发阈值
    dream_min_episodes: int = 5                  # 深度反思最少 Episode 数
    dream_superseded_keep_days: int = 7          # superseded 知识保留天数
    dream_stale_min_utility: float = 0.2         # 低效用阈值
    dream_stale_max_age_days: int = 90           # 低效用知识最大保留天数
    dream_merge_similarity_threshold: float = 0.7  # 知识合并 Jaccard 阈值
    dream_merge_min_group_size: int = 3          # 触发合并的最小组大小
    dream_isolated_node_max_age_days: int = 30   # 孤立图谱节点最大保留天数
    dream_cold_max_age_days: int = 30            # MTM 冷页面最大保留天数
    dream_llm_timeout_seconds: int = 60          # Dream LLM 调用超时

    # === Ring 2: Skill 进化 (DGM Patch) 配置 ===
    skill_evolution_enabled: bool = False        # 默认禁用，需显式开启
    skill_evolution_error_threshold: float = 0.3 # 错误率达 30% 才触发进化
    skill_evolution_min_calls: int = 5           # 至少 5 次调用才有统计意义
    skill_evolution_window_days: int = 7         # 统计窗口 7 天
    skill_evolution_patch_per_day: int = 1       # 每个 skill 每天最多 patch 1 次

    # === Ring 3: 能力盲区检测配置 ===
    capability_gap_detection_enabled: bool = True   # 默认启用检测
    capability_gap_trigger_threshold: int = 3       # 累积 3 次失败触发进化任务
    capability_gap_cooldown_hours: int = 24         # 同一工具 24h 内不重复触发

    # === Token 优化配置 ===
    # 工具结果 LLM 总结
    tool_result_summarize_enabled: bool = True
    tool_result_summarize_threshold: int = 20000   # 字符数，超过触发 LLM 总结
    tool_result_summarize_hard_limit: int = 50000  # 总结失败时的硬截断上限
    tool_result_summarize_timeout: float = 60.0

    # 单条消息上限
    context_single_message_max_chars: int = 30000

    # 分级截断 truncatable_to
    context_tool_result_truncatable_chars: int = 300    # MEDIUM 超预算时截到此长度
    context_tool_error_truncatable_chars: int = 1000    # HIGH 错误消息截到此长度

    # === LLM KV Cache 配置 ===
    llm_cache_control_enabled: bool = False  # 是否启用 Anthropic cache_control blocks

    # === Skill 执行链路配置 ===
    mcp_tools_enabled: bool = False
    sandbox_provider: str = "auto"
    sandbox_runtime_bin: str = "docker"
    sandbox_image: str = ""
    sandbox_work_dir: str = ""
    sandbox_project_mount_readonly: bool = True
    sandbox_network_mode: str = "bridge"
    sandbox_container_project_root: str = "/workspace/project"
    sandbox_container_workdir: str = "/workspace/workdir"
    sandbox_settings_path: str = ""
    sandbox_check_runtime: bool = True

    @classmethod
    def from_env(cls, env_file: str = ".env") -> "V4Config":
        """CLI 模式：纯 os.getenv + dotenv，零 FastAPI 依赖"""
        try:
            from dotenv import load_dotenv
            load_dotenv(env_file)
        except ImportError:
            pass

        project_root = os.getenv("PROJECT_ROOT", str(Path.cwd()))
        skills_dir = os.getenv("SKILLS_DIR", str(Path(project_root) / ".claude" / "skills"))

        return cls(
            skills_dir=skills_dir,
            project_root=project_root,
            anthropic_api_key=os.getenv("ANTHROPIC_AUTH_TOKEN", ""),
            anthropic_base_url=os.getenv("ANTHROPIC_BASE_URL", ""),
            anthropic_model=os.getenv("ANTHROPIC_MODEL", ""),
            anthropic_small_fast_model=os.getenv("ANTHROPIC_SMALL_FAST_MODEL", ""),
            anthropic_api_key_backup=os.getenv("ANTHROPIC_AUTH_TOKEN_BACKUP", ""),
            anthropic_base_url_backup=os.getenv("ANTHROPIC_BASE_URL_BACKUP", ""),
            anthropic_model_backup=os.getenv("ANTHROPIC_MODEL_BACKUP", ""),
            anthropic_api_key_c=os.getenv("ANTHROPIC_AUTH_TOKEN_C", ""),
            anthropic_base_url_c=os.getenv("ANTHROPIC_BASE_URL_C", ""),
            anthropic_model_c=os.getenv("ANTHROPIC_MODEL_C", ""),
            anthropic_small_fast_model_c=os.getenv("ANTHROPIC_SMALL_FAST_MODEL_C", ""),
            experience_ttl=int(os.getenv("V4_EXPERIENCE_TTL", str(3600 * 24 * 30))),
            conversation_history_ttl=int(os.getenv("V4_CONVERSATION_HISTORY_TTL", str(3600 * 24 * 7))),
            conversation_summary_ttl=int(os.getenv("V4_CONVERSATION_SUMMARY_TTL", str(3600 * 24 * 7))),
            experience_min_user_msg_length=int(os.getenv("V4_EXPERIENCE_MIN_USER_MSG_LENGTH", "10")),
            experience_min_assistant_msg_length=int(os.getenv("V4_EXPERIENCE_MIN_ASSISTANT_MSG_LENGTH", "50")),
            experience_max_preferences=int(os.getenv("V4_EXPERIENCE_MAX_PREFERENCES", "15")),
            experience_max_insights=int(os.getenv("V4_EXPERIENCE_MAX_INSIGHTS", "30")),
            experience_max_patterns=int(os.getenv("V4_EXPERIENCE_MAX_PATTERNS", "15")),
            experience_max_corrections=int(os.getenv("V4_EXPERIENCE_MAX_CORRECTIONS", "15")),
            setting_sources=os.getenv("V4_SETTING_SOURCES", "project"),
            system_prompt_mode=os.getenv("V4_SYSTEM_PROMPT_MODE", "append"),
            session_fallback_enabled=_to_bool(os.getenv("V4_SESSION_FALLBACK_ENABLED", "true")),
            default_user_id=int(os.getenv("V4_DEFAULT_USER_ID", "1")),
            instance_id=os.getenv("AGENT_INSTANCE_ID", f"agent-{socket.gethostname()[:8]}"),
            claude_dir=os.getenv("CLAUDE_DIR", "/root/.claude"),
            session_rebuild_max_size=int(os.getenv("V4_SESSION_REBUILD_MAX_SIZE", str(180 * 1024))),
            policy_error_cleanup_enabled=_to_bool(os.getenv("V4_POLICY_ERROR_CLEANUP_ENABLED", "true")),
            session_auto_rebuild_enabled=_to_bool(os.getenv("V4_SESSION_AUTO_REBUILD_ENABLED", "true")),
            compress_user_msg_max_length=int(os.getenv("V4_COMPRESS_USER_MSG_MAX_LENGTH", "2000")),
            compress_assistant_text_max_length=int(os.getenv("V4_COMPRESS_ASSISTANT_TEXT_MAX_LENGTH", "500")),
            compress_tool_input_max_length=int(os.getenv("V4_COMPRESS_TOOL_INPUT_MAX_LENGTH", "200")),
            compress_tool_output_max_length=int(os.getenv("V4_COMPRESS_TOOL_OUTPUT_MAX_LENGTH", "500")),
            intel_search_max_steps=int(os.getenv("INTEL_SEARCH_MAX_STEPS", "30")),
            intel_search_max_time=float(os.getenv("INTEL_SEARCH_MAX_TIME_SECONDS", "600.0")),
            intel_search_max_depth=int(os.getenv("INTEL_SEARCH_MAX_DEPTH", "5")),
            intel_search_max_downloads=int(os.getenv("INTEL_SEARCH_MAX_DOWNLOADS", "10")),
            intel_search_max_image_analyses=int(os.getenv("INTEL_SEARCH_MAX_IMAGE_ANALYSES", "15")),
            intel_search_zai_mcp_enabled=_to_bool(os.getenv("INTEL_SEARCH_ZAI_MCP_ENABLED", "true")),
            session_storage_backend=os.getenv("V4_SESSION_STORAGE_BACKEND", "sqlite"),
            sqlite_db_path_template=os.getenv("V4_SQLITE_DB_PATH_TEMPLATE", "app/data/sessions/{instance_id}/memory.db"),
            sqlite_wal_mode=_to_bool(os.getenv("V4_SQLITE_WAL_MODE", "true")),
            sqlite_busy_timeout_ms=int(os.getenv("V4_SQLITE_BUSY_TIMEOUT_MS", "30000")),
            context_window_override=int(os.getenv("V4_CONTEXT_WINDOW_OVERRIDE", "0")),
            context_budget_session_file=float(os.getenv("V4_CONTEXT_BUDGET_SESSION_FILE", "0.0")),
            context_budget_system_prompt=float(os.getenv("V4_CONTEXT_BUDGET_SYSTEM_PROMPT", "0.0")),
            context_budget_experience=float(os.getenv("V4_CONTEXT_BUDGET_EXPERIENCE", "0.0")),
            context_budget_generation=float(os.getenv("V4_CONTEXT_BUDGET_GENERATION", "0.0")),
            progressive_summarize_enabled=_to_bool(os.getenv("V4_PROGRESSIVE_SUMMARIZE_ENABLED", "true")),
            summarize_identity_preservation=_to_bool(os.getenv("V4_SUMMARIZE_IDENTITY_PRESERVATION", "true")),
            memory_engine_enabled=_to_bool(os.getenv("MEMORY_ENGINE_ENABLED", "false")),
            mtm_max_pages=int(os.getenv("MEMORY_ENGINE_MTM_MAX_PAGES", "200")),
            mtm_heat_alpha=float(os.getenv("MEMORY_ENGINE_MTM_HEAT_ALPHA", "1.0")),
            mtm_heat_beta=float(os.getenv("MEMORY_ENGINE_MTM_HEAT_BETA", "0.5")),
            mtm_heat_gamma=float(os.getenv("MEMORY_ENGINE_MTM_HEAT_GAMMA", "2.0")),
            mtm_heat_tau=float(os.getenv("MEMORY_ENGINE_MTM_HEAT_TAU", "168.0")),
            mtm_max_age_days=int(os.getenv("MEMORY_ENGINE_MTM_MAX_AGE_DAYS", "90")),
            ltm_promotion_threshold=float(os.getenv("MEMORY_ENGINE_LTM_PROMOTION_THRESHOLD", "5.0")),
            user_profile_enabled=_to_bool(os.getenv("MEMORY_ENGINE_USER_PROFILE_ENABLED", "true")),
            memory_budget_ratio=float(os.getenv("MEMORY_ENGINE_BUDGET_RATIO", "0.10")),
            memory_cleanup_interval_hours=int(os.getenv("MEMORY_ENGINE_CLEANUP_INTERVAL_HOURS", "24")),
            llm_budget_enabled=_to_bool(os.getenv("LLM_BUDGET_ENABLED", "true")),
            llm_budget_max_chars=int(os.getenv("LLM_BUDGET_MAX_CHARS", "150000")),
            llm_budget_min_full_articles=int(os.getenv("LLM_BUDGET_MIN_FULL_ARTICLES", "5")),
            llm_budget_max_full_articles=int(os.getenv("LLM_BUDGET_MAX_FULL_ARTICLES", "15")),
            llm_budget_high_score_threshold=float(os.getenv("LLM_BUDGET_HIGH_SCORE_THRESHOLD", "60.0")),
            llm_budget_summary_max_chars=int(os.getenv("LLM_BUDGET_SUMMARY_MAX_CHARS", "800")),
            llm_budget_timeseries_max_records=int(os.getenv("LLM_BUDGET_TIMESERIES_MAX_RECORDS", "30")),
            llm_budget_strip_fields=os.getenv(
                "LLM_BUDGET_STRIP_FIELDS",
                "street_view_base64,boundary_geojson,geojson,screenshot_base64,base64,trace,tracks,polyline,areas,circles,tile_url,tiles,route_info"
            ),
            knowledge_engine_enabled=_to_bool(os.getenv("KNOWLEDGE_ENGINE_ENABLED", "true")),
            knowledge_engine_config_path=os.getenv("KNOWLEDGE_ENGINE_CONFIG_PATH", "agent_core/knowledge/knowledge_engine.json"),
            knowledge_engine_retrieve_timeout_ms=int(os.getenv("KNOWLEDGE_ENGINE_RETRIEVE_TIMEOUT_MS", "50")),
            knowledge_graph_enabled=_to_bool(os.getenv("KNOWLEDGE_GRAPH_ENABLED", "true")),
            knowledge_graph_max_hops=int(os.getenv("KNOWLEDGE_GRAPH_MAX_HOPS", "2")),
            knowledge_graph_max_edges_in_prompt=int(os.getenv("KNOWLEDGE_GRAPH_MAX_EDGES_IN_PROMPT", "15")),
            prediction_extraction_enabled=_to_bool(os.getenv("PREDICTION_EXTRACTION_ENABLED", "true")),
            prediction_staleness_days=int(os.getenv("PREDICTION_STALENESS_DAYS", "30")),
            prediction_verify_timeout=int(os.getenv("PREDICTION_VERIFY_TIMEOUT", "30")),
            max_iterations=int(os.getenv("V4_MAX_ITERATIONS", "100")),
            max_timeout_seconds=int(os.getenv("V4_MAX_TIMEOUT_SECONDS", "3600")),
            max_tokens_per_turn=int(os.getenv("V4_MAX_TOKENS_PER_TURN", "16384")),
            loop_max_truncation_retry=int(os.getenv("V4_LOOP_MAX_TRUNCATION_RETRY", "2")),
            loop_treat_unknown_stop_as_complete=_to_bool(os.getenv("V4_LOOP_TREAT_UNKNOWN_STOP_AS_COMPLETE", "true")),
            parallel_tool_execution=_to_bool(os.getenv("V4_PARALLEL_TOOL_EXECUTION", "true")),
            parallel_max_concurrent=int(os.getenv("V4_PARALLEL_MAX_CONCURRENT", "8")),
            parallel_per_tool_timeout=float(os.getenv("V4_PARALLEL_PER_TOOL_TIMEOUT", "120.0")),
            context_compaction_enabled=_to_bool(os.getenv("V4_CONTEXT_COMPACTION_ENABLED", "true")),
            context_compaction_threshold=float(os.getenv("V4_CONTEXT_COMPACTION_THRESHOLD", "0.70")),
            context_compaction_keep_recent=int(os.getenv("V4_CONTEXT_COMPACTION_KEEP_RECENT", "6")),
            permission_guard_enabled=_to_bool(os.getenv("V4_PERMISSION_GUARD_ENABLED", "false")),
            subagent_enabled=_to_bool(os.getenv("V4_SUBAGENT_ENABLED", "true")),
            subagent_max_depth=int(os.getenv("V4_SUBAGENT_MAX_DEPTH", "3")),
            subagent_max_iterations=int(os.getenv("V4_SUBAGENT_MAX_ITERATIONS", "100")),
            bg_subagent_max_iterations=int(os.getenv("V4_BG_SUBAGENT_MAX_ITERATIONS", "0")),
            bg_subagent_max_timeout_seconds=int(os.getenv("V4_BG_SUBAGENT_MAX_TIMEOUT_SECONDS", "18000")),
            skill_evolution_enabled=_to_bool(os.getenv("SKILL_EVOLUTION_ENABLED", "false")),
            skill_evolution_error_threshold=float(os.getenv("SKILL_EVOLUTION_ERROR_THRESHOLD", "0.3")),
            skill_evolution_min_calls=int(os.getenv("SKILL_EVOLUTION_MIN_CALLS", "5")),
            skill_evolution_window_days=int(os.getenv("SKILL_EVOLUTION_WINDOW_DAYS", "7")),
            skill_evolution_patch_per_day=int(os.getenv("SKILL_EVOLUTION_PATCH_PER_DAY", "1")),
            capability_gap_detection_enabled=_to_bool(os.getenv("CAPABILITY_GAP_DETECTION_ENABLED", "true")),
            capability_gap_trigger_threshold=int(os.getenv("CAPABILITY_GAP_TRIGGER_THRESHOLD", "3")),
            capability_gap_cooldown_hours=int(os.getenv("CAPABILITY_GAP_COOLDOWN_HOURS", "24")),
            llm_cache_control_enabled=_to_bool(os.getenv("LLM_CACHE_CONTROL_ENABLED", "false")),
            mcp_tools_enabled=_to_bool(os.getenv("V4_MCP_TOOLS_ENABLED", "false")),
            sandbox_provider=os.getenv("V4_SANDBOX_PROVIDER", "auto"),
            sandbox_runtime_bin=os.getenv("V4_SANDBOX_RUNTIME_BIN", "docker"),
            sandbox_image=os.getenv("V4_SANDBOX_IMAGE", ""),
            sandbox_work_dir=os.getenv("V4_SANDBOX_WORK_DIR", ""),
            sandbox_project_mount_readonly=_to_bool(os.getenv("V4_SANDBOX_PROJECT_MOUNT_READONLY", "true")),
            sandbox_network_mode=os.getenv("V4_SANDBOX_NETWORK_MODE", "bridge"),
            sandbox_container_project_root=os.getenv("V4_SANDBOX_CONTAINER_PROJECT_ROOT", "/workspace/project"),
            sandbox_container_workdir=os.getenv("V4_SANDBOX_CONTAINER_WORKDIR", "/workspace/workdir"),
            sandbox_settings_path=os.getenv("V4_SANDBOX_SETTINGS_PATH", ""),
            sandbox_check_runtime=_to_bool(os.getenv("V4_SANDBOX_CHECK_RUNTIME", "true")),
        )

    @classmethod
    def from_dict(cls, d: dict) -> "V4Config":
        """从普通 dict 构建 V4Config — 不依赖任何 app 层类型

        调用方（app 层）负责将 Settings 对象展开为 dict 后传入。
        """
        if not isinstance(d, dict):
            raise TypeError(f"from_dict() expects a dict, got {type(d).__name__}")

        project_root = d.get("project_root") or str(
            Path(__file__).parent.parent.absolute()
        )
        skills_dir = d.get("skills_dir") or str(Path(project_root) / ".claude" / "skills")

        def _g(key, default=""):
            return d.get(key, default) or default

        def _gi(key, default=0):
            return int(d.get(key, default) or default)

        def _gf(key, default=0.0):
            return float(d.get(key, default) or default)

        def _gb(key, default=True):
            return _to_bool(d.get(key, default))

        return cls(
            skills_dir=skills_dir,
            project_root=project_root,
            anthropic_api_key=_g("anthropic_auth_token"),
            anthropic_base_url=_g("anthropic_base_url"),
            anthropic_model=_g("anthropic_model"),
            anthropic_small_fast_model=_g("anthropic_small_fast_model"),
            anthropic_api_key_backup=_g("anthropic_auth_token_backup"),
            anthropic_base_url_backup=_g("anthropic_base_url_backup"),
            anthropic_model_backup=_g("anthropic_model_backup"),
            anthropic_api_key_c=_g("anthropic_auth_token_c"),
            anthropic_base_url_c=_g("anthropic_base_url_c"),
            anthropic_model_c=_g("anthropic_model_c"),
            anthropic_small_fast_model_c=_g("anthropic_small_fast_model_c"),
            experience_ttl=_gi("experience_ttl", 3600 * 24 * 30),
            conversation_history_ttl=_gi("conversation_history_ttl", 3600 * 24 * 7),
            conversation_summary_ttl=_gi("conversation_summary_ttl", 3600 * 24 * 7),
            experience_min_user_msg_length=_gi("experience_min_user_msg_length", 10),
            experience_min_assistant_msg_length=_gi("experience_min_assistant_msg_length", 50),
            experience_max_preferences=_gi("experience_max_preferences", 15),
            experience_max_insights=_gi("experience_max_insights", 30),
            experience_max_patterns=_gi("experience_max_patterns", 15),
            experience_max_corrections=_gi("experience_max_corrections", 15),
            setting_sources=_g("setting_sources", "project"),
            system_prompt_mode=_g("system_prompt_mode", "append"),
            session_fallback_enabled=_gb("session_fallback_enabled", True),
            default_user_id=_gi("default_user_id", 1),
            instance_id=_g("instance_id", f"agent-{socket.gethostname()[:8]}"),
            claude_dir=_g("claude_dir", "/root/.claude"),
            session_rebuild_max_size=_gi("session_rebuild_max_size", 180 * 1024),
            policy_error_cleanup_enabled=_gb("policy_error_cleanup_enabled", True),
            session_auto_rebuild_enabled=_gb("session_auto_rebuild_enabled", True),
            compress_user_msg_max_length=_gi("compress_user_msg_max_length", 2000),
            compress_assistant_text_max_length=_gi("compress_assistant_text_max_length", 500),
            compress_tool_input_max_length=_gi("compress_tool_input_max_length", 200),
            compress_tool_output_max_length=_gi("compress_tool_output_max_length", 500),
            intel_search_max_steps=_gi("intel_search_max_steps", 30),
            intel_search_max_time=_gf("intel_search_max_time", 600.0),
            intel_search_max_depth=_gi("intel_search_max_depth", 5),
            intel_search_max_downloads=_gi("intel_search_max_downloads", 10),
            intel_search_max_image_analyses=_gi("intel_search_max_image_analyses", 15),
            intel_search_zai_mcp_enabled=_gb("intel_search_zai_mcp_enabled", True),
            session_storage_backend=_g("session_storage_backend", "sqlite"),
            sqlite_db_path_template=_g("sqlite_db_path_template", "app/data/sessions/{instance_id}/memory.db"),
            sqlite_wal_mode=_gb("sqlite_wal_mode", True),
            sqlite_busy_timeout_ms=_gi("sqlite_busy_timeout_ms", 5000),
            context_window_override=_gi("context_window_override", 0),
            context_budget_session_file=_gf("context_budget_session_file", 0.0),
            context_budget_system_prompt=_gf("context_budget_system_prompt", 0.0),
            context_budget_experience=_gf("context_budget_experience", 0.0),
            context_budget_generation=_gf("context_budget_generation", 0.0),
            progressive_summarize_enabled=_gb("progressive_summarize_enabled", True),
            summarize_identity_preservation=_gb("summarize_identity_preservation", True),
            memory_engine_enabled=_gb("memory_engine_enabled", False),
            mtm_max_pages=_gi("mtm_max_pages", 200),
            mtm_heat_alpha=_gf("mtm_heat_alpha", 1.0),
            mtm_heat_beta=_gf("mtm_heat_beta", 0.5),
            mtm_heat_gamma=_gf("mtm_heat_gamma", 2.0),
            mtm_heat_tau=_gf("mtm_heat_tau", 168.0),
            mtm_max_age_days=_gi("mtm_max_age_days", 90),
            ltm_promotion_threshold=_gf("ltm_promotion_threshold", 5.0),
            user_profile_enabled=_gb("user_profile_enabled", True),
            memory_budget_ratio=_gf("memory_budget_ratio", 0.10),
            memory_cleanup_interval_hours=_gi("memory_cleanup_interval_hours", 24),
            llm_budget_enabled=_gb("llm_budget_enabled", True),
            llm_budget_max_chars=_gi("llm_budget_max_chars", 150000),
            llm_budget_min_full_articles=_gi("llm_budget_min_full_articles", 5),
            llm_budget_max_full_articles=_gi("llm_budget_max_full_articles", 15),
            llm_budget_high_score_threshold=_gf("llm_budget_high_score_threshold", 60.0),
            llm_budget_summary_max_chars=_gi("llm_budget_summary_max_chars", 800),
            llm_budget_timeseries_max_records=_gi("llm_budget_timeseries_max_records", 30),
            llm_budget_strip_fields=_g(
                "llm_budget_strip_fields",
                "street_view_base64,boundary_geojson,geojson,screenshot_base64,base64,trace,tracks,polyline,areas,circles,tile_url,tiles,route_info"
            ),
            knowledge_engine_enabled=_gb("knowledge_engine_enabled", True),
            knowledge_engine_config_path=_g("knowledge_engine_config_path", "agent_core/knowledge/knowledge_engine.json"),
            knowledge_engine_retrieve_timeout_ms=_gi("knowledge_engine_retrieve_timeout_ms", 50),
            knowledge_graph_enabled=_gb("knowledge_graph_enabled", True),
            knowledge_graph_max_hops=_gi("knowledge_graph_max_hops", 2),
            knowledge_graph_max_edges_in_prompt=_gi("knowledge_graph_max_edges_in_prompt", 15),
            prediction_extraction_enabled=_gb("prediction_extraction_enabled", True),
            prediction_staleness_days=_gi("prediction_staleness_days", 30),
            prediction_verify_timeout=_gi("prediction_verify_timeout", 30),
            max_iterations=_gi("max_iterations", 30),
            max_timeout_seconds=_gi("max_timeout_seconds", 720),
            max_tokens_per_turn=_gi("max_tokens_per_turn", 16384),
            loop_max_truncation_retry=_gi("loop_max_truncation_retry", 2),
            loop_treat_unknown_stop_as_complete=_gb("loop_treat_unknown_stop_as_complete", True),
            parallel_tool_execution=_gb("parallel_tool_execution", True),
            parallel_max_concurrent=_gi("parallel_max_concurrent", 8),
            parallel_per_tool_timeout=_gf("parallel_per_tool_timeout", 120.0),
            context_compaction_enabled=_gb("context_compaction_enabled", True),
            context_compaction_threshold=_gf("context_compaction_threshold", 0.70),
            context_compaction_keep_recent=_gi("context_compaction_keep_recent", 6),
            permission_guard_enabled=_gb("permission_guard_enabled", False),
            subagent_enabled=_gb("subagent_enabled", True),
            subagent_max_depth=_gi("subagent_max_depth", 3),
            subagent_max_iterations=_gi("subagent_max_iterations", 100),
            bg_subagent_max_iterations=_gi("bg_subagent_max_iterations", 100),
            bg_subagent_max_timeout_seconds=_gi("bg_subagent_max_timeout_seconds", 3600),
            skill_evolution_enabled=_gb("skill_evolution_enabled", False),
            skill_evolution_error_threshold=_gf("skill_evolution_error_threshold", 0.3),
            skill_evolution_min_calls=_gi("skill_evolution_min_calls", 5),
            skill_evolution_window_days=_gi("skill_evolution_window_days", 7),
            skill_evolution_patch_per_day=_gi("skill_evolution_patch_per_day", 1),
            capability_gap_detection_enabled=_gb("capability_gap_detection_enabled", True),
            capability_gap_trigger_threshold=_gi("capability_gap_trigger_threshold", 3),
            capability_gap_cooldown_hours=_gi("capability_gap_cooldown_hours", 24),
            llm_cache_control_enabled=_gb("llm_cache_control_enabled", False),
            mcp_tools_enabled=_gb("mcp_tools_enabled", False),
            sandbox_provider=_g("sandbox_provider", "auto"),
            sandbox_runtime_bin=_g("sandbox_runtime_bin", "docker"),
            sandbox_image=_g("sandbox_image", ""),
            sandbox_work_dir=_g("sandbox_work_dir", ""),
            sandbox_project_mount_readonly=_gb("sandbox_project_mount_readonly", True),
            sandbox_network_mode=_g("sandbox_network_mode", "bridge"),
            sandbox_container_project_root=_g("sandbox_container_project_root", "/workspace/project"),
            sandbox_container_workdir=_g("sandbox_container_workdir", "/workspace/workdir"),
            sandbox_settings_path=_g("sandbox_settings_path", ""),
            sandbox_check_runtime=_gb("sandbox_check_runtime", True),
        )

    def get_env_vars(self) -> dict:
        """构建 SDK 所需的环境变量"""
        env_vars = {}
        if self.anthropic_api_key:
            env_vars["ANTHROPIC_AUTH_TOKEN"] = self.anthropic_api_key
        if self.anthropic_base_url:
            env_vars["ANTHROPIC_BASE_URL"] = self.anthropic_base_url
        if self.anthropic_model:
            env_vars["ANTHROPIC_MODEL"] = self.anthropic_model
        env_vars["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        return env_vars

    def get_backup_env_vars(self) -> dict:
        """构建 Backup SDK 所需的环境变量（容灾切换用）"""
        env_vars = {}
        if self.anthropic_api_key_backup:
            env_vars["ANTHROPIC_AUTH_TOKEN"] = self.anthropic_api_key_backup
        if self.anthropic_base_url_backup:
            env_vars["ANTHROPIC_BASE_URL"] = self.anthropic_base_url_backup
        if self.anthropic_model_backup:
            env_vars["ANTHROPIC_MODEL"] = self.anthropic_model_backup
        env_vars["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        return env_vars

    def has_backup(self) -> bool:
        """是否配置了 Backup 端点"""
        return bool(self.anthropic_api_key_backup and self.anthropic_base_url_backup)


# 别名：AgentCoreConfig 是正式名称，V4Config 保留向后兼容
AgentCoreConfig = V4Config
