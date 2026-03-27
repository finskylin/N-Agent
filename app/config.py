"""
Configuration
应用配置
"""
from pydantic_settings import BaseSettings
from pydantic import model_validator
from typing import Optional
import os
from dotenv import dotenv_values


class Settings(BaseSettings):
    """应用配置"""

    # App
    app_name: str = "Stock Agent Service"
    app_version: str = "1.0.0"
    debug: bool = True
    app_env: str = "development"
    log_level: str = "INFO"

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # Java Backend
    backend_url: str = "http://localhost:8080"

    # Primary application database (SQLite by default)
    database_url: str = "sqlite+aiosqlite:///./agent.db"


    # Auth
    jwt_secret: str = "daydaygrowth_super_secret_key_change_me_in_prod_with_enough_length"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24 * 7  # 7 days

    # Claude / Anthropic - 从 .env 文件优先读取
    anthropic_base_url: Optional[str] = None
    anthropic_auth_token: Optional[str] = None
    anthropic_model: Optional[str] = None
    anthropic_small_fast_model: Optional[str] = None
    claude_model: Optional[str] = None

    # ========== 新增：Planning Agent 独立配置 ==========
    # Planning Agent 使用的 LLM 配置（可以与主配置不同）
    anthropic_base_url_plan: Optional[str] = None
    anthropic_auth_token_plan: Optional[str] = None
    anthropic_model_plan: Optional[str] = None
    anthropic_small_fast_model_plan: Optional[str] = None

    # ========== 新增：Intent Analysis Agent 独立配置 ==========
    # Intent Analysis Agent 使用的 LLM 配置（可以与主配置不同）
    anthropic_base_url_decide: Optional[str] = None
    anthropic_auth_token_decide: Optional[str] = None
    anthropic_model_decide: Optional[str] = None
    anthropic_small_fast_model_decide: Optional[str] = None

    # ========== Backup LLM 配置（容灾切换）==========
    anthropic_base_url_backup: Optional[str] = None
    anthropic_auth_token_backup: Optional[str] = None
    anthropic_model_backup: Optional[str] = None
    anthropic_small_fast_model_backup: Optional[str] = None

    # ========== LLM Failover 配置 ==========
    llm_failover_threshold: int = 3
    llm_endpoints_config_path: Optional[str] = None  # 默认 config/llm_endpoints.json

    # ========== 新增：思考链配置 ==========
    # 思考链缓存过期时间（秒），默认 1 小时
    thinking_cache_ttl: int = 3600

    # ========== 视觉模型配置 ==========
    vision_model: str = "qwen3-vl-plus-2025-09-23"
    vision_api_base_url: Optional[str] = None   # 留空则复用 anthropic_base_url
    vision_api_key: Optional[str] = None         # 留空则复用 anthropic_auth_token

    # ========== 非主流程 LLM 配置（知识库摘要、报告生成等）==========
    llm_api_key: Optional[str] = None        # 留空则复用 anthropic_auth_token
    llm_base_url: Optional[str] = None       # 留空则复用 anthropic_base_url
    llm_model: str = "glm-4.7"              # 模型名

    # External Agents
    openai_api_key: Optional[str] = None
    research_agent_api_key: Optional[str] = None
    quant_agent_api_key: Optional[str] = None
    sentiment_agent_api_key: Optional[str] = None

    # V4 Persistence
    v4_default_user_id: int = 1
    v4_mcp_tools_enabled: bool = False
    v4_sandbox_provider: str = "auto"
    v4_sandbox_runtime_bin: str = "docker"
    v4_sandbox_image: str = ""
    v4_sandbox_work_dir: str = ""
    v4_sandbox_project_mount_readonly: bool = True
    v4_sandbox_network_mode: str = "bridge"
    v4_sandbox_container_project_root: str = "/workspace/project"
    v4_sandbox_container_workdir: str = "/workspace/workdir"
    v4_sandbox_settings_path: str = ""
    v4_sandbox_check_runtime: bool = True
    cron_store_path: str = "app/data/cron/jobs.json"

    # DingTalk Stream Mode (钉钉机器人)
    dingtalk_enabled: bool = True  # 默认开启
    dingtalk_client_id: Optional[str] = None
    dingtalk_client_secret: Optional[str] = None

    # Feishu (飞书机器人)
    feishu_enabled: bool = False  # 默认关闭（需配置后开启）
    feishu_app_id: Optional[str] = None
    feishu_app_secret: Optional[str] = None
    feishu_encrypt_key: Optional[str] = None          # Webhook 加密 key（可选）
    feishu_verification_token: Optional[str] = None   # Webhook 验证 token（可选）
    feishu_domain: str = "feishu"                     # feishu（中国）或 lark（国际）

    # CORS
    cors_origins: list[str] = ["*"]

    # GAIA Benchmark
    gaia_benchmark_config_path: Optional[str] = "app/config/benchmark/gaia.json"
    gaia_dataset_path: Optional[str] = "app/data/benchmark/gaia"

    # GitHub Skill Storage
    github_skill_repo_url: Optional[str] = None
    github_auth_type: str = "token"
    github_skill_token: Optional[str] = None
    github_skill_cache_dir: str = "/tmp/skill_repos"
    github_skill_cache_ttl: int = 300
    github_skill_storage_enabled: bool = False
    github_skill_fallback_strategy: str = "fail"

    @model_validator(mode='before')
    @classmethod
    def load_env_file_priority(cls, values):
        """让 .env 文件配置优先于系统环境变量"""
        env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
        if os.path.exists(env_file):
            env_values = dotenv_values(env_file)
            # .env 文件的值优先覆盖
            for key, value in env_values.items():
                if value is not None and value != "":
                    # 显式传入的环境变量优先，避免最小集部署时被仓库 .env 反向覆盖
                    if key not in values or values.get(key) in (None, ""):
                        values[key] = value
        return values

    class Config:
        env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
        env_file_encoding = "utf-8"
        extra = "ignore"
        case_sensitive = False

settings = Settings()
