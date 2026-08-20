"""Persistent MVP components for the quick-flow run list."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from aiohttp import web

from .inbox import connect_db


QUICK_FLOW_HISTORY_PREFIX = "quick_flow_history:"
QUICK_FLOW_SCHEMA_VERSION = 1
MAX_QUICK_FLOW_BYTES = 2 * 1024 * 1024
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
TokenValidator = Callable[[web.Request], bool]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_id(value: Any) -> str:
    candidate = str(value or "").strip()
    if not _RUN_ID_PATTERN.fullmatch(candidate):
        raise ValueError("流水线记录 ID 无效")
    return candidate


def _json_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        encoded = json.dumps(
            dict(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("流水线记录包含无法保存的数据") from error
    if len(encoded) > MAX_QUICK_FLOW_BYTES:
        raise ValueError("流水线记录过大，无法保存")
    decoded = json.loads(encoded.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("流水线记录格式无效")
    return decoded


class QuickFlowHistoryRepository:
    """Repository: store each run independently in the existing SQLite file."""

    def __init__(
        self,
        db_file: str | Path,
        *,
        key_prefix: str = QUICK_FLOW_HISTORY_PREFIX,
    ) -> None:
        self.db_file = Path(db_file)
        self.key_prefix = str(key_prefix)

    def save(self, run_id: str, envelope: Mapping[str, Any]) -> None:
        payload = json.dumps(
            dict(envelope), ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        connection = connect_db(str(self.db_file))
        try:
            connection.execute(
                """
                INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (f"{self.key_prefix}{run_id}", payload),
            )
            connection.commit()
        finally:
            connection.close()

    def records(self) -> list[dict[str, Any]]:
        connection = connect_db(str(self.db_file))
        try:
            rows = connection.execute(
                "SELECT value FROM settings WHERE key GLOB ?",
                (f"{self.key_prefix}*",),
            ).fetchall()
        finally:
            connection.close()
        records: list[dict[str, Any]] = []
        for row in rows:
            try:
                decoded = json.loads(str(row["value"]))
            except (TypeError, ValueError):
                continue
            if isinstance(decoded, dict):
                records.append(decoded)
        return records

    def delete(self, run_id: str) -> bool:
        connection = connect_db(str(self.db_file))
        try:
            cursor = connection.execute(
                "DELETE FROM settings WHERE key = ?",
                (f"{self.key_prefix}{run_id}",),
            )
            connection.commit()
            return cursor.rowcount > 0
        finally:
            connection.close()


class QuickFlowHistoryModel:
    """Model: validate snapshots and reconcile runs left active by a restart."""

    def __init__(
        self,
        repository: QuickFlowHistoryRepository,
        *,
        server_instance_id: str | None = None,
    ) -> None:
        self.repository = repository
        self.server_instance_id = server_instance_id or str(uuid4())

    def save(self, flow: Mapping[str, Any]) -> dict[str, Any]:
        snapshot = _json_snapshot(flow)
        run_id = _run_id(snapshot.get("runId"))
        snapshot["runId"] = run_id
        saved_at = _utc_now()
        self.repository.save(
            run_id,
            {
                "schemaVersion": QUICK_FLOW_SCHEMA_VERSION,
                "serverInstanceId": self.server_instance_id,
                "savedAt": saved_at,
                "flow": snapshot,
            },
        )
        return snapshot

    def _recover_interrupted(
        self, envelope: Mapping[str, Any], flow: dict[str, Any]
    ) -> dict[str, Any]:
        if str(flow.get("status") or "") != "running":
            return flow
        if str(envelope.get("serverInstanceId") or "") == self.server_instance_id:
            return flow
        recovered_at = _utc_now()
        logs = list(flow.get("logs") or [])
        logs.append(
            {
                "at": recovered_at,
                "status": "error",
                "message": "服务器重启后恢复历史记录；未完成流程已标记为中断",
            }
        )
        flow.update(
            status="failed",
            interrupted=True,
            finishedAt=recovered_at,
            currentAction="服务器重启导致前端流水线中断",
            message="服务器重启前流程未完成；记录已永久保留",
            logs=logs[-120:],
        )
        return self.save(flow)

    def list(self) -> list[dict[str, Any]]:
        flows: list[dict[str, Any]] = []
        for envelope in self.repository.records():
            raw_flow = envelope.get("flow")
            if not isinstance(raw_flow, Mapping):
                continue
            try:
                flow = _json_snapshot(raw_flow)
                _run_id(flow.get("runId"))
                flows.append(self._recover_interrupted(envelope, flow))
            except ValueError:
                continue
        return sorted(
            flows,
            key=lambda item: (
                str(item.get("startedAt") or ""),
                str(item.get("runId") or ""),
            ),
        )

    def delete(self, run_id: str) -> bool:
        return self.repository.delete(_run_id(run_id))


class QuickFlowHistoryView:
    """View: create stable JSON responses for the browser Presenter."""

    @staticmethod
    def history(items: list[dict[str, Any]]) -> dict[str, Any]:
        return {"ok": True, "items": items, "count": len(items)}

    @staticmethod
    def saved(flow: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "flow": flow}

    @staticmethod
    def deleted(run_id: str, deleted: bool) -> dict[str, Any]:
        return {"ok": True, "runId": run_id, "deleted": deleted}


class QuickFlowHistoryPresenter:
    """Presenter: coordinate the persistence Model and response View."""

    def __init__(self, model: QuickFlowHistoryModel) -> None:
        self.model = model

    async def history(self) -> dict[str, Any]:
        items = await asyncio.to_thread(self.model.list)
        return QuickFlowHistoryView.history(items)

    async def save(self, flow: Mapping[str, Any]) -> dict[str, Any]:
        saved = await asyncio.to_thread(self.model.save, flow)
        return QuickFlowHistoryView.saved(saved)

    async def delete(self, run_id: str) -> dict[str, Any]:
        deleted = await asyncio.to_thread(self.model.delete, run_id)
        return QuickFlowHistoryView.deleted(run_id, deleted)


class QuickFlowHistoryRouteAdapter:
    """HTTP adapter for authenticated quick-flow history mutations."""

    def __init__(
        self,
        presenter: QuickFlowHistoryPresenter,
        *,
        token_validator: TokenValidator,
    ) -> None:
        self.presenter = presenter
        self.token_validator = token_validator

    def _forbidden(self, request: web.Request) -> web.Response | None:
        if self.token_validator(request):
            return None
        return web.json_response({"ok": False, "error": "本地请求令牌无效"}, status=403)

    @staticmethod
    async def _payload(request: web.Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except Exception as error:
            raise ValueError("请求格式无效") from error
        if not isinstance(payload, dict):
            raise ValueError("请求格式无效")
        return payload

    async def history(self, _request: web.Request) -> web.Response:
        result = await self.presenter.history()
        return web.json_response(result, headers={"Cache-Control": "no-store"})

    async def save(self, request: web.Request) -> web.Response:
        forbidden = self._forbidden(request)
        if forbidden is not None:
            return forbidden
        try:
            payload = await self._payload(request)
            flow = payload.get("flow")
            if not isinstance(flow, Mapping):
                raise ValueError("流水线记录格式无效")
            result = await self.presenter.save(flow)
        except ValueError as error:
            return web.json_response({"ok": False, "error": str(error)}, status=400)
        return web.json_response(result, headers={"Cache-Control": "no-store"})

    async def delete(self, request: web.Request) -> web.Response:
        forbidden = self._forbidden(request)
        if forbidden is not None:
            return forbidden
        try:
            payload = await self._payload(request)
            run_id = _run_id(payload.get("runId"))
            result = await self.presenter.delete(run_id)
        except ValueError as error:
            return web.json_response({"ok": False, "error": str(error)}, status=400)
        return web.json_response(result, headers={"Cache-Control": "no-store"})


def setup_quick_flow_history_routes(
    app: web.Application,
    *,
    token_validator: TokenValidator,
    presenter: QuickFlowHistoryPresenter | None = None,
) -> QuickFlowHistoryPresenter:
    """Compose MVP collaborators and register the quick-flow history API."""

    selected = presenter or QuickFlowHistoryPresenter(
        QuickFlowHistoryModel(QuickFlowHistoryRepository(app["db_file"]))
    )
    adapter = QuickFlowHistoryRouteAdapter(selected, token_validator=token_validator)
    app.router.add_get("/api/quick-flow/history", adapter.history)
    app.router.add_post("/api/quick-flow/history", adapter.save)
    app.router.add_post("/api/quick-flow/history/delete", adapter.delete)
    return selected


__all__ = [
    "QUICK_FLOW_HISTORY_PREFIX",
    "QuickFlowHistoryModel",
    "QuickFlowHistoryPresenter",
    "QuickFlowHistoryRepository",
    "QuickFlowHistoryRouteAdapter",
    "QuickFlowHistoryView",
    "setup_quick_flow_history_routes",
]
