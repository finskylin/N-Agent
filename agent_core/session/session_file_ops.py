"""
SessionFileOps — .jsonl 文件读写

支持会话历史的导入导出（文件级备份）。
"""
import json
from pathlib import Path
from typing import List, Dict
from loguru import logger


class SessionFileOps:
    """会话文件操作"""

    @staticmethod
    def export_to_jsonl(messages: List[Dict], filepath: str):
        """导出对话历史到 .jsonl 文件"""
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            for msg in messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        logger.info(f"[SessionFileOps] Exported {len(messages)} messages to {filepath}")

    @staticmethod
    def import_from_jsonl(filepath: str) -> List[Dict]:
        """从 .jsonl 文件导入对话历史"""
        if not Path(filepath).exists():
            logger.warning(f"[SessionFileOps] File not found: {filepath}")
            return []
        messages = []
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        messages.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        logger.info(f"[SessionFileOps] Imported {len(messages)} messages from {filepath}")
        return messages
