"""
Skill 输出处理器

统一处理 Skill 执行后的输出，完成：
1. 数据序列化
2. 存储策略决策（内联 vs 本地文件）
3. 数据库持久化
4. SSE 事件生成
5. UI 组件提取
"""
import json
from datetime import datetime
from typing import Any, Dict, List, Optional
from loguru import logger

from agent_core.skill_output import SkillOutput, CollectedItem, ProcessResult
from .models import V4SkillOutput


INLINE_STORAGE_THRESHOLD = 64 * 1024  # 64KB - 内联存储阈值
LOCAL_SKILL_DATA_BUCKET = "skill-data"


class OutputProcessor:
    """
    Skill 输出处理器

    负责将 SkillOutput 存储到数据库和本地文件，并生成前端事件
    """

    def __init__(self, db_session=None):
        """
        初始化处理器

        Args:
            db_session: SQLAlchemy 异步 session（可选，延迟获取）
        """
        self._db = db_session

    async def process(
        self,
        output: SkillOutput,
        db_session=None
    ) -> ProcessResult:
        """
        处理 Skill 输出

        Args:
            output: Skill 输出结构
            db_session: 数据库会话（可选，优先使用）

        Returns:
            ProcessResult: 包含 db_id, download_url, ui_components, sse_events
        """
        result = ProcessResult()
        db = db_session or self._db

        try:
            # 1. 序列化原始数据
            raw_json = output.serialize_raw_data()
            raw_size = len(raw_json.encode('utf-8'))

            # 2. 序列化 UI 组件
            ui_components_json = json.dumps(output.ui_components, ensure_ascii=False)

            # 3. 决定存储方式
            raw_data_json = None
            raw_data_url = None

            if raw_size < INLINE_STORAGE_THRESHOLD:
                # 内联存储
                raw_data_json = raw_json
                logger.debug(f"[OutputProcessor] Inline storage: {raw_size} bytes")
            else:
                # 本地文件存储
                raw_data_url = await self._upload_to_local_storage(output, raw_json)
                if raw_data_url:
                    result.download_url = raw_data_url
                    logger.info(f"[OutputProcessor] Local file storage: {raw_size} bytes -> {raw_data_url}")
                else:
                    # 避免静默丢数，落盘失败时退回数据库内联存储
                    raw_data_json = raw_json
                    logger.warning(
                        f"[OutputProcessor] Local storage unavailable, fallback to inline storage: {raw_size} bytes"
                    )

            # 4. 创建数据库记录
            db_record = V4SkillOutput(
                execution_id=output.execution_id,
                user_id=output.user_id,
                session_id=output.session_id,
                skill_name=output.skill_name,
                query=output.query,
                raw_data_json=raw_data_json,
                raw_data_url=raw_data_url,
                raw_data_size=raw_size,
                items_count=output.items_count,
                ui_components_json=ui_components_json,
                component_version="1.0",
                success=output.success,
                error_message=output.error,
                duration_ms=output.duration_ms,
                executed_at=output.executed_at,
            )

            # 5. 保存到数据库
            if db:
                db.add(db_record)
                await db.commit()
                await db.refresh(db_record)
                result.db_id = db_record.id
                output.db_id = db_record.id
                logger.debug(f"[OutputProcessor] Saved to database: id={db_record.id}")
            else:
                logger.warning("[OutputProcessor] No db_session, skipping database save")

            # 6. 设置结果
            result.ui_components = output.ui_components
            result.sse_events = self._generate_sse_events(output)

            # 7. 更新 output 的存储信息
            output.raw_data_url = raw_data_url

        except Exception as e:
            logger.error(f"[OutputProcessor] Failed to process output: {e}")
            result.sse_events = [{
                "event": "error",
                "data": {"error": str(e), "skill_name": output.skill_name}
            }]

        return result

    async def _upload_to_local_storage(self, output: SkillOutput, data: str) -> Optional[str]:
        """
        将大数据保存到本地对象存储

        Args:
            output: Skill 输出
            data: JSON 字符串

        Returns:
            下载 URL，失败返回 None
        """
        try:
            date_str = output.executed_at.strftime("%Y-%m-%d") if output.executed_at else datetime.now().strftime("%Y-%m-%d")
            object_name = f"{output.user_id}/{date_str}/{output.session_id}/{output.execution_id}.json"
            from app.services.file_generator import save_bytes_to_local_storage

            storage = save_bytes_to_local_storage(
                file_data=data.encode("utf-8"),
                bucket_name=LOCAL_SKILL_DATA_BUCKET,
                object_name=object_name,
            )
            return storage["download_url"]
        except Exception as e:
            logger.error(f"[OutputProcessor] Local storage write failed: {e}")
            return None

    def _generate_sse_events(self, output: SkillOutput) -> List[Dict[str, Any]]:
        """
        生成 SSE 事件

        Args:
            output: Skill 输出

        Returns:
            SSE 事件列表
        """
        events = []

        # 1. tool_done 事件
        events.append({
            "event": "tool_done",
            "data": {
                "skill_name": output.skill_name,
                "execution_id": output.execution_id,
                "success": output.success,
                "duration_ms": output.duration_ms,
                "items_count": output.items_count,
            }
        })

        # 2. component 事件（每个 UI 组件一个）
        for component in output.ui_components:
            events.append({
                "event": "component",
                "data": {
                    "skill_name": output.skill_name,
                    "execution_id": output.execution_id,
                    "component": component
                }
            })

        return events


class OutputProcessorFactory:
    """
    OutputProcessor 工厂

    提供全局单例和便捷创建方法
    """

    _instance: Optional[OutputProcessor] = None

    @classmethod
    def get_instance(cls) -> OutputProcessor:
        """获取全局单例"""
        if cls._instance is None:
            cls._instance = OutputProcessor()
        return cls._instance

    @classmethod
    def create(cls, db_session=None) -> OutputProcessor:
        """创建新实例"""
        return OutputProcessor(db_session=db_session)


# 便捷函数
def get_output_processor() -> OutputProcessor:
    """获取输出处理器实例"""
    return OutputProcessorFactory.get_instance()
