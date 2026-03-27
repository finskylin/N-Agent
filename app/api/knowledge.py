"""
知识库 API 路由

提供知识文件上传、管理、查询、摘要等接口。
保留原有知识条目查询端点，新增文件管理端点。
所有文件管理端点加认证 + 私有过滤。

NOTE: 固定路径端点 (/tree, /files, /categories) 必须在
      动态路径端点 (/{user_id}/search) 之前注册，否则 FastAPI
      会将 "tree" 误匹配为 user_id。
"""
import os
from typing import Optional
from fastapi import APIRouter, Query, HTTPException, UploadFile, File, Form, Depends, Request
from loguru import logger
from pydantic import BaseModel


router = APIRouter(prefix="/api/v1/knowledge", tags=["Knowledge"])

MINIO_KNOWLEDGE_BUCKET = os.getenv("MINIO_KNOWLEDGE_BUCKET", "knowledge")


# ============================================================
# 认证依赖
# ============================================================

async def get_current_user_id(request: Request) -> str:
    """从 Request Header 获取当前用户 ID"""
    return request.headers.get("x-user-id", "1")


# ============================================================
# 请求模型
# ============================================================

class CreateCategoryRequest(BaseModel):
    label: str
    description: str = ""
    icon: str = "📚"
    scope: str = "public"  # public | private


class UpdateCategoryRequest(BaseModel):
    label: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    scope: Optional[str] = None
    auto_inject: Optional[bool] = None


def get_knowledge_store():
    """获取知识库存储实例"""
    from app.agent.v4.knowledge import KnowledgeStore
    return KnowledgeStore()


# ============================================================
# 固定路径端点（必须在 /{user_id} 之前注册）
# ============================================================

# --- 树形结构 ---

@router.get("/tree")
async def get_tree(
    current_user_id: str = Depends(get_current_user_id),
):
    """获取树形结构（已按可见性过滤 private）"""
    from app.services.knowledge_file_service import get_tree as svc_get_tree

    try:
        tree = await svc_get_tree(user_id=current_user_id)
        return {"code": 200, "data": tree}
    except Exception as e:
        logger.error(f"[Knowledge API] Get tree failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# --- 分类 CRUD ---

@router.get("/categories")
async def list_categories(
    current_user_id: str = Depends(get_current_user_id),
):
    """获取分类列表（含文件数，已过滤 private）"""
    from app.services.knowledge_file_service import get_categories_with_stats

    try:
        categories = await get_categories_with_stats(user_id=current_user_id)
        return {"code": 200, "data": categories}
    except Exception as e:
        logger.error(f"[Knowledge API] List categories failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/categories")
async def create_category(
    body: CreateCategoryRequest,
    current_user_id: str = Depends(get_current_user_id),
):
    """创建知识库"""
    from app.services.knowledge_file_service import create_category as svc_create

    try:
        result = await svc_create(
            label=body.label,
            description=body.description,
            icon=body.icon,
            scope=body.scope,
            user_id=current_user_id,
        )
        return {"code": 200, "data": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[Knowledge API] Create category failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/categories/{category}")
async def update_category(
    category: str,
    body: UpdateCategoryRequest,
    current_user_id: str = Depends(get_current_user_id),
):
    """编辑知识库信息"""
    from app.services.knowledge_file_service import update_category as svc_update

    try:
        updates = body.model_dump(exclude_none=True)
        result = await svc_update(
            category=category,
            updates=updates,
            user_id=current_user_id,
        )
        if not result:
            raise HTTPException(status_code=404, detail="知识库不存在")
        return {"code": 200, "data": result}
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Knowledge API] Update category failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/categories/{category}")
async def delete_category(
    category: str,
    current_user_id: str = Depends(get_current_user_id),
):
    """删除知识库（系统知识库不可删）"""
    from app.services.knowledge_file_service import delete_category as svc_delete

    try:
        success = await svc_delete(category=category, user_id=current_user_id)
        if not success:
            raise HTTPException(status_code=404, detail="知识库不存在")
        return {"code": 200, "message": "删除成功"}
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Knowledge API] Delete category failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/categories/{category}/summary")
async def get_category_summary(category: str):
    """获取目录摘要"""
    from app.services.knowledge_file_service import KnowledgeMetadataCache

    cache = KnowledgeMetadataCache.get_instance()
    summaries = cache.get_directory_summaries()
    summary = summaries.get(category, "")

    if not summary:
        return {"code": 200, "data": {"category": category, "summary": ""}}

    return {"code": 200, "data": {"category": category, "summary": summary}}


# --- 文件管理端点 ---

@router.post("/files/upload")
async def upload_file(
    file: UploadFile = File(...),
    category: str = Form("general"),
    scope: str = Form("public"),
    current_user_id: str = Depends(get_current_user_id),
):
    """
    上传文件到知识库

    - file: 上传的文件
    - category: 分类 (system/projects/general/动态知识库path)
    - scope: 范围 (public/private)
    - user_id 从请求 header 获取
    """
    from app.services.knowledge_file_service import upload_and_process_file

    try:
        file_data = await file.read()
        if not file_data:
            raise HTTPException(status_code=400, detail="文件内容为空")

        result = await upload_and_process_file(
            file_data=file_data,
            file_name=file.filename,
            user_id=current_user_id,
            category=category,
            scope=scope,
        )
        return {"code": 200, "data": result}

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[Knowledge API] Upload failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/files")
async def list_files(
    scope: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    file_type: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user_id: str = Depends(get_current_user_id),
):
    """文件列表查询（支持筛选和分页，private 文件只返回自己的）"""
    from app.services.knowledge_file_service import list_knowledge_files

    try:
        result = await list_knowledge_files(
            scope=scope,
            category=category,
            file_type=file_type,
            keyword=keyword,
            status=status,
            page=page,
            page_size=page_size,
            user_id=current_user_id,
        )
        return {"code": 200, "data": result}
    except Exception as e:
        logger.error(f"[Knowledge API] List files failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/files/{file_id}")
async def get_file_detail(
    file_id: int,
    current_user_id: str = Depends(get_current_user_id),
):
    """获取文件详情"""
    from app.services.knowledge_file_service import get_knowledge_file

    record = await get_knowledge_file(file_id)
    if not record:
        raise HTTPException(status_code=404, detail="文件不存在")
    return {"code": 200, "data": record}


@router.put("/files/{file_id}")
async def update_file_metadata(
    file_id: int,
    body: dict,
    current_user_id: str = Depends(get_current_user_id),
):
    """
    更新文件元数据

    允许更新: category, scope
    """
    from app.services.knowledge_file_service import (
        get_knowledge_file,
        update_knowledge_file_record,
    )

    record = await get_knowledge_file(file_id)
    if not record:
        raise HTTPException(status_code=404, detail="文件不存在")

    allowed_fields = {"category", "scope"}
    updates = {k: v for k, v in body.items() if k in allowed_fields}
    if not updates:
        raise HTTPException(status_code=400, detail="无有效更新字段")

    await update_knowledge_file_record(file_id, updates)
    updated = await get_knowledge_file(file_id)
    return {"code": 200, "data": updated}


@router.delete("/files/{file_id}")
async def delete_file(
    file_id: int,
    current_user_id: str = Depends(get_current_user_id),
):
    """软删除文件"""
    from app.services.knowledge_file_service import soft_delete_knowledge_file

    try:
        success = await soft_delete_knowledge_file(file_id, user_id=current_user_id)
        if not success:
            raise HTTPException(status_code=404, detail="文件不存在")
        return {"code": 200, "message": "删除成功"}
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.post("/files/{file_id}/reprocess")
async def reprocess_file(file_id: int):
    """重新处理文件（重新提取文本和生成摘要）"""
    from app.services.knowledge_file_service import reprocess_file as do_reprocess

    success = await do_reprocess(file_id)
    if not success:
        raise HTTPException(status_code=404, detail="文件不存在")
    return {"code": 200, "message": "已启动重新处理"}


# ============================================================
# 动态路径端点（必须在固定路径之后注册）
# ============================================================

@router.get("/{user_id}/search")
async def search_knowledge(
    user_id: str,
    q: Optional[str] = Query(None, description="搜索关键词"),
    entities: Optional[str] = Query(None, description="实体过滤，逗号分隔"),
    topics: Optional[str] = Query(None, description="主题过滤，逗号分隔"),
    category: Optional[str] = Query(None, description="分类: fact/insight/data/conclusion"),
    limit: int = Query(20, le=100)
):
    try:
        store = get_knowledge_store()
        results = await store.search(
            user_id=user_id,
            query=q,
            entities=entities.split(",") if entities else None,
            topics=topics.split(",") if topics else None,
            category=category,
            limit=limit
        )
        return {"code": 200, "data": {"total": len(results), "entries": results}}
    except Exception as e:
        logger.error(f"[Knowledge API] Search failed: {e}")
        return {"code": 500, "message": str(e), "data": {"total": 0, "entries": []}}


@router.get("/{user_id}/entries/{entry_id}")
async def get_knowledge_entry(user_id: str, entry_id: str):
    try:
        store = get_knowledge_store()
        entry = await store.get_entry(user_id, entry_id)
        if not entry:
            raise HTTPException(status_code=404, detail="Knowledge entry not found")
        return {"code": 200, "data": entry.to_dict()}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Knowledge API] Get entry failed: {e}")
        return {"code": 500, "message": str(e)}


@router.get("/{user_id}/index")
async def get_knowledge_index(user_id: str):
    store = get_knowledge_store()
    index_url = store.get_index_url(user_id)
    return {"code": 200, "data": {"user_id": user_id, "index_url": index_url}}


@router.get("/{user_id}/stats")
async def get_knowledge_stats(user_id: str):
    try:
        store = get_knowledge_store()
        stats = await store.get_stats(user_id)
        return {"code": 200, "data": stats}
    except Exception as e:
        logger.error(f"[Knowledge API] Get stats failed: {e}")
        return {"code": 500, "message": str(e), "data": {"total_entries": 0}}
