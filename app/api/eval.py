"""
Agent 能力评测框架 — REST API

端点：
  POST /api/v1/eval/run                触发评测，返回 EvalReport
  GET  /api/v1/eval/results            列出历史评测结果
  GET  /api/v1/eval/results/{run_id}   获取单次结果详情
"""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from loguru import logger
from pydantic import BaseModel

from app.eval.models import EvalRunConfig
from app.eval.report import _to_dict

router = APIRouter(prefix="/eval", tags=["Eval"])

# 评测结果存储目录
_RESULTS_DIR = Path(__file__).parent.parent / "data" / "eval" / "results"

# Agent 自身地址（eval runner 调用自己）
_AGENT_BASE_URL = os.getenv(
    "EVAL_AGENT_BASE_URL",
    f"http://localhost:{os.getenv('PORT', '8000')}",
)


def _get_runner():
    """延迟初始化 EvalRunner（避免模块加载时就连接数据库）"""
    from app.eval.runner import EvalRunner
    return EvalRunner(
        agent_base_url=_AGENT_BASE_URL,
        results_dir=str(_RESULTS_DIR),
        llm_provider=None,
    )


# ── 请求/响应模型 ──────────────────────────────────────────────────────────────

class EvalRunRequest(BaseModel):
    question_set: str = "general"
    question_ids: Optional[List[str]] = None
    user_id: int = 88888
    compare_mode: bool = False
    baseline_run_id: Optional[str] = None


class EvalResultSummary(BaseModel):
    run_id: str
    question_set: str
    total_questions: int
    avg_total_score: float
    avg_trace_score: float
    avg_judge_score: float
    report_path: str
    results_path: str


# ── 端点 ───────────────────────────────────────────────────────────────────────

@router.post("/run")
async def run_eval(req: EvalRunRequest) -> Dict[str, Any]:
    """
    触发评测

    同步执行（等待所有题目完成后返回报告）。
    大规模评测建议使用 question_ids 指定少量题目验证，或分批执行。
    """
    config = EvalRunConfig(
        question_set=req.question_set,
        question_ids=req.question_ids,
        user_id=req.user_id,
        compare_mode=req.compare_mode,
        baseline_run_id=req.baseline_run_id,
    )

    runner = _get_runner()
    try:
        report = await runner.run(config)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"[EvalAPI] run failed: {e}")
        raise HTTPException(status_code=500, detail=f"Eval run failed: {e}")

    return _to_dict(report)


@router.get("/results")
async def list_results() -> List[EvalResultSummary]:
    """列出历史评测结果（按时间倒序，最多返回 50 条）"""
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    json_files = sorted(
        _RESULTS_DIR.glob("eval_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:50]

    summaries: List[EvalResultSummary] = []
    for f in json_files:
        try:
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
            summaries.append(
                EvalResultSummary(
                    run_id=data.get("run_id", f.stem.replace("eval_", "")),
                    question_set=data.get("question_set", ""),
                    total_questions=data.get("total_questions", 0),
                    avg_total_score=data.get("avg_total_score", 0.0),
                    avg_trace_score=data.get("avg_trace_score", 0.0),
                    avg_judge_score=data.get("avg_judge_score", 0.0),
                    report_path=data.get("report_path", ""),
                    results_path=data.get("results_path", str(f)),
                )
            )
        except Exception as e:
            logger.warning(f"[EvalAPI] Failed to load result file {f}: {e}")

    return summaries


@router.get("/results/{run_id}")
async def get_result(run_id: str) -> Dict[str, Any]:
    """获取单次评测结果详情"""
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    json_path = _RESULTS_DIR / f"eval_{run_id}.json"
    if not json_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Eval result '{run_id}' not found",
        )

    try:
        with open(json_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"[EvalAPI] Failed to load result {run_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to load result: {e}")
