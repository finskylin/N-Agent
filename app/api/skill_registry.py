"""
Skill Registry REST API -- 提供 Skill 注册/注销/查询接口

端点:
- POST   /api/v1/skills/register  — 注册外部 API Skill
- DELETE /api/v1/skills/{name}    — 注销 Skill
- GET    /api/v1/skills/          — 查询所有已注册 Skill
- GET    /api/v1/skills/stats     — 统计信息
"""
from typing import Dict, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from loguru import logger


router = APIRouter(prefix="/api/v1/skills", tags=["skills"])


# ── 请求/响应模型 ──

class RegisterExternalAPIRequest(BaseModel):
    name: str = Field(..., description="Skill 名称（唯一）")
    description: str = Field(..., description="描述")
    endpoint: str = Field(..., description="API 端点 URL")
    method: str = Field(default="POST", description="HTTP 方法")
    headers: Optional[Dict[str, str]] = Field(default=None, description="请求头")
    input_schema: Optional[Dict] = Field(default=None, description="输入 JSON Schema")
    output_schema: Optional[Dict] = Field(default=None, description="输出 JSON Schema")
    timeout: int = Field(default=30, description="超时（秒）")
    category: str = Field(default="external", description="分类")
    priority: int = Field(default=40, description="优先级")


class RegisteredSkillResponse(BaseModel):
    name: str
    display_name: str = ""
    description: str = ""
    category: str = ""
    source: str = ""
    priority: int = 50


# ── 全局 registry 引用（在 main.py 中注入） ──

_registry = None


def set_registry(registry):
    """设置全局 SkillRegistryV2 实例（由 main.py 调用）"""
    global _registry
    _registry = registry


def _get_registry():
    """获取全局 SkillRegistryV2 实例"""
    if _registry is None:
        raise HTTPException(
            status_code=503,
            detail="Skill registry not initialized"
        )
    return _registry


# ── 端点 ──

@router.post("/register", response_model=RegisteredSkillResponse)
async def register_external_api(req: RegisterExternalAPIRequest):
    """注册外部 API 为 Skill"""
    registry = _get_registry()
    try:
        skill = registry.register_external_api(
            name=req.name,
            description=req.description,
            endpoint=req.endpoint,
            method=req.method,
            headers=req.headers,
            input_schema=req.input_schema,
            output_schema=req.output_schema,
            timeout=req.timeout,
            category=req.category,
            priority=req.priority,
        )
        return RegisteredSkillResponse(
            name=skill.name,
            display_name=skill.display_name,
            description=skill.description,
            category=skill.category,
            source=skill.source,
            priority=skill.priority,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{name}")
async def unregister_skill(name: str):
    """注销 Skill"""
    registry = _get_registry()
    success = registry.unregister_external_api(name)
    if not success:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found in external registry")
    return {"status": "ok", "name": name}


@router.get("/", response_model=list)
async def list_skills():
    """查询所有已注册 Skill"""
    registry = _get_registry()
    all_skills = registry.get_all_registered()
    return [
        {
            "name": s.name,
            "display_name": s.display_name,
            "description": s.description,
            "category": s.category,
            "source": s.source,
            "priority": s.priority,
        }
        for s in all_skills
    ]


@router.get("/stats")
async def skill_stats():
    """统计信息"""
    registry = _get_registry()
    return registry.get_stats()
