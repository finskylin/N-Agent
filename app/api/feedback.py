# -*- coding: utf-8 -*-
"""
Feedback API
用户反馈和学习系统 API 端点
"""

from typing import Optional, List, Dict, Any
from datetime import datetime
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from loguru import logger

router = APIRouter(prefix="/feedback", tags=["feedback"])


class FeedbackRequest(BaseModel):
    """反馈请求模型"""
    session_id: str = Field(..., description="会话 ID")
    component_name: str = Field(..., description="组件名称")
    data_pattern: str = Field(..., description="数据模式")
    feedback_type: str = Field(..., description="反馈类型: click, view_duration, dismiss, explicit, conversion, error")
    feedback_value: float = Field(..., ge=0, le=1, description="反馈值 (0-1)")
    context: Optional[Dict[str, Any]] = Field(default=None, description="反馈上下文")
    metadata: Optional[Dict[str, Any]] = Field(default=None, description="元数据")


class FeedbackResponse(BaseModel):
    """反馈响应模型"""
    success: bool
    message: str
    record_id: Optional[str] = None


class PerformanceStatsResponse(BaseModel):
    """性能统计响应模型"""
    component_name: str
    data_pattern: str
    total_interactions: int
    positive_rate: float
    negative_rate: float
    avg_score: float
    trend: str
    priority_adjustment: int


class ABTestRequest(BaseModel):
    """A/B 测试请求模型"""
    test_name: str = Field(..., description="测试名称")
    data_pattern: str = Field(..., description="数据模式")
    control_component: str = Field(..., description="对照组组件")
    variant_component: str = Field(..., description="变体组件")
    traffic_ratio: float = Field(default=0.5, ge=0, le=1, description="流量分配比例")


class ABTestResponse(BaseModel):
    """A/B 测试响应模型"""
    success: bool
    test_name: str
    message: str


@router.post("/submit", response_model=FeedbackResponse)
async def submit_feedback(request: FeedbackRequest):
    """
    提交用户反馈

    用于收集用户对 UI 组件的交互反馈，支持以下反馈类型：
    - click: 点击反馈
    - view_duration: 查看时长
    - dismiss: 关闭/忽略
    - explicit: 显式反馈（如评分）
    - conversion: 转化
    - error: 错误
    """
    try:
        from app.agent.core import get_feedback_learning_system, FeedbackType

        feedback_system = get_feedback_learning_system()

        # 转换反馈类型
        try:
            feedback_type = FeedbackType(request.feedback_type)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid feedback_type: {request.feedback_type}"
            )

        # 记录反馈
        feedback_system.record_feedback(
            session_id=request.session_id,
            component_name=request.component_name,
            data_pattern=request.data_pattern,
            feedback_type=feedback_type,
            feedback_value=request.feedback_value,
            context=request.context,
            metadata=request.metadata
        )

        logger.info(f"Feedback recorded: {request.component_name} - {request.feedback_type}")

        return FeedbackResponse(
            success=True,
            message="Feedback recorded successfully",
            record_id=f"{request.session_id}_{datetime.now().timestamp()}"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error recording feedback: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stats/{component_name}", response_model=PerformanceStatsResponse)
async def get_component_stats(
    component_name: str,
    data_pattern: str = Query(..., description="数据模式")
):
    """
    获取组件性能统计

    返回指定组件在特定数据模式下的性能统计信息
    """
    try:
        from app.agent.core import get_feedback_learning_system

        feedback_system = get_feedback_learning_system()
        stats = feedback_system.get_performance_stats(component_name, data_pattern)

        if not stats:
            raise HTTPException(
                status_code=404,
                detail=f"No stats found for {component_name} with pattern {data_pattern}"
            )

        # 计算优先级调整
        priority_adj = feedback_system.get_priority_adjustment(component_name, data_pattern)

        return PerformanceStatsResponse(
            component_name=component_name,
            data_pattern=data_pattern,
            total_interactions=stats.total_interactions,
            positive_rate=stats.positive_rate,
            negative_rate=stats.negative_rate,
            avg_score=stats.avg_score,
            trend=stats.trend,
            priority_adjustment=priority_adj
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stats", response_model=List[Dict[str, Any]])
async def get_all_stats(
    limit: int = Query(default=50, ge=1, le=200, description="返回数量限制")
):
    """
    获取所有组件性能统计

    返回所有已记录组件的性能统计信息
    """
    try:
        from app.agent.core import get_feedback_learning_system

        feedback_system = get_feedback_learning_system()

        # 获取所有性能记录
        all_stats = []
        store = feedback_system._store

        conn = store._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT component_name, data_pattern, total_interactions,
                   positive_rate, negative_rate, avg_score, trend, last_updated
            FROM component_performance
            ORDER BY last_updated DESC
            LIMIT ?
        """, (limit,))

        rows = cursor.fetchall()
        for row in rows:
            all_stats.append({
                "component_name": row[0],
                "data_pattern": row[1],
                "total_interactions": row[2],
                "positive_rate": row[3],
                "negative_rate": row[4],
                "avg_score": row[5],
                "trend": row[6],
                "last_updated": row[7],
                "priority_adjustment": feedback_system.get_priority_adjustment(row[0], row[1])
            })

        return all_stats

    except Exception as e:
        logger.error(f"Error getting all stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ab-test/start", response_model=ABTestResponse)
async def start_ab_test(request: ABTestRequest):
    """
    启动 A/B 测试

    创建新的 A/B 测试，比较两个组件在相同数据模式下的性能
    """
    try:
        from app.agent.core import get_feedback_learning_system

        feedback_system = get_feedback_learning_system()

        feedback_system.start_ab_test(
            test_name=request.test_name,
            data_pattern=request.data_pattern,
            control_component=request.control_component,
            variant_component=request.variant_component,
            traffic_ratio=request.traffic_ratio
        )

        logger.info(f"A/B test started: {request.test_name}")

        return ABTestResponse(
            success=True,
            test_name=request.test_name,
            message="A/B test started successfully"
        )

    except Exception as e:
        logger.error(f"Error starting A/B test: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ab-test/stop/{test_name}", response_model=ABTestResponse)
async def stop_ab_test(test_name: str):
    """
    停止 A/B 测试
    """
    try:
        from app.agent.core import get_feedback_learning_system

        feedback_system = get_feedback_learning_system()
        feedback_system.stop_ab_test(test_name)

        logger.info(f"A/B test stopped: {test_name}")

        return ABTestResponse(
            success=True,
            test_name=test_name,
            message="A/B test stopped successfully"
        )

    except Exception as e:
        logger.error(f"Error stopping A/B test: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/ab-test/results/{test_name}")
async def get_ab_test_results(test_name: str):
    """
    获取 A/B 测试结果

    返回指定 A/B 测试的结果和统计分析
    """
    try:
        from app.agent.core import get_feedback_learning_system

        feedback_system = get_feedback_learning_system()
        results = feedback_system.get_ab_test_results(test_name)

        if not results:
            raise HTTPException(
                status_code=404,
                detail=f"A/B test not found: {test_name}"
            )

        return {
            "success": True,
            "test_name": test_name,
            "results": results
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting A/B test results: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/priority/{component_name}")
async def get_priority_adjustment(
    component_name: str,
    data_pattern: str = Query(..., description="数据模式")
):
    """
    获取组件优先级调整值

    返回基于用户反馈学习计算的组件优先级调整值
    """
    try:
        from app.agent.core import get_feedback_learning_system

        feedback_system = get_feedback_learning_system()
        adjustment = feedback_system.get_priority_adjustment(component_name, data_pattern)

        return {
            "component_name": component_name,
            "data_pattern": data_pattern,
            "priority_adjustment": adjustment
        }

    except Exception as e:
        logger.error(f"Error getting priority adjustment: {e}")
        raise HTTPException(status_code=500, detail=str(e))
