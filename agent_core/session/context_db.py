"""
Session Context SQLite Storage Layer

替代 Redis 作为 Session 上下文的主存储:
- CLISessionStore: app_session_id → cli_session_id 映射
- ConversationHistory: 跨请求对话历史
- ConversationSummaries: 对话摘要（含 SummaryChain）
- ExperienceStore: 用户经验知识库
- MidTermMemory: 中期记忆页面（MTM 热度管理）
- UserProfile: 用户画像（LTM 维度化存储）

特性:
- WAL 模式：支持同机多实例并发读写
- 异步接口：通过 aiosqlite 提供 async API
- 自动建表：首次使用时自动创建 schema
- 懒初始化：仅在首次调用时创建目录和数据库
"""
import json
import time
from pathlib import Path
from typing import List, Dict, Optional

import aiosqlite
from loguru import logger


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cli_sessions (
    session_id     TEXT NOT NULL,
    user_id        INTEGER NOT NULL,
    instance_id    TEXT NOT NULL,
    cli_session_id TEXT NOT NULL,
    updated_at     INTEGER NOT NULL,
    PRIMARY KEY (instance_id, user_id, session_id)
);

CREATE TABLE IF NOT EXISTS conversation_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    user_id     INTEGER NOT NULL,
    instance_id TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    metadata    TEXT,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_msg_lookup
    ON conversation_messages(instance_id, user_id, session_id, created_at);

CREATE TABLE IF NOT EXISTS conversation_summaries (
    session_id    TEXT NOT NULL,
    user_id       INTEGER NOT NULL,
    instance_id   TEXT NOT NULL,
    summary_text  TEXT NOT NULL,
    summary_chain TEXT,
    updated_at    INTEGER NOT NULL,
    PRIMARY KEY (instance_id, user_id, session_id)
);

CREATE TABLE IF NOT EXISTS experiences (
    session_id  TEXT NOT NULL,
    user_id     INTEGER NOT NULL,
    instance_id TEXT NOT NULL,
    dimensions  TEXT NOT NULL,
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (instance_id, user_id, session_id)
);

CREATE TABLE IF NOT EXISTS mtm_pages (
    page_id             TEXT NOT NULL,
    user_id             INTEGER NOT NULL,
    instance_id         TEXT NOT NULL,
    session_id          TEXT NOT NULL,
    summary             TEXT NOT NULL,
    topics              TEXT NOT NULL,
    entities            TEXT NOT NULL,
    msg_range_start     INTEGER DEFAULT 0,
    msg_range_end       INTEGER DEFAULT 0,
    interaction_length  INTEGER DEFAULT 0,
    visit_count         INTEGER DEFAULT 1,
    heat_score          REAL DEFAULT 0.0,
    created_at          INTEGER NOT NULL,
    last_access_at      INTEGER NOT NULL,
    summary_embedding   BLOB,
    PRIMARY KEY (instance_id, user_id, page_id)
);
CREATE INDEX IF NOT EXISTS idx_mtm_heat
    ON mtm_pages(instance_id, user_id, heat_score DESC);
CREATE INDEX IF NOT EXISTS idx_mtm_session
    ON mtm_pages(instance_id, user_id, session_id);

CREATE TABLE IF NOT EXISTS user_profiles (
    user_id       INTEGER NOT NULL,
    instance_id   TEXT NOT NULL,
    dimensions    TEXT NOT NULL,
    update_count  INTEGER DEFAULT 0,
    updated_at    INTEGER NOT NULL,
    PRIMARY KEY (instance_id, user_id)
);

CREATE TABLE IF NOT EXISTS user_experiences (
    user_id        INTEGER NOT NULL,
    instance_id    TEXT NOT NULL,
    dimension      TEXT NOT NULL,
    text           TEXT NOT NULL,
    score          REAL DEFAULT 0.5,
    source_session TEXT,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL,
    PRIMARY KEY (instance_id, user_id, dimension, text)
);
CREATE INDEX IF NOT EXISTS idx_user_exp_score
    ON user_experiences(instance_id, user_id, dimension, score DESC);

-- ════════ Capability Gap Detection ════════

CREATE TABLE IF NOT EXISTS capability_gaps (
    tool_name      TEXT NOT NULL PRIMARY KEY,
    error_summary  TEXT NOT NULL,
    session_id     TEXT,
    count          INTEGER DEFAULT 1,
    last_triggered REAL DEFAULT 0,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);

-- ════════ Knowledge Engine Tables (8 张) ════════

CREATE TABLE IF NOT EXISTS episodes (
    episode_id       TEXT PRIMARY KEY,
    user_id          INTEGER NOT NULL,
    instance_id      TEXT NOT NULL,
    session_id       TEXT,
    query            TEXT,
    skill_executions TEXT,
    confidence_result TEXT,
    feedback         TEXT,
    total_duration_ms REAL DEFAULT 0,
    success          INTEGER DEFAULT 1,
    created_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_episodes_user
    ON episodes(instance_id, user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS knowledge_units (
    unit_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    instance_id TEXT NOT NULL,
    category TEXT NOT NULL,
    text TEXT NOT NULL,
    tags TEXT,
    utility REAL DEFAULT 0.5,
    confidence REAL DEFAULT 0.5,
    access_count INTEGER DEFAULT 0,
    hit_count INTEGER DEFAULT 0,
    feedback_reinforcements INTEGER DEFAULT 0,
    feedback_decays INTEGER DEFAULT 0,
    event_time REAL,
    ingestion_time REAL NOT NULL,
    valid_from REAL NOT NULL,
    valid_until REAL,
    superseded_by TEXT,
    supersedes TEXT,
    update_reason TEXT,
    source_episode_id TEXT,
    created_at REAL NOT NULL,
    last_accessed REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ku_temporal
    ON knowledge_units(instance_id, user_id, valid_from, valid_until);
CREATE INDEX IF NOT EXISTS idx_ku_supersede
    ON knowledge_units(superseded_by);

CREATE TABLE IF NOT EXISTS skill_profiles (
    skill_name           TEXT NOT NULL,
    user_id              INTEGER NOT NULL,
    instance_id          TEXT NOT NULL,
    total_executions     INTEGER DEFAULT 0,
    success_count        INTEGER DEFAULT 0,
    failure_count        INTEGER DEFAULT 0,
    avg_duration_ms      REAL DEFAULT 0,
    avg_confidence       REAL DEFAULT 0,
    like_count           INTEGER DEFAULT 0,
    dislike_count        INTEGER DEFAULT 0,
    satisfaction_score   REAL DEFAULT 0.5,
    cognition_version_count INTEGER DEFAULT 0,
    last_execution_at    REAL DEFAULT 0,
    updated_at           REAL NOT NULL,
    PRIMARY KEY (instance_id, user_id, skill_name)
);

CREATE TABLE IF NOT EXISTS preferences (
    preference_id     TEXT PRIMARY KEY,
    user_id           INTEGER NOT NULL,
    instance_id       TEXT NOT NULL,
    dimension         TEXT NOT NULL,
    value             TEXT NOT NULL,
    confidence        REAL DEFAULT 0.5,
    source_episode_id TEXT,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS cognition_changes (
    change_id TEXT PRIMARY KEY,
    old_unit_id TEXT NOT NULL,
    new_unit_id TEXT NOT NULL,
    reason TEXT,
    change_type TEXT DEFAULT 'update',
    timestamp REAL NOT NULL,
    user_id INTEGER NOT NULL,
    instance_id TEXT NOT NULL,
    affected_skills TEXT,
    confidence_delta REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cc_timeline
    ON cognition_changes(instance_id, user_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS cognition_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    instance_id TEXT NOT NULL,
    snapshot_time REAL NOT NULL,
    snapshot_type TEXT DEFAULT 'daily',
    active_knowledge_count INTEGER,
    category_stats TEXT,
    avg_utility REAL,
    avg_confidence REAL,
    skill_profile_summary TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cs_time
    ON cognition_snapshots(instance_id, user_id, snapshot_time DESC);

CREATE TABLE IF NOT EXISTS evolution_tasks (
    task_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    instance_id TEXT NOT NULL,
    gap_description TEXT,
    status TEXT DEFAULT 'pending',
    phase TEXT DEFAULT 'gap',
    exploration_log TEXT,
    result_knowledge_ids TEXT,
    knowledge_snapshot_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    completed_at REAL
);

CREATE TABLE IF NOT EXISTS crystallized_skills (
    crystal_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    instance_id TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    description TEXT,
    workflow TEXT,
    prompt_template TEXT,
    source_episodes TEXT,
    status TEXT DEFAULT 'candidate',
    rejection_reason TEXT,
    test_result TEXT,
    occurrences INTEGER DEFAULT 0,
    success_rate REAL DEFAULT 0,
    like_count INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""


class SessionContextDB:
    """
    SQLite 存储管理器

    所有方法均为 async，内部通过 aiosqlite 操作。
    每次操作独立获取连接（aiosqlite.connect），
    避免长期持有连接导致并发问题。

    Args:
        db_path: SQLite 数据库文件路径
        wal_mode: 是否启用 WAL 模式（推荐开启，支持并发读）
        busy_timeout_ms: 写锁等待超时（毫秒）
    """

    def __init__(
        self,
        db_path: str = "app/data/sessions/default/memory.db",
        wal_mode: bool = True,
        busy_timeout_ms: int = 5000,
    ):
        self._db_path = db_path
        self._wal_mode = wal_mode
        self._busy_timeout_ms = busy_timeout_ms
        self._initialized = False

    async def _ensure_init(self):
        """懒初始化：首次调用时创建目录和表"""
        if self._initialized:
            return
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            if self._wal_mode:
                await db.execute("PRAGMA journal_mode=WAL")
            await db.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
            await db.executescript(SCHEMA_SQL)
            await self._run_migration(db)
            await db.commit()
        self._initialized = True
        logger.info(f"[SessionContextDB] Initialized at {self._db_path}")

    async def _run_migration(self, db: aiosqlite.Connection):
        """兼容性迁移：对已有数据库补充新列/新表"""
        migrations = [
            ("ALTER TABLE mtm_pages ADD COLUMN summary_embedding BLOB",
             "added summary_embedding to mtm_pages"),
            ("ALTER TABLE knowledge_units ADD COLUMN text_embedding BLOB",
             "added text_embedding to knowledge_units"),
            ("ALTER TABLE user_experiences ADD COLUMN text_embedding BLOB",
             "added text_embedding to user_experiences"),
            # knowledge_edges 时序字段
            ("ALTER TABLE knowledge_edges ADD COLUMN observed_at REAL",
             "added observed_at to knowledge_edges"),
            ("ALTER TABLE knowledge_edges ADD COLUMN expires_at REAL",
             "added expires_at to knowledge_edges"),
            ("ALTER TABLE knowledge_edges ADD COLUMN edge_status TEXT DEFAULT 'active'",
             "added edge_status to knowledge_edges"),
            ("ALTER TABLE knowledge_edges ADD COLUMN version INTEGER DEFAULT 1",
             "added version to knowledge_edges"),
            # 知识来源追踪
            ("ALTER TABLE knowledge_units ADD COLUMN source_type TEXT DEFAULT 'unknown'",
             "added source_type to knowledge_units"),
            ("ALTER TABLE knowledge_units ADD COLUMN source_batch_id TEXT DEFAULT NULL",
             "added source_batch_id to knowledge_units"),
        ]
        for sql, desc in migrations:
            try:
                await db.execute(sql)
                logger.info(f"[SessionContextDB] Migration: {desc}")
            except Exception:
                pass  # 列已存在，忽略

        # 知识来源追踪索引（幂等建立）
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS idx_ku_source_type ON knowledge_units(source_type)",
            "CREATE INDEX IF NOT EXISTS idx_ku_source_batch ON knowledge_units(source_batch_id)",
        ]:
            try:
                await db.execute(idx_sql)
            except Exception:
                pass

        # 知识图谱表（幂等建表）
        graph_schema = """
CREATE TABLE IF NOT EXISTS knowledge_nodes (
    node_id      TEXT NOT NULL,
    user_id      INTEGER NOT NULL,
    instance_id  TEXT NOT NULL,
    name         TEXT NOT NULL,
    node_type    TEXT NOT NULL DEFAULT 'concept',
    description  TEXT,
    source_unit_id TEXT,
    access_count INTEGER DEFAULT 0,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    valid_until  REAL,
    PRIMARY KEY (instance_id, user_id, node_id)
);
CREATE INDEX IF NOT EXISTS idx_kn_name
    ON knowledge_nodes(instance_id, user_id, name);
CREATE INDEX IF NOT EXISTS idx_kn_type
    ON knowledge_nodes(instance_id, user_id, node_type);

CREATE TABLE IF NOT EXISTS knowledge_edges (
    edge_id         TEXT NOT NULL,
    user_id         INTEGER NOT NULL,
    instance_id     TEXT NOT NULL,
    source_node_id  TEXT NOT NULL,
    target_node_id  TEXT NOT NULL,
    relation        TEXT NOT NULL,
    relation_type   TEXT NOT NULL DEFAULT 'general',
    weight          REAL DEFAULT 1.0,
    condition       TEXT,
    source_unit_id  TEXT,
    created_at      REAL NOT NULL,
    valid_until     REAL,
    PRIMARY KEY (instance_id, user_id, edge_id)
);
CREATE INDEX IF NOT EXISTS idx_ke_source
    ON knowledge_edges(instance_id, user_id, source_node_id);
CREATE INDEX IF NOT EXISTS idx_ke_target
    ON knowledge_edges(instance_id, user_id, target_node_id);
"""
        try:
            await db.executescript(graph_schema)
            logger.info("[SessionContextDB] Migration: knowledge graph tables ready")
        except Exception as e:
            logger.warning(f"[SessionContextDB] Graph table migration failed: {e}")

        # 预测记录表（幂等建表）
        prediction_schema = """
CREATE TABLE IF NOT EXISTS prediction_records (
    pred_id         TEXT PRIMARY KEY,
    user_id         INTEGER NOT NULL,
    instance_id     TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    subject         TEXT NOT NULL,
    prediction_text TEXT NOT NULL,
    direction       TEXT,
    timeframe       TEXT,
    verify_before   REAL NOT NULL,
    status          TEXT DEFAULT 'pending',
    actual_outcome  TEXT,
    accuracy        REAL,
    verification_note TEXT,
    verified_at     REAL,
    created_at      REAL NOT NULL,
    source_edge_id  TEXT
);
CREATE INDEX IF NOT EXISTS idx_pred_pending
    ON prediction_records(instance_id, user_id, status, verify_before);
CREATE INDEX IF NOT EXISTS idx_pred_subject
    ON prediction_records(instance_id, user_id, subject, created_at DESC);
"""
        try:
            await db.executescript(prediction_schema)
            logger.info("[SessionContextDB] Migration: prediction_records table ready")
        except Exception as e:
            logger.warning(f"[SessionContextDB] Prediction table migration failed: {e}")

        # 学习评测快照表 + 基础评测用例表（幂等建表）
        eval_schema = """
CREATE TABLE IF NOT EXISTS learn_snapshots (
    snapshot_id     TEXT PRIMARY KEY,
    user_id         INTEGER NOT NULL,
    instance_id     TEXT NOT NULL,
    snapshot_type   TEXT NOT NULL,
    learn_cycle_id  TEXT NOT NULL,
    triggered_by    TEXT NOT NULL,
    total_verified  INTEGER DEFAULT 0,
    correct_count   INTEGER DEFAULT 0,
    wrong_count     INTEGER DEFAULT 0,
    accuracy_rate   REAL DEFAULT 0.0,
    subject_stats   TEXT,
    active_rules_count INTEGER DEFAULT 0,
    new_rules_count INTEGER DEFAULT 0,
    baseline_pass   INTEGER DEFAULT 1,
    baseline_score  REAL DEFAULT 0.0,
    baseline_detail TEXT,
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ls_user
    ON learn_snapshots(instance_id, user_id, snapshot_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ls_cycle
    ON learn_snapshots(learn_cycle_id);

CREATE TABLE IF NOT EXISTS baseline_cases (
    case_id             TEXT PRIMARY KEY,
    user_id             INTEGER NOT NULL,
    instance_id         TEXT NOT NULL,
    subject             TEXT NOT NULL,
    category            TEXT NOT NULL,
    question            TEXT NOT NULL,
    expected_direction  TEXT NOT NULL,
    expected_keywords   TEXT,
    difficulty          TEXT DEFAULT 'basic',
    source              TEXT DEFAULT 'auto_extracted',
    source_pred_id      TEXT,
    created_at          REAL NOT NULL,
    is_active           INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_bc_user
    ON baseline_cases(instance_id, user_id, subject, is_active);
"""
        try:
            await db.executescript(eval_schema)
            logger.info("[SessionContextDB] Migration: learn_snapshots + baseline_cases tables ready")
        except Exception as e:
            logger.warning(f"[SessionContextDB] Eval table migration failed: {e}")

    def _connect(self):
        """获取数据库连接（返回 async context manager）"""
        return aiosqlite.connect(self._db_path)

    async def _setup_conn(self, db: aiosqlite.Connection):
        """设置连接参数"""
        db.row_factory = aiosqlite.Row
        if self._wal_mode:
            await db.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")

    # ──── CLI Session 映射 ────

    async def get_cli_session(
        self, session_id: str, user_id: int, instance_id: str,
    ) -> Optional[str]:
        """获取 CLI session ID 映射"""
        await self._ensure_init()
        async with self._connect() as db:
            await self._setup_conn(db)
            cursor = await db.execute(
                "SELECT cli_session_id FROM cli_sessions "
                "WHERE instance_id=? AND user_id=? AND session_id=?",
                (instance_id, user_id, session_id),
            )
            row = await cursor.fetchone()
            return row["cli_session_id"] if row else None

    async def save_cli_session(
        self, session_id: str, user_id: int, instance_id: str,
        cli_session_id: str,
    ):
        """保存 CLI session ID 映射（upsert）"""
        await self._ensure_init()
        async with self._connect() as db:
            await self._setup_conn(db)
            await db.execute(
                "INSERT OR REPLACE INTO cli_sessions "
                "(session_id, user_id, instance_id, cli_session_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, user_id, instance_id, cli_session_id, int(time.time())),
            )
            await db.commit()

    async def delete_cli_session(
        self, session_id: str, user_id: int, instance_id: str,
    ):
        """删除 CLI session ID 映射"""
        await self._ensure_init()
        async with self._connect() as db:
            await self._setup_conn(db)
            await db.execute(
                "DELETE FROM cli_sessions "
                "WHERE instance_id=? AND user_id=? AND session_id=?",
                (instance_id, user_id, session_id),
            )
            await db.commit()

    # ──── 对话历史 ────

    async def get_history(
        self, session_id: str, user_id: int, instance_id: str,
        limit: int = 30,
    ) -> List[Dict[str, str]]:
        """获取对话历史消息（按时间正序返回）"""
        await self._ensure_init()
        async with self._connect() as db:
            await self._setup_conn(db)
            cursor = await db.execute(
                "SELECT role, content, metadata FROM conversation_messages "
                "WHERE instance_id=? AND user_id=? AND session_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (instance_id, user_id, session_id, limit),
            )
            rows = await cursor.fetchall()
            messages = []
            for row in reversed(rows):
                msg = {"role": row["role"], "content": row["content"]}
                if row["metadata"]:
                    try:
                        msg["metadata"] = json.loads(row["metadata"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                messages.append(msg)
            return messages

    async def append_message(
        self, session_id: str, user_id: int, instance_id: str,
        role: str, content: str, metadata: Optional[Dict] = None,
    ):
        """追加一条对话消息"""
        await self._ensure_init()
        async with self._connect() as db:
            await self._setup_conn(db)
            await db.execute(
                "INSERT INTO conversation_messages "
                "(session_id, user_id, instance_id, role, content, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id, user_id, instance_id, role, content,
                    json.dumps(metadata, ensure_ascii=False) if metadata else None,
                    int(time.time()),
                ),
            )
            await db.commit()

    async def update_last_assistant(
        self, session_id: str, user_id: int, instance_id: str,
        content: str, metadata_json: Optional[str] = None,
    ):
        """更新最后一条 assistant 消息的内容"""
        await self._ensure_init()
        async with self._connect() as db:
            await self._setup_conn(db)
            # 找到最新一条 assistant 的 rowid
            cursor = await db.execute(
                "SELECT rowid FROM conversation_messages "
                "WHERE instance_id=? AND user_id=? AND session_id=? AND role='assistant' "
                "ORDER BY created_at DESC LIMIT 1",
                (instance_id, user_id, session_id),
            )
            row = await cursor.fetchone()
            if row:
                metadata = None
                if metadata_json:
                    try:
                        metadata = json.loads(metadata_json)
                    except (json.JSONDecodeError, TypeError):
                        pass
                await db.execute(
                    "UPDATE conversation_messages SET content=?, metadata=? WHERE rowid=?",
                    (
                        content,
                        json.dumps(metadata, ensure_ascii=False) if metadata else None,
                        row["rowid"],
                    ),
                )
                await db.commit()

    async def save_history(
        self, session_id: str, user_id: int, instance_id: str,
        messages: List[Dict[str, str]],
    ):
        """全量覆盖保存对话历史（先删后插）"""
        await self._ensure_init()
        async with self._connect() as db:
            await self._setup_conn(db)
            await db.execute(
                "DELETE FROM conversation_messages "
                "WHERE instance_id=? AND user_id=? AND session_id=?",
                (instance_id, user_id, session_id),
            )
            now = int(time.time())
            for i, msg in enumerate(messages):
                metadata_str = None
                if "metadata" in msg and msg["metadata"]:
                    metadata_str = json.dumps(msg["metadata"], ensure_ascii=False)
                await db.execute(
                    "INSERT INTO conversation_messages "
                    "(session_id, user_id, instance_id, role, content, metadata, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id, user_id, instance_id,
                        msg.get("role", "user"),
                        msg.get("content", ""),
                        metadata_str,
                        now + i,  # 保持顺序
                    ),
                )
            await db.commit()

    async def trim_history(
        self, session_id: str, user_id: int, instance_id: str,
        keep_recent: int = 30,
    ):
        """保留最近 N 条消息，删除更早的"""
        await self._ensure_init()
        async with self._connect() as db:
            await self._setup_conn(db)
            # 使用 id（自增主键）来精确定位要保留的消息
            await db.execute(
                "DELETE FROM conversation_messages "
                "WHERE instance_id=? AND user_id=? AND session_id=? "
                "AND id NOT IN ("
                "  SELECT id FROM conversation_messages "
                "  WHERE instance_id=? AND user_id=? AND session_id=? "
                "  ORDER BY id DESC LIMIT ?"
                ")",
                (
                    instance_id, user_id, session_id,
                    instance_id, user_id, session_id,
                    keep_recent,
                ),
            )
            await db.commit()

    # ──── 对话摘要 ────

    async def get_summary(
        self, session_id: str, user_id: int, instance_id: str,
    ) -> Optional[str]:
        """获取对话摘要文本"""
        await self._ensure_init()
        async with self._connect() as db:
            await self._setup_conn(db)
            cursor = await db.execute(
                "SELECT summary_text FROM conversation_summaries "
                "WHERE instance_id=? AND user_id=? AND session_id=?",
                (instance_id, user_id, session_id),
            )
            row = await cursor.fetchone()
            return row["summary_text"] if row else None

    async def save_summary(
        self, session_id: str, user_id: int, instance_id: str,
        summary_text: str, summary_chain: Optional[str] = None,
    ):
        """保存对话摘要（upsert）"""
        await self._ensure_init()
        async with self._connect() as db:
            await self._setup_conn(db)
            await db.execute(
                "INSERT OR REPLACE INTO conversation_summaries "
                "(session_id, user_id, instance_id, summary_text, summary_chain, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session_id, user_id, instance_id,
                    summary_text, summary_chain, int(time.time()),
                ),
            )
            await db.commit()

    async def get_summary_chain(
        self, session_id: str, user_id: int, instance_id: str,
    ) -> Optional[str]:
        """获取摘要链 JSON（ProgressiveSummarizer 用）"""
        await self._ensure_init()
        async with self._connect() as db:
            await self._setup_conn(db)
            cursor = await db.execute(
                "SELECT summary_chain FROM conversation_summaries "
                "WHERE instance_id=? AND user_id=? AND session_id=?",
                (instance_id, user_id, session_id),
            )
            row = await cursor.fetchone()
            return row["summary_chain"] if row else None

    # ──── 经验知识 ────

    async def get_experience(
        self, session_id: str, user_id: int, instance_id: str,
    ) -> Optional[Dict]:
        """获取用户经验（返回 dimensions dict）"""
        await self._ensure_init()
        async with self._connect() as db:
            await self._setup_conn(db)
            cursor = await db.execute(
                "SELECT dimensions FROM experiences "
                "WHERE instance_id=? AND user_id=? AND session_id=?",
                (instance_id, user_id, session_id),
            )
            row = await cursor.fetchone()
            if row:
                try:
                    return json.loads(row["dimensions"])
                except (json.JSONDecodeError, TypeError):
                    return None
            return None

    async def save_experience(
        self, session_id: str, user_id: int, instance_id: str,
        dimensions: Dict,
    ):
        """保存用户经验（upsert，全量替换）"""
        await self._ensure_init()
        async with self._connect() as db:
            await self._setup_conn(db)
            await db.execute(
                "INSERT OR REPLACE INTO experiences "
                "(session_id, user_id, instance_id, dimensions, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    session_id, user_id, instance_id,
                    json.dumps(dimensions, ensure_ascii=False),
                    int(time.time()),
                ),
            )
            await db.commit()

    # ──── MTM 页面（中期记忆） ────

    async def get_mtm_pages(
        self, user_id: int, instance_id: str,
        order_by_heat: bool = True, limit: int = 200,
    ) -> List[Dict]:
        """获取用户所有 MTM 页面（按热度降序）"""
        await self._ensure_init()
        async with self._connect() as db:
            await self._setup_conn(db)
            order = "heat_score DESC" if order_by_heat else "created_at DESC"
            cursor = await db.execute(
                f"SELECT * FROM mtm_pages "
                f"WHERE instance_id=? AND user_id=? "
                f"ORDER BY {order} LIMIT ?",
                (instance_id, user_id, limit),
            )
            rows = await cursor.fetchall()
            return [self._mtm_row_to_dict(row) for row in rows]

    async def get_mtm_page(
        self, page_id: str, user_id: int, instance_id: str,
    ) -> Optional[Dict]:
        """获取单个 MTM 页面"""
        await self._ensure_init()
        async with self._connect() as db:
            await self._setup_conn(db)
            cursor = await db.execute(
                "SELECT * FROM mtm_pages "
                "WHERE instance_id=? AND user_id=? AND page_id=?",
                (instance_id, user_id, page_id),
            )
            row = await cursor.fetchone()
            return self._mtm_row_to_dict(row) if row else None

    async def get_mtm_pages_by_session(
        self, session_id: str, user_id: int, instance_id: str,
    ) -> List[Dict]:
        """获取指定 session 的 MTM 页面"""
        await self._ensure_init()
        async with self._connect() as db:
            await self._setup_conn(db)
            cursor = await db.execute(
                "SELECT * FROM mtm_pages "
                "WHERE instance_id=? AND user_id=? AND session_id=? "
                "ORDER BY heat_score DESC",
                (instance_id, user_id, session_id),
            )
            rows = await cursor.fetchall()
            return [self._mtm_row_to_dict(row) for row in rows]

    async def get_mtm_pages_with_embedding(
        self, user_id: int, instance_id: str, limit: int = 200,
    ) -> List[Dict]:
        """获取所有 MTM 页面，包含 summary_embedding BLOB（用于语义向量召回）"""
        await self._ensure_init()
        async with self._connect() as db:
            await self._setup_conn(db)
            cursor = await db.execute(
                "SELECT *, summary_embedding FROM mtm_pages "
                "WHERE instance_id=? AND user_id=? "
                "ORDER BY heat_score DESC LIMIT ?",
                (instance_id, user_id, limit),
            )
            rows = await cursor.fetchall()
            result = []
            for row in rows:
                d = self._mtm_row_to_dict(row)
                # summary_embedding 是 bytes 或 None，直接保留
                try:
                    d["summary_embedding"] = bytes(row["summary_embedding"]) if row["summary_embedding"] else None
                except Exception:
                    d["summary_embedding"] = None
                result.append(d)
            return result

    async def save_mtm_page(
        self, user_id: int, instance_id: str, page: Dict,
    ):
        """保存 MTM 页面（upsert）"""
        await self._ensure_init()
        now = int(time.time())
        async with self._connect() as db:
            await self._setup_conn(db)
            await db.execute(
                "INSERT OR REPLACE INTO mtm_pages "
                "(page_id, user_id, instance_id, session_id, summary, "
                "topics, entities, msg_range_start, msg_range_end, "
                "interaction_length, visit_count, heat_score, "
                "created_at, last_access_at, summary_embedding) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    page["page_id"], user_id, instance_id,
                    page.get("session_id", ""),
                    page.get("summary", ""),
                    json.dumps(page.get("topics", []), ensure_ascii=False),
                    json.dumps(page.get("entities", []), ensure_ascii=False),
                    page.get("msg_range_start", 0),
                    page.get("msg_range_end", 0),
                    page.get("interaction_length", 0),
                    page.get("visit_count", 1),
                    page.get("heat_score", 0.0),
                    page.get("created_at", now),
                    page.get("last_access_at", now),
                    page.get("summary_embedding"),  # Optional[bytes]
                ),
            )
            await db.commit()

    async def update_mtm_page(
        self, page_id: str, user_id: int, instance_id: str,
        updates: Dict,
    ):
        """部分更新 MTM 页面字段"""
        await self._ensure_init()
        allowed_fields = {
            "summary", "topics", "entities", "visit_count",
            "heat_score", "last_access_at", "interaction_length",
            "msg_range_start", "msg_range_end", "summary_embedding",
        }
        set_clauses = []
        values = []
        for key, val in updates.items():
            if key not in allowed_fields:
                continue
            if key in ("topics", "entities"):
                val = json.dumps(val, ensure_ascii=False)
            set_clauses.append(f"{key}=?")
            values.append(val)

        if not set_clauses:
            return

        values.extend([instance_id, user_id, page_id])
        async with self._connect() as db:
            await self._setup_conn(db)
            await db.execute(
                f"UPDATE mtm_pages SET {', '.join(set_clauses)} "
                f"WHERE instance_id=? AND user_id=? AND page_id=?",
                values,
            )
            await db.commit()

    async def delete_mtm_pages(
        self, page_ids: List[str], user_id: int, instance_id: str,
    ):
        """批量删除 MTM 页面"""
        if not page_ids:
            return
        await self._ensure_init()
        placeholders = ",".join(["?"] * len(page_ids))
        async with self._connect() as db:
            await self._setup_conn(db)
            await db.execute(
                f"DELETE FROM mtm_pages "
                f"WHERE instance_id=? AND user_id=? AND page_id IN ({placeholders})",
                [instance_id, user_id] + list(page_ids),
            )
            await db.commit()

    async def delete_expired_mtm_pages(
        self, user_id: int, instance_id: str, max_age_seconds: int,
    ) -> int:
        """删除过期 MTM 页面，返回删除条数"""
        await self._ensure_init()
        cutoff = int(time.time()) - max_age_seconds
        async with self._connect() as db:
            await self._setup_conn(db)
            cursor = await db.execute(
                "DELETE FROM mtm_pages "
                "WHERE instance_id=? AND user_id=? AND last_access_at < ?",
                (instance_id, user_id, cutoff),
            )
            await db.commit()
            return cursor.rowcount

    async def count_mtm_pages(
        self, user_id: int, instance_id: str,
    ) -> int:
        """统计 MTM 页面数量"""
        await self._ensure_init()
        async with self._connect() as db:
            await self._setup_conn(db)
            cursor = await db.execute(
                "SELECT COUNT(*) as cnt FROM mtm_pages "
                "WHERE instance_id=? AND user_id=?",
                (instance_id, user_id),
            )
            row = await cursor.fetchone()
            return row["cnt"] if row else 0

    @staticmethod
    def _mtm_row_to_dict(row) -> Dict:
        """将 MTM 页面行转为 dict"""
        d = {
            "page_id": row["page_id"],
            "user_id": row["user_id"],
            "instance_id": row["instance_id"],
            "session_id": row["session_id"],
            "summary": row["summary"],
            "msg_range_start": row["msg_range_start"],
            "msg_range_end": row["msg_range_end"],
            "interaction_length": row["interaction_length"],
            "visit_count": row["visit_count"],
            "heat_score": row["heat_score"],
            "created_at": row["created_at"],
            "last_access_at": row["last_access_at"],
        }
        try:
            d["topics"] = json.loads(row["topics"])
        except (json.JSONDecodeError, TypeError):
            d["topics"] = []
        try:
            d["entities"] = json.loads(row["entities"])
        except (json.JSONDecodeError, TypeError):
            d["entities"] = []
        return d

    # ──── 用户画像（长期记忆） ────

    async def get_user_profile(
        self, user_id: int, instance_id: str,
    ) -> Optional[Dict]:
        """获取用户画像"""
        await self._ensure_init()
        async with self._connect() as db:
            await self._setup_conn(db)
            cursor = await db.execute(
                "SELECT dimensions, update_count, updated_at FROM user_profiles "
                "WHERE instance_id=? AND user_id=?",
                (instance_id, user_id),
            )
            row = await cursor.fetchone()
            if row:
                try:
                    return {
                        "user_id": user_id,
                        "instance_id": instance_id,
                        "dimensions": json.loads(row["dimensions"]),
                        "update_count": row["update_count"],
                        "updated_at": row["updated_at"],
                    }
                except (json.JSONDecodeError, TypeError):
                    return None
            return None

    async def save_user_profile(
        self, user_id: int, instance_id: str,
        dimensions: Dict, update_count: int = 0,
    ):
        """保存用户画像（upsert）"""
        await self._ensure_init()
        async with self._connect() as db:
            await self._setup_conn(db)
            await db.execute(
                "INSERT OR REPLACE INTO user_profiles "
                "(user_id, instance_id, dimensions, update_count, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    user_id, instance_id,
                    json.dumps(dimensions, ensure_ascii=False),
                    update_count,
                    int(time.time()),
                ),
            )
            await db.commit()

    # ──── 用户全局经验（跨 session） ────

    async def get_user_experiences(
        self, user_id: int, instance_id: str,
        dimension: Optional[str] = None, limit: int = 50,
    ) -> List[Dict]:
        """获取用户级全局经验（跨 session，按 score 降序）"""
        await self._ensure_init()
        async with self._connect() as db:
            await self._setup_conn(db)
            if dimension:
                cursor = await db.execute(
                    "SELECT dimension, text, score, source_session, created_at, updated_at "
                    "FROM user_experiences "
                    "WHERE instance_id=? AND user_id=? AND dimension=? "
                    "ORDER BY score DESC LIMIT ?",
                    (instance_id, user_id, dimension, limit),
                )
            else:
                cursor = await db.execute(
                    "SELECT dimension, text, score, source_session, created_at, updated_at "
                    "FROM user_experiences "
                    "WHERE instance_id=? AND user_id=? "
                    "ORDER BY score DESC LIMIT ?",
                    (instance_id, user_id, limit),
                )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def upsert_user_experience(
        self, user_id: int, instance_id: str,
        dimension: str, text: str, score: float,
        source_session: str,
    ):
        """写入或更新用户级全局经验（自动去重，score 取最大值）"""
        await self._ensure_init()
        now = int(time.time())
        async with self._connect() as db:
            await self._setup_conn(db)
            await db.execute(
                "INSERT INTO user_experiences "
                "(user_id, instance_id, dimension, text, score, source_session, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(instance_id, user_id, dimension, text) DO UPDATE SET "
                "score = MAX(excluded.score, user_experiences.score), "
                "updated_at = excluded.updated_at",
                (user_id, instance_id, dimension, text, score, source_session, now, now),
            )
            await db.commit()

    async def evict_user_experiences(
        self, user_id: int, instance_id: str,
        dimension: str, max_items: int,
    ):
        """淘汰低价值全局经验（保留 top-N，按 score DESC）"""
        await self._ensure_init()
        async with self._connect() as db:
            await self._setup_conn(db)
            await db.execute(
                "DELETE FROM user_experiences WHERE rowid IN ("
                "  SELECT rowid FROM user_experiences "
                "  WHERE instance_id=? AND user_id=? AND dimension=? "
                "  ORDER BY score DESC LIMIT -1 OFFSET ?"
                ")",
                (instance_id, user_id, dimension, max_items),
            )
            await db.commit()

    async def batch_upsert_user_experiences(
        self, user_id: int, instance_id: str,
        items: List[Dict],
        max_items_per_dim: Optional[Dict[str, int]] = None,
    ):
        """批量写入用户级全局经验（单事务，减少锁竞争）"""
        if not items:
            return
        await self._ensure_init()
        now = int(time.time())
        async with self._connect() as db:
            await self._setup_conn(db)
            for item in items:
                await db.execute(
                    "INSERT INTO user_experiences "
                    "(user_id, instance_id, dimension, text, score, source_session, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(instance_id, user_id, dimension, text) DO UPDATE SET "
                    "score = MAX(excluded.score, user_experiences.score), "
                    "updated_at = excluded.updated_at",
                    (
                        user_id, instance_id,
                        item["dimension"], item["text"],
                        item.get("score", 0.5),
                        item.get("source_session", ""),
                        now, now,
                    ),
                )
            if max_items_per_dim:
                dims_to_evict = set(i["dimension"] for i in items)
                for dim in dims_to_evict:
                    max_n = max_items_per_dim.get(dim)
                    if max_n:
                        await db.execute(
                            "DELETE FROM user_experiences WHERE rowid IN ("
                            "  SELECT rowid FROM user_experiences "
                            "  WHERE instance_id=? AND user_id=? AND dimension=? "
                            "  ORDER BY score DESC LIMIT -1 OFFSET ?"
                            ")",
                            (instance_id, user_id, dim, max_n),
                        )
            await db.commit()

    # ──── knowledge_units embedding ────

    async def set_knowledge_embedding(self, unit_id: str, blob: bytes):
        """更新 knowledge_units 的 text_embedding BLOB"""
        await self._ensure_init()
        async with self._connect() as db:
            await self._setup_conn(db)
            await db.execute(
                "UPDATE knowledge_units SET text_embedding = ? WHERE unit_id = ?",
                (blob, unit_id),
            )
            await db.commit()

    async def get_knowledge_units_with_embedding(
        self, user_id: int, instance_id: str,
    ) -> List[Dict]:
        """获取所有活跃 knowledge_units（含 text_embedding BLOB）"""
        await self._ensure_init()
        async with self._connect() as db:
            await self._setup_conn(db)
            cursor = await db.execute(
                "SELECT unit_id, category, text, tags, utility, confidence, "
                "access_count, hit_count, feedback_reinforcements, feedback_decays, "
                "event_time, ingestion_time, valid_from, valid_until, "
                "superseded_by, supersedes, update_reason, source_episode_id, "
                "created_at, last_accessed, text_embedding "
                "FROM knowledge_units "
                "WHERE user_id = ? AND instance_id = ? AND valid_until IS NULL",
                (user_id, instance_id),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    # ──── user_experiences embedding ────

    async def set_user_experience_embedding(
        self, user_id: int, instance_id: str,
        dimension: str, text: str, blob: bytes,
    ):
        """更新 user_experiences 的 text_embedding BLOB"""
        await self._ensure_init()
        async with self._connect() as db:
            await self._setup_conn(db)
            await db.execute(
                "UPDATE user_experiences SET text_embedding = ? "
                "WHERE instance_id = ? AND user_id = ? AND dimension = ? AND text = ?",
                (blob, instance_id, user_id, dimension, text),
            )
            await db.commit()

    async def get_user_experiences_with_embedding(
        self, user_id: int, instance_id: str,
        dimensions: Optional[List[str]] = None,
    ) -> List[Dict]:
        """获取 user_experiences（含 text_embedding BLOB）"""
        await self._ensure_init()
        async with self._connect() as db:
            await self._setup_conn(db)
            if dimensions:
                placeholders = ",".join("?" * len(dimensions))
                cursor = await db.execute(
                    f"SELECT dimension, text, score, text_embedding "
                    f"FROM user_experiences "
                    f"WHERE instance_id=? AND user_id=? AND dimension IN ({placeholders}) "
                    f"ORDER BY score DESC",
                    [instance_id, user_id] + list(dimensions),
                )
            else:
                cursor = await db.execute(
                    "SELECT dimension, text, score, text_embedding "
                    "FROM user_experiences "
                    "WHERE instance_id=? AND user_id=? "
                    "ORDER BY score DESC",
                    (instance_id, user_id),
                )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    # ──── 生命周期 ────

    async def close(self):
        """关闭（当前实现无需显式关闭，连接在 async with 中自动释放）"""
        logger.debug("[SessionContextDB] close() called (no-op with per-call connections)")
