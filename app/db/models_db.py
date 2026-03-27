"""
Database Models (SQLAlchemy ORM)
app 层专属数据模型（用户管理、知识库、任务管理）
V4 对话链路数据由 agent_core/session/context_db.py 管理
"""
from datetime import datetime
from typing import Optional, List, Dict, Any
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, JSON, BigInteger
from sqlalchemy.orm import relationship, declarative_base
from sqlalchemy.sql import func

Base = declarative_base()
SQLITE_PK_TYPE = BigInteger().with_variant(Integer, "sqlite")

class User(Base):
    __tablename__ = "users"

    id = Column(SQLITE_PK_TYPE, primary_key=True, index=True, autoincrement=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    nickname = Column(String(100))
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class TaskGroup(Base):
    """任务组：一次问答产生的多个后台任务的聚合单元"""
    __tablename__ = "task_groups"

    group_id        = Column(String(64), primary_key=True)
    session_id      = Column(String(100), nullable=False)
    user_id         = Column(String(100))
    channel         = Column(String(20), default="web")
    callback_prompt = Column(Text)          # 完成后让 Agent 执行的提示词，None 用默认模板
    status          = Column(String(20), default="running")  # running/completed/partial_failed/failed
    total_tasks     = Column(Integer, default=0)
    completed_tasks = Column(Integer, default=0)
    failed_tasks    = Column(Integer, default=0)
    created_at      = Column(DateTime, default=func.now())
    finished_at     = Column(DateTime)

    def to_dict(self) -> dict:
        return {
            "group_id": self.group_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "channel": self.channel,
            "callback_prompt": self.callback_prompt,
            "status": self.status,
            "total_tasks": self.total_tasks,
            "completed_tasks": self.completed_tasks,
            "failed_tasks": self.failed_tasks,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class Task(Base):
    """后台任务（docker_operator 启动的后台进程/容器任务）"""
    __tablename__ = "tasks"

    task_id        = Column(String(64), primary_key=True)
    name           = Column(String(255), nullable=False)
    description    = Column(Text)

    # 执行信息
    exec_mode      = Column(String(20), default="local")   # local / docker / toolbox
    command        = Column(Text)
    pid            = Column(Integer)
    container_id   = Column(String(128))
    container_name = Column(String(128))
    log_path       = Column(String(512))

    # 状态
    status         = Column(String(20), default="running")  # running/completed/failed/cancelled
    exit_code      = Column(Integer)        # 进程退出码（toolbox .done 文件写入）
    result         = Column(Text)
    error          = Column(Text)

    # 归属
    created_by     = Column(String(100))
    session_id     = Column(String(100))
    group_id       = Column(String(64))     # 所属任务组（可为空）

    created_at     = Column(DateTime, default=func.now())
    started_at     = Column(DateTime)
    finished_at    = Column(DateTime)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "description": self.description,
            "exec_mode": self.exec_mode,
            "command": self.command,
            "pid": self.pid,
            "container_id": self.container_id,
            "container_name": self.container_name,
            "log_path": self.log_path,
            "status": self.status,
            "exit_code": self.exit_code,
            "result": self.result,
            "error": self.error,
            "created_by": self.created_by,
            "session_id": self.session_id,
            "group_id": self.group_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class KnowledgeFile(Base):
    """知识库文件元数据"""
    __tablename__ = "knowledge_files"

    id = Column(SQLITE_PK_TYPE, primary_key=True, autoincrement=True)
    user_id = Column(String(100), nullable=False, index=True)
    scope = Column(String(20), nullable=False, default="public")

    # 文件信息
    file_name = Column(String(255), nullable=False)
    file_type = Column(String(20), nullable=False)
    file_size = Column(BigInteger, default=0)
    mime_type = Column(String(100))

    # 存储路径
    local_path = Column(String(512))
    minio_url = Column(String(512))
    minio_object_name = Column(String(512))

    # 分类
    category = Column(String(100), nullable=False, default="general")

    # 文件级元数据（LLM 生成）
    summary = Column(Text)
    keywords = Column(JSON)

    # 目录级摘要（冗余存储）
    directory_summary = Column(Text)

    # 处理状态
    status = Column(String(20), default="processing")
    process_error = Column(Text)
    page_count = Column(Integer)
    word_count = Column(Integer)

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "scope": self.scope,
            "file_name": self.file_name,
            "file_type": self.file_type,
            "file_size": self.file_size,
            "mime_type": self.mime_type,
            "local_path": self.local_path,
            "minio_url": self.minio_url,
            "minio_object_name": self.minio_object_name,
            "category": self.category,
            "summary": self.summary,
            "keywords": self.keywords,
            "status": self.status,
            "process_error": self.process_error,
            "page_count": self.page_count,
            "word_count": self.word_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class KnowledgeCategorySummary(Base):
    """知识库分类(知识库主表)"""
    __tablename__ = "knowledge_category_summaries"

    id = Column(SQLITE_PK_TYPE, primary_key=True, autoincrement=True)
    category = Column(String(100), nullable=False, unique=True)
    label = Column(String(255))
    description = Column(Text)
    icon = Column(String(50), default='📚')
    scope = Column(String(20), default='public')
    auto_inject = Column(Integer, default=1)  # TINYINT(1)
    is_system = Column(Integer, default=0)     # TINYINT(1)
    created_by = Column(String(100))
    summary_content = Column(Text)
    file_count = Column(Integer, default=0)
    total_size = Column(BigInteger, default=0)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "path": self.category,
            "category": self.category,
            "label": self.label or self.category,
            "description": self.description or "",
            "icon": self.icon or "📚",
            "scope": self.scope or "public",
            "auto_inject": bool(self.auto_inject),
            "is_system": bool(self.is_system),
            "created_by": self.created_by,
            "file_count": self.file_count or 0,
            "total_size": self.total_size or 0,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
