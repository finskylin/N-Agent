"""
Cron Skill

Native scheduler management skill backed by the application CronService.
stdin JSON → stdout JSON
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional



# ── Inline data models (mirror of app.services.cron_service, no app import) ──

@dataclass
class CronSchedule:
    kind: str
    at: Optional[str] = None
    every_seconds: Optional[int] = None
    cron_expr: Optional[str] = None
    timezone: Optional[str] = None


@dataclass
class CronPayload:
    message: str
    callback: Dict[str, Any] = field(default_factory=dict)
    session_id: Optional[str] = None
    auto_approve_plan: bool = True
    alert_threshold: Optional[str] = None   # 触发阈值描述（自然语言）
    silent_if_no_signal: bool = False        # 未达阈值时静默


@dataclass
class CronJobState:
    next_run_at_ts: Optional[float] = None
    last_run_at_ts: Optional[float] = None
    last_status: Optional[str] = None
    last_error: Optional[str] = None


def _now_ts() -> float:
    return time.time()


def _to_iso(ts: Optional[float]) -> Optional[str]:
    if not ts:
        return None
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


@dataclass
class CronJob:
    id: str
    name: str
    schedule: CronSchedule
    payload: CronPayload
    enabled: bool = True
    delete_after_run: bool = False
    created_at_ts: float = field(default_factory=_now_ts)
    updated_at_ts: float = field(default_factory=_now_ts)
    state: CronJobState = field(default_factory=CronJobState)

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "delete_after_run": self.delete_after_run,
            "schedule": asdict(self.schedule),
            "message": self.payload.message,
            "session_id": self.payload.session_id,
            "created_at": _to_iso(self.created_at_ts),
            "updated_at": _to_iso(self.updated_at_ts),
            "next_run_at": _to_iso(self.state.next_run_at_ts),
            "last_run_at": _to_iso(self.state.last_run_at_ts),
            "last_status": self.state.last_status,
            "last_error": self.state.last_error,
        }


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def _deserialize_job(raw: Dict[str, Any]) -> "CronJob":
    return CronJob(
        id=raw["id"],
        name=raw["name"],
        schedule=CronSchedule(**raw["schedule"]),
        payload=CronPayload(**raw["payload"]),
        enabled=raw.get("enabled", True),
        delete_after_run=raw.get("delete_after_run", False),
        created_at_ts=raw.get("created_at_ts", _now_ts()),
        updated_at_ts=raw.get("updated_at_ts", _now_ts()),
        state=CronJobState(**raw.get("state", {})),
    )


def _parse_iso_datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid ISO datetime '{value}'") from exc


def _get_timezone(tz_name: Optional[str]):
    if not tz_name:
        return datetime.now().astimezone().tzinfo
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tz_name)
    except Exception as exc:
        raise ValueError(f"unknown timezone '{tz_name}'") from exc


def _expand_cron_field(fld: str, lower: int, upper: int) -> List[int]:
    values = set()
    for raw_part in fld.split(","):
        part = raw_part.strip()
        if not part:
            raise ValueError("empty cron field")
        if part == "*":
            values.update(range(lower, upper + 1))
            continue
        if part.startswith("*/"):
            step = int(part[2:])
            if step <= 0:
                raise ValueError("cron step must be positive")
            values.update(range(lower, upper + 1, step))
            continue
        if "/" in part:
            base, step_str = part.split("/", 1)
            step = int(step_str)
            if step <= 0:
                raise ValueError("cron step must be positive")
            if base == "*":
                start, end = lower, upper
            elif "-" in base:
                start_str, end_str = base.split("-", 1)
                start, end = int(start_str), int(end_str)
            else:
                start = end = int(base)
            if start < lower or end > upper or start > end:
                raise ValueError(f"cron field out of range: {part}")
            values.update(range(start, end + 1, step))
            continue
        if "-" in part:
            start_str, end_str = part.split("-", 1)
            start, end = int(start_str), int(end_str)
            if start < lower or end > upper or start > end:
                raise ValueError(f"cron field out of range: {part}")
            values.update(range(start, end + 1))
            continue
        value = int(part)
        if value < lower or value > upper:
            raise ValueError(f"cron field out of range: {part}")
        values.add(value)
    return sorted(values)


def _normalize_weekday(value: int) -> int:
    return 0 if value in (0, 7) else value


def _cron_matches(dt: datetime, expr: str) -> bool:
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError("cron_expr must have 5 fields")
    minute_values = _expand_cron_field(fields[0], 0, 59)
    hour_values = _expand_cron_field(fields[1], 0, 23)
    day_values = _expand_cron_field(fields[2], 1, 31)
    month_values = _expand_cron_field(fields[3], 1, 12)
    weekday_values = {_normalize_weekday(v) for v in _expand_cron_field(fields[4], 0, 7)}
    current_weekday = _normalize_weekday((dt.weekday() + 1) % 7)
    return (
        dt.minute in minute_values
        and dt.hour in hour_values
        and dt.day in day_values
        and dt.month in month_values
        and current_weekday in weekday_values
    )


def _compute_next_run(schedule: CronSchedule, now_ts: float) -> Optional[float]:
    if schedule.kind == "at":
        if not schedule.at:
            return None
        run_at = _parse_iso_datetime(schedule.at).timestamp()
        return run_at if run_at > now_ts else None
    if schedule.kind == "every":
        if not schedule.every_seconds or schedule.every_seconds <= 0:
            return None
        return now_ts + schedule.every_seconds
    if schedule.kind == "cron":
        if not schedule.cron_expr:
            return None
        tzinfo = _get_timezone(schedule.timezone)
        cursor = datetime.fromtimestamp(now_ts, tz=tzinfo).replace(second=0, microsecond=0)
        cursor += timedelta(minutes=1)
        for _ in range(366 * 24 * 60):
            if _cron_matches(cursor, schedule.cron_expr):
                return cursor.timestamp()
            cursor += timedelta(minutes=1)
        return None
    return None


def _validate_schedule(schedule: CronSchedule) -> None:
    if schedule.kind not in {"at", "every", "cron"}:
        raise ValueError(f"unsupported schedule kind '{schedule.kind}'")
    if schedule.kind == "at":
        if schedule.every_seconds or schedule.cron_expr:
            raise ValueError("at schedule cannot include every_seconds or cron_expr")
        if not schedule.at:
            raise ValueError("at schedule requires at")
        _parse_iso_datetime(schedule.at)
        return
    if schedule.kind == "every":
        if schedule.at or schedule.cron_expr:
            raise ValueError("every schedule cannot include at or cron_expr")
        if not schedule.every_seconds or schedule.every_seconds <= 0:
            raise ValueError("every schedule requires positive every_seconds")
        return
    if schedule.kind == "cron":
        if schedule.at or schedule.every_seconds:
            raise ValueError("cron schedule cannot include at or every_seconds")
        if not schedule.cron_expr:
            raise ValueError("cron schedule requires cron_expr")
        _get_timezone(schedule.timezone)
        _cron_matches(datetime.now(), schedule.cron_expr)


# ── Inline CronService (reads/writes JSON store directly, no app import) ──────

class _InlineCronService:
    """Minimal CronService used by the Cron skill to manage the shared JSON store."""

    def __init__(self, store_path: Path) -> None:
        self._store_path = store_path
        self._lock = asyncio.Lock()

    def _load_jobs(self) -> List[CronJob]:
        if not self._store_path.exists():
            return []
        try:
            payload = json.loads(self._store_path.read_text(encoding="utf-8"))
            return [_deserialize_job(item) for item in payload.get("jobs", [])]
        except Exception:
            return []

    def _save_jobs(self, jobs: List[CronJob]) -> None:
        payload = {"version": 1, "jobs": [asdict(job) for job in jobs]}
        _atomic_write_json(self._store_path, payload)

    async def list_jobs(self) -> List[CronJob]:
        async with self._lock:
            return self._load_jobs()

    async def add_job(
        self,
        *,
        name: str,
        schedule: CronSchedule,
        payload: CronPayload,
        delete_after_run: bool = False,
    ) -> CronJob:
        _validate_schedule(schedule)
        now_ts = _now_ts()
        next_run = _compute_next_run(schedule, now_ts)
        if next_run is None:
            raise ValueError("schedule does not produce a future run time")

        job = CronJob(
            id=uuid.uuid4().hex[:12],
            name=name,
            schedule=schedule,
            payload=payload,
            delete_after_run=delete_after_run,
        )
        job.state.next_run_at_ts = next_run

        async with self._lock:
            jobs = self._load_jobs()
            jobs = [j for j in jobs if j.name != name]
            jobs.append(job)
            self._save_jobs(jobs)
        return job

    async def remove_job(self, *, job_id: Optional[str] = None, name: Optional[str] = None) -> bool:
        if not job_id and not name:
            raise ValueError("job_id or name is required")
        async with self._lock:
            jobs = self._load_jobs()
            before = len(jobs)
            jobs = [
                j for j in jobs
                if not ((job_id and j.id == job_id) or (name and j.name == name))
            ]
            removed = len(jobs) != before
            if removed:
                self._save_jobs(jobs)
        return removed


def _get_cron_store_path() -> Path:
    """Resolve the cron JSON store path from env (mirrors app.config.settings.cron_store_path)."""
    store_path_str = os.getenv("CRON_STORE_PATH", "app/data/cron/jobs.json")
    path = Path(store_path_str)
    if not path.is_absolute():
        # 相对路径基于服务根目录（skills 目录的上两级）
        _service_root = Path(__file__).resolve().parent.parent.parent.parent
        path = _service_root / path
    return path


async def _get_or_init_cron_service() -> _InlineCronService:
    return _InlineCronService(store_path=_get_cron_store_path())

# ─────────────────────────────────────────────────────────────────────────────


def _slugify(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "_", (value or "").strip()).strip("_").lower()
    return text or "cron_job"


def _normalize_callback(value: Any) -> Dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {}
    return {}


def _extract_dingtalk_callback(params: Dict[str, Any]) -> Dict[str, Any]:
    callback = {
        "channel": "dingtalk",
        "sender_id": params.get("dingtalk_sender_id", ""),
        "staff_id": params.get("dingtalk_staff_id", ""),
        "sender_nick": params.get("dingtalk_sender", ""),
        "conversation_id": params.get("dingtalk_conversation_id", ""),
        "conversation_type": params.get("dingtalk_conversation_type", ""),
        "robot_code": params.get("dingtalk_robot_code", ""),
    }
    return {key: value for key, value in callback.items() if value}


def _extract_feishu_callback(params: Dict[str, Any]) -> Dict[str, Any]:
    callback = {
        "channel": "feishu",
        "open_id": params.get("feishu_open_id", ""),
        "chat_id": params.get("feishu_chat_id", ""),
        "chat_type": params.get("feishu_chat_type", "p2p"),
        "message_id": params.get("feishu_message_id", ""),
    }
    return {key: value for key, value in callback.items() if value}


def _extract_callback(params: Dict[str, Any]) -> Dict[str, Any]:
    """根据来源渠道自动提取对应 callback，优先飞书再钉钉。"""
    if params.get("feishu_open_id") or params.get("feishu_chat_id"):
        return _extract_feishu_callback(params)
    if params.get("dingtalk_sender_id") or params.get("dingtalk_conversation_id"):
        return _extract_dingtalk_callback(params)
    return {}




class CronSkill:
    @property
    def name(self) -> str:
        return "cron"

    @property
    def description(self) -> str:
        return "创建、列出和删除应用内定时任务，不依赖系统 crontab"

    @property
    def category(self) -> str:
        return "automation"

    @property
    def dependencies(self) -> List[str]:
        return []

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "action": {
                "type": "string",
                "enum": ["add", "list", "remove"],
                "description": "操作类型",
            },
            "message": {
                "type": "string",
                "description": "定时触发时要执行的任务描述",
            },
            "job_name": {
                "type": "string",
                "description": "任务名称，可选，不传则自动生成",
            },
            "every_seconds": {
                "type": "integer",
                "description": "固定间隔调度，单位秒",
            },
            "cron_expr": {
                "type": "string",
                "description": "5 段 cron 表达式，例如 '0 9 * * 1-5'",
            },
            "at": {
                "type": "string",
                "description": "单次执行的 ISO 时间，例如 '2026-03-12T18:30:00'",
            },
            "timezone": {
                "type": "string",
                "description": "cron 表达式使用的 IANA 时区，例如 'Asia/Shanghai'",
            },
            "job_id": {
                "type": "string",
                "description": "删除任务时使用的 job_id",
            },
            "callback": {
                "type": "object",
                "description": "可选回调上下文，例如钉钉会话信息",
            },
            "session_id": {
                "type": "string",
                "description": "可选会话 ID，供回调执行时复用",
            },
            "auto_approve_plan": {
                "type": "boolean",
                "description": "回调执行时是否自动批准 plan，默认 true",
            },
            "alert_threshold": {
                "type": "string",
                "description": "触发通知的阈值描述（自然语言），如'任意股票涨跌幅超过3%'、'出现买入信号'。不填则每次都发送报告",
            },
            "silent_if_no_signal": {
                "type": "boolean",
                "description": "当分析结果未达到 alert_threshold 时，静默不发送消息给用户。需配合 alert_threshold 使用，默认 false",
            },
        }

    async def execute(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """执行 cron 操作，返回 dict {success, data, error}"""
        action = (params.get("action") or "").strip().lower()
        if not action:
            return {"success": False, "error": "缺少 action，必须是 add/list/remove"}

        cron_service = await _get_or_init_cron_service()

        if action == "list":
            jobs = await cron_service.list_jobs()
            return {
                "success": True,
                "data": {"jobs": [job.to_public_dict() for job in jobs], "count": len(jobs)},
                "message": f"已列出 {len(jobs)} 个定时任务",
            }

        if action == "remove":
            job_id = params.get("job_id")
            job_name = params.get("job_name")
            removed = await cron_service.remove_job(job_id=job_id, name=job_name)
            if not removed:
                return {
                    "success": False,
                    "error": "未找到要删除的定时任务，请提供正确的 job_id 或 job_name",
                }
            return {
                "success": True,
                "data": {"removed": True, "job_id": job_id, "job_name": job_name},
                "message": "定时任务已删除",
            }

        if action != "add":
            return {"success": False, "error": f"不支持的 action: {action}"}

        message = (params.get("message") or params.get("task") or "").strip()
        if not message:
            return {"success": False, "error": "add 操作缺少 message"}

        if params.get("cron_schedule") and not params.get("cron_expr"):
            params["cron_expr"] = params.get("cron_schedule")

        try:
            schedule = self._build_schedule(params)
            callback = _normalize_callback(params.get("callback"))
            # 从请求上下文中提取渠道信息（loop.py 注入了 dingtalk_*/feishu_* 到 params）
            _ctx_callback = _extract_callback(params)
            if not callback:
                callback = _ctx_callback
            elif _ctx_callback:
                # LLM 提供的 callback 可能缺少 sender_nick / staff_id 等字段，用上下文补全
                for _k, _v in _ctx_callback.items():
                    if _k not in callback or not callback[_k]:
                        callback[_k] = _v

            session_id = params.get("session_id")
            delete_after_run = bool(params.get("delete_after_run"))
            if schedule.kind == "at":
                delete_after_run = True

            job_name = (params.get("job_name") or "").strip() or _slugify(message[:40])
            job = await cron_service.add_job(
                name=job_name,
                schedule=schedule,
                payload=CronPayload(
                    message=message,
                    callback=callback,
                    session_id=str(session_id) if session_id else None,
                    auto_approve_plan=bool(params.get("auto_approve_plan", True)),
                    alert_threshold=params.get("alert_threshold") or None,
                    silent_if_no_signal=bool(params.get("silent_if_no_signal", False)),
                ),
                delete_after_run=delete_after_run,
            )
        except Exception as exc:
            return {"success": False, "error": str(exc)}

        return {
            "success": True,
            "data": job.to_public_dict(),
            "message": f"定时任务已创建: {job.name}",
        }

    def _build_schedule(self, params: Dict[str, Any]):
        every_seconds = params.get("every_seconds")
        cron_expr = (params.get("cron_expr") or "").strip()
        at = (params.get("at") or "").strip()
        timezone = (params.get("timezone") or params.get("tz") or "").strip() or None

        mode_count = sum(bool(value) for value in (every_seconds, cron_expr, at))
        if mode_count != 1:
            raise ValueError("add 操作必须且只能提供一种调度方式: every_seconds / cron_expr / at")

        if every_seconds:
            return CronSchedule(kind="every", every_seconds=int(every_seconds))
        if cron_expr:
            return CronSchedule(kind="cron", cron_expr=cron_expr, timezone=timezone)
        return CronSchedule(kind="at", at=at)


def _main() -> None:
    params: Dict[str, Any] = {}
    if not sys.stdin.isatty():
        try:
            raw = sys.stdin.read().strip()
            if raw:
                params = json.loads(raw)
        except Exception:
            params = {}

    parser = argparse.ArgumentParser(description="Run CronSkill directly")
    parser.add_argument("--action", type=str)
    parser.add_argument("--message", type=str)
    parser.add_argument("--job-name", dest="job_name", type=str)
    parser.add_argument("--every-seconds", dest="every_seconds", type=int)
    parser.add_argument("--cron-expr", dest="cron_expr", type=str)
    parser.add_argument("--at", type=str)
    parser.add_argument("--timezone", dest="timezone", type=str)
    parser.add_argument("--job-id", dest="job_id", type=str)
    args = parser.parse_args()
    for key, value in vars(args).items():
        if value is not None:
            params[key] = value

    async def _run() -> None:
        skill = CronSkill()
        result = await skill.execute(params)
        print(json.dumps(result, ensure_ascii=False, default=str))

    asyncio.run(_run())


if __name__ == "__main__":
    _main()
