"""
GAIA Benchmark 数据模型
"""
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


class GaiaQuestion(BaseModel):
    """GAIA 数据集中的一道题"""
    task_id: str
    question: str
    level: int                          # 1, 2, 3
    final_answer: str                   # ground truth
    file_name: Optional[str] = None
    file_path: Optional[str] = None


class GaiaResult(BaseModel):
    """单题评测结果"""
    task_id: str
    level: int
    question: str
    ground_truth: str
    model_answer: Optional[str] = None      # agent 原始输出
    extracted_answer: Optional[str] = None   # 提取的 FINAL ANSWER
    correct: bool = False
    latency_ms: float = 0.0
    error: Optional[str] = None


class BenchmarkRunConfig(BaseModel):
    """评测运行配置"""
    levels: List[int] = Field(default=[1, 2, 3])
    limit: Optional[int] = None
    task_ids: List[str] = Field(default_factory=list)
    max_concurrency: int = 3
    timeout_per_question: int = 300
    agent_base_url: str = "http://localhost:8000"
    dry_run: bool = False


class BenchmarkRunReport(BaseModel):
    """评测运行报告"""
    run_id: str
    status: str                             # completed | failed | dry_run
    total_questions: int
    total_correct: int
    overall_accuracy: float
    per_level: Dict[int, Dict[str, Any]]
    duration_seconds: float
    report_path: str
    results_path: str
    errors: List[Dict[str, str]] = Field(default_factory=list)
