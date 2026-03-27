"""
GAIA 数据集加载器

从本地 JSONL 文件加载 GAIA 数据集，支持按 level/limit/task_ids 过滤。
"""
import json
from pathlib import Path
from typing import Optional

from loguru import logger

from app.benchmark.models import GaiaQuestion


class GaiaDatasetLoader:
    """从本地 JSONL 加载 GAIA 数据集"""

    def __init__(self, base_path: str, split: str = "validation", metadata_file: str = "metadata.jsonl"):
        self.base_path = Path(base_path)
        self.split = split
        self.metadata_file = metadata_file

    @property
    def metadata_path(self) -> Path:
        return self.base_path / self.split / self.metadata_file

    def load(
        self,
        levels: Optional[list[int]] = None,
        limit: Optional[int] = None,
        task_ids: Optional[list[str]] = None,
    ) -> list[GaiaQuestion]:
        """
        加载并过滤 GAIA 数据集。

        Args:
            levels: 只加载指定 level 的题目（1, 2, 3）
            limit: 最多返回的题目数量
            task_ids: 只加载指定 task_id 的题目

        Returns:
            GaiaQuestion 列表
        """
        path = self.metadata_path
        if not path.exists():
            raise FileNotFoundError(
                f"GAIA metadata file not found: {path}\n"
                f"Please download the dataset first:\n"
                f"  python -c \"from datasets import load_dataset; "
                f"ds = load_dataset('gaia-benchmark/GAIA', '2023_all'); "
                f"ds.save_to_disk('{self.base_path}')\""
            )

        questions: list[GaiaQuestion] = []
        task_id_set = set(task_ids) if task_ids else None

        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as e:
                    logger.warning(f"Skipping invalid JSON line: {e}")
                    continue

                # 映射字段（GAIA 数据集字段名可能有差异）
                question = GaiaQuestion(
                    task_id=raw.get("task_id", ""),
                    question=raw.get("Question", raw.get("question", "")),
                    level=int(raw.get("Level", raw.get("level", 0))),
                    final_answer=raw.get("Final answer", raw.get("final_answer", "")),
                    file_name=raw.get("file_name"),
                    file_path=raw.get("file_path"),
                )

                # 过滤 level
                if levels and question.level not in levels:
                    continue

                # 过滤 task_ids
                if task_id_set and question.task_id not in task_id_set:
                    continue

                questions.append(question)

        # 应用 limit
        if limit is not None and limit > 0:
            questions = questions[:limit]

        logger.info(
            f"[GAIA Dataset] Loaded {len(questions)} questions "
            f"(levels={levels}, limit={limit}, task_ids={len(task_ids) if task_ids else 'all'})"
        )
        return questions
