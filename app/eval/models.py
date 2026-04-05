"""
Agent 能力评测框架 — 数据模型
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any


@dataclass
class EvalQuestion:
    """评测题目"""
    id: str
    category: str
    difficulty: str
    question: str
    expected_tools: List[str]
    required_tools: List[str]
    judge_focus: List[str]
    hallucination_check: Dict[str, Any]


@dataclass
class ToolCallRecord:
    """单次工具调用记录"""
    skill_name: str
    skill_input: Dict[str, Any]
    skill_output: str
    created_at: float


@dataclass
class TraceScore:
    """工具调用轨迹评分"""
    required_coverage: float   # 必选工具覆盖率 (0-1)
    expected_coverage: float   # 期望工具覆盖率 (0-1)
    data_citation: float       # 数据引用率 (0-1)
    hallucination_free: float  # 幻觉洁净度 (0-1)
    total: float               # 综合得分 (0-10)
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class JudgeScore:
    """LLM 裁判评分"""
    data_source: float         # 数据来源 (0-10)
    logic_coherence: float     # 逻辑自洽 (0-10)
    conclusion_support: float  # 结论支撑 (0-10)
    total: float               # 综合得分 (0-10)
    reasons: Dict[str, str] = field(default_factory=dict)
    overall_comment: str = ""


@dataclass
class EvalResult:
    """单题评测结果"""
    question_id: str
    question: str
    answer: str
    trace: List[ToolCallRecord]
    trace_score: TraceScore
    judge_score: JudgeScore
    total_score: float
    duration_seconds: float
    error: Optional[str] = None


@dataclass
class StepwiseResult:
    """分步评测结果（单步）"""
    step: str             # baseline / after_skill_evolution / after_knowledge / after_rollback
    score: float
    run_id: str
    timestamp: float
    delta: Optional[float] = None
    action: str = "keep"  # keep / rollback
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalReport:
    """完整评测报告"""
    run_id: str
    question_set: str
    total_questions: int
    avg_total_score: float
    avg_trace_score: float
    avg_judge_score: float
    by_category: Dict[str, Any]
    by_difficulty: Dict[str, Any]
    results: List[EvalResult]
    compare: Optional[Dict[str, Any]] = None
    report_path: str = ""
    results_path: str = ""
    triggered_by: str = "manual"
    stepwise_results: Optional[List[StepwiseResult]] = None
    rollbacks: Optional[List[Dict[str, Any]]] = None


@dataclass
class EvalRunConfig:
    """评测运行配置"""
    question_set: str = "general"
    question_ids: Optional[List[str]] = None
    user_id: int = 88888
    compare_mode: bool = False
    baseline_run_id: Optional[str] = None
    triggered_by: str = "manual"
