"""
GAIA Benchmark API 端点

提供:
- POST /benchmark/gaia/run       — 触发评测
- GET  /benchmark/gaia/results   — 获取历史结果列表
- GET  /benchmark/gaia/results/{run_id}  — 获取特定评测结果
"""
from typing import Optional

from fastapi import APIRouter, HTTPException
from loguru import logger

from app.benchmark.models import BenchmarkRunConfig, BenchmarkRunReport
from app.benchmark.runner import GaiaBenchmarkRunner
from app.benchmark.report import GaiaBenchmarkReport

router = APIRouter(prefix="/benchmark/gaia", tags=["Benchmark"])


def _get_runner() -> GaiaBenchmarkRunner:
    return GaiaBenchmarkRunner()


def _get_report_gen() -> GaiaBenchmarkReport:
    return GaiaBenchmarkReport()


@router.post("/run", response_model=BenchmarkRunReport)
async def run_gaia_benchmark(config: Optional[BenchmarkRunConfig] = None):
    """
    触发 GAIA 评测

    发送题目到 /api/v1/chat/v4/send 并评分。
    """
    if config is None:
        config = BenchmarkRunConfig()

    logger.info(f"[Benchmark API] Starting GAIA run: levels={config.levels}, limit={config.limit}")

    try:
        runner = _get_runner()
        report = await runner.run(config)
        return report
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"[Benchmark API] Run failed: {e}")
        raise HTTPException(status_code=500, detail=f"Benchmark run failed: {e}")


@router.get("/results")
async def list_gaia_results():
    """获取历史评测结果列表"""
    report_gen = _get_report_gen()
    results = report_gen.list_results()
    return {"results": results, "total": len(results)}


@router.get("/results/{run_id}")
async def get_gaia_result(run_id: str):
    """获取特定评测的完整 JSON 结果"""
    report_gen = _get_report_gen()
    result = report_gen.get_result(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return result
