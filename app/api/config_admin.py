"""
Configuration Management API
配置管理相关的 API 端点
"""
import json
from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime
from loguru import logger

# config_auto_generator removed - configuration now managed via SDK
# from app.agent.core.config_auto_generator import get_config_auto_generator, GenerationResult

class GenerationResult:
    """Placeholder for removed GenerationResult"""
    def __init__(self):
        self.success = False
        self.message = "Config auto-generator has been removed. Use SDK native configuration."


def get_config_auto_generator():
    """Placeholder for removed config_auto_generator"""
    return None


router = APIRouter(prefix="/api/v1/admin/config", tags=["Config Management"])


class ConfigRegenerateRequest(BaseModel):
    """配置重新生成请求"""
    force: bool = False
    include_skills: bool = True
    include_agents: bool = True
    include_tools: bool = True
    include_mcps: bool = True
    llm_model: str = "claude-3-5-sonnet-20241022"


class ConfigRollbackRequest(BaseModel):
    """配置回滚请求"""
    to_timestamp: str
    reason: str


class TaskResponse(BaseModel):
    """任务响应"""
    task_id: str
    status: str
    estimated_time_seconds: int
    result_url: str


class TaskDetailResponse(BaseModel):
    """任务详情响应"""
    task_id: str
    status: str
    started_at: str
    completed_at: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


# 全局任务存储（生产环境应使用 Redis 或数据库）
_tasks: Dict[str, GenerationResult] = {}


@router.post("/regenerate", response_model=TaskResponse)
async def regenerate_config(
    request: ConfigRegenerateRequest,
    background_tasks: BackgroundTasks
):
    """
    手动触发配置重新生成

    功能：
    1. 扫描所有 Skills/Agents/Tools/MCPs
    2. 调用 LLM 自动生成配置
    3. 验证生成的配置
    4. 备份旧配置
    5. 应用新配置
    """
    try:
        logger.info("Config regeneration requested")

        # 创建生成器
        generator = get_config_auto_generator()

        # 在后台执行生成任务
        task_id = f"config_regenerate_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        async def run_generation():
            try:
                result = await generator.generate(
                    force=request.force,
                    model=request.llm_model
                )
                _tasks[task_id] = result
                logger.info(f"Generation task {task_id} completed with status: {result.status}")
            except Exception as e:
                logger.error(f"Generation task {task_id} failed: {e}")
                _tasks[task_id] = GenerationResult(
                    task_id=task_id,
                    status="failed",
                    started_at=datetime.now().isoformat(),
                    completed_at=datetime.now().isoformat(),
                    error=str(e)
                )

        # 添加后台任务
        background_tasks.add_task(run_generation)

        # 先创建一个 started 状态的任务记录
        _tasks[task_id] = GenerationResult(
            task_id=task_id,
            status="started",
            started_at=datetime.now().isoformat()
        )

        return TaskResponse(
            task_id=task_id,
            status="started",
            estimated_time_seconds=30,
            result_url=f"/api/v1/admin/config/tasks/{task_id}"
        )

    except Exception as e:
        logger.error(f"Failed to start config regeneration: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to start regeneration: {str(e)}")


@router.get("/tasks/{task_id}", response_model=TaskDetailResponse)
async def get_task_status(task_id: str):
    """
    查询配置生成任务状态

    返回任务的执行状态、进度和结果
    """
    if task_id not in _tasks:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    result = _tasks[task_id]

    # 构建响应
    response_data = {
        "task_id": result.task_id,
        "status": result.status,
        "started_at": result.started_at,
        "completed_at": result.completed_at,
        "error": result.error
    }

    # 如果任务完成，添加结果详情
    if result.status == "completed":
        response_data["result"] = {
            "generated_files": result.generated_files,
            "stats": {
                "skills_processed": result.stats.skills_processed,
                "agents_processed": result.stats.agents_processed,
                "tools_processed": result.stats.tools_processed,
                "mcps_processed": result.stats.mcps_processed,
                "keywords_extracted": result.stats.keywords_extracted,
                "intents_mapped": result.stats.intents_mapped
            },
            "validation": result.validation,
            "backup_path": result.backup_path
        }

    return TaskDetailResponse(**response_data)


@router.post("/rollback")
async def rollback_config(request: ConfigRollbackRequest):
    """
    配置回滚到历史版本

    从备份目录恢复指定时间点的配置
    """
    try:
        logger.info(f"Config rollback requested to {request.to_timestamp}: {request.reason}")

        generator = get_config_auto_generator()

        # 查找备份目录
        backup_path = generator.backup_dir / request.to_timestamp
        if not backup_path.exists():
            raise HTTPException(
                status_code=404,
                detail=f"Backup not found for timestamp: {request.to_timestamp}"
            )

        # 恢复 intent 配置
        intent_backup = backup_path / "intent"
        if intent_backup.exists():
            import shutil
            intent_dir = generator.config_dir / "intent"
            if intent_dir.exists():
                shutil.rmtree(intent_dir)
            shutil.copytree(intent_backup, intent_dir)
            logger.info(f"Restored intent config from {intent_backup}")

        # 恢复 models 配置
        models_backup = backup_path / "models"
        if models_backup.exists():
            import shutil
            models_dir = generator.config_dir / "models"
            if models_dir.exists():
                shutil.rmtree(models_dir)
            shutil.copytree(models_backup, models_dir)
            logger.info(f"Restored models config from {models_backup}")

        # 触发热加载
        # TODO: 调用热加载接口

        return {
            "status": "success",
            "message": f"Config rolled back to {request.to_timestamp}",
            "timestamp": datetime.now().isoformat()
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to rollback config: {e}")
        raise HTTPException(status_code=500, detail=f"Rollback failed: {str(e)}")


@router.get("/backups")
async def list_backups():
    """
    列出所有可用的配置备份

    返回备份时间戳列表
    """
    try:
        generator = get_config_auto_generator()

        if not generator.backup_dir.exists():
            return {"backups": []}

        backups = []
        for backup_path in generator.backup_dir.iterdir():
            if backup_path.is_dir():
                backups.append({
                    "timestamp": backup_path.name,
                    "path": str(backup_path.relative_to(generator.project_root))
                })

        # 按时间戳倒序排列
        backups.sort(key=lambda x: x["timestamp"], reverse=True)

        return {"backups": backups}

    except Exception as e:
        logger.error(f"Failed to list backups: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list backups: {str(e)}")


@router.get("/status")
async def get_config_status():
    """
    获取配置系统状态

    返回当前配置的版本、生成时间等信息
    """
    try:
        generator = get_config_auto_generator()

        # 读取当前配置文件信息
        intent_dir = generator.config_dir / "intent"
        status = {
            "intent_configs": {},
            "models_configs": {},
            "auto_generated": False
        }

        # 检查 intent 配置
        if intent_dir.exists():
            for config_file in intent_dir.glob("*.json"):
                try:
                    with open(config_file, 'r') as f:
                        config_data = json.load(f)
                        status["intent_configs"][config_file.name] = {
                            "version": config_data.get("version", "unknown"),
                            "last_updated": config_data.get("generated_at", config_data.get("last_updated", "unknown"))
                        }
                        if config_data.get("version") == "auto-generated":
                            status["auto_generated"] = True
                except Exception as e:
                    logger.warning(f"Failed to read {config_file}: {e}")

        return status

    except Exception as e:
        logger.error(f"Failed to get config status: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get status: {str(e)}")
