"""
Application cron service.

Uses a local JSON job store plus an in-process asyncio timer, similar to nanobot's
CronService design, without depending on system crontab or toolbox.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from loguru import logger


CronJobCallback = Callable[["CronJob"], Awaitable[Optional[str]]]


def _now_ts() -> float:
    return time.time()


def _to_iso(ts: Optional[float]) -> Optional[str]:
    if not ts:
        return None
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


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


def _expand_cron_field(field: str, lower: int, upper: int) -> List[int]:
    values = set()
    for raw_part in field.split(","):
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
        # Validate expression.
        _cron_matches(datetime.now(), schedule.cron_expr)


def _deserialize_job(raw: Dict[str, Any]) -> CronJob:
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


class CronService:
    def __init__(
        self,
        store_path: Path,
        on_job: Optional[CronJobCallback] = None,
    ) -> None:
        self._store_path = store_path
        self._on_job = on_job
        self._jobs: List[CronJob] = []
        self._lock = asyncio.Lock()
        self._timer_task: Optional[asyncio.Task] = None
        self._poll_task: Optional[asyncio.Task] = None  # 常驻文件轮询任务
        self._running = False

    async def start(self) -> None:
        async with self._lock:
            self._load_jobs_locked()
            self._recompute_jobs_locked()
            self._save_jobs_locked()
            self._running = True
            self._arm_timer_locked()
        self._start_file_poll()
        logger.info("[CronService] Started with {} jobs", len(self._jobs))

    async def stop(self) -> None:
        async with self._lock:
            self._running = False
            if self._timer_task:
                self._timer_task.cancel()
                self._timer_task = None
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None
        logger.info("[CronService] Stopped")

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
            self._load_jobs_locked()
            self._jobs = [existing for existing in self._jobs if existing.name != name]
            self._jobs.append(job)
            self._save_jobs_locked()
            self._arm_timer_locked()
        logger.info("[CronService] Added job '{}' ({})", job.name, job.id)
        return job

    async def list_jobs(self) -> List[CronJob]:
        async with self._lock:
            self._load_jobs_locked()
            return [_deserialize_job(asdict(job)) for job in self._jobs]

    async def remove_job(self, *, job_id: Optional[str] = None, name: Optional[str] = None) -> bool:
        if not job_id and not name:
            raise ValueError("job_id or name is required")

        async with self._lock:
            self._load_jobs_locked()
            before = len(self._jobs)
            self._jobs = [
                job for job in self._jobs
                if not ((job_id and job.id == job_id) or (name and job.name == name))
            ]
            removed = len(self._jobs) != before
            if removed:
                self._save_jobs_locked()
                self._arm_timer_locked()
        logger.info("[CronService] Remove job id={} name={} removed={}", job_id, name, removed)
        return removed

    def _load_jobs_locked(self) -> None:
        if not self._store_path.exists():
            self._jobs = []
            return
        try:
            payload = json.loads(self._store_path.read_text(encoding="utf-8"))
            self._jobs = [_deserialize_job(item) for item in payload.get("jobs", [])]
        except Exception as exc:
            logger.warning("[CronService] Failed to load jobs: {}", exc)
            self._jobs = []

    def _save_jobs_locked(self) -> None:
        payload = {
            "version": 1,
            "jobs": [asdict(job) for job in self._jobs],
        }
        _atomic_write_json(self._store_path, payload)

    def _recompute_jobs_locked(self) -> None:
        now_ts = _now_ts()
        for job in self._jobs:
            if job.enabled:
                job.state.next_run_at_ts = _compute_next_run(job.schedule, now_ts)

    def _next_wake_ts_locked(self) -> Optional[float]:
        future_runs = [
            job.state.next_run_at_ts
            for job in self._jobs
            if job.enabled and job.state.next_run_at_ts
        ]
        return min(future_runs) if future_runs else None

    def _start_file_poll(self) -> None:
        """启动常驻文件轮询任务，每30秒检测外部新增 job 并重新 arm timer。"""
        if self._poll_task and not self._poll_task.done():
            return

        async def _file_poll_loop() -> None:
            try:
                while True:
                    await asyncio.sleep(30)
                    if not self._running:
                        return
                    async with self._lock:
                        old_ids = {j.id for j in self._jobs}
                        self._load_jobs_locked()
                        new_ids = {j.id for j in self._jobs}
                        if new_ids != old_ids:
                            logger.info("[CronService] File poll detected job changes, re-arming timer")
                            self._arm_timer_locked()
            except asyncio.CancelledError:
                return

        self._poll_task = asyncio.create_task(_file_poll_loop(), name="cron_service_file_poll")

    def _arm_timer_locked(self) -> None:
        if self._timer_task:
            self._timer_task.cancel()
            self._timer_task = None

        if not self._running:
            return

        next_wake = self._next_wake_ts_locked()
        # 当无 job 时，用轮询 tick 检测外部写入的新 job（skill 直接写文件的场景）
        if not next_wake:
            poll_interval = 10.0  # 10 秒轮询一次

            async def _poll_tick() -> None:
                try:
                    await asyncio.sleep(poll_interval)
                    async with self._lock:
                        self._load_jobs_locked()
                        self._arm_timer_locked()
                except asyncio.CancelledError:
                    return

            self._timer_task = asyncio.create_task(_poll_tick(), name="cron_service_poll")
            return

        delay = max(0.0, next_wake - _now_ts())

        async def _tick() -> None:
            try:
                await asyncio.sleep(delay)
                await self._process_due_jobs()
            except asyncio.CancelledError:
                return

        self._timer_task = asyncio.create_task(_tick(), name="cron_service_tick")

    async def _process_due_jobs(self) -> None:
        async with self._lock:
            self._load_jobs_locked()
            now_ts = _now_ts()
            due_ids = [
                job.id
                for job in self._jobs
                if job.enabled and job.state.next_run_at_ts and job.state.next_run_at_ts <= now_ts
            ]

        for job_id in due_ids:
            await self._execute_job(job_id)

        async with self._lock:
            self._load_jobs_locked()
            self._arm_timer_locked()

    async def _execute_job(self, job_id: str) -> None:
        async with self._lock:
            self._load_jobs_locked()
            job = next((item for item in self._jobs if item.id == job_id), None)
            if not job or not job.enabled:
                return
            now_ts = _now_ts()
            if not job.state.next_run_at_ts or job.state.next_run_at_ts > now_ts:
                return
            snapshot = _deserialize_job(asdict(job))

        status = "ok"
        error = None
        try:
            logger.info("[CronService] Executing job '{}' ({})", snapshot.name, snapshot.id)
            if self._on_job:
                await self._on_job(snapshot)
        except Exception as exc:
            status = "error"
            error = str(exc)
            logger.error("[CronService] Job '{}' failed: {}", snapshot.name, exc)

        async with self._lock:
            self._load_jobs_locked()
            current = next((item for item in self._jobs if item.id == job_id), None)
            if not current:
                return
            current.state.last_run_at_ts = _now_ts()
            current.state.last_status = status
            current.state.last_error = error
            current.updated_at_ts = _now_ts()
            if current.delete_after_run and status == "ok":
                self._jobs = [item for item in self._jobs if item.id != job_id]
            else:
                current.state.next_run_at_ts = _compute_next_run(current.schedule, _now_ts())
            self._save_jobs_locked()


_cron_service: Optional[CronService] = None


async def init_cron_service(
    store_path: Path,
    on_job: Optional[CronJobCallback] = None,
) -> CronService:
    global _cron_service
    if _cron_service is None:
        _cron_service = CronService(store_path=store_path, on_job=on_job)
        await _cron_service.start()
    elif on_job is not None:
        _cron_service._on_job = on_job
    return _cron_service


def get_cron_service() -> CronService:
    if _cron_service is None:
        raise RuntimeError("cron service not initialized")
    return _cron_service


async def shutdown_cron_service() -> None:
    global _cron_service
    if _cron_service is not None:
        await _cron_service.stop()
        _cron_service = None
