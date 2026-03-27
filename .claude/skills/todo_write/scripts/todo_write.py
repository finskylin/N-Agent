"""
todo_write skill — 任务清单管理

操作：
  write  — 创建/替换当前 session 的 todo 列表
  update — 更新单条 todo 状态
  read   — 读取当前 todo 列表
  clear  — 清除当前 session 的 todo 列表

遵守 Skill 架构约束：
  - 不 import agent_core.*、app.*
  - 通过环境变量获取配置，直接操作 SQLite
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import List, Optional


# ─── 配置 ────────────────────────────────────────────────────────────────────

def _get_db_path(instance_id: str) -> str:
    template = os.getenv(
        "V4_SQLITE_DB_PATH_TEMPLATE",
        "app/data/sessions/{instance_id}/memory.db",
    )
    return template.replace("{instance_id}", instance_id)


def _get_instance_id() -> str:
    import socket
    return os.getenv("AGENT_INSTANCE_ID", f"agent-{socket.gethostname()[:8]}")


# ─── DB 工具 ──────────────────────────────────────────────────────────────────

def _open_db(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_table(conn)
    return conn


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS todos (
            id          TEXT NOT NULL,
            session_id  TEXT NOT NULL,
            content     TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'pending',
            note        TEXT NOT NULL DEFAULT '',
            position    INTEGER NOT NULL DEFAULT 0,
            created_at  INTEGER NOT NULL,
            updated_at  INTEGER NOT NULL,
            PRIMARY KEY (id, session_id)
        )
    """)
    conn.commit()


# ─── 状态符号 ─────────────────────────────────────────────────────────────────

STATUS_ICON = {
    "pending":     "⏳",
    "in_progress": "🔧",
    "completed":   "✅",
    "failed":      "❌",
}

VALID_STATUSES = {"pending", "in_progress", "completed", "failed"}


# ─── actions ──────────────────────────────────────────────────────────────────

def _action_write(conn: sqlite3.Connection, session_id: str, todos: list) -> dict:
    """替换整个 todo 列表"""
    if not todos:
        return {"error": "todos 列表不能为空"}

    now = int(time.time())
    # 清除当前 session 旧数据
    conn.execute("DELETE FROM todos WHERE session_id = ?", (session_id,))

    rows = []
    for i, todo in enumerate(todos):
        todo_id = todo.get("id", f"step_{i+1}")
        content = todo.get("content", "").strip()
        status = todo.get("status", "pending")
        if not content:
            return {"error": f"todo[{i}].content 不能为空"}
        if status not in VALID_STATUSES:
            status = "pending"
        rows.append((todo_id, session_id, content, status, "", i, now, now))

    conn.executemany(
        "INSERT INTO todos (id, session_id, content, status, note, position, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()

    result_todos = _read_todos(conn, session_id)
    return {
        "action": "write",
        "total": len(result_todos),
        "todos": result_todos,
        "summary": f"已创建 {len(result_todos)} 个任务步骤，开始执行...",
    }


def _action_update(
    conn: sqlite3.Connection,
    session_id: str,
    todo_id: str,
    status: str,
    note: str = "",
) -> dict:
    """更新单条 todo 状态"""
    if status not in VALID_STATUSES:
        return {"error": f"无效状态 '{status}'，可选：{', '.join(VALID_STATUSES)}"}

    now = int(time.time())
    cur = conn.execute(
        "UPDATE todos SET status=?, note=?, updated_at=? "
        "WHERE id=? AND session_id=?",
        (status, note, now, todo_id, session_id),
    )
    conn.commit()

    if cur.rowcount == 0:
        return {"error": f"todo_id '{todo_id}' 不存在（session_id={session_id}）"}

    result_todos = _read_todos(conn, session_id)
    completed = sum(1 for t in result_todos if t["status"] == "completed")
    failed = sum(1 for t in result_todos if t["status"] == "failed")
    total = len(result_todos)

    icon = STATUS_ICON.get(status, "")
    summary = f"{icon} [{todo_id}] → {status}"
    if note:
        summary += f"（{note}）"
    summary += f" | 进度 {completed}/{total}"
    if failed:
        summary += f"，{failed} 个失败"

    return {
        "action": "update",
        "todo_id": todo_id,
        "status": status,
        "todos": result_todos,
        "summary": summary,
    }


def _action_read(conn: sqlite3.Connection, session_id: str) -> dict:
    """读取当前 todo 列表"""
    todos = _read_todos(conn, session_id)
    if not todos:
        return {
            "action": "read",
            "total": 0,
            "todos": [],
            "summary": "当前没有任务清单",
        }

    total = len(todos)
    completed = sum(1 for t in todos if t["status"] == "completed")
    in_progress = sum(1 for t in todos if t["status"] == "in_progress")
    failed = sum(1 for t in todos if t["status"] == "failed")

    lines = []
    for t in todos:
        icon = STATUS_ICON.get(t["status"], "⏳")
        line = f"{icon} {t['content']}"
        if t["note"]:
            line += f"（{t['note']}）"
        lines.append(line)

    summary = f"进度 {completed}/{total}"
    if in_progress:
        summary += f"，{in_progress} 个执行中"
    if failed:
        summary += f"，{failed} 个失败"

    return {
        "action": "read",
        "total": total,
        "completed": completed,
        "in_progress": in_progress,
        "failed": failed,
        "todos": todos,
        "display": "\n".join(lines),
        "summary": summary,
    }


def _action_clear(conn: sqlite3.Connection, session_id: str) -> dict:
    """清除当前 session 的 todo 列表"""
    conn.execute("DELETE FROM todos WHERE session_id = ?", (session_id,))
    conn.commit()
    return {
        "action": "clear",
        "summary": "任务清单已清除",
    }


def _read_todos(conn: sqlite3.Connection, session_id: str) -> list:
    cur = conn.execute(
        "SELECT id, content, status, note FROM todos "
        "WHERE session_id = ? ORDER BY position ASC",
        (session_id,),
    )
    return [
        {
            "id": row["id"],
            "content": row["content"],
            "status": row["status"],
            "note": row["note"],
            "icon": STATUS_ICON.get(row["status"], "⏳"),
        }
        for row in cur.fetchall()
    ]


# ─── 入口 ──────────────────────────────────────────────────────────────────────

def main():
    try:
        raw = sys.stdin.read().strip()
        params = json.loads(raw)
    except Exception as e:
        print(json.dumps({"error": f"参数解析失败: {e}"}))
        sys.exit(1)

    action = params.get("action", "").strip()
    session_id = params.get("session_id", "").strip()

    if not action:
        print(json.dumps({"error": "缺少必填参数: action"}))
        sys.exit(1)
    if not session_id:
        print(json.dumps({"error": "缺少必填参数: session_id"}))
        sys.exit(1)

    instance_id = _get_instance_id()
    db_path = _get_db_path(instance_id)

    try:
        conn = _open_db(db_path)
    except Exception as e:
        print(json.dumps({"error": f"数据库连接失败: {e}"}))
        sys.exit(1)

    try:
        if action == "write":
            todos = params.get("todos")
            if not isinstance(todos, list):
                print(json.dumps({"error": "action=write 时 todos 必须是数组"}))
                sys.exit(1)
            result = _action_write(conn, session_id, todos)

        elif action == "update":
            todo_id = params.get("todo_id", "").strip()
            status = params.get("status", "").strip()
            note = params.get("note", "")
            if not todo_id:
                print(json.dumps({"error": "action=update 时 todo_id 必填"}))
                sys.exit(1)
            if not status:
                print(json.dumps({"error": "action=update 时 status 必填"}))
                sys.exit(1)
            result = _action_update(conn, session_id, todo_id, status, note)

        elif action == "read":
            result = _action_read(conn, session_id)

        elif action == "clear":
            result = _action_clear(conn, session_id)

        else:
            result = {"error": f"未知 action '{action}'，可选：write / update / read / clear"}

    except Exception as e:
        result = {"error": f"执行失败: {e}"}
    finally:
        conn.close()

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
