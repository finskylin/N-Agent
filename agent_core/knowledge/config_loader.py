"""
Knowledge Engine 配置加载器

从 agent_core/knowledge/knowledge_engine.json 加载配置。
所有模块通过此加载器读取参数，禁止硬编码。
"""
import json
from pathlib import Path
from typing import Dict, Any

from loguru import logger


_DEFAULT_CONFIG = {
    "enabled": False,
    "episode_tracker": {
        "max_episodes_per_user": 1000,
        "result_summary_max_chars": 200,
        "params_max_chars": 500,
    },
    "feedback_learner": {
        "enabled": True,
        "reinforce_delta": 0.1,
        "decay_delta": 0.15,
        "preference_dimensions": ["style", "depth", "format", "topic_interest", "risk_tolerance"],
        "llm_timeout_seconds": 10,
    },
    "distiller": {
        "enabled": True,
        "min_skills_for_distill": 1,
        "max_knowledge_per_episode": 5,
        "max_knowledge_text_chars": 100,
        "conflict_detection": True,
        "llm_timeout_seconds": 15,
    },
    "temporal": {
        "enabled": True,
        "snapshot_interval": "daily",
        "max_version_chain_length": 100,
        "max_cognition_changes": 1000,
        "max_snapshots": 180,
        "archive_after_days": 360,
        "never_delete_knowledge": True,
        "trend_window_days": 30,
        "trend_threshold": 0.05,
    },
    "heat_score": {
        "decay_half_life_hours": 720,
        "frequency_saturation": 10,
        "feedback_reinforce_weight": 0.05,
        "feedback_decay_weight": 0.08,
        "feedback_boost_cap": 0.2,
        "feedback_penalty_cap": 0.3,
        "w_frequency": 0.25,
        "w_utility": 0.35,
        "w_recency": 0.25,
        "base_offset": 0.15,
    },
    "store": {
        "max_knowledge_per_user": 1600,
        "max_skill_profiles": 200,
        "max_preferences_per_user": 40,
        "min_heat_threshold": 0.05,
        "cleanup_max_age_days": 180,
    },
    "retriever": {
        "default_top_k": 10,
        "default_token_budget": 2000,
        "score_weights": {"tag_overlap": 0.4, "utility": 0.3, "heat": 0.3},
        "min_score_threshold": 0.1,
        "inject_cognition_changes": True,
        "max_cognition_changes_in_prompt": 3,
    },
    "reflection": {
        "enabled": True,
        "batch_size": 10,
        "interval_hours": 24,
        "consecutive_failure_trigger": 3,
        "min_failure_pattern_count": 2,
        "include_cognition_changes": True,
        "auto_snapshot": True,
        "llm_timeout_seconds": 30,
    },
    "evolution": {
        "enabled": True,
        "max_concurrent_tasks": 3,
        "seek_timeout_seconds": 60,
        "max_tasks_per_user": 40,
        "task_ttl_days": 30,
        "create_snapshot_on_complete": True,
    },
    "crystallizer": {
        "enabled": True,
        "min_occurrences": 3,
        "min_success_rate": 0.8,
        "min_like_count": 2,
        "max_crystals_per_user": 100,
        "auto_deploy": False,
        "pre_deploy_test": True,
        "min_boost_pp": 0.0,
        "llm_timeout_seconds": 20,
    },
    "benchmark": {
        "enabled": False,
        "schedule": "weekly",
        "dimensions": ["accumulation", "crystallization", "temporal", "evolution"],
        "report_path": "reports/knowledge_engine_bench/",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并配置，override 优先"""
    result = base.copy()
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def load_knowledge_config(config_path: str = "agent_core/knowledge/knowledge_engine.json") -> Dict[str, Any]:
    """
    加载知识引擎配置。

    优先从指定路径加载 JSON 文件，缺失的字段用默认值填充。
    加载失败时返回默认配置（enabled=False）。
    """
    config = _DEFAULT_CONFIG.copy()

    try:
        p = Path(config_path)
        if not p.is_absolute():
            # 尝试从项目根目录加载
            candidates = [
                Path(__file__).parent.parent.parent / config_path,
                Path.cwd() / config_path,
            ]
            for c in candidates:
                if c.exists():
                    p = c
                    break

        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
            config = _deep_merge(_DEFAULT_CONFIG, raw)
            logger.info(f"[KnowledgeConfig] Loaded from {p}")
        else:
            logger.warning(
                f"[KnowledgeConfig] Config file not found at {config_path}, "
                f"using defaults (enabled=False)"
            )
    except Exception as e:
        logger.warning(f"[KnowledgeConfig] Load failed: {e}, using defaults")

    return config
