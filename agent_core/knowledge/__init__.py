"""
AgentCore Knowledge Engine — 自进化知识引擎

8 层架构 + 时序认知追溯:
1. EpisodeTracker     — 执行轨迹采集
2. FeedbackLearner    — 用户反馈学习
3. KnowledgeDistiller — 知识蒸馏
4. KnowledgeStore     — 知识存储
5. KnowledgeRetriever — 知识检索
6. ReflectionEngine   — 反思引擎
7. EvolutionTaskManager — 进化任务
8. SkillCrystallizer  — 经验结晶
"""

from .models import (
    KnowledgeUnit,
    CognitionChange,
    Episode,
    SkillExecution,
    SkillProfile,
    PreferenceUnit,
    EvolutionTask,
    CrystallizedSkill,
    CognitionSnapshot,
    HeatScoreConfig,
    LearnSnapshot,
    BaselineCase,
    EvalResult,
)
from .config_loader import load_knowledge_config
from .store import KnowledgeStore
from .temporal import TemporalKnowledgeManager
from .episode_tracker import EpisodeTracker
from .guard import KnowledgeEngineGuard
from .source_registry import (
    KnowledgeSource,
    KnowledgeSourceReader,
    KnowledgeSourceRegistry,
    get_registry,
    reset_registry,
)
from .skill_evolver import SkillEvolver
from .dream_consolidator import DreamConsolidator

__all__ = [
    "KnowledgeUnit",
    "CognitionChange",
    "Episode",
    "SkillExecution",
    "SkillProfile",
    "PreferenceUnit",
    "EvolutionTask",
    "CrystallizedSkill",
    "CognitionSnapshot",
    "HeatScoreConfig",
    "LearnSnapshot",
    "BaselineCase",
    "EvalResult",
    "load_knowledge_config",
    "KnowledgeStore",
    "TemporalKnowledgeManager",
    "EpisodeTracker",
    "KnowledgeEngineGuard",
    # Knowledge Source Registry
    "KnowledgeSource",
    "KnowledgeSourceReader",
    "KnowledgeSourceRegistry",
    "get_registry",
    "reset_registry",
    # Ring 2: Skill 进化
    "SkillEvolver",
    # AutoDream: 周期性深度记忆整合
    "DreamConsolidator",
]
