"""
知识库文件管理服务

职责:
1. 文件上传（本地 + MinIO 双写）
2. 文本提取（通过 document_parser 公共层）
3. LLM 生成摘要 + 关键词
4. 目录级 _summary.md 维护
5. Database CRUD
6. 内存缓存（供 prompt_builder 同步读取）
"""

import os
import time
import json
import asyncio
from copy import deepcopy
from datetime import datetime
from typing import Dict, Any, Optional, List
from pathlib import Path

from loguru import logger

from app.services.document_parser import (
    parse_file,
    detect_file_type,
    detect_mime_type,
    EXT_TO_MIME,
)

from app.config import settings


_LOCAL_STORE_LOCK = asyncio.Lock()


def _load_knowledge_config() -> Dict[str, Any]:
    """从 knowledge.json 加载配置"""
    config_path = Path(__file__).parent.parent.parent / "agent_core" / "config" / "knowledge" / "knowledge.json"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"[KnowledgeService] Failed to load knowledge.json: {e}")
        return {
            "enabled": True,
            "base_dir": "app/knowledge",
            "categories": [],
            "inject_rules": {},
            "upload": {},
        }


def _get_knowledge_base_path() -> Path:
    """获取知识库根目录"""
    config = _load_knowledge_config()
    base_dir = config.get("base_dir", "app/knowledge")
    return Path(base_dir)


def _use_local_metadata_store() -> bool:
    """仅在完全未配置 DATABASE_URL 时才退回本地 JSON 元数据。"""
    return not bool(getattr(settings, "database_url", ""))


def _get_local_metadata_store_path() -> Path:
    meta_dir = _get_knowledge_base_path() / ".metadata"
    meta_dir.mkdir(parents=True, exist_ok=True)
    return meta_dir / "knowledge_store.json"


def _default_local_store() -> Dict[str, Any]:
    return {
        "next_file_id": 1,
        "files": [],
        "categories": [],
    }


def _normalize_local_store(store: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(store or {})
    normalized.setdefault("next_file_id", 1)
    normalized.setdefault("files", [])
    normalized.setdefault("categories", [])
    return normalized


def _load_local_store_sync() -> Dict[str, Any]:
    path = _get_local_metadata_store_path()
    if not path.exists():
        return _default_local_store()
    try:
        return _normalize_local_store(json.loads(path.read_text(encoding="utf-8")))
    except Exception as e:
        logger.warning(f"[KnowledgeService] Failed to read local metadata store: {e}")
        return _default_local_store()


def _save_local_store_sync(store: Dict[str, Any]):
    path = _get_local_metadata_store_path()
    path.write_text(
        json.dumps(_normalize_local_store(store), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


async def _read_local_store() -> Dict[str, Any]:
    async with _LOCAL_STORE_LOCK:
        return deepcopy(_load_local_store_sync())


async def _write_local_store(store: Dict[str, Any]):
    async with _LOCAL_STORE_LOCK:
        _save_local_store_sync(store)


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _sync_category_file_stats(store: Dict[str, Any]) -> None:
    active_files = [f for f in store.get("files", []) if f.get("status") != "deleted"]
    totals: Dict[str, Dict[str, int]] = {}
    for file_item in active_files:
        cat = file_item.get("category", "")
        item = totals.setdefault(cat, {"file_count": 0, "total_size": 0})
        item["file_count"] += 1
        item["total_size"] += int(file_item.get("file_size") or 0)

    for cat in store.get("categories", []):
        stats = totals.get(cat.get("category", ""), {"file_count": 0, "total_size": 0})
        cat["file_count"] = stats["file_count"]
        cat["total_size"] = stats["total_size"]
        cat["updated_at"] = _now_str()


def _find_local_category(store: Dict[str, Any], category: str) -> Optional[Dict[str, Any]]:
    for item in store.get("categories", []):
        if item.get("category") == category:
            return item
    return None


# ============================================================
# 目录摘要缓存（单例，供 prompt_builder 同步读取）
# ============================================================

class KnowledgeMetadataCache:
    """
    单例缓存，从本地 _summary.md 读取目录摘要。
    prompt_builder 同步调用 get_directory_summaries()。
    """
    _instance = None
    _cache: Dict[str, str] = {}
    _last_refresh: float = 0
    TTL = 300  # 5 分钟

    @classmethod
    def get_instance(cls) -> "KnowledgeMetadataCache":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def get_directory_summaries(self) -> Dict[str, str]:
        """同步方法：返回 {category_path: summary_text}"""
        if time.time() - self._last_refresh > self.TTL:
            self._refresh_sync()
        return dict(self._cache)

    def _refresh_sync(self):
        """从本地 _summary.md 文件读取"""
        config = _load_knowledge_config()
        knowledge_base = _get_knowledge_base_path()
        summary_file_name = config.get("inject_rules", {}).get("summary_file_name", "_summary.md")

        new_cache = {}
        for cat in config.get("categories", []):
            if not cat.get("auto_inject", True):
                continue
            cat_path = cat["path"]
            summary_path = knowledge_base / cat_path / summary_file_name
            if summary_path.exists():
                try:
                    new_cache[cat_path] = summary_path.read_text(encoding="utf-8")
                except Exception as e:
                    logger.warning(f"[KnowledgeCache] Failed to read {summary_path}: {e}")

        self._cache = new_cache
        self._last_refresh = time.time()
        logger.debug(f"[KnowledgeCache] Refreshed: {len(new_cache)} categories")

    def invalidate(self):
        """上传/删除后立即失效缓存"""
        self._last_refresh = 0


def upload_bytes_to_local_storage(
    file_data: bytes,
    bucket_name: str,
    object_name: str,
    content_type: str = "application/octet-stream",
) -> str:
    """将字节写入本地对象存储，并返回下载 URL。"""
    try:
        from app.services.file_generator import save_bytes_to_local_storage

        storage = save_bytes_to_local_storage(
            file_data=file_data,
            bucket_name=bucket_name,
            object_name=object_name,
        )
        logger.info(
            f"[KnowledgeService] Stored locally: {bucket_name}/{storage['object_name']} "
            f"({len(file_data)} bytes, content_type={content_type})"
        )
        return storage["download_url"]
    except Exception as e:
        logger.error(f"[KnowledgeService] Local object storage failed: {e}")
        return ""


# ============================================================
# LLM 摘要生成
# ============================================================

async def _generate_file_summary_and_keywords(text_content: str) -> Dict[str, Any]:
    """
    用 LLM 生成文件摘要和关键词

    Returns:
        {"summary": "...", "keywords": ["kw1", "kw2", ...]}
    """
    if not text_content or len(text_content.strip()) < 10:
        return {"summary": "", "keywords": []}

    # 截断过长内容
    truncated = text_content[:8000]

    prompt = f"""请对以下文档内容生成：
1. 一句话摘要（不超过100个字，概括文档的核心内容）
2. 关键词列表（5-10个，反映文档主要主题）

请以 JSON 格式输出：
{{"summary": "摘要文本", "keywords": ["关键词1", "关键词2", ...]}}

文档内容：
{truncated}"""

    try:
        from agent_core.agentloop.llm_provider import call_llm
        import re

        content = await call_llm(
            prompt=prompt,
            use_small_fast=True,
            max_tokens=500,
            timeout=30.0,
            temperature=0.3,
        )

        # 解析 JSON
        json_match = re.search(r'\{[\s\S]*\}', content)
        if json_match:
            parsed = json.loads(json_match.group())
            return {
                "summary": str(parsed.get("summary", ""))[:100],
                "keywords": list(parsed.get("keywords", []))[:10],
            }

    except Exception as e:
        logger.warning(f"[KnowledgeService] LLM summary failed: {e}")

    return _fallback_summary(text_content)


def _fallback_summary(text_content: str) -> Dict[str, Any]:
    """降级摘要：取前100字作为摘要"""
    clean = text_content.strip().replace("\n", " ")[:100]
    return {"summary": clean, "keywords": []}


# ============================================================
# 目录摘要 _summary.md 管理
# ============================================================

async def update_directory_summary(
    category: str,
    new_file_info: Optional[Dict] = None,
    removed_file_name: Optional[str] = None,
):
    """
    更新目录级 _summary.md

    Args:
        category: 分类路径 (如 "system")
        new_file_info: 新增文件信息 {"file_name", "file_type", "summary", "keywords"}
        removed_file_name: 被删除的文件名
    """
    config = _load_knowledge_config()
    knowledge_base = _get_knowledge_base_path()
    summary_file_name = config.get("inject_rules", {}).get("summary_file_name", "_summary.md")
    max_overview_chars = config.get("inject_rules", {}).get("summary_max_overview_chars", 500)
    max_keywords = config.get("inject_rules", {}).get("summary_max_keywords", 30)

    summary_path = knowledge_base / category / summary_file_name
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    # 读取现有摘要
    existing = ""
    if summary_path.exists():
        existing = summary_path.read_text(encoding="utf-8")

    # 新增文件
    if new_file_info:
        file_name = new_file_info.get("file_name", "")
        # 检查是否已存在
        if file_name in existing:
            logger.info(f"[KnowledgeService] {file_name} already in summary, skipping")
            return

        file_type = new_file_info.get("file_type", "").upper()
        summary = new_file_info.get("summary", "")[:100]
        keywords = new_file_info.get("keywords", [])
        kw_str = ", ".join(keywords) if keywords else ""

        # 追加到文件索引
        new_row = f"| {file_name} | {file_type} | {kw_str} | {summary} |"
        existing = _append_to_file_index(existing, new_row, category)

    # 删除文件
    if removed_file_name:
        existing = _remove_from_file_index(existing, removed_file_name)

    # 更新关键词聚合（从所有索引行提取）
    existing = _update_keywords_section(existing, max_keywords)

    # 更新统计
    file_count = existing.count("| ") // 4  # 粗略估算
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    existing = _update_header(existing, category, now_str, file_count)

    # 限制概述长度
    existing = _enforce_overview_limit(existing, max_overview_chars)

    # 三重持久化
    # 1. 本地文件
    summary_path.write_text(existing, encoding="utf-8")
    logger.info(f"[KnowledgeService] Updated {summary_path}")

    # 2. 本地对象存储
    bucket = config.get("upload", {}).get("minio_bucket", "knowledge")
    object_name = f"{category}/{summary_file_name}"
    upload_bytes_to_local_storage(
        existing.encode("utf-8"),
        bucket,
        object_name,
        "text/markdown",
    )

    # 3. MySQL（通过 category_summaries 表）
    try:
        await _update_category_summary_in_db(category, existing, file_count)
    except Exception as e:
        logger.warning(f"[KnowledgeService] DB summary update failed: {e}")

    # 4. 失效缓存
    KnowledgeMetadataCache.get_instance().invalidate()


def _append_to_file_index(content: str, new_row: str, category: str) -> str:
    """在文件索引表末尾追加新行"""
    marker = "## 文件索引"
    kw_marker = "## 关键词聚合"

    if marker not in content:
        # 初始化摘要文档
        content = f"""# {category} 知识库摘要

> 最后更新: - | 文件数: 0

## 内容概述
本目录知识库资料。

## 文件索引

| 文件 | 类型 | 关键词 | 简述 |
|------|------|--------|------|
{new_row}

## 关键词聚合
"""
        return content

    # 找到关键词聚合段之前的位置插入
    if kw_marker in content:
        idx = content.index(kw_marker)
        content = content[:idx] + new_row + "\n\n" + content[idx:]
    else:
        content = content.rstrip() + "\n" + new_row + "\n"

    return content


def _remove_from_file_index(content: str, file_name: str) -> str:
    """从文件索引中移除指定文件行"""
    lines = content.split("\n")
    filtered = [line for line in lines if file_name not in line]
    return "\n".join(filtered)


def _update_keywords_section(content: str, max_keywords: int) -> str:
    """从文件索引中提取关键词并更新聚合段"""
    import re

    # 提取所有 "| xxx | TYPE | kw1, kw2 | summary |" 行中的关键词
    all_keywords = set()
    for line in content.split("\n"):
        if line.startswith("|") and "|" in line:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 5 and parts[3] and parts[1] not in ("文件", "---", ""):
                for kw in parts[3].split(","):
                    kw = kw.strip()
                    if kw and kw not in ("关键词", "---"):
                        all_keywords.add(kw)

    kw_text = ", ".join(sorted(all_keywords)[:max_keywords])
    marker = "## 关键词聚合"
    if marker in content:
        idx = content.index(marker)
        content = content[:idx] + f"{marker}\n{kw_text}\n"
    else:
        content = content.rstrip() + f"\n\n{marker}\n{kw_text}\n"

    return content


def _update_header(content: str, category: str, date_str: str, file_count: int) -> str:
    """更新摘要头部的统计信息"""
    import re
    content = re.sub(
        r'> 最后更新:.*',
        f'> 最后更新: {date_str} | 文件数: {file_count}',
        content,
    )
    return content


def _enforce_overview_limit(content: str, max_chars: int) -> str:
    """确保内容概述不超过字符限制"""
    start_marker = "## 内容概述"
    end_marker = "## 文件索引"

    if start_marker in content and end_marker in content:
        start_idx = content.index(start_marker) + len(start_marker)
        end_idx = content.index(end_marker)
        overview = content[start_idx:end_idx].strip()
        if len(overview) > max_chars:
            overview = overview[:max_chars] + "..."
            content = content[:start_idx] + "\n" + overview + "\n\n" + content[end_idx:]

    return content


# ============================================================
# 数据库操作
# ============================================================

async def _get_db_session():
    """获取异步数据库 session"""
    from app.db.database import AsyncSessionLocal
    return AsyncSessionLocal()


async def create_knowledge_file_record(data: Dict[str, Any]) -> int:
    """创建知识库文件记录，返回 ID"""
    if _use_local_metadata_store():
        store = await _read_local_store()
        file_id = int(store.get("next_file_id", 1))
        record = dict(data)
        record["id"] = file_id
        record.setdefault("summary", "")
        record.setdefault("keywords", [])
        record.setdefault("word_count", None)
        record.setdefault("page_count", None)
        record.setdefault("process_error", None)
        record.setdefault("created_at", _now_str())
        record.setdefault("updated_at", record["created_at"])
        store["files"].append(record)
        store["next_file_id"] = file_id + 1
        _sync_category_file_stats(store)
        await _write_local_store(store)
        return file_id

    from app.db.models_db import KnowledgeFile

    async with await _get_db_session() as session:
        record = KnowledgeFile(**data)
        session.add(record)
        await session.commit()
        await session.refresh(record)
        return int(record.id)


async def update_knowledge_file_record(file_id: int, updates: Dict[str, Any]):
    """更新知识库文件记录"""
    if _use_local_metadata_store():
        store = await _read_local_store()
        updated = False
        for item in store.get("files", []):
            if int(item.get("id", 0)) == int(file_id):
                for key, value in updates.items():
                    item[key] = value
                item["updated_at"] = _now_str()
                updated = True
                break
        if updated:
            _sync_category_file_stats(store)
            await _write_local_store(store)
        return

    from sqlalchemy import select
    from app.db.models_db import KnowledgeFile

    async with await _get_db_session() as session:
        result = await session.execute(
            select(KnowledgeFile).where(KnowledgeFile.id == file_id)
        )
        record = result.scalar_one_or_none()
        if not record:
            return
        for key, value in updates.items():
            setattr(record, key, value)
        await session.commit()


async def get_knowledge_file(file_id: int) -> Optional[Dict]:
    """获取单个文件记录"""
    if _use_local_metadata_store():
        store = await _read_local_store()
        for item in store.get("files", []):
            if int(item.get("id", 0)) == int(file_id):
                return deepcopy(item)
        return None

    from sqlalchemy import select
    from app.db.models_db import KnowledgeFile

    async with await _get_db_session() as session:
        result = await session.execute(
            select(KnowledgeFile).where(KnowledgeFile.id == file_id)
        )
        row = result.scalar_one_or_none()
        if not row:
            return None
        data = row.to_dict()
        data["directory_summary"] = row.directory_summary
        return data


async def list_knowledge_files(
    scope: Optional[str] = None,
    category: Optional[str] = None,
    file_type: Optional[str] = None,
    keyword: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    文件列表查询（带筛选和分页）

    user_id 用于过滤 private 知识库的文件：
    - private 分类的文件只返回 user_id 上传的
    - public 分类的文件全返回
    """
    if _use_local_metadata_store():
        store = await _read_local_store()
        categories = {c.get("category"): c for c in store.get("categories", [])}
        items = []
        for item in store.get("files", []):
            if item.get("status") == "deleted":
                continue
            cat_meta = categories.get(item.get("category"), {})
            if user_id and cat_meta.get("scope") == "private" and str(item.get("user_id")) != str(user_id):
                continue
            if scope and item.get("scope") != scope:
                continue
            if category and item.get("category") != category:
                continue
            if file_type and item.get("file_type") != file_type:
                continue
            if status and item.get("status") != status:
                continue
            if keyword:
                haystacks = [
                    str(item.get("file_name", "")),
                    str(item.get("summary", "")),
                    " ".join(item.get("keywords", []) or []),
                ]
                if keyword.lower() not in " ".join(haystacks).lower():
                    continue
            items.append(deepcopy(item))

        items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        total = len(items)
        start = (page - 1) * page_size
        end = start + page_size
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": items[start:end],
        }

    from sqlalchemy import desc, select
    from app.db.models_db import KnowledgeCategorySummary, KnowledgeFile

    async with await _get_db_session() as session:
        category_rows = await session.execute(select(KnowledgeCategorySummary))
        categories = {row.category: row for row in category_rows.scalars().all()}

        stmt = select(KnowledgeFile).where(KnowledgeFile.status != "deleted")
        if scope:
            stmt = stmt.where(KnowledgeFile.scope == scope)
        if category:
            stmt = stmt.where(KnowledgeFile.category == category)
        if file_type:
            stmt = stmt.where(KnowledgeFile.file_type == file_type)
        if status:
            stmt = stmt.where(KnowledgeFile.status == status)

        rows = (await session.execute(stmt.order_by(desc(KnowledgeFile.created_at)))).scalars().all()

    items = []
    kw = (keyword or "").lower()
    for row in rows:
        cat_meta = categories.get(row.category)
        if user_id and cat_meta and cat_meta.scope == "private" and str(row.user_id) != str(user_id):
            continue
        if kw:
            haystacks = [
                row.file_name or "",
                row.summary or "",
                " ".join(row.keywords or []),
            ]
            if kw not in " ".join(haystacks).lower():
                continue
        item = row.to_dict()
        item["directory_summary"] = row.directory_summary
        items.append(item)

    total = len(items)
    start = (page - 1) * page_size
    end = start + page_size
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": items[start:end],
    }


async def soft_delete_knowledge_file(file_id: int, user_id: Optional[str] = None) -> bool:
    """
    软删除文件

    Args:
        file_id: 文件 ID
        user_id: 当前用户 ID，用于 private 文件权限检查
    """
    record = await get_knowledge_file(file_id)
    if not record:
        return False

    # 权限检查：private 分类的文件只能被上传者删除
    if user_id:
        if _use_local_metadata_store():
            store = await _read_local_store()
            cat_record = _find_local_category(store, record["category"])
            if cat_record and cat_record.get("scope") == "private":
                if str(record.get("user_id")) != str(user_id):
                    raise PermissionError("无权删除此文件")
        else:
            from sqlalchemy import select as _select
            from app.db.models_db import KnowledgeCategorySummary
            try:
                async with await _get_db_session() as session:
                    cat_row = await session.execute(
                        _select(KnowledgeCategorySummary).where(
                            KnowledgeCategorySummary.category == record["category"]
                        )
                    )
                    cat_record = cat_row.scalar_one_or_none()
                    if cat_record and cat_record.scope == "private":
                        if str(record.get("user_id")) != str(user_id):
                            raise PermissionError("无权删除此文件")
            except PermissionError:
                raise
            except Exception:
                pass  # 分类不存在时不阻塞删除

    await update_knowledge_file_record(file_id, {"status": "deleted"})

    # 更新目录摘要
    try:
        await update_directory_summary(
            category=record["category"],
            removed_file_name=record["file_name"],
        )
    except Exception as e:
        logger.warning(f"[KnowledgeService] Summary update after delete failed: {e}")

    return True


async def _update_category_summary_in_db(category: str, summary_content: str, file_count: int):
    """更新分类摘要到数据库"""
    if _use_local_metadata_store():
        store = await _read_local_store()
        cat = _find_local_category(store, category)
        if cat:
            cat["summary_content"] = summary_content
            cat["file_count"] = file_count
            cat["updated_at"] = _now_str()
            _sync_category_file_stats(store)
            await _write_local_store(store)
        return

    from sqlalchemy import select
    from app.db.models_db import KnowledgeCategorySummary

    async with await _get_db_session() as session:
        result = await session.execute(
            select(KnowledgeCategorySummary).where(KnowledgeCategorySummary.category == category)
        )
        record = result.scalar_one_or_none()
        if record is None:
            record = KnowledgeCategorySummary(
                category=category,
                label=category,
                scope="public",
                auto_inject=1,
                is_system=0,
            )
            session.add(record)
        record.summary_content = summary_content
        record.file_count = file_count
        await session.commit()


async def get_categories_with_stats(user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    从 DB 读取所有分类（public 全返回 + private 只返回 user_id 的）

    Args:
        user_id: 当前用户 ID，用于过滤 private 知识库
    """
    if _use_local_metadata_store():
        store = await _read_local_store()
        _sync_category_file_stats(store)
        await _write_local_store(store)
        categories = []
        for row_dict in store.get("categories", []):
            if row_dict.get("scope") == "private":
                if not user_id or str(row_dict.get("created_by")) != str(user_id):
                    continue
            categories.append({
                "id": row_dict.get("id"),
                "path": row_dict.get("category"),
                "label": row_dict.get("label") or row_dict.get("category"),
                "description": row_dict.get("description") or "",
                "icon": row_dict.get("icon") or "📚",
                "scope": row_dict.get("scope") or "public",
                "auto_inject": bool(row_dict.get("auto_inject", 1)),
                "is_system": bool(row_dict.get("is_system", 0)),
                "created_by": row_dict.get("created_by"),
                "file_count": row_dict.get("file_count") or 0,
                "total_size": row_dict.get("total_size") or 0,
            })
        categories.sort(key=lambda x: (not x["is_system"], x["scope"], x["path"]))
        return categories

    try:
        from sqlalchemy import select
        from app.db.models_db import KnowledgeCategorySummary, KnowledgeFile

        async with await _get_db_session() as session:
            rows = (await session.execute(select(KnowledgeCategorySummary))).scalars().all()
            file_rows = (await session.execute(
                select(KnowledgeFile).where(KnowledgeFile.status != "deleted")
            )).scalars().all()

        stats: Dict[str, Dict[str, int]] = {}
        for file_row in file_rows:
            item = stats.setdefault(file_row.category, {"file_count": 0, "total_size": 0})
            item["file_count"] += 1
            item["total_size"] += int(file_row.file_size or 0)

        categories = []
        for row in rows:
            row_dict = row.to_dict()
            real_stats = stats.get(row.category, {"file_count": 0, "total_size": 0})
            # 过滤 private: 只有自己的或 public 的才返回
            if row_dict.get("scope") == "private":
                if not user_id or str(row_dict.get("created_by")) != str(user_id):
                    continue
            categories.append({
                "id": row_dict.get("id"),
                "path": row_dict.get("category"),
                "label": row_dict.get("label") or row_dict.get("category"),
                "description": row_dict.get("description") or "",
                "icon": row_dict.get("icon") or "📚",
                "scope": row_dict.get("scope") or "public",
                "auto_inject": bool(row_dict.get("auto_inject", 1)),
                "is_system": bool(row_dict.get("is_system", 0)),
                "created_by": row_dict.get("created_by"),
                "file_count": real_stats["file_count"],
                "total_size": real_stats["total_size"],
            })
        categories.sort(key=lambda x: (not x["is_system"], x["scope"], x["path"]))
        return categories

    except Exception as e:
        logger.error(f"[KnowledgeService] get_categories_with_stats failed: {e}")
        # 降级：从 knowledge.json 读取
        config = _load_knowledge_config()
        return [
            {
                "path": cat["path"],
                "label": cat.get("label", cat["path"]),
                "description": cat.get("description", ""),
                "icon": "📚",
                "scope": "public",
                "auto_inject": cat.get("auto_inject", True),
                "is_system": True,
                "file_count": 0,
                "total_size": 0,
            }
            for cat in config.get("categories", [])
        ]


async def get_tree(user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """获取树形结构（已按可见性过滤）"""
    return await get_categories_with_stats(user_id=user_id)


def _generate_category_path(label: str) -> str:
    """从中文名生成 path slug"""
    import hashlib
    slug = hashlib.md5(label.encode()).hexdigest()[:8]
    return f"kb_{slug}"


async def create_category(
    label: str,
    description: str = "",
    icon: str = "📚",
    scope: str = "public",
    user_id: str = "1",
) -> Dict[str, Any]:
    """创建知识库"""
    path = _generate_category_path(label)

    if _use_local_metadata_store():
        store = await _read_local_store()
        while _find_local_category(store, path):
            path = f"{path}_{int(time.time()) % 10000}"
        next_ids = [int(c.get("id", 0)) for c in store.get("categories", []) if c.get("id") is not None]
        category_record = {
            "id": (max(next_ids) + 1) if next_ids else 1,
            "category": path,
            "label": label,
            "description": description,
            "icon": icon,
            "scope": scope,
            "auto_inject": 1 if scope == "public" else 0,
            "is_system": 0,
            "created_by": user_id,
            "file_count": 0,
            "total_size": 0,
            "created_at": _now_str(),
            "updated_at": _now_str(),
        }
        store["categories"].append(category_record)
        await _write_local_store(store)
    else:
        from sqlalchemy import select
        from app.db.models_db import KnowledgeCategorySummary

        async with await _get_db_session() as session:
            existing = await session.execute(
                select(KnowledgeCategorySummary).where(KnowledgeCategorySummary.category == path)
            )
            if existing.scalar_one_or_none():
                # slug 冲突，加 timestamp
                path = f"{path}_{int(time.time()) % 10000}"

            session.add(
                KnowledgeCategorySummary(
                    category=path,
                    label=label,
                    description=description,
                    icon=icon,
                    scope=scope,
                    auto_inject=1 if scope == "public" else 0,
                    is_system=0,
                    created_by=user_id,
                )
            )
            await session.commit()

    # 创建本地目录
    knowledge_base = _get_knowledge_base_path()
    (knowledge_base / path).mkdir(parents=True, exist_ok=True)

    logger.info(f"[KnowledgeService] Created category: {path} ({label})")
    return {
        "path": path,
        "label": label,
        "description": description,
        "icon": icon,
        "scope": scope,
        "is_system": False,
        "created_by": user_id,
        "file_count": 0,
        "total_size": 0,
    }


async def update_category(
    category: str,
    updates: Dict[str, Any],
    user_id: str = "1",
) -> Optional[Dict[str, Any]]:
    """编辑知识库信息（只有创建者可编辑）"""
    if _use_local_metadata_store():
        store = await _read_local_store()
        record_dict = _find_local_category(store, category)
        if not record_dict:
            return None
        if record_dict.get("is_system"):
            raise ValueError("系统知识库不可编辑")
        if str(record_dict.get("created_by")) != str(user_id):
            raise PermissionError("无权编辑此知识库")
        allowed = {"label", "description", "icon", "scope", "auto_inject"}
        filtered = {k: v for k, v in updates.items() if k in allowed}
        if not filtered:
            raise ValueError("无有效更新字段")
        record_dict.update(filtered)
        record_dict["updated_at"] = _now_str()
        await _write_local_store(store)
        return deepcopy(record_dict)

    async with await _get_db_session() as session:
        from sqlalchemy import select
        from app.db.models_db import KnowledgeCategorySummary

        row = await session.execute(
            select(KnowledgeCategorySummary).where(KnowledgeCategorySummary.category == category)
        )
        record = row.scalar_one_or_none()
        if not record:
            return None

        record_dict = record.to_dict()
        if record_dict.get("is_system"):
            raise ValueError("系统知识库不可编辑")
        if str(record_dict.get("created_by")) != str(user_id):
            raise PermissionError("无权编辑此知识库")

        # 允许更新的字段
        allowed = {"label", "description", "icon", "scope", "auto_inject"}
        filtered = {k: v for k, v in updates.items() if k in allowed}
        if not filtered:
            raise ValueError("无有效更新字段")

        for key, value in filtered.items():
            setattr(record, key, value)
        await session.commit()
        return record.to_dict()


async def delete_category(category: str, user_id: str = "1") -> bool:
    """删除知识库（is_system=1 不可删除）"""
    if _use_local_metadata_store():
        store = await _read_local_store()
        record_dict = _find_local_category(store, category)
        if not record_dict:
            return False
        if record_dict.get("is_system"):
            raise ValueError("系统知识库不可删除")
        if str(record_dict.get("created_by")) != str(user_id):
            raise PermissionError("无权删除此知识库")
        for item in store.get("files", []):
            if item.get("category") == category:
                item["status"] = "deleted"
                item["updated_at"] = _now_str()
        store["categories"] = [c for c in store.get("categories", []) if c.get("category") != category]
        await _write_local_store(store)
        logger.info(f"[KnowledgeService] Deleted category: {category}")
        KnowledgeMetadataCache.get_instance().invalidate()
        return True

    async with await _get_db_session() as session:
        from sqlalchemy import select
        from app.db.models_db import KnowledgeCategorySummary, KnowledgeFile

        row = await session.execute(
            select(KnowledgeCategorySummary).where(KnowledgeCategorySummary.category == category)
        )
        record = row.scalar_one_or_none()
        if not record:
            return False

        record_dict = record.to_dict()
        if record_dict.get("is_system"):
            raise ValueError("系统知识库不可删除")
        if str(record_dict.get("created_by")) != str(user_id):
            raise PermissionError("无权删除此知识库")

        files = await session.execute(
            select(KnowledgeFile).where(KnowledgeFile.category == category)
        )
        for file_row in files.scalars().all():
            file_row.status = "deleted"
        await session.delete(record)
        await session.commit()

    logger.info(f"[KnowledgeService] Deleted category: {category}")
    KnowledgeMetadataCache.get_instance().invalidate()
    return True


async def seed_system_categories():
    """从 knowledge.json 读取预设分类，seed 到 DB（仅首次/增量）"""
    config = _load_knowledge_config()
    categories = config.get("categories", [])
    if not categories:
        logger.info("[KnowledgeService] No categories in knowledge.json to seed")
        return

    if _use_local_metadata_store():
        store = await _read_local_store()
        next_ids = [int(c.get("id", 0)) for c in store.get("categories", []) if c.get("id") is not None]
        next_id = (max(next_ids) + 1) if next_ids else 1
        changed = False
        for cat in categories:
            cat_path = cat["path"]
            label = cat.get("label", cat_path)
            description = cat.get("description", "")
            auto_inject = 1 if cat.get("auto_inject", True) else 0
            icon = cat.get("icon", "📚")
            icon_map = {"system": "⚙️", "projects": "📁", "public": "🌍", "private": "🔒"}
            if icon == "📚":
                icon = icon_map.get(cat_path, "📚")
            scope = "private" if cat_path == "private" else "public"

            existing = _find_local_category(store, cat_path)
            if existing:
                existing.update({
                    "label": label or existing.get("label"),
                    "description": description or existing.get("description"),
                    "icon": icon or existing.get("icon"),
                    "scope": scope,
                    "auto_inject": auto_inject,
                    "is_system": 1,
                    "updated_at": _now_str(),
                })
            else:
                store["categories"].append({
                    "id": next_id,
                    "category": cat_path,
                    "label": label,
                    "description": description,
                    "icon": icon,
                    "scope": scope,
                    "auto_inject": auto_inject,
                    "is_system": 1,
                    "created_by": None,
                    "file_count": 0,
                    "total_size": 0,
                    "created_at": _now_str(),
                    "updated_at": _now_str(),
                })
                next_id += 1
            changed = True
        if changed:
            _sync_category_file_stats(store)
            await _write_local_store(store)
            logger.info(f"[KnowledgeService] Seeded {len(categories)} system categories to local store")
        return

    try:
        from sqlalchemy import select
        from app.db.models_db import KnowledgeCategorySummary

        async with await _get_db_session() as session:
            for cat in categories:
                cat_path = cat["path"]
                label = cat.get("label", cat_path)
                description = cat.get("description", "")
                auto_inject = 1 if cat.get("auto_inject", True) else 0
                icon = cat.get("icon", "📚")
                # 映射默认图标
                icon_map = {
                    "system": "⚙️",
                    "projects": "📁",
                    "public": "🌍",
                    "private": "🔒",
                }
                if icon == "📚":
                    icon = icon_map.get(cat_path, "📚")

                scope = "private" if cat_path == "private" else "public"

                existing = await session.execute(
                    select(KnowledgeCategorySummary).where(KnowledgeCategorySummary.category == cat_path)
                )
                record = existing.scalar_one_or_none()
                if record is None:
                    record = KnowledgeCategorySummary(category=cat_path, created_by=None)
                    session.add(record)
                record.label = label
                record.description = description
                record.icon = icon
                record.scope = scope
                record.auto_inject = auto_inject
                record.is_system = 1
            await session.commit()
            logger.info(f"[KnowledgeService] Seeded {len(categories)} system categories to database")
    except Exception as e:
        logger.warning(f"[KnowledgeService] Seed system categories failed: {e}")


# ============================================================
# 文件上传 + 处理主流程
# ============================================================

async def upload_and_process_file(
    file_data: bytes,
    file_name: str,
    user_id: str,
    category: str = "general",
    scope: str = "public",
) -> Dict[str, Any]:
    """
    上传文件并启动后台处理

    Returns:
        创建的文件记录 dict
    """
    config = _load_knowledge_config()
    upload_config = config.get("upload", {})

    # 1. 校验文件类型
    ext = Path(file_name).suffix.lower()
    allowed = upload_config.get("allowed_extensions", [])
    if allowed and ext not in allowed:
        raise ValueError(f"不支持的文件类型: {ext}")

    # 2. 校验文件大小
    max_size_mb = upload_config.get("max_file_size_mb", 50)
    if len(file_data) > max_size_mb * 1024 * 1024:
        raise ValueError(f"文件大小超过限制: {max_size_mb}MB")

    # 3. 保存到本地
    knowledge_base = _get_knowledge_base_path()
    timestamp = int(time.time())
    safe_name = f"{timestamp}_{file_name}"

    if scope == "private":
        local_dir = knowledge_base / "private" / user_id / category
    else:
        local_dir = knowledge_base / scope if scope != "public" else knowledge_base / category

    # 如果 scope 不是 private，按 category 组织
    if scope == "public":
        local_dir = knowledge_base / category

    local_dir.mkdir(parents=True, exist_ok=True)
    local_path = local_dir / safe_name

    with open(local_path, "wb") as f:
        f.write(file_data)
    logger.info(f"[KnowledgeService] Saved locally: {local_path}")

    # 4. 上传到本地对象存储
    bucket = upload_config.get("minio_bucket", "knowledge")
    if scope == "private":
        object_name = f"private/{user_id}/{category}/{safe_name}"
    else:
        object_name = f"{category}/{safe_name}"

    mime_type = detect_mime_type(file_name)
    storage_url = upload_bytes_to_local_storage(file_data, bucket, object_name, mime_type)

    # 5. 创建 DB 记录
    file_type = detect_file_type(file_name) or ext.lstrip(".")
    record_data = {
        "user_id": user_id,
        "scope": scope,
        "file_name": file_name,
        "file_type": file_type,
        "file_size": len(file_data),
        "mime_type": mime_type,
        "local_path": str(local_path),
        "minio_url": storage_url,
        "minio_object_name": object_name,
        "category": category,
        "status": "processing",
    }
    file_id = await create_knowledge_file_record(record_data)
    record_data["id"] = file_id

    # 6. 后台任务：文本提取 + 摘要生成
    from app.utils.background_task_manager import create_background_task
    create_background_task(
        _process_file_background(file_id, str(local_path), category),
        task_name="process_knowledge_file"
    )

    return record_data


async def _process_file_background(file_id: int, local_path: str, category: str):
    """后台文件处理：文本提取 → LLM 摘要 → 目录摘要更新"""
    try:
        logger.info(f"[KnowledgeService] Background processing file #{file_id}")

        # a. 文本提取
        parsed = await parse_file(local_path)
        text_content = parsed.get("text_content", "")
        word_count = parsed.get("word_count", len(text_content))
        page_count = parsed.get("page_count")

        if parsed.get("error"):
            await update_knowledge_file_record(file_id, {
                "status": "error",
                "process_error": parsed["error"],
            })
            return

        # b. LLM 生成摘要 + 关键词
        summary_data = await _generate_file_summary_and_keywords(text_content)

        # c. 更新 DB 记录
        updates = {
            "status": "active",
            "summary": summary_data.get("summary", ""),
            "keywords": summary_data.get("keywords", []),
            "word_count": word_count,
        }
        if page_count:
            updates["page_count"] = page_count
        await update_knowledge_file_record(file_id, updates)

        # d. 更新目录 _summary.md
        record = await get_knowledge_file(file_id)
        if record:
            await update_directory_summary(
                category=category,
                new_file_info={
                    "file_name": record["file_name"],
                    "file_type": record["file_type"],
                    "summary": summary_data.get("summary", ""),
                    "keywords": summary_data.get("keywords", []),
                },
            )

        logger.info(f"[KnowledgeService] File #{file_id} processed successfully")

    except Exception as e:
        logger.error(f"[KnowledgeService] Background processing failed for #{file_id}: {e}")
        try:
            await update_knowledge_file_record(file_id, {
                "status": "error",
                "process_error": str(e),
            })
        except Exception:
            pass


async def reprocess_file(file_id: int) -> bool:
    """重新处理文件"""
    record = await get_knowledge_file(file_id)
    if not record:
        return False

    await update_knowledge_file_record(file_id, {"status": "processing", "process_error": None})
    from app.utils.background_task_manager import create_background_task
    create_background_task(
        _process_file_background(
            file_id, record["local_path"], record["category"]
        ),
        task_name="reprocess_knowledge_file"
    )
    return True


# ============================================================
# 卡住文件恢复
# ============================================================

async def recover_stuck_processing_files(stuck_threshold_hours: float = 1.0) -> int:
    """
    将长时间处于 "processing" 状态的文件标记为 "error"。

    Args:
        stuck_threshold_hours: 超过此时长（小时）仍为 processing 则视为卡死，默认 1 小时

    Returns:
        恢复的文件数量
    """
    from datetime import timezone

    threshold_seconds = stuck_threshold_hours * 3600
    now = datetime.now()
    recovered = 0

    result = await list_knowledge_files(status="processing", page=1, page_size=1000)
    files = result.get("items", [])

    for f in files:
        updated_at_str = f.get("updated_at", "")
        if not updated_at_str:
            continue
        try:
            updated_at = datetime.fromisoformat(updated_at_str)
        except ValueError:
            continue

        elapsed = (now - updated_at).total_seconds()
        if elapsed >= threshold_seconds:
            file_id = f.get("id")
            file_name = f.get("file_name", "?")
            logger.warning(
                f"[KnowledgeService] File #{file_id} ({file_name}) stuck in 'processing' "
                f"for {elapsed/3600:.1f}h — marking as error"
            )
            try:
                await update_knowledge_file_record(file_id, {
                    "status": "error",
                    "process_error": f"Processing timeout after {elapsed/3600:.1f}h",
                })
                recovered += 1
            except Exception as e:
                logger.error(f"[KnowledgeService] Failed to recover file #{file_id}: {e}")

    if recovered:
        logger.info(f"[KnowledgeService] Recovered {recovered} stuck file(s)")
    return recovered
