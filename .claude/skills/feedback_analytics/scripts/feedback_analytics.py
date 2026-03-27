"""
feedback_analytics — 反馈统计分析技能脚本

查询 Agent 回答的用户满意度统计，或新增一条反馈记录。
数据源: MySQL v4_report_feedback 表（直接通过 aiomysql + 环境变量连接）

使用方式:
  python3 feedback_analytics.py --action stats
  python3 feedback_analytics.py --action stats --start-date 2026-01-01 --channel dingtalk
  python3 feedback_analytics.py --action add_feedback --report-id ID --rating like
  echo '{"action":"stats"}' | python3 feedback_analytics.py
"""

import argparse
import asyncio
import json
import sys
import os


async def _get_db_connection():
    """Get database connection from env vars."""
    import aiomysql
    host = os.getenv("DB_HOST", "localhost")
    port = int(os.getenv("DB_PORT", "3306"))
    user = os.getenv("DB_USER", "root")
    password = os.getenv("DB_PASSWORD", "")
    db = os.getenv("DB_NAME", "agent_db")
    return await aiomysql.connect(host=host, port=port, user=user, password=password, db=db)


async def action_stats(
    start_date=None,
    end_date=None,
    channel=None,
) -> dict:
    """查询多维度反馈统计"""
    try:
        import aiomysql
        conn = await _get_db_connection()
        async with conn.cursor(aiomysql.DictCursor) as cursor:
            where_clauses = []
            args = []
            if start_date:
                where_clauses.append("created_at >= %s")
                args.append(start_date)
            if end_date:
                where_clauses.append("created_at <= %s")
                args.append(end_date)
            if channel:
                where_clauses.append("channel = %s")
                args.append(channel)
            where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
            await cursor.execute(
                f"SELECT rating, COUNT(*) as count FROM v4_report_feedback {where_sql} GROUP BY rating",
                args
            )
            rows = await cursor.fetchall()
        conn.close()
        stats = {row["rating"]: row["count"] for row in rows}
        total = sum(stats.values())
        return {
            "total": total,
            "like": stats.get("like", 0),
            "dislike": stats.get("dislike", 0),
            "like_rate": round(stats.get("like", 0) / total * 100, 1) if total > 0 else 0,
        }
    except Exception as e:
        return {"error": str(e), "total": 0, "like": 0, "dislike": 0}


async def action_add_feedback(
    report_id: str,
    rating: str,
    comment: str = None,
    channel: str = "web",
    session_id: str = None,
) -> dict:
    """新增或更新一条反馈记录"""
    if rating not in ("like", "dislike"):
        return {"status": "error", "error": "rating 必须是 like 或 dislike"}
    if not report_id:
        return {"status": "error", "error": "report_id 不能为空"}
    try:
        import aiomysql
        conn = await _get_db_connection()
        async with conn.cursor() as cursor:
            await cursor.execute(
                """INSERT INTO v4_report_feedback (report_id, session_id, rating, comment, channel)
                   VALUES (%s, %s, %s, %s, %s)
                   ON DUPLICATE KEY UPDATE rating=%s, comment=%s""",
                (report_id, session_id or report_id, rating, comment, channel, rating, comment)
            )
        await conn.commit()
        conn.close()
        return {"status": "success", "message": "反馈已保存"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


async def run(params: dict) -> dict:
    action = params.get("action", "stats")

    if action == "stats":
        data = await action_stats(
            start_date=params.get("start_date"),
            end_date=params.get("end_date"),
            channel=params.get("channel"),
        )
        return {"status": "success", "data": data}

    elif action == "add_feedback":
        result = await action_add_feedback(
            report_id=params.get("report_id", ""),
            rating=params.get("rating", ""),
            comment=params.get("comment"),
            channel=params.get("channel", "web"),
            session_id=params.get("session_id"),
        )
        return result

    else:
        return {"status": "error", "error": f"未知 action: {action}，支持 stats / add_feedback"}


def main():
    params = {}

    # 支持 stdin JSON
    if not sys.stdin.isatty():
        try:
            raw = sys.stdin.read().strip()
            if raw:
                params = json.loads(raw)
        except Exception:
            pass

    # 支持 CLI 参数（覆盖 stdin）
    parser = argparse.ArgumentParser(description="反馈统计分析技能")
    parser.add_argument("--action", type=str, dest="action",
                        choices=["stats", "add_feedback"], default="stats",
                        help="动作: stats(统计) 或 add_feedback(新增反馈)")
    parser.add_argument("--start-date", type=str, dest="start_date",
                        help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end-date", type=str, dest="end_date",
                        help="截止日期 YYYY-MM-DD")
    parser.add_argument("--channel", type=str, dest="channel",
                        choices=["web", "dingtalk"], help="渠道筛选")
    parser.add_argument("--report-id", type=str, dest="report_id",
                        help="报告ID (add_feedback时必填)")
    parser.add_argument("--rating", type=str, dest="rating",
                        choices=["like", "dislike"], help="评分 like/dislike")
    parser.add_argument("--comment", type=str, dest="comment",
                        help="评论文字")
    parser.add_argument("--session-id", type=str, dest="session_id",
                        help="会话ID")

    args = parser.parse_args()
    for k, v in vars(args).items():
        if v is not None:
            params[k] = v

    result = asyncio.run(run(params))
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    main()
