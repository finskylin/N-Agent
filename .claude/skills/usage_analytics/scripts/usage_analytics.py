"""
usage_analytics — Agent 使用量统计分析 Skill

数据源：
  - v4_skill_outputs  : skill 执行记录（skill_name, user_id, duration_ms, executed_at, success）
  - v4_report_feedback: 用户反馈（rating, tags, channel, created_at）

直接用 aiosqlite 读取 SQLite 文件，不依赖 agent_core / app。
数据库路径：DATABASE_URL 环境变量（sqlite:///xxx.db）或 APP_DB_PATH 兜底。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, date
from typing import Dict, List, Optional, Tuple


# ─────────────────────────────────────────────────────────────────────────────
# DB 路径解析
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_db_path() -> str:
    """从 DATABASE_URL 或 APP_DB_PATH 解析 SQLite 文件路径"""
    db_url = os.getenv("DATABASE_URL", "")
    if db_url:
        # sqlite+aiosqlite:///./agent.db  或  sqlite:///./agent.db
        m = re.search(r"sqlite(?:\+aiosqlite)?://(/?.+)", db_url)
        if m:
            path = m.group(1)
            # sqlite:///./agent.db → ./agent.db
            if path.startswith("/./") or path.startswith("///"):
                path = path.lstrip("/")
            if not os.path.isabs(path):
                project_root = os.getenv("PROJECT_ROOT", os.getcwd())
                path = os.path.join(project_root, path)
            return path

    # 兜底
    fallback = os.getenv("APP_DB_PATH", "")
    if fallback:
        return fallback

    project_root = os.getenv("PROJECT_ROOT", os.getcwd())
    return os.path.join(project_root, "agent.db")


async def _open_db(path: str):
    try:
        import aiosqlite
    except ImportError:
        raise RuntimeError("aiosqlite not installed. Run: pip install aiosqlite")
    if not os.path.exists(path):
        raise FileNotFoundError(f"App DB not found: {path}")
    return await aiosqlite.connect(path)


# ─────────────────────────────────────────────────────────────────────────────
# 日期处理
# ─────────────────────────────────────────────────────────────────────────────

def _parse_dates(params: dict) -> Tuple[str, str]:
    today = date.today()
    end_str = params.get("end_date") or today.strftime("%Y-%m-%d")
    start_str = params.get("start_date") or (today - timedelta(days=6)).strftime("%Y-%m-%d")

    # 规范化为 "YYYY-MM-DD 00:00:00" / "YYYY-MM-DD 23:59:59"
    start_dt = f"{start_str} 00:00:00"
    end_dt = f"{end_str} 23:59:59"
    return start_str, end_str, start_dt, end_dt


# ─────────────────────────────────────────────────────────────────────────────
# 指标查询
# ─────────────────────────────────────────────────────────────────────────────

async def _query_overview(db, start_dt: str, end_dt: str, user_id: Optional[int]) -> dict:
    """总览：总请求数、独立用户数、工具调用次数、成功率"""
    user_filter = "AND user_id = ?" if user_id else ""
    params = [start_dt, end_dt]
    if user_id:
        params.append(user_id)

    # 排除内部 __session_metadata__ 记录
    async with db.execute(
        f"""
        SELECT
            COUNT(*) as total,
            COUNT(DISTINCT user_id) as unique_users,
            SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as success_cnt
        FROM v4_skill_outputs
        WHERE executed_at >= ? AND executed_at <= ?
          AND skill_name != '__session_metadata__'
          {user_filter}
        """,
        params,
    ) as cur:
        row = await cur.fetchone()

    total, unique_users, success_cnt = row or (0, 0, 0)
    total = total or 0
    success_rate = round((success_cnt or 0) / total, 3) if total > 0 else 0.0

    return {
        "total_requests": total,
        "unique_users": unique_users or 0,
        "success_rate": success_rate,
        "success_count": success_cnt or 0,
        "failed_count": total - (success_cnt or 0),
    }


async def _query_daily_trend(db, start_dt: str, end_dt: str, user_id: Optional[int]) -> list:
    """每日使用量趋势"""
    user_filter = "AND user_id = ?" if user_id else ""
    params = [start_dt, end_dt]
    if user_id:
        params.append(user_id)

    async with db.execute(
        f"""
        SELECT
            DATE(executed_at) as day,
            COUNT(*) as requests,
            COUNT(DISTINCT user_id) as users,
            SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as success_cnt
        FROM v4_skill_outputs
        WHERE executed_at >= ? AND executed_at <= ?
          AND skill_name != '__session_metadata__'
          {user_filter}
        GROUP BY DATE(executed_at)
        ORDER BY day ASC
        """,
        params,
    ) as cur:
        rows = await cur.fetchall()

    return [
        {
            "date": row[0],
            "requests": row[1],
            "unique_users": row[2],
            "success_count": row[3],
        }
        for row in rows
    ]


async def _query_user_ranking(db, start_dt: str, end_dt: str, top_n: int) -> list:
    """用户使用量排行"""
    async with db.execute(
        """
        SELECT
            user_id,
            COUNT(*) as requests,
            SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as success_cnt,
            AVG(duration_ms) as avg_dur,
            MAX(duration_ms) as max_dur,
            MIN(executed_at) as first_at,
            MAX(executed_at) as last_at
        FROM v4_skill_outputs
        WHERE executed_at >= ? AND executed_at <= ?
          AND skill_name != '__session_metadata__'
        GROUP BY user_id
        ORDER BY requests DESC
        LIMIT ?
        """,
        (start_dt, end_dt, top_n),
    ) as cur:
        rows = await cur.fetchall()

    return [
        {
            "user_id": row[0],
            "requests": row[1],
            "success_count": row[2],
            "avg_duration_ms": round(row[3] or 0),
            "max_duration_ms": row[4] or 0,
            "first_request_at": row[5],
            "last_request_at": row[6],
        }
        for row in rows
    ]


async def _query_tool_ranking(db, start_dt: str, end_dt: str, user_id: Optional[int], top_n: int) -> list:
    """工具调用频次排行"""
    user_filter = "AND user_id = ?" if user_id else ""
    params = [start_dt, end_dt]
    if user_id:
        params.append(user_id)
    params.append(top_n)

    async with db.execute(
        f"""
        SELECT
            skill_name,
            COUNT(*) as calls,
            SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as success_cnt,
            AVG(duration_ms) as avg_dur,
            MAX(duration_ms) as max_dur,
            MIN(duration_ms) as min_dur
        FROM v4_skill_outputs
        WHERE executed_at >= ? AND executed_at <= ?
          AND skill_name != '__session_metadata__'
          {user_filter}
        GROUP BY skill_name
        ORDER BY calls DESC
        LIMIT ?
        """,
        params,
    ) as cur:
        rows = await cur.fetchall()

    result = []
    for row in rows:
        calls = row[1] or 0
        success = row[2] or 0
        result.append({
            "tool_name": row[0],
            "calls": calls,
            "success_count": success,
            "success_rate": round(success / calls, 3) if calls > 0 else 0.0,
            "avg_duration_ms": round(row[3] or 0),
            "max_duration_ms": row[4] or 0,
            "min_duration_ms": row[5] or 0,
        })
    return result


async def _query_latency(db, start_dt: str, end_dt: str, user_id: Optional[int]) -> dict:
    """耗时统计：均值、最大、最小、P50、P90"""
    user_filter = "AND user_id = ?" if user_id else ""
    params = [start_dt, end_dt]
    if user_id:
        params.append(user_id)

    async with db.execute(
        f"""
        SELECT duration_ms
        FROM v4_skill_outputs
        WHERE executed_at >= ? AND executed_at <= ?
          AND skill_name != '__session_metadata__'
          AND duration_ms > 0
          {user_filter}
        ORDER BY duration_ms ASC
        """,
        params,
    ) as cur:
        rows = await cur.fetchall()

    if not rows:
        return {"avg_ms": 0, "max_ms": 0, "min_ms": 0, "p50_ms": 0, "p90_ms": 0, "sample_count": 0}

    durations = [r[0] for r in rows]
    n = len(durations)
    p50 = durations[int(n * 0.5)]
    p90 = durations[int(n * 0.9)]

    return {
        "avg_ms": round(sum(durations) / n),
        "max_ms": max(durations),
        "min_ms": min(durations),
        "p50_ms": p50,
        "p90_ms": p90,
        "sample_count": n,
    }


async def _query_feedback(db, start_dt: str, end_dt: str, user_id: Optional[int]) -> dict:
    """反馈统计：好评率、差评数、标签频次、渠道分布"""
    user_filter = "AND user_id = ?" if user_id else ""
    params = [start_dt, end_dt]
    if user_id:
        params.append(user_id)

    async with db.execute(
        f"""
        SELECT rating, tags, channel, created_at
        FROM v4_report_feedback
        WHERE created_at >= ? AND created_at <= ?
          {user_filter}
        ORDER BY created_at DESC
        """,
        params,
    ) as cur:
        rows = await cur.fetchall()

    if not rows:
        return {
            "total": 0, "likes": 0, "dislikes": 0, "like_rate": 0.0,
            "top_tags": [], "by_channel": {}, "recent_comments": [],
        }

    likes = sum(1 for r in rows if r[0] == "like")
    dislikes = sum(1 for r in rows if r[0] == "dislike")
    total = len(rows)

    # 标签统计
    tag_counter: Dict[str, int] = defaultdict(int)
    for row in rows:
        if row[1]:
            for tag in row[1].split(","):
                tag = tag.strip()
                if tag:
                    tag_counter[tag] += 1
    top_tags = sorted(tag_counter.items(), key=lambda x: x[1], reverse=True)[:10]

    # 渠道分布
    channel_counter: Dict[str, int] = defaultdict(int)
    for row in rows:
        channel_counter[row[2] or "unknown"] += 1

    return {
        "total": total,
        "likes": likes,
        "dislikes": dislikes,
        "like_rate": round(likes / total, 3) if total > 0 else 0.0,
        "top_tags": [list(t) for t in top_tags],
        "by_channel": dict(channel_counter),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────────────

async def run(params: dict) -> dict:
    start_str, end_str, start_dt, end_dt = _parse_dates(params)
    user_id = params.get("user_id")
    if user_id is not None:
        user_id = int(user_id)
    top_n = int(params.get("top_n") or 10)

    requested = set(params.get("metrics") or [])
    # 空 = 返回全部
    all_metrics = {"overview", "daily_trend", "user_ranking", "tool_ranking", "latency", "feedback"}
    metrics = requested if requested else all_metrics

    db_path = _resolve_db_path()
    try:
        db = await _open_db(db_path)
    except FileNotFoundError as e:
        return {"error": str(e), "hint": "APP_DB_PATH 或 DATABASE_URL 环境变量可能未配置"}
    except Exception as e:
        return {"error": f"无法打开数据库: {e}"}

    result: dict = {
        "period": {"start": start_str, "end": end_str},
        "user_filter": user_id,
    }

    try:
        tasks = {}
        if "overview" in metrics:
            tasks["overview"] = _query_overview(db, start_dt, end_dt, user_id)
        if "daily_trend" in metrics:
            tasks["daily_trend"] = _query_daily_trend(db, start_dt, end_dt, user_id)
        if "user_ranking" in metrics and not user_id:
            tasks["user_ranking"] = _query_user_ranking(db, start_dt, end_dt, top_n)
        if "tool_ranking" in metrics:
            tasks["tool_ranking"] = _query_tool_ranking(db, start_dt, end_dt, user_id, top_n)
        if "latency" in metrics:
            tasks["latency"] = _query_latency(db, start_dt, end_dt, user_id)
        if "feedback" in metrics:
            tasks["feedback"] = _query_feedback(db, start_dt, end_dt, user_id)

        # 并发执行所有查询
        keys = list(tasks.keys())
        values = await asyncio.gather(*[tasks[k] for k in keys], return_exceptions=True)

        for k, v in zip(keys, values):
            if isinstance(v, Exception):
                result[k] = {"error": str(v)}
            else:
                result[k] = v

    finally:
        await db.close()

    return result


def main():
    raw = sys.stdin.read().strip()
    try:
        params = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"JSON 解析失败: {e}"}))
        sys.exit(1)

    output = asyncio.run(run(params))
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
