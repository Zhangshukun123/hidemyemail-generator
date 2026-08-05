from __future__ import annotations

import asyncio
import json
import math
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .inbox import connect_db


SCHEDULE_SETTING_KEY = "scheduled_generation_state_v1"
DEFAULT_BATCH_SIZE = 5
DEFAULT_INTERVAL_SECONDS = 60 * 60
MAX_LOG_ITEMS = 100

GenerateBatch = Callable[[str, int], Awaitable[dict[str, Any]]]
Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)


def _generation_error(result: dict[str, Any]) -> str:
    error = result.get("error") if isinstance(result, dict) else None
    if isinstance(error, dict):
        return str(
            error.get("message")
            or error.get("reason")
            or error.get("code")
            or "生成数量不足"
        )
    return str(error or "生成数量不足")


class ScheduledGenerationManager:
    """Persist and run the fixed hourly Hide My Email inventory schedule."""

    def __init__(
        self,
        *,
        db_file: Path,
        generate_batch: GenerateBatch,
        batch_size: int = DEFAULT_BATCH_SIZE,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        default_enabled: bool = True,
        clock: Clock = utc_now,
    ) -> None:
        self.db_file = Path(db_file)
        self.generate_batch = generate_batch
        self.batch_size = max(1, int(batch_size))
        self.interval_seconds = max(0.01, float(interval_seconds))
        self.default_enabled = bool(default_enabled)
        self.clock = clock
        self._state: dict[str, Any] = {}
        self._state_lock = asyncio.Lock()
        self._run_lock = asyncio.Lock()
        self._wakeup = asyncio.Event()
        self._task: asyncio.Task | None = None

    def _new_state(self, now: datetime) -> dict[str, Any]:
        enabled = self.default_enabled
        next_run = now + timedelta(seconds=self.interval_seconds) if enabled else None
        state: dict[str, Any] = {
            "enabled": enabled,
            "running": False,
            "status": "waiting" if enabled else "paused",
            "batchSize": self.batch_size,
            "intervalSeconds": int(self.interval_seconds),
            "startedAt": _timestamp(now) if enabled else "",
            "nextRunAt": _timestamp(next_run) if next_run else "",
            "lastRunAt": "",
            "lastSuccessAt": "",
            "lastError": "",
            "lastOutcome": "",
            "totalRuns": 0,
            "totalGenerated": 0,
            "logs": [],
        }
        if enabled:
            self._append_log(
                state,
                "info",
                "定时生成已启用；从现在开始计时，第一批将在 1 小时后生成 5 个邮箱",
                now,
            )
        return state

    def _normalize_state(self, value: Any, now: datetime) -> dict[str, Any]:
        if not isinstance(value, dict):
            return self._new_state(now)
        state = self._new_state(now)
        state.update(
            {
                key: value[key]
                for key in (
                    "enabled",
                    "running",
                    "status",
                    "startedAt",
                    "nextRunAt",
                    "lastRunAt",
                    "lastSuccessAt",
                    "lastError",
                    "lastOutcome",
                    "totalRuns",
                    "totalGenerated",
                    "logs",
                )
                if key in value
            }
        )
        state["enabled"] = bool(state.get("enabled"))
        state["batchSize"] = self.batch_size
        state["intervalSeconds"] = int(self.interval_seconds)
        state["totalRuns"] = max(0, int(state.get("totalRuns") or 0))
        state["totalGenerated"] = max(0, int(state.get("totalGenerated") or 0))
        logs = state.get("logs")
        state["logs"] = list(logs[-MAX_LOG_ITEMS:]) if isinstance(logs, list) else []

        if state.get("running"):
            # A prior process may have stopped after iCloud accepted some aliases.
            # Wait a full interval before retrying instead of risking a duplicate burst.
            state["running"] = False
            state["status"] = "waiting" if state["enabled"] else "paused"
            if state["enabled"]:
                state["nextRunAt"] = _timestamp(
                    now + timedelta(seconds=self.interval_seconds)
                )
            self._append_log(
                state,
                "warning",
                "检测到上次生成被服务重启中断；已重新等待完整 1 小时",
                now,
            )
        elif state["enabled"]:
            state["status"] = "waiting"
            if _parse_timestamp(state.get("nextRunAt")) is None:
                state["nextRunAt"] = _timestamp(
                    now + timedelta(seconds=self.interval_seconds)
                )
        else:
            state["status"] = "paused"
            state["nextRunAt"] = ""
        return state

    @staticmethod
    def _append_log(
        state: dict[str, Any], level: str, message: str, at: datetime
    ) -> None:
        logs = state.setdefault("logs", [])
        logs.append(
            {
                "at": _timestamp(at),
                "level": str(level or "info"),
                "message": str(message or "")[:1000],
            }
        )
        del logs[:-MAX_LOG_ITEMS]

    def _load_from_db(self) -> dict[str, Any] | None:
        conn = connect_db(str(self.db_file))
        try:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (SCHEDULE_SETTING_KEY,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        try:
            value = json.loads(str(row["value"] or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _save_to_db(self, state: dict[str, Any]) -> None:
        conn = connect_db(str(self.db_file))
        try:
            conn.execute(
                """
                INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (
                    SCHEDULE_SETTING_KEY,
                    json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    async def _persist(self) -> None:
        await asyncio.to_thread(self._save_to_db, deepcopy(self._state))

    async def initialize(self) -> dict[str, Any]:
        async with self._state_lock:
            if self._state:
                return self.snapshot()
            now = _as_utc(self.clock())
            stored = await asyncio.to_thread(self._load_from_db)
            self._state = self._normalize_state(stored, now)
            await self._persist()
            return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        state = deepcopy(self._state)
        next_run = _parse_timestamp(state.get("nextRunAt"))
        if state.get("enabled") and next_run:
            remaining = max(0.0, (next_run - _as_utc(self.clock())).total_seconds())
            state["secondsUntilNext"] = int(math.ceil(remaining))
        else:
            state["secondsUntilNext"] = None
        return state

    async def configure(self, *, enabled: bool) -> dict[str, Any]:
        await self.initialize()
        async with self._state_lock:
            now = _as_utc(self.clock())
            enabled = bool(enabled)
            was_enabled = bool(self._state.get("enabled"))
            if enabled and not was_enabled:
                self._state.update(
                    enabled=True,
                    status="waiting",
                    startedAt=_timestamp(now),
                    nextRunAt=_timestamp(
                        now + timedelta(seconds=self.interval_seconds)
                    ),
                    lastError="",
                )
                self._append_log(
                    self._state,
                    "info",
                    "定时生成已启用；从现在开始计时，第一批将在 1 小时后生成 5 个邮箱",
                    now,
                )
            elif not enabled and was_enabled:
                self._state.update(
                    enabled=False,
                    status="running" if self._state.get("running") else "paused",
                    nextRunAt="",
                )
                self._append_log(
                    self._state,
                    "warning",
                    "定时生成已暂停；不会继续生成或启动注册",
                    now,
                )
            await self._persist()
            snapshot = self.snapshot()
        self._wakeup.set()
        return snapshot

    async def tick(self) -> bool:
        await self.initialize()
        async with self._run_lock:
            async with self._state_lock:
                now = _as_utc(self.clock())
                next_run = _parse_timestamp(self._state.get("nextRunAt"))
                if (
                    not self._state.get("enabled")
                    or self._state.get("running")
                    or next_run is None
                    or now < next_run
                ):
                    return False
                self._state.update(
                    running=True,
                    status="running",
                    lastRunAt=_timestamp(now),
                    lastError="",
                )
                self._append_log(
                    self._state,
                    "info",
                    f"到达计划时间，开始生成 {self.batch_size} 个邮箱（仅入库，不注册）",
                    now,
                )
                await self._persist()

            generated: list[str] = []
            error_message = ""
            try:
                label = f"Hourly inventory {now.astimezone().strftime('%Y-%m-%d %H:%M')}"
                result = await self.generate_batch(label, self.batch_size)
                generated = [
                    str(item or "").strip().lower()
                    for item in result.get("emails", [])
                    if str(item or "").strip()
                ]
                if not result.get("ok") or len(generated) != self.batch_size:
                    error_message = _generation_error(result)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                error_message = str(error or "定时生成失败")[:500]

            async with self._state_lock:
                finished = _as_utc(self.clock())
                self._state["running"] = False
                self._state["totalRuns"] = int(self._state.get("totalRuns") or 0) + 1
                self._state["totalGenerated"] = int(
                    self._state.get("totalGenerated") or 0
                ) + len(generated)
                self._state["nextRunAt"] = (
                    _timestamp(finished + timedelta(seconds=self.interval_seconds))
                    if self._state.get("enabled")
                    else ""
                )
                if error_message:
                    self._state.update(
                        status="waiting" if self._state.get("enabled") else "paused",
                        lastOutcome="failed",
                        lastError=error_message,
                    )
                    self._append_log(
                        self._state,
                        "error",
                        f"本轮生成失败：已生成 {len(generated)}/{self.batch_size} 个；{error_message}",
                        finished,
                    )
                else:
                    self._state.update(
                        status="waiting" if self._state.get("enabled") else "paused",
                        lastOutcome="success",
                        lastSuccessAt=_timestamp(finished),
                        lastError="",
                    )
                    self._append_log(
                        self._state,
                        "success",
                        f"本轮完成：已生成 {len(generated)}/{self.batch_size} 个邮箱并存入库存；未启动注册",
                        finished,
                    )
                await self._persist()
            return True

    async def _run_loop(self) -> None:
        while True:
            await self.tick()
            snapshot = self.snapshot()
            if snapshot.get("enabled"):
                delay = max(0.05, min(float(snapshot.get("secondsUntilNext") or 1), 60.0))
            else:
                delay = 60.0
            self._wakeup.clear()
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def start(self) -> dict[str, Any]:
        await self.initialize()
        if not self._task or self._task.done():
            self._task = asyncio.create_task(self._run_loop())
        return self.snapshot()

    async def close(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_INTERVAL_SECONDS",
    "SCHEDULE_SETTING_KEY",
    "ScheduledGenerationManager",
]
