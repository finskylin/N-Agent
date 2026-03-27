"""
V4 ORM 模型 — SQLite 存储

6 张 v4_* 前缀表，与 V3 的 chat_sessions/chat_messages 等完全独立。

设计要点:
- session_id 为 String(255)，与 V4 使用字符串 session_id 一致
- user_id 默认 1，所有表都有该字段用于未来用户隔离
- 无外键关联（各表独立写入，避免插入顺序依赖）

表清单:
- v4_sessions: 会话元数据
- v4_conversation_messages: 对话消息
- v4_conversation_summaries: 对话摘要
- v4_experiences: 经验知识
- v4_cli_sessions: CLI Session 映射
- v4_session_messages: Session 消息存储
- v4_skill_outputs: Skill 执行输出（新增）
"""
from sqlalchemy import (
    Column, BigInteger, String, Text, Float, DateTime, Index, UniqueConstraint,
    Integer, Boolean,
)
from sqlalchemy.sql import func

from app.db.models_db import Base

SQLITE_PK_TYPE = BigInteger().with_variant(Integer, "sqlite")
LARGE_TEXT_TYPE = Text()


class V4Session(Base):
    """V4 会话元数据"""
    __tablename__ = "v4_sessions"

    id = Column(SQLITE_PK_TYPE, primary_key=True, autoincrement=True)
    session_id = Column(String(255), nullable=False, index=True)
    user_id = Column(BigInteger, nullable=False, default=1, index=True)
    title = Column(String(512), nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("session_id", "user_id", name="uq_v4_sessions_sid_uid"),
    )


class V4ConversationMessage(Base):
    """V4 对话消息"""
    __tablename__ = "v4_conversation_messages"

    id = Column(SQLITE_PK_TYPE, primary_key=True, autoincrement=True)
    session_id = Column(String(255), nullable=False)
    user_id = Column(BigInteger, nullable=False, default=1)
    role = Column(String(20), nullable=False)  # user | assistant | system
    content = Column(Text, nullable=False)
    metadata_json = Column(Text, nullable=True)  # JSON: {confidence_data, report_feedback, ...}
    created_at = Column(DateTime, default=func.now())

    __table_args__ = (
        Index("ix_v4_conv_msg_sid_uid", "session_id", "user_id"),
    )


class V4ConversationSummary(Base):
    """V4 对话摘要"""
    __tablename__ = "v4_conversation_summaries"

    id = Column(SQLITE_PK_TYPE, primary_key=True, autoincrement=True)
    session_id = Column(String(255), nullable=False)
    user_id = Column(BigInteger, nullable=False, default=1)
    summary = Column(Text, nullable=False)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("session_id", "user_id", name="uq_v4_conv_summary_sid_uid"),
    )


class V4Experience(Base):
    """V4 经验知识条目"""
    __tablename__ = "v4_experiences"

    id = Column(SQLITE_PK_TYPE, primary_key=True, autoincrement=True)
    session_id = Column(String(255), nullable=False)
    user_id = Column(BigInteger, nullable=False, default=1)
    dimension = Column(String(50), nullable=False)  # user_preferences | stock_insights | learned_patterns | corrections
    text = Column(Text, nullable=False)
    score = Column(Float, nullable=False, default=0.5)
    created_ts = Column(BigInteger, nullable=False, default=0)
    created_at = Column(DateTime, default=func.now())

    __table_args__ = (
        Index("ix_v4_exp_sid_uid_dim", "session_id", "user_id", "dimension"),
    )


class V4CLISession(Base):
    """V4 CLI Session ID 映射"""
    __tablename__ = "v4_cli_sessions"

    id = Column(SQLITE_PK_TYPE, primary_key=True, autoincrement=True)
    instance_id = Column(String(64), nullable=False, default="default")  # v2: 多实例隔离
    session_id = Column(String(255), nullable=False)
    user_id = Column(BigInteger, nullable=False, default=1)
    cli_session_id = Column(String(512), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("instance_id", "session_id", "user_id", name="uq_v4_cli_sessions_inst_sid_uid"),
        Index("ix_v4_cli_sessions_lookup", "instance_id", "user_id", "session_id"),
    )


class V4SessionMessage(Base):
    """
    V4 Session 消息存储（用于 session 重建）

    存储压缩后的 CLI session 消息，支持:
    - 按 instance_id 多实例隔离
    - 按时间倒序读取（由近到远重建策略）
    - 记录原始大小和压缩大小
    """
    __tablename__ = "v4_session_messages"

    id = Column(SQLITE_PK_TYPE, primary_key=True, autoincrement=True)
    instance_id = Column(String(64), nullable=False, default="default")
    user_id = Column(BigInteger, nullable=False, default=1)
    session_id = Column(String(255), nullable=False)
    message_uuid = Column(String(64), nullable=False)  # 消息 UUID
    parent_uuid = Column(String(64), nullable=True)    # 父消息 UUID
    message_type = Column(String(32), nullable=False)  # user, assistant, tool_result, etc.
    content_compressed = Column(LARGE_TEXT_TYPE, nullable=False)  # 压缩后的 JSON 内容
    content_size = Column(BigInteger, nullable=False, default=0)   # 压缩后大小（字节）
    original_size = Column(BigInteger, nullable=False, default=0)  # 原始大小（字节）
    created_at = Column(DateTime, default=func.now())

    __table_args__ = (
        Index("ix_v4_sess_msg_lookup", "instance_id", "user_id", "session_id", "created_at"),
        UniqueConstraint("instance_id", "session_id", "message_uuid", name="uq_v4_sess_msg_uuid"),
    )


class V4SkillOutput(Base):
    """
    V4 Skill 执行输出

    存储 Skill 执行后的原始数据和 UI 组件配置，支持：
    - 内联存储（< 64KB）：数据存 raw_data_json 字段
    - MinIO 存储（>= 64KB）：数据上传 MinIO，存 raw_data_url 链接
    - UI 组件持久化：支持历史 Session 的组件恢复渲染

    设计文档: doc_design/agent2/data_store.md
    """
    __tablename__ = "v4_skill_outputs"

    id = Column(SQLITE_PK_TYPE, primary_key=True, autoincrement=True)
    execution_id = Column(String(64), nullable=False, unique=True)  # UUID
    user_id = Column(BigInteger, nullable=False, default=1)
    session_id = Column(String(255), nullable=False)
    skill_name = Column(String(100), nullable=False)
    query = Column(Text, nullable=True)

    # === 数据存储 ===
    raw_data_json = Column(LARGE_TEXT_TYPE, nullable=True)      # 原始数据 JSON (内联存储)
    raw_data_url = Column(String(512), nullable=True)    # MinIO 下载链接 (大数据)
    raw_data_size = Column(Integer, default=0)           # 数据大小 (字节)
    items_count = Column(Integer, default=0)             # 数据条数

    # === UI 组件 ===
    ui_components_json = Column(LARGE_TEXT_TYPE, nullable=True)  # UI 组件配置 JSON
    component_version = Column(String(20), nullable=True)  # 组件版本号

    # === 渲染快照（可选，用于离线查看）===
    rendered_html = Column(LARGE_TEXT_TYPE, nullable=True)      # 渲染后的 HTML 快照

    # === 场景关联 ===
    tab_id = Column(String(20), nullable=True)           # 关联的场景 Tab ID
    scene_role = Column(String(20), nullable=True)       # 布局角色: layer/panel/overlay/standalone

    # === 关联信息 ===
    message_id = Column(BigInteger, nullable=True)       # 关联的 assistant 消息 ID

    # === 执行信息 ===
    success = Column(Boolean, default=True)
    error_message = Column(Text, nullable=True)
    duration_ms = Column(Integer, default=0)
    executed_at = Column(DateTime, nullable=False, default=func.now())
    created_at = Column(DateTime, default=func.now())

    __table_args__ = (
        Index("ix_v4_skill_outputs_user_session", "user_id", "session_id"),
        Index("ix_v4_skill_outputs_skill_time", "skill_name", "executed_at"),
        Index("ix_v4_skill_outputs_execution", "execution_id"),
        Index("ix_v4_skill_outputs_message", "message_id"),
    )


class V4ReportFeedback(Base):
    """V4 报告反馈（点赞/踩 + 标签 + 文本）"""
    __tablename__ = "v4_report_feedback"

    id = Column(SQLITE_PK_TYPE, primary_key=True, autoincrement=True)
    report_id = Column(String(64), nullable=False, index=True)
    session_id = Column(String(255), nullable=False)
    user_id = Column(BigInteger, nullable=False, default=1)
    channel = Column(String(20), nullable=False, default="web")  # web | dingtalk
    rating = Column(String(10), nullable=False)  # like | dislike
    tags = Column(String(512), nullable=True)  # 逗号分隔: "数据准确,分析深入"
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime, default=func.now())

    __table_args__ = (
        UniqueConstraint("report_id", "user_id", name="uq_report_feedback_rid_uid"),
        Index("ix_report_feedback_session", "session_id", "user_id"),
    )
