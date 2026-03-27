"""
async_task skill — 查询和管理后台异步任务状态
通过 agent-service REST API 操作 Task / TaskGroup
"""
import json
import sys
import os
import urllib.request
import urllib.error
from datetime import datetime


_AGENT_SERVICE_URL = os.environ.get("AGENT_SERVICE_URL", "http://agent-service:8000")


def _api(method: str, path: str, body: dict = None) -> dict:
    url = f"{_AGENT_SERVICE_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()[:200]
        return {"error": f"HTTP {e.code}: {body_text}"}
    except Exception as e:
        return {"error": str(e)}


def _elapsed(created_at: str) -> int:
    """返回从创建到现在的秒数"""
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        now = datetime.now(dt.tzinfo)
        return int((now - dt).total_seconds())
    except Exception:
        return -1


def _format_task(t: dict) -> dict:
    result = {
        "task_id": t.get("task_id", ""),
        "name": t.get("name", ""),
        "status": t.get("status", ""),
        "command": (t.get("command") or "")[:120],
        "log_path": t.get("log_path", ""),
        "exit_code": t.get("exit_code"),
        "created_at": t.get("created_at", ""),
    }
    if t.get("created_at"):
        result["elapsed_seconds"] = _elapsed(t["created_at"])
    # 日志尾部
    log_path = t.get("log_path", "")
    if log_path and os.path.exists(log_path):
        try:
            with open(log_path, "r", errors="replace") as f:
                lines = f.readlines()
                result["log_tail"] = "".join(lines[-20:]).strip()
        except Exception:
            pass
    return result


def main():
    raw = sys.stdin.read().strip()
    params = json.loads(raw) if raw else {}

    action = params.get("action", "list")
    task_id = params.get("task_id", "")
    status_filter = params.get("status")
    limit = params.get("limit", 20)

    # ── query ──────────────────────────────────────────────────────────────────
    if action == "query":
        if not task_id:
            print(json.dumps({"status": "error", "error": "task_id 必填"}))
            return
        t = _api("GET", f"/api/v1/tasks/{task_id}")
        if "error" in t:
            print(json.dumps({"status": "error", "error": t["error"]}))
            return
        print(json.dumps({"for_llm": _format_task(t)}, ensure_ascii=False))

    # ── list ───────────────────────────────────────────────────────────────────
    elif action == "list":
        path = f"/api/v1/tasks/?limit={limit}"
        if status_filter:
            path += f"&status={status_filter}"
        data = _api("GET", path)
        if "error" in data:
            print(json.dumps({"status": "error", "error": data["error"]}))
            return
        tasks = data if isinstance(data, list) else data.get("tasks", [])
        formatted = [_format_task(t) for t in tasks]
        summary = {
            "total": len(formatted),
            "running": sum(1 for t in formatted if t.get("status") == "running"),
            "completed": sum(1 for t in formatted if t.get("status") == "completed"),
            "failed": sum(1 for t in formatted if t.get("status") == "failed"),
        }
        print(json.dumps({"for_llm": {"summary": summary, "tasks": formatted}}, ensure_ascii=False))

    # ── cancel ─────────────────────────────────────────────────────────────────
    elif action == "cancel":
        if not task_id:
            print(json.dumps({"status": "error", "error": "task_id 必填"}))
            return
        result = _api("DELETE", f"/api/v1/tasks/{task_id}")
        if "error" in result:
            print(json.dumps({"status": "error", "error": result["error"]}))
            return
        print(json.dumps({"for_llm": {"ok": True, "task_id": task_id, "status": "cancelled"}}, ensure_ascii=False))

    else:
        print(json.dumps({"status": "error", "error": f"未知 action: {action}，支持 query/list/cancel"}))


if __name__ == "__main__":
    main()
