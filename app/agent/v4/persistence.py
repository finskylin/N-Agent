"""
V4 Persistence Layer — App 层业务数据持久化

负责 skill 执行输出、报告反馈、session 元数据等 UI 展示数据。
Session 上下文（对话历史、经验、CLI session 映射）已迁移至 agent_core/session/。

表:
- v4_sessions:        session 生命周期元数据
- v4_skill_outputs:   skill 执行结果 + UI 组件
- v4_report_feedback: 用户对报告的好/差评
"""
import json
from datetime import datetime
from typing import Optional, Dict, List, Any
from loguru import logger
from sqlalchemy import text, select, delete, and_, desc


class V4PersistenceLayer:
    """App 层业务持久化 — skill 输出、反馈、session 元数据"""

    def __init__(self, default_user_id: int = 1, default_instance_id: str = "default"):
        self._default_user_id = default_user_id
        self._default_instance_id = default_instance_id

    def _get_session(self):
        """获取异步数据库 session"""
        from app.db.database import AsyncSessionLocal
        return AsyncSessionLocal()

    async def _upsert_one(self, db, model, filters: Dict[str, Any], values: Dict[str, Any]):
        """通用 upsert，兼容 SQLite/MySQL。"""
        result = await db.execute(select(model).filter_by(**filters))
        row = result.scalar_one_or_none()
        if row is None:
            row = model(**values)
            db.add(row)
        else:
            for key, value in values.items():
                setattr(row, key, value)
        return row


    # ================================================================
    # Session 元数据（Scene Tabs 等）
    # ================================================================

    async def get_session_metadata(self, session_id: str, user_id: Optional[int] = None) -> Dict:
        """
        读取 session 级别的元数据（scene_tabs 等）

        使用 V4SkillOutput 表的特殊记录存储（skill_name = '__session_metadata__'）
        """
        uid = user_id or self._default_user_id
        try:
            from .models import V4SkillOutput
            async with self._get_session() as db:
                stmt = (
                    select(V4SkillOutput.raw_data_json)
                    .where(
                        and_(
                            V4SkillOutput.session_id == session_id,
                            V4SkillOutput.user_id == uid,
                            V4SkillOutput.skill_name == "__session_metadata__",
                        )
                    )
                    .order_by(desc(V4SkillOutput.executed_at))
                    .limit(1)
                )
                result = await db.execute(stmt)
                row = result.scalar_one_or_none()
                if row:
                    try:
                        return json.loads(row)
                    except (json.JSONDecodeError, TypeError):
                        pass
                return {}
        except Exception as e:
            logger.warning(f"[V4-Persist] get_session_metadata error: {e}")
            return {}

    async def update_session_metadata(self, session_id: str, metadata: Dict, user_id: Optional[int] = None):
        """
        更新 session 级别的元数据（upsert 语义）

        读取现有 → merge → INSERT OR UPDATE
        """
        uid = user_id or self._default_user_id
        try:
            # 读取现有 metadata
            existing = await self.get_session_metadata(session_id, user_id=uid)
            # 合并
            existing.update(metadata)
            merged_json = json.dumps(existing, ensure_ascii=False, default=str)

            from .models import V4SkillOutput
            async with self._get_session() as db:
                # 查找现有记录
                stmt = select(V4SkillOutput).where(
                    and_(
                        V4SkillOutput.session_id == session_id,
                        V4SkillOutput.user_id == uid,
                        V4SkillOutput.skill_name == "__session_metadata__",
                    )
                ).limit(1)
                result = await db.execute(stmt)
                record = result.scalar_one_or_none()

                if record:
                    # 更新
                    record.raw_data_json = merged_json
                    record.raw_data_size = len(merged_json)
                else:
                    # 插入
                    import uuid as _uuid
                    new_record = V4SkillOutput(
                        execution_id=f"meta_{_uuid.uuid4().hex[:12]}",
                        user_id=uid,
                        session_id=session_id,
                        skill_name="__session_metadata__",
                        query="",
                        raw_data_json=merged_json,
                        raw_data_size=len(merged_json),
                        success=True,
                    )
                    db.add(new_record)

                await db.commit()
                logger.debug(f"[V4-Persist] Updated session metadata: {session_id}")
        except Exception as e:
            logger.warning(f"[V4-Persist] update_session_metadata error: {e}")

    # ================================================================
    # Session 基础元数据
    # ================================================================

    async def ensure_session(
        self, session_id: str, user_id: Optional[int] = None, title: Optional[str] = None,
    ):
        """确保 session 元数据存在 (upsert)"""
        uid = user_id or self._default_user_id
        try:
            from .models import V4Session
            async with self._get_session() as db:
                values = dict(
                    session_id=session_id,
                    user_id=uid,
                )
                if title:
                    values["title"] = title

                await self._upsert_one(
                    db,
                    V4Session,
                    filters={"session_id": session_id, "user_id": uid},
                    values=values,
                )
                await db.commit()
        except Exception as e:
            logger.warning(f"[V4-Persist] ensure_session error: {e}")

    # ================================================================
    # 查询 API（供 REST 接口使用）
    # ================================================================

    async def create_session(
        self, user_id: Optional[int] = None, title: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        创建新会话

        Args:
            user_id: 用户 ID
            title: 会话标题（可选）

        Returns:
            新会话信息: {"session_id": "...", "title": "...", "created_at": "..."}
        """
        import uuid
        uid = user_id or self._default_user_id
        session_id = str(int(datetime.now().timestamp() * 1000))  # 使用时间戳作为 session_id

        try:
            from .models import V4Session
            async with self._get_session() as db:
                new_session = V4Session(
                    session_id=session_id,
                    user_id=uid,
                    title=title or f"新会话 {session_id[-6:]}",
                )
                db.add(new_session)
                await db.commit()
                await db.refresh(new_session)

                logger.info(f"[V4-Persist] Created new session: {session_id} for user {uid}")
                return {
                    "session_id": new_session.session_id,
                    "user_id": new_session.user_id,
                    "title": new_session.title,
                    "created_at": new_session.created_at.isoformat() if new_session.created_at else None,
                    "updated_at": new_session.updated_at.isoformat() if new_session.updated_at else None,
                }
        except Exception as e:
            logger.error(f"[V4-Persist] create_session error: {e}")
            raise

    async def list_sessions(
        self, user_id: Optional[int] = None, limit: int = 50, offset: int = 0,
    ) -> List[Dict]:
        """列出用户的所有会话"""
        uid = user_id or self._default_user_id
        try:
            from .models import V4Session
            async with self._get_session() as db:
                stmt = (
                    select(
                        V4Session.session_id,
                        V4Session.user_id,
                        V4Session.title,
                        V4Session.created_at,
                        V4Session.updated_at,
                    )
                    .where(V4Session.user_id == uid)
                    .order_by(V4Session.updated_at.desc())
                    .offset(offset)
                    .limit(limit)
                )
                result = await db.execute(stmt)
                rows = result.all()
                return [
                    {
                        "session_id": r.session_id,
                        "user_id": r.user_id,
                        "title": r.title,
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.warning(f"[V4-Persist] list_sessions error: {e}")
            return []

    async def get_session_detail(
        self, session_id: str, user_id: Optional[int] = None,
    ) -> Optional[Dict]:
        """
        获取单个会话详情（含消息列表和 UI 组件）

        Returns:
            {
                "session_id": "xxx",
                "user_id": 1,
                "title": "会话标题",
                "message_count": 10,
                "messages": [{"role": "user", "content": "..."}, ...],
                "components": [{"type": "stock_info_card", "data": {...}}, ...],
                "created_at": "...",
                "updated_at": "..."
            }
        """
        uid = user_id or self._default_user_id
        try:
            from .models import V4Session, V4ConversationMessage, V4SkillOutput
            from sqlalchemy import func
            async with self._get_session() as db:
                # 1. 会话信息
                stmt = select(V4Session).where(
                    and_(
                        V4Session.session_id == session_id,
                        V4Session.user_id == uid,
                    )
                )
                result = await db.execute(stmt)
                session = result.scalar_one_or_none()
                if not session:
                    return None

                # 2. 消息列表（含 metadata）
                msg_stmt = (
                    select(
                        V4ConversationMessage.role,
                        V4ConversationMessage.content,
                        V4ConversationMessage.metadata_json,
                    )
                    .where(
                        and_(
                            V4ConversationMessage.session_id == session_id,
                            V4ConversationMessage.user_id == uid,
                        )
                    )
                    .order_by(V4ConversationMessage.id.asc())
                    .limit(100)
                )
                msg_result = await db.execute(msg_stmt)
                messages = []
                for r in msg_result.all():
                    msg_dict = {"role": r.role, "content": r.content}
                    if r.metadata_json:
                        try:
                            msg_dict["metadata"] = json.loads(r.metadata_json)
                        except (json.JSONDecodeError, TypeError):
                            pass
                    messages.append(msg_dict)

                # 3. UI 组件（从 v4_skill_outputs 加载）
                #    排除: 已关联场景 tab 的（由 loadSceneTabOutputs 单独加载）
                #    排除: __session_metadata__ 内部记录
                components = []
                try:
                    skill_stmt = (
                        select(
                            V4SkillOutput.skill_name,
                            V4SkillOutput.ui_components_json,
                            V4SkillOutput.raw_data_json,
                        )
                        .where(
                            and_(
                                V4SkillOutput.session_id == session_id,
                                V4SkillOutput.user_id == uid,
                                V4SkillOutput.success == True,
                                V4SkillOutput.skill_name != "__session_metadata__",
                                V4SkillOutput.tab_id.is_(None),
                            )
                        )
                        .order_by(V4SkillOutput.executed_at.asc())
                        .limit(200)
                    )
                    skill_result = await db.execute(skill_stmt)
                    skill_rows = skill_result.all()

                    for row in skill_rows:
                        # 优先使用预存的 ui_components_json
                        if row.ui_components_json:
                            try:
                                ui_comps = json.loads(row.ui_components_json)
                                if isinstance(ui_comps, list):
                                    components.extend(ui_comps)
                                elif isinstance(ui_comps, dict):
                                    components.append(ui_comps)
                            except (json.JSONDecodeError, TypeError):
                                pass
                        # 否则从 raw_data_json 重建 UI 组件
                        elif row.raw_data_json:
                            try:
                                raw_data = json.loads(row.raw_data_json)
                                # 使用 UI 选择器重建组件
                                comp = self._rebuild_ui_component(row.skill_name, raw_data)
                                if comp:
                                    components.append(comp)
                            except (json.JSONDecodeError, TypeError):
                                pass
                except Exception as e:
                    logger.warning(f"[V4-Persist] Failed to load components: {e}")

                # 4. 场景 Tab 数据（从 session metadata 加载）
                scene_tabs = []
                try:
                    meta = await self.get_session_metadata(session_id, user_id=uid)
                    scene_tabs = meta.get("scene_tabs", [])
                except Exception:
                    pass

                # 5. 提取 tool_calls（从 assistant 消息的 metadata 中汇总）
                tools = []
                for m in messages:
                    if m.get("role") == "assistant" and m.get("metadata"):
                        tc_list = m["metadata"].get("tool_calls", [])
                        for tc in tc_list:
                            tools.append({
                                "tool_name": tc.get("name", ""),
                                "input_keys": tc.get("input_keys", []),
                            })

                # 5b. Fallback: 如果 metadata 无 tool_calls，从 v4_skill_outputs 补充
                if not tools:
                    try:
                        _so_stmt = (
                            select(V4SkillOutput.skill_name)
                            .where(
                                and_(
                                    V4SkillOutput.session_id == session_id,
                                    V4SkillOutput.user_id == uid,
                                    V4SkillOutput.success == True,
                                    V4SkillOutput.skill_name != "__session_metadata__",
                                )
                            )
                            .order_by(V4SkillOutput.executed_at.asc())
                            .limit(30)
                        )
                        _so_result = await db.execute(_so_stmt)
                        _so_rows = _so_result.all()
                        for r in _so_rows:
                            tools.append({
                                "tool_name": r.skill_name,
                                "input_keys": [],
                            })
                    except Exception:
                        pass

                return {
                    "session_id": session.session_id,
                    "user_id": session.user_id,
                    "title": session.title,
                    "message_count": len(messages),
                    "messages": messages,
                    "components": components,
                    "scene_tabs": scene_tabs,
                    "tools": tools,
                    "created_at": session.created_at.isoformat() if session.created_at else None,
                    "updated_at": session.updated_at.isoformat() if session.updated_at else None,
                }
        except Exception as e:
            logger.warning(f"[V4-Persist] get_session_detail error: {e}")
            import traceback
            logger.debug(f"[V4-Persist] get_session_detail traceback: {traceback.format_exc()}")
            return None

    def _rebuild_ui_component(self, skill_name: str, raw_data: dict) -> Optional[dict]:
        """
        从原始数据重建 UI 组件配置

        基于 skill 名称和数据结构推断组件类型
        """
        if not raw_data or not isinstance(raw_data, dict):
            return None

        # 技能名到组件类型的映射
        skill_component_map = {
            "stock_query": "stock_info_card",
            "stock_resolver": "stock_info_card",
            "realtime_quote": "stock_info_card",
            "web_search": "insight_highlights",
            "historical_data": "stock_chart",
            "technical_indicators": "technical_chart",
            "rating": "rating_card",
            "prediction": "prediction_card",
            "money_flow": "money_flow_card",
            "sentiment_analysis": "sentiment_card",
            "financial_report": "financial_card",
            "flight_tracker": "flight_tracker_card",
        }

        component_type = skill_component_map.get(skill_name)
        if not component_type:
            # 尝试从数据结构推断
            if "insights" in raw_data:
                component_type = "insight_highlights"
            elif "ts_code" in raw_data and "price" in raw_data:
                component_type = "stock_info_card"
            elif "chart" in raw_data or "series" in raw_data:
                component_type = "stock_chart"
            else:
                return None

        return {
            "type": component_type,
            "data": raw_data
        }

    async def delete_session(
        self, session_id: str, user_id: Optional[int] = None,
    ) -> bool:
        """删除会话及其所有消息"""
        uid = user_id or self._default_user_id
        try:
            from .models import V4Session, V4ConversationMessage
            async with self._get_session() as db:
                # 先删除消息
                del_msg_stmt = delete(V4ConversationMessage).where(
                    and_(
                        V4ConversationMessage.session_id == session_id,
                        V4ConversationMessage.user_id == uid,
                    )
                )
                await db.execute(del_msg_stmt)

                # 再删除会话
                del_session_stmt = delete(V4Session).where(
                    and_(
                        V4Session.session_id == session_id,
                        V4Session.user_id == uid,
                    )
                )
                result = await db.execute(del_session_stmt)
                await db.commit()

                deleted = result.rowcount > 0
                if deleted:
                    logger.info(f"[V4-Persist] Deleted session: {session_id}")
                return deleted
        except Exception as e:
            logger.warning(f"[V4-Persist] delete_session error: {e}")
            return False

    async def update_session(
        self, session_id: str, user_id: Optional[int] = None, title: Optional[str] = None,
    ) -> bool:
        """更新会话信息（如标题）"""
        uid = user_id or self._default_user_id
        try:
            from .models import V4Session
            async with self._get_session() as db:
                stmt = select(V4Session).where(
                    and_(
                        V4Session.session_id == session_id,
                        V4Session.user_id == uid,
                    )
                )
                result = await db.execute(stmt)
                session = result.scalar_one_or_none()
                if not session:
                    return False

                if title is not None:
                    session.title = title

                await db.commit()
                logger.info(f"[V4-Persist] Updated session: {session_id}, title={title}")
                return True
        except Exception as e:
            logger.warning(f"[V4-Persist] update_session error: {e}")
            return False

    # ================================================================
    # Skill Outputs (Skill 执行输出)
    # ================================================================

    async def save_skill_output(
        self,
        execution_id: str,
        session_id: str,
        skill_name: str,
        query: str,
        raw_data_json: Optional[str] = None,
        raw_data_url: Optional[str] = None,
        raw_data_size: int = 0,
        items_count: int = 0,
        ui_components_json: Optional[str] = None,
        success: bool = True,
        error_message: Optional[str] = None,
        duration_ms: int = 0,
        executed_at: Optional[str] = None,
        user_id: Optional[int] = None,
        message_id: Optional[int] = None,
        tab_id: Optional[str] = None,
        scene_role: Optional[str] = None,
    ) -> Optional[int]:
        """
        保存 Skill 执行输出

        Args:
            execution_id: 执行 UUID
            session_id: 会话 ID
            skill_name: 技能名称
            query: 原始查询
            raw_data_json: 原始数据 JSON（内联存储）
            raw_data_url: MinIO 下载链接（大数据）
            raw_data_size: 数据大小
            items_count: 数据条数
            ui_components_json: UI 组件配置
            success: 执行是否成功
            error_message: 错误信息
            duration_ms: 执行耗时
            executed_at: 执行时间
            user_id: 用户 ID
            message_id: 关联消息 ID

        Returns:
            数据库记录 ID
        """
        uid = user_id or self._default_user_id
        try:
            from .models import V4SkillOutput
            from datetime import datetime
            async with self._get_session() as db:
                record = V4SkillOutput(
                    execution_id=execution_id,
                    user_id=uid,
                    session_id=session_id,
                    skill_name=skill_name,
                    query=query,
                    raw_data_json=raw_data_json,
                    raw_data_url=raw_data_url,
                    raw_data_size=raw_data_size,
                    items_count=items_count,
                    ui_components_json=ui_components_json,
                    tab_id=tab_id,
                    scene_role=scene_role,
                    success=success,
                    error_message=error_message,
                    duration_ms=duration_ms,
                    executed_at=datetime.fromisoformat(executed_at) if executed_at else datetime.now(),
                    message_id=message_id,
                )
                db.add(record)
                await db.commit()
                await db.refresh(record)
                logger.debug(f"[V4-Persist] Saved skill output: {execution_id}")
                return record.id
        except Exception as e:
            logger.warning(f"[V4-Persist] save_skill_output error: {e}")
            return None

    async def load_skill_outputs(
        self,
        session_id: str,
        user_id: Optional[int] = None,
        skill_name: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict]:
        """
        加载 Session 的所有 Skill 输出

        Args:
            session_id: 会话 ID
            user_id: 用户 ID
            skill_name: 技能名称筛选（可选）
            limit: 最大返回数量
            offset: 偏移量

        Returns:
            Skill 输出列表
        """
        uid = user_id or self._default_user_id
        try:
            from .models import V4SkillOutput
            async with self._get_session() as db:
                conditions = [
                    V4SkillOutput.session_id == session_id,
                    V4SkillOutput.user_id == uid,
                ]
                if skill_name:
                    conditions.append(V4SkillOutput.skill_name == skill_name)

                stmt = (
                    select(
                        V4SkillOutput.id,
                        V4SkillOutput.execution_id,
                        V4SkillOutput.skill_name,
                        V4SkillOutput.query,
                        V4SkillOutput.items_count,
                        V4SkillOutput.raw_data_size,
                        V4SkillOutput.raw_data_url,
                        V4SkillOutput.ui_components_json,
                        V4SkillOutput.success,
                        V4SkillOutput.duration_ms,
                        V4SkillOutput.executed_at,
                    )
                    .where(and_(*conditions))
                    .order_by(desc(V4SkillOutput.executed_at))
                    .offset(offset)
                    .limit(limit)
                )
                result = await db.execute(stmt)
                rows = result.all()

                outputs = []
                for r in rows:
                    ui_components = []
                    if r.ui_components_json:
                        try:
                            ui_components = json.loads(r.ui_components_json)
                        except:
                            pass

                    outputs.append({
                        "id": r.id,
                        "execution_id": r.execution_id,
                        "skill_name": r.skill_name,
                        "query": r.query,
                        "items_count": r.items_count,
                        "raw_data_size": r.raw_data_size,
                        "raw_data_url": r.raw_data_url,
                        "ui_components": ui_components,
                        "success": r.success,
                        "duration_ms": r.duration_ms,
                        "executed_at": r.executed_at.isoformat() if r.executed_at else None,
                    })

                if outputs:
                    logger.debug(
                        f"[V4-Persist] Loaded {len(outputs)} skill outputs for session={session_id}"
                    )
                return outputs
        except Exception as e:
            logger.warning(f"[V4-Persist] load_skill_outputs error: {e}")
            return []

    async def get_skill_output(
        self,
        execution_id: str,
        user_id: Optional[int] = None,
        include_raw_data: bool = False,
    ) -> Optional[Dict]:
        """
        获取单次 Skill 执行详情

        Args:
            execution_id: 执行 UUID
            user_id: 用户 ID
            include_raw_data: 是否返回原始数据

        Returns:
            Skill 执行详情
        """
        uid = user_id or self._default_user_id
        try:
            from .models import V4SkillOutput
            async with self._get_session() as db:
                stmt = select(V4SkillOutput).where(
                    and_(
                        V4SkillOutput.execution_id == execution_id,
                        V4SkillOutput.user_id == uid,
                    )
                )
                result = await db.execute(stmt)
                record = result.scalar_one_or_none()

                if not record:
                    return None

                # 解析 UI 组件
                ui_components = []
                if record.ui_components_json:
                    try:
                        ui_components = json.loads(record.ui_components_json)
                    except:
                        pass

                # 解析原始数据（如果需要）
                raw_data = None
                if include_raw_data and record.raw_data_json:
                    try:
                        raw_data = json.loads(record.raw_data_json)
                    except:
                        pass

                return {
                    "id": record.id,
                    "execution_id": record.execution_id,
                    "session_id": record.session_id,
                    "skill_name": record.skill_name,
                    "query": record.query,
                    "items_count": record.items_count,
                    "raw_data_size": record.raw_data_size,
                    "raw_data": raw_data,
                    "raw_data_url": record.raw_data_url,
                    "ui_components": ui_components,
                    "success": record.success,
                    "error_message": record.error_message,
                    "duration_ms": record.duration_ms,
                    "executed_at": record.executed_at.isoformat() if record.executed_at else None,
                    "created_at": record.created_at.isoformat() if record.created_at else None,
                }
        except Exception as e:
            logger.warning(f"[V4-Persist] get_skill_output error: {e}")
            return None

    async def count_skill_outputs(
        self,
        session_id: str,
        user_id: Optional[int] = None,
    ) -> int:
        """统计 Session 的 Skill 输出数量"""
        uid = user_id or self._default_user_id
        try:
            from .models import V4SkillOutput
            from sqlalchemy import func
            async with self._get_session() as db:
                stmt = select(func.count()).where(
                    and_(
                        V4SkillOutput.session_id == session_id,
                        V4SkillOutput.user_id == uid,
                    )
                )
                result = await db.execute(stmt)
                return result.scalar() or 0
        except Exception as e:
            logger.warning(f"[V4-Persist] count_skill_outputs error: {e}")
            return 0

    async def get_tab_skill_outputs(
        self,
        session_id: str,
        tab_id: str,
        user_id: Optional[int] = None,
    ) -> List[Dict]:
        """
        获取特定 Tab 下的所有 skill outputs（用于历史 session 场景重建）

        Args:
            session_id: 会话 ID
            tab_id: 场景 Tab ID
            user_id: 用户 ID

        Returns:
            关联该 Tab 的 skill output 列表
        """
        uid = user_id or self._default_user_id
        try:
            from .models import V4SkillOutput
            async with self._get_session() as db:
                stmt = (
                    select(
                        V4SkillOutput.id,
                        V4SkillOutput.execution_id,
                        V4SkillOutput.skill_name,
                        V4SkillOutput.query,
                        V4SkillOutput.raw_data_json,
                        V4SkillOutput.ui_components_json,
                        V4SkillOutput.tab_id,
                        V4SkillOutput.scene_role,
                        V4SkillOutput.success,
                        V4SkillOutput.executed_at,
                    )
                    .where(
                        and_(
                            V4SkillOutput.session_id == session_id,
                            V4SkillOutput.user_id == uid,
                            V4SkillOutput.tab_id == tab_id,
                            V4SkillOutput.success == True,
                        )
                    )
                    .order_by(V4SkillOutput.executed_at.asc())
                )
                result = await db.execute(stmt)
                rows = result.all()

                outputs = []
                for r in rows:
                    ui_components = []
                    if r.ui_components_json:
                        try:
                            ui_components = json.loads(r.ui_components_json)
                        except (json.JSONDecodeError, TypeError):
                            pass

                    outputs.append({
                        "id": r.id,
                        "execution_id": r.execution_id,
                        "skill_name": r.skill_name,
                        "query": r.query,
                        "raw_data_json": r.raw_data_json,
                        "ui_components": ui_components,
                        "tab_id": r.tab_id,
                        "scene_role": r.scene_role,
                        "executed_at": r.executed_at.isoformat() if r.executed_at else None,
                    })

                if outputs:
                    logger.debug(
                        f"[V4-Persist] Loaded {len(outputs)} tab skill outputs "
                        f"for session={session_id}, tab={tab_id}"
                    )
                return outputs
        except Exception as e:
            logger.warning(f"[V4-Persist] get_tab_skill_outputs error: {e}")
            return []

    # ================================================================
    # Report Feedback (报告反馈)
    # ================================================================

    async def save_report_feedback(
        self,
        report_id: str,
        session_id: str,
        rating: str,
        tags: Optional[str] = None,
        comment: Optional[str] = None,
        channel: str = "web",
        user_id: Optional[int] = None,
    ):
        """保存报告反馈 (upsert: 同一用户对同一报告只保留一条)"""
        uid = user_id or self._default_user_id
        try:
            from .models import V4ReportFeedback
            async with self._get_session() as db:
                await self._upsert_one(
                    db,
                    V4ReportFeedback,
                    filters={"report_id": report_id, "user_id": uid},
                    values={
                        "report_id": report_id,
                        "session_id": session_id,
                        "user_id": uid,
                        "channel": channel,
                        "rating": rating,
                        "tags": tags,
                        "comment": comment,
                    },
                )
                await db.commit()
                logger.info(f"[V4-Persist] Saved report feedback: report={report_id}, rating={rating}")
        except Exception as e:
            logger.warning(f"[V4-Persist] save_report_feedback error: {e}")

    async def get_report_feedback(
        self,
        report_id: str,
        user_id: Optional[int] = None,
    ) -> Optional[Dict]:
        """查询用户对某报告的反馈"""
        uid = user_id or self._default_user_id
        try:
            from .models import V4ReportFeedback
            async with self._get_session() as db:
                stmt = select(
                    V4ReportFeedback.rating,
                    V4ReportFeedback.tags,
                    V4ReportFeedback.comment,
                    V4ReportFeedback.channel,
                    V4ReportFeedback.created_at,
                ).where(
                    and_(
                        V4ReportFeedback.report_id == report_id,
                        V4ReportFeedback.user_id == uid,
                    )
                )
                result = await db.execute(stmt)
                row = result.one_or_none()
                if row:
                    return {
                        "rating": row.rating,
                        "tags": row.tags.split(",") if row.tags else [],
                        "comment": row.comment,
                        "channel": row.channel,
                        "created_at": row.created_at.isoformat() if row.created_at else None,
                    }
                return None
        except Exception as e:
            logger.warning(f"[V4-Persist] get_report_feedback error: {e}")
            return None

    # ================================================================
    # Feedback Statistics (反馈统计)
    # ================================================================

    async def get_feedback_statistics(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> dict:
        """
        聚合查询 v4_report_feedback 表，返回多维度反馈统计

        Args:
            start_date: 起始日期 (YYYY-MM-DD)，默认近 30 天
            end_date:   截止日期 (YYYY-MM-DD)，默认今天
            channel:    渠道筛选 (web | dingtalk)，默认全部

        Returns:
            {
                "total": int,
                "like_count": int,
                "dislike_count": int,
                "like_rate": float,          # 0.0 ~ 1.0
                "by_channel": {"web": {...}, "dingtalk": {...}},
                "by_date": [{"date": "YYYY-MM-DD", "like": int, "dislike": int}, ...],
                "recent_comments": [{"rating": str, "comment": str, "channel": str, "created_at": str}, ...],
            }
        """
        from sqlalchemy import func as sqlfunc, case
        try:
            from .models import V4ReportFeedback
            async with self._get_session() as db:
                # 构建过滤条件
                conditions = []
                if start_date:
                    conditions.append(V4ReportFeedback.created_at >= start_date)
                else:
                    # 默认近 30 天
                    from datetime import timedelta
                    default_start = datetime.now() - timedelta(days=30)
                    conditions.append(V4ReportFeedback.created_at >= default_start)
                if end_date:
                    conditions.append(V4ReportFeedback.created_at <= f"{end_date} 23:59:59")
                if channel:
                    conditions.append(V4ReportFeedback.channel == channel)

                # 1. 总体统计
                total_stmt = select(
                    sqlfunc.count().label("total"),
                    sqlfunc.sum(case((V4ReportFeedback.rating == "like", 1), else_=0)).label("like_count"),
                    sqlfunc.sum(case((V4ReportFeedback.rating == "dislike", 1), else_=0)).label("dislike_count"),
                ).where(and_(*conditions)) if conditions else select(
                    sqlfunc.count().label("total"),
                    sqlfunc.sum(case((V4ReportFeedback.rating == "like", 1), else_=0)).label("like_count"),
                    sqlfunc.sum(case((V4ReportFeedback.rating == "dislike", 1), else_=0)).label("dislike_count"),
                )
                total_result = await db.execute(total_stmt)
                total_row = total_result.one()
                total = total_row.total or 0
                like_count = total_row.like_count or 0
                dislike_count = total_row.dislike_count or 0
                like_rate = round(like_count / total, 4) if total > 0 else 0.0

                # 2. 按渠道统计
                by_channel = {}
                channel_stmt = select(
                    V4ReportFeedback.channel,
                    sqlfunc.count().label("cnt"),
                    sqlfunc.sum(case((V4ReportFeedback.rating == "like", 1), else_=0)).label("likes"),
                    sqlfunc.sum(case((V4ReportFeedback.rating == "dislike", 1), else_=0)).label("dislikes"),
                ).where(and_(*conditions)).group_by(V4ReportFeedback.channel) if conditions else select(
                    V4ReportFeedback.channel,
                    sqlfunc.count().label("cnt"),
                    sqlfunc.sum(case((V4ReportFeedback.rating == "like", 1), else_=0)).label("likes"),
                    sqlfunc.sum(case((V4ReportFeedback.rating == "dislike", 1), else_=0)).label("dislikes"),
                ).group_by(V4ReportFeedback.channel)
                channel_result = await db.execute(channel_stmt)
                for r in channel_result.all():
                    cnt = r.cnt or 0
                    likes = r.likes or 0
                    dislikes = r.dislikes or 0
                    by_channel[r.channel] = {
                        "total": cnt,
                        "like": likes,
                        "dislike": dislikes,
                        "like_rate": round(likes / cnt, 4) if cnt > 0 else 0.0,
                    }

                # 3. 按日期统计
                by_date = []
                date_stmt = select(
                    sqlfunc.date(V4ReportFeedback.created_at).label("dt"),
                    sqlfunc.sum(case((V4ReportFeedback.rating == "like", 1), else_=0)).label("likes"),
                    sqlfunc.sum(case((V4ReportFeedback.rating == "dislike", 1), else_=0)).label("dislikes"),
                ).where(and_(*conditions)).group_by(
                    sqlfunc.date(V4ReportFeedback.created_at)
                ).order_by(
                    sqlfunc.date(V4ReportFeedback.created_at)
                ) if conditions else select(
                    sqlfunc.date(V4ReportFeedback.created_at).label("dt"),
                    sqlfunc.sum(case((V4ReportFeedback.rating == "like", 1), else_=0)).label("likes"),
                    sqlfunc.sum(case((V4ReportFeedback.rating == "dislike", 1), else_=0)).label("dislikes"),
                ).group_by(
                    sqlfunc.date(V4ReportFeedback.created_at)
                ).order_by(
                    sqlfunc.date(V4ReportFeedback.created_at)
                )
                date_result = await db.execute(date_stmt)
                for r in date_result.all():
                    by_date.append({
                        "date": str(r.dt) if r.dt else "",
                        "like": r.likes or 0,
                        "dislike": r.dislikes or 0,
                    })

                # 4. 最近评论（有 comment 的反馈，最新 20 条）
                recent_comments = []
                comment_stmt = select(
                    V4ReportFeedback.rating,
                    V4ReportFeedback.comment,
                    V4ReportFeedback.channel,
                    V4ReportFeedback.created_at,
                ).where(
                    and_(
                        V4ReportFeedback.comment.isnot(None),
                        V4ReportFeedback.comment != "",
                        *conditions,
                    )
                ).order_by(desc(V4ReportFeedback.created_at)).limit(20)
                comment_result = await db.execute(comment_stmt)
                for r in comment_result.all():
                    recent_comments.append({
                        "rating": r.rating,
                        "comment": r.comment,
                        "channel": r.channel,
                        "created_at": r.created_at.isoformat() if r.created_at else "",
                    })

                return {
                    "total": total,
                    "like_count": like_count,
                    "dislike_count": dislike_count,
                    "like_rate": like_rate,
                    "by_channel": by_channel,
                    "by_date": by_date,
                    "recent_comments": recent_comments,
                }
        except Exception as e:
            logger.warning(f"[V4-Persist] get_feedback_statistics error: {e}")
            return {
                "total": 0,
                "like_count": 0,
                "dislike_count": 0,
                "like_rate": 0.0,
                "by_channel": {},
                "by_date": [],
                "recent_comments": [],
                "error": str(e),
            }

    # ================================================================
    # 建表
    # ================================================================
    # Ring 2: Skill 错误统计（供 SkillEvolver 回调）
    # ================================================================

    async def get_skill_error_stats(self, window_days: int = 7) -> List[Dict]:
        """
        统计各 skill 在窗口期内的调用次数和错误率。

        返回: [{"skill_name", "total_calls", "error_count", "error_rate", "recent_errors"}]
        """
        try:
            from .models import V4SkillOutput
            from datetime import timedelta
            since = datetime.utcnow() - timedelta(days=window_days)

            async with self._get_session() as db:
                # 查询窗口期内所有 skill 调用（排除内部元数据）
                stmt = text(
                    "SELECT skill_name, "
                    "  COUNT(*) AS total_calls, "
                    "  SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS error_count "
                    "FROM v4_skill_outputs "
                    "WHERE executed_at >= :since "
                    "  AND skill_name NOT LIKE '\\_%\\_%' ESCAPE '\\' "
                    "GROUP BY skill_name "
                    "HAVING total_calls >= 1 "
                    "ORDER BY error_count DESC"
                )
                result = await db.execute(stmt, {"since": since})
                rows = result.fetchall()

                stats = []
                for row in rows:
                    skill_name, total_calls, error_count = row[0], row[1], row[2]
                    error_rate = error_count / total_calls if total_calls > 0 else 0.0

                    # 获取最近错误详情
                    recent_errors = []
                    if error_count > 0:
                        err_stmt = text(
                            "SELECT error_message FROM v4_skill_outputs "
                            "WHERE skill_name = :name AND success = 0 "
                            "  AND executed_at >= :since "
                            "ORDER BY executed_at DESC LIMIT 10"
                        )
                        err_result = await db.execute(err_stmt, {"name": skill_name, "since": since})
                        recent_errors = [r[0] for r in err_result.fetchall() if r[0]]

                    stats.append({
                        "skill_name": skill_name,
                        "total_calls": total_calls,
                        "error_count": error_count,
                        "error_rate": round(error_rate, 4),
                        "recent_errors": recent_errors,
                    })

                return stats
        except Exception as e:
            logger.warning(f"[V4-Persist] get_skill_error_stats error: {e}")
            return []

    # ================================================================

    @staticmethod
    async def create_tables():
        """创建 v4_* 前缀的表（IF NOT EXISTS）"""
        try:
            from app.db.database import engine
            from app.db.models_db import Base
            # 导入 models 确保表定义已注册到 Base.metadata
            from . import models as _v4_models  # noqa: F401

            # 筛选 v4_* 前缀的表
            v4_tables = [
                t for t in Base.metadata.sorted_tables
                if t.name.startswith("v4_")
            ]

            if not v4_tables:
                logger.warning("[V4-Persist] No v4_* tables found in metadata")
                return

            async with engine.begin() as conn:
                await conn.run_sync(
                    lambda sync_conn: Base.metadata.create_all(
                        bind=sync_conn, tables=v4_tables, checkfirst=True
                    )
                )

            table_names = [t.name for t in v4_tables]
            logger.info(f"[V4-Persist] Tables created/verified: {table_names}")
        except Exception as e:
            logger.warning(f"[V4-Persist] create_tables error: {e}")
