"""
Knowledge Engine 数据模型

所有数据模型定义，含时序字段支持认知追溯。
参考: Graphiti 双时间戳 + MemRL 价值检索 + Live-Evo 反馈强化。
"""
import math
import time
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from uuid import uuid4


@dataclass
class HeatScoreConfig:
    """热力值计算参数 — 从 agent_core/knowledge/knowledge_engine.json 的 heat_score 段加载"""
    decay_half_life_hours: float = 720.0
    frequency_saturation: int = 10
    feedback_reinforce_weight: float = 0.05
    feedback_decay_weight: float = 0.08
    feedback_boost_cap: float = 0.2
    feedback_penalty_cap: float = 0.3
    w_frequency: float = 0.25
    w_utility: float = 0.35
    w_recency: float = 0.25
    base_offset: float = 0.15

    @classmethod
    def from_config(cls, cfg: dict) -> "HeatScoreConfig":
        hs = cfg.get("heat_score", {})
        return cls(
            decay_half_life_hours=hs.get("decay_half_life_hours", 720.0),
            frequency_saturation=hs.get("frequency_saturation", 10),
            feedback_reinforce_weight=hs.get("feedback_reinforce_weight", 0.05),
            feedback_decay_weight=hs.get("feedback_decay_weight", 0.08),
            feedback_boost_cap=hs.get("feedback_boost_cap", 0.2),
            feedback_penalty_cap=hs.get("feedback_penalty_cap", 0.3),
            w_frequency=hs.get("w_frequency", 0.25),
            w_utility=hs.get("w_utility", 0.35),
            w_recency=hs.get("w_recency", 0.25),
            base_offset=hs.get("base_offset", 0.15),
        )


@dataclass
class KnowledgeUnit:
    """
    知识单元 — 含时序字段，支持认知追溯。
    参考 Graphiti 双时间戳 + MemRL Q-value。
    """
    unit_id: str = ""
    category: str = ""          # skill_insight / domain_fact / strategy_rule / user_cognition
    text: str = ""              # 知识正文
    tags: List[str] = field(default_factory=list)

    # 价值评估（MemRL 启发）
    utility: float = 0.5
    confidence: float = 0.5
    access_count: int = 0
    hit_count: int = 0

    # 用户反馈强化（Live-Evo 启发）
    feedback_reinforcements: int = 0
    feedback_decays: int = 0

    # 时序管理（Graphiti 双时间戳）
    event_time: Optional[float] = None       # 事实在真实世界成立的时间
    ingestion_time: float = 0.0              # 系统学到这个知识的时间
    valid_from: float = 0.0                  # 知识版本生效时间
    valid_until: Optional[float] = None      # 知识版本失效时间（None=当前有效）
    superseded_by: Optional[str] = None      # 被哪条新知识替代
    supersedes: Optional[str] = None         # 替代了哪条旧知识
    update_reason: Optional[str] = None      # 更新原因

    # 来源
    source_episode_id: str = ""
    created_at: float = 0.0
    last_accessed: float = 0.0

    def __post_init__(self):
        if not self.unit_id:
            self.unit_id = str(uuid4())
        now = time.time()
        if not self.created_at:
            self.created_at = now
        if not self.last_accessed:
            self.last_accessed = now
        if not self.ingestion_time:
            self.ingestion_time = now
        if not self.valid_from:
            self.valid_from = now

    @property
    def is_current(self) -> bool:
        """知识是否当前有效"""
        return self.valid_until is None or self.valid_until > time.time()

    def heat_score(self, config: HeatScoreConfig) -> float:
        """
        热力值计算: 兼顾频率、效用、时效、反馈。
        所有权重从 HeatScoreConfig 读取，不硬编码。
        """
        hours_since = (time.time() - self.last_accessed) / 3600
        recency = math.exp(-hours_since / max(config.decay_half_life_hours, 1.0))
        frequency = min(self.access_count / max(config.frequency_saturation, 1), 1.0)
        hit_rate = self.hit_count / max(self.access_count, 1)
        feedback_boost = min(
            self.feedback_reinforcements * config.feedback_reinforce_weight,
            config.feedback_boost_cap,
        )
        feedback_penalty = min(
            self.feedback_decays * config.feedback_decay_weight,
            config.feedback_penalty_cap,
        )
        base = (
            config.w_frequency * frequency
            + config.w_utility * self.utility
            + config.w_recency * recency * (0.5 + 0.5 * hit_rate)
        )
        return max(0.0, min(1.0, base + feedback_boost - feedback_penalty + config.base_offset))

    def to_dict(self) -> dict:
        return {
            "unit_id": self.unit_id,
            "category": self.category,
            "text": self.text,
            "tags": self.tags,
            "utility": self.utility,
            "confidence": self.confidence,
            "access_count": self.access_count,
            "hit_count": self.hit_count,
            "feedback_reinforcements": self.feedback_reinforcements,
            "feedback_decays": self.feedback_decays,
            "event_time": self.event_time,
            "ingestion_time": self.ingestion_time,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "superseded_by": self.superseded_by,
            "supersedes": self.supersedes,
            "update_reason": self.update_reason,
            "source_episode_id": self.source_episode_id,
            "created_at": self.created_at,
            "last_accessed": self.last_accessed,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "KnowledgeUnit":
        tags = d.get("tags", [])
        if isinstance(tags, str):
            import json
            try:
                tags = json.loads(tags)
            except (json.JSONDecodeError, TypeError):
                tags = []
        return cls(
            unit_id=d.get("unit_id", ""),
            category=d.get("category", ""),
            text=d.get("text", ""),
            tags=tags,
            utility=d.get("utility", 0.5),
            confidence=d.get("confidence", 0.5),
            access_count=d.get("access_count", 0),
            hit_count=d.get("hit_count", 0),
            feedback_reinforcements=d.get("feedback_reinforcements", 0),
            feedback_decays=d.get("feedback_decays", 0),
            event_time=d.get("event_time"),
            ingestion_time=d.get("ingestion_time", 0.0),
            valid_from=d.get("valid_from", 0.0),
            valid_until=d.get("valid_until"),
            superseded_by=d.get("superseded_by"),
            supersedes=d.get("supersedes"),
            update_reason=d.get("update_reason"),
            source_episode_id=d.get("source_episode_id", ""),
            created_at=d.get("created_at", 0.0),
            last_accessed=d.get("last_accessed", 0.0),
        )


@dataclass
class CognitionChange:
    """认知变迁记录 — 追溯知识版本更新原因"""
    change_id: str = ""
    old_unit_id: str = ""
    new_unit_id: str = ""
    reason: str = ""
    change_type: str = "update"     # update / correction / reinforcement / decay
    timestamp: float = 0.0
    user_id: int = 0
    instance_id: str = ""
    affected_skills: List[str] = field(default_factory=list)
    confidence_delta: float = 0.0

    def __post_init__(self):
        if not self.change_id:
            self.change_id = str(uuid4())
        if not self.timestamp:
            self.timestamp = time.time()

    def to_dict(self) -> dict:
        return {
            "change_id": self.change_id,
            "old_unit_id": self.old_unit_id,
            "new_unit_id": self.new_unit_id,
            "reason": self.reason,
            "change_type": self.change_type,
            "timestamp": self.timestamp,
            "user_id": self.user_id,
            "instance_id": self.instance_id,
            "affected_skills": self.affected_skills,
            "confidence_delta": self.confidence_delta,
        }


@dataclass
class SkillExecution:
    """单次 Skill 执行记录"""
    skill_name: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    result_summary: str = ""
    duration_ms: float = 0.0
    success: bool = True
    timestamp: float = 0.0

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()


@dataclass
class Episode:
    """一次完整对话的执行轨迹"""
    episode_id: str = ""
    user_id: int = 0
    instance_id: str = ""
    session_id: str = ""
    query: str = ""
    skill_executions: List[SkillExecution] = field(default_factory=list)
    feedback: Optional[Dict[str, Any]] = None
    total_duration_ms: float = 0.0
    success: bool = True
    created_at: float = 0.0
    # 对话上下文（用于思维链提取，不持久化到 Episode 表）
    assistant_response: str = ""
    conversation_context: str = ""

    def __post_init__(self):
        if not self.episode_id:
            self.episode_id = str(uuid4())
        if not self.created_at:
            self.created_at = time.time()

    def to_dict(self) -> dict:
        import json
        return {
            "episode_id": self.episode_id,
            "user_id": self.user_id,
            "instance_id": self.instance_id,
            "session_id": self.session_id,
            "query": self.query,
            "skill_executions": json.dumps(
                [{"skill_name": se.skill_name, "params": se.params,
                  "result_summary": se.result_summary, "duration_ms": se.duration_ms,
                  "success": se.success, "timestamp": se.timestamp}
                 for se in self.skill_executions],
                ensure_ascii=False,
            ),
            "feedback": json.dumps(self.feedback, ensure_ascii=False) if self.feedback else None,
            "total_duration_ms": self.total_duration_ms,
            "success": 1 if self.success else 0,
            "created_at": self.created_at,
        }


@dataclass
class SkillProfile:
    """Skill 能力档案"""
    skill_name: str = ""
    user_id: int = 0
    instance_id: str = ""
    total_executions: int = 0
    success_count: int = 0
    failure_count: int = 0
    avg_duration_ms: float = 0.0
    avg_confidence: float = 0.0
    like_count: int = 0
    dislike_count: int = 0
    satisfaction_score: float = 0.5
    cognition_version_count: int = 0
    last_execution_at: float = 0.0
    updated_at: float = 0.0

    def __post_init__(self):
        if not self.updated_at:
            self.updated_at = time.time()

    @property
    def success_rate(self) -> float:
        return self.success_count / max(self.total_executions, 1)

    def to_dict(self) -> dict:
        return {
            "skill_name": self.skill_name,
            "user_id": self.user_id,
            "instance_id": self.instance_id,
            "total_executions": self.total_executions,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "avg_duration_ms": self.avg_duration_ms,
            "avg_confidence": self.avg_confidence,
            "like_count": self.like_count,
            "dislike_count": self.dislike_count,
            "satisfaction_score": self.satisfaction_score,
            "cognition_version_count": self.cognition_version_count,
            "last_execution_at": self.last_execution_at,
            "updated_at": self.updated_at,
        }


@dataclass
class PreferenceUnit:
    """用户偏好单元"""
    preference_id: str = ""
    user_id: int = 0
    instance_id: str = ""
    dimension: str = ""         # style / depth / format / topic_interest / risk_tolerance
    value: str = ""
    confidence: float = 0.5
    source_episode_id: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    def __post_init__(self):
        if not self.preference_id:
            self.preference_id = str(uuid4())
        now = time.time()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now

    def to_dict(self) -> dict:
        return {
            "preference_id": self.preference_id,
            "user_id": self.user_id,
            "instance_id": self.instance_id,
            "dimension": self.dimension,
            "value": self.value,
            "confidence": self.confidence,
            "source_episode_id": self.source_episode_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class EvolutionTask:
    """进化任务"""
    task_id: str = ""
    user_id: int = 0
    instance_id: str = ""
    gap_description: str = ""
    status: str = "pending"     # pending / in_progress / completed / failed
    phase: str = "gap"          # gap / seek / synthesize / integrate
    exploration_log: List[Dict[str, Any]] = field(default_factory=list)
    result_knowledge_ids: List[str] = field(default_factory=list)
    knowledge_snapshot_id: Optional[str] = None
    created_at: float = 0.0
    updated_at: float = 0.0
    completed_at: Optional[float] = None

    def __post_init__(self):
        if not self.task_id:
            self.task_id = str(uuid4())
        now = time.time()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now

    def to_dict(self) -> dict:
        import json
        return {
            "task_id": self.task_id,
            "user_id": self.user_id,
            "instance_id": self.instance_id,
            "gap_description": self.gap_description,
            "status": self.status,
            "phase": self.phase,
            "exploration_log": json.dumps(self.exploration_log, ensure_ascii=False),
            "result_knowledge_ids": json.dumps(self.result_knowledge_ids, ensure_ascii=False),
            "knowledge_snapshot_id": self.knowledge_snapshot_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
        }


@dataclass
class CrystallizedSkill:
    """结晶 Skill"""
    crystal_id: str = ""
    user_id: int = 0
    instance_id: str = ""
    skill_name: str = ""
    description: str = ""
    workflow: str = ""          # 工作流定义（JSON）
    prompt_template: str = ""
    source_episodes: List[str] = field(default_factory=list)
    status: str = "candidate"   # candidate / approved / deployed / rejected
    rejection_reason: str = ""
    test_result: Optional[Dict[str, Any]] = None
    occurrences: int = 0
    success_rate: float = 0.0
    like_count: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0

    def __post_init__(self):
        if not self.crystal_id:
            self.crystal_id = str(uuid4())
        now = time.time()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now

    def to_dict(self) -> dict:
        import json
        return {
            "crystal_id": self.crystal_id,
            "user_id": self.user_id,
            "instance_id": self.instance_id,
            "skill_name": self.skill_name,
            "description": self.description,
            "workflow": self.workflow,
            "prompt_template": self.prompt_template,
            "source_episodes": json.dumps(self.source_episodes, ensure_ascii=False),
            "status": self.status,
            "rejection_reason": self.rejection_reason,
            "test_result": json.dumps(self.test_result, ensure_ascii=False) if self.test_result else None,
            "occurrences": self.occurrences,
            "success_rate": self.success_rate,
            "like_count": self.like_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class CognitionSnapshot:
    """认知快照 — 定期快照加速追溯查询"""
    snapshot_id: str = ""
    user_id: int = 0
    instance_id: str = ""
    snapshot_time: float = 0.0
    snapshot_type: str = "daily"   # daily / weekly / monthly / reflection / evolution
    active_knowledge_count: int = 0
    category_stats: Dict[str, int] = field(default_factory=dict)
    avg_utility: float = 0.0
    avg_confidence: float = 0.0
    skill_profile_summary: Dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0

    def __post_init__(self):
        if not self.snapshot_id:
            self.snapshot_id = str(uuid4())
        now = time.time()
        if not self.snapshot_time:
            self.snapshot_time = now
        if not self.created_at:
            self.created_at = now

    def to_dict(self) -> dict:
        import json
        return {
            "snapshot_id": self.snapshot_id,
            "user_id": self.user_id,
            "instance_id": self.instance_id,
            "snapshot_time": self.snapshot_time,
            "snapshot_type": self.snapshot_type,
            "active_knowledge_count": self.active_knowledge_count,
            "category_stats": json.dumps(self.category_stats, ensure_ascii=False),
            "avg_utility": self.avg_utility,
            "avg_confidence": self.avg_confidence,
            "skill_profile_summary": json.dumps(self.skill_profile_summary, ensure_ascii=False),
            "created_at": self.created_at,
        }


@dataclass
class LearnSnapshot:
    """学习准确率快照 — 每次学习前后记录，用于滚动对比"""
    snapshot_id: str = ""
    user_id: int = 0
    instance_id: str = ""
    snapshot_type: str = "pre_learn"   # pre_learn / post_learn
    learn_cycle_id: str = ""
    triggered_by: str = "schedule"     # schedule / incremental_cron
    total_verified: int = 0
    correct_count: int = 0
    wrong_count: int = 0
    accuracy_rate: float = 0.0
    subject_stats: Dict[str, Dict] = field(default_factory=dict)
    active_rules_count: int = 0
    new_rules_count: int = 0
    baseline_pass: bool = True
    baseline_score: float = 0.0
    baseline_detail: Dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0

    def __post_init__(self):
        if not self.snapshot_id:
            self.snapshot_id = f"ls_{uuid4().hex[:8]}"
        if not self.created_at:
            self.created_at = time.time()

    def to_dict(self) -> dict:
        import json
        return {
            "snapshot_id": self.snapshot_id,
            "user_id": self.user_id,
            "instance_id": self.instance_id,
            "snapshot_type": self.snapshot_type,
            "learn_cycle_id": self.learn_cycle_id,
            "triggered_by": self.triggered_by,
            "total_verified": self.total_verified,
            "correct_count": self.correct_count,
            "wrong_count": self.wrong_count,
            "accuracy_rate": self.accuracy_rate,
            "subject_stats": json.dumps(self.subject_stats, ensure_ascii=False),
            "active_rules_count": self.active_rules_count,
            "new_rules_count": self.new_rules_count,
            "baseline_pass": 1 if self.baseline_pass else 0,
            "baseline_score": self.baseline_score,
            "baseline_detail": json.dumps(self.baseline_detail, ensure_ascii=False),
            "created_at": self.created_at,
        }


@dataclass
class BaselineCase:
    """基础评测用例 — 常识性预测，用于回归检测"""
    case_id: str = ""
    user_id: int = 0
    instance_id: str = ""
    subject: str = ""
    category: str = ""              # stock / military / geopolitical
    question: str = ""
    expected_direction: str = ""    # up / down / stable / other
    expected_keywords: List[str] = field(default_factory=list)
    difficulty: str = "basic"
    source: str = "auto_extracted"  # manual / auto_extracted
    source_pred_id: str = ""
    created_at: float = 0.0
    is_active: bool = True

    def __post_init__(self):
        if not self.case_id:
            self.case_id = f"bc_{uuid4().hex[:8]}"
        if not self.created_at:
            self.created_at = time.time()

    def to_dict(self) -> dict:
        import json
        return {
            "case_id": self.case_id,
            "user_id": self.user_id,
            "instance_id": self.instance_id,
            "subject": self.subject,
            "category": self.category,
            "question": self.question,
            "expected_direction": self.expected_direction,
            "expected_keywords": json.dumps(self.expected_keywords, ensure_ascii=False),
            "difficulty": self.difficulty,
            "source": self.source,
            "source_pred_id": self.source_pred_id,
            "created_at": self.created_at,
            "is_active": 1 if self.is_active else 0,
        }


@dataclass
class EvalResult:
    """单次学习周期的评测结果"""
    learn_cycle_id: str = ""
    triggered_by: str = ""
    prev_accuracy: float = 0.0
    curr_accuracy: float = 0.0
    accuracy_delta: float = 0.0
    subject_deltas: Dict[str, float] = field(default_factory=dict)
    baseline_pass: bool = True
    baseline_score: float = 0.0
    overall_pass: bool = True
    alert_level: str = "none"       # none / warning / critical
    alert_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "learn_cycle_id": self.learn_cycle_id,
            "triggered_by": self.triggered_by,
            "prev_accuracy": self.prev_accuracy,
            "curr_accuracy": self.curr_accuracy,
            "accuracy_delta": self.accuracy_delta,
            "subject_deltas": self.subject_deltas,
            "baseline_pass": self.baseline_pass,
            "baseline_score": self.baseline_score,
            "overall_pass": self.overall_pass,
            "alert_level": self.alert_level,
            "alert_reason": self.alert_reason,
        }
