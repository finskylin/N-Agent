
from fastapi import APIRouter, HTTPException, Query
from typing import Optional
from loguru import logger

router = APIRouter(prefix="/stock", tags=["stock"])


async def _execute_skill(skill_name: str, params: dict) -> dict:
    """通过 V4SkillExecutor 执行 skill（subprocess 模式）"""
    from agent_core.skill_discovery import SkillDiscovery
    from agent_core.skill_executor import V4SkillExecutor
    from app.agent.v4.v4_config import V4Config

    config = V4Config.from_settings()
    discovery = SkillDiscovery(config.skills_dir)
    discovery.scan()
    executor = V4SkillExecutor(discovery)
    return await executor.execute(skill_name, params)


@router.get("/{ts_code}/quote")
async def get_stock_quote(
    ts_code: str,
    ts_code_query: Optional[str] = Query(None, alias="ts_code")
):
    """获取股票实时行情"""
    result = await _execute_skill("realtime_quote", {"ts_code": ts_code})
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result


@router.get("/{ts_code}/fundamental")
async def get_stock_fundamental(
    ts_code: str,
    ts_code_query: Optional[str] = Query(None, alias="ts_code")
):
    """获取股票基本面数据"""
    result = await _execute_skill("financial_report", {"ts_code": ts_code})
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result
