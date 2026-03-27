"""
知识库存储 - 本地文件实现

提供知识条目的本地存储和检索能力，目录层级保持与原对象存储一致。
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional

from loguru import logger

from .models import KnowledgeEntry
from app.services.file_generator import build_download_url_for_path, get_local_object_store_dir


class KnowledgeStore:
    """
    知识库存储 - 本地文件实现

    存储结构:
    app/data/object_storage/knowledge/
    └── {user_id}/
        ├── index.json
        └── entries/
            ├── {id}.json
            └── {id}.txt
    """

    def __init__(self, bucket: str = None):
        self._root = get_local_object_store_dir() / "knowledge"

    def _ensure_user_root(self, user_id: str) -> Path:
        user_root = self._root / str(user_id)
        (user_root / "entries").mkdir(parents=True, exist_ok=True)
        return user_root

    def _index_path(self, user_id: str) -> Path:
        return self._ensure_user_root(user_id) / "index.json"

    def _entry_json_path(self, user_id: str, entry_id: str) -> Path:
        return self._ensure_user_root(user_id) / "entries" / f"{entry_id}.json"

    def _entry_txt_path(self, user_id: str, entry_id: str) -> Path:
        return self._ensure_user_root(user_id) / "entries" / f"{entry_id}.txt"

    async def save_entries(
        self,
        user_id: str,
        entries: List[KnowledgeEntry]
    ) -> Dict[str, str]:
        urls = {}

        for entry in entries:
            entry.user_id = user_id
            try:
                json_path = self._entry_json_path(user_id, entry.entry_id)
                txt_path = self._entry_txt_path(user_id, entry.entry_id)

                json_path.write_text(
                    json.dumps(entry.to_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                txt_path.write_text(entry.to_text(), encoding="utf-8")
                urls[entry.entry_id] = build_download_url_for_path(json_path)
            except Exception as e:
                logger.error(f"[KnowledgeStore] Failed to save entry {entry.entry_id}: {e}")

        await self._update_index(user_id, entries)
        logger.info(f"[KnowledgeStore] Saved {len(urls)} entries for user {user_id}")
        return urls

    async def _update_index(self, user_id: str, new_entries: List[KnowledgeEntry]):
        index_path = self._index_path(user_id)
        if index_path.exists():
            try:
                index = json.loads(index_path.read_text(encoding="utf-8"))
            except Exception:
                index = {"user_id": user_id, "entries": [], "updated_at": None}
        else:
            index = {"user_id": user_id, "entries": [], "updated_at": None}

        existing_entries = {
            item.get("entry_id"): item
            for item in index.get("entries", [])
            if item.get("entry_id")
        }
        for entry in new_entries:
            existing_entries[entry.entry_id] = {
                "entry_id": entry.entry_id,
                "category": entry.category,
                "question": entry.question[:100],
                "entities": entry.entities,
                "topics": entry.topics,
                "created_at": entry.created_at.isoformat() if entry.created_at else None,
                "confidence": entry.confidence,
            }

        index["entries"] = sorted(
            existing_entries.values(),
            key=lambda item: item.get("created_at") or "",
            reverse=True,
        )
        index["updated_at"] = datetime.now().isoformat()
        index["total_count"] = len(index["entries"])
        index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    async def search(
        self,
        user_id: str,
        query: str = None,
        entities: List[str] = None,
        topics: List[str] = None,
        category: str = None,
        limit: int = 20
    ) -> List[Dict[str, Any]]:
        index_path = self._index_path(user_id)
        if not index_path.exists():
            return []

        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except Exception:
            return []

        results = []
        for meta in index.get("entries", []):
            if category and meta.get("category") != category:
                continue
            if entities and not set(entities) & set(meta.get("entities", [])):
                continue
            if topics and not set(topics) & set(meta.get("topics", [])):
                continue
            if query and query.lower() not in meta.get("question", "").lower():
                continue
            results.append(meta)

        results.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return results[:limit]

    async def get_entry(self, user_id: str, entry_id: str) -> Optional[KnowledgeEntry]:
        path = self._entry_json_path(user_id, entry_id)
        if not path.exists():
            logger.warning(f"[KnowledgeStore] Entry not found: {entry_id}")
            return None

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return KnowledgeEntry.from_dict(data)
        except Exception as e:
            logger.warning(f"[KnowledgeStore] Failed to read entry {entry_id}: {e}")
            return None

    async def get_stats(self, user_id: str) -> Dict[str, Any]:
        index_path = self._index_path(user_id)
        if index_path.exists():
            try:
                index = json.loads(index_path.read_text(encoding="utf-8"))
            except Exception:
                index = {"entries": []}
        else:
            index = {"entries": []}

        entries = index.get("entries", [])
        category_counts = {}
        topic_counts = {}
        entity_counts = {}

        for entry in entries:
            cat = entry.get("category", "unknown")
            category_counts[cat] = category_counts.get(cat, 0) + 1

            for topic in entry.get("topics", []):
                topic_counts[topic] = topic_counts.get(topic, 0) + 1

            for entity in entry.get("entities", []):
                entity_counts[entity] = entity_counts.get(entity, 0) + 1

        return {
            "total_entries": len(entries),
            "by_category": category_counts,
            "top_topics": sorted(topic_counts.items(), key=lambda x: -x[1])[:10],
            "top_entities": sorted(entity_counts.items(), key=lambda x: -x[1])[:10],
            "updated_at": index.get("updated_at"),
        }

    def get_index_url(self, user_id: str) -> str:
        return build_download_url_for_path(self._index_path(user_id))
