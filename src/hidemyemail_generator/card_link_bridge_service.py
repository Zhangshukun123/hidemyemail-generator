from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit


WORKER_MESSAGE_PREFIX = "HME_CARD_LINK_WORKER:"
WORKER_PROTOCOL_VERSION = 1
MAX_PROTOCOL_LINE_BYTES = 1024 * 1024
MAX_PROGRESS_LOGS = 200
SHARED_CARD_LINK_METHODS = frozenset(
    {"de_oaics_paypal", "paypal_us", "paypal_gb"}
)
_SENSITIVE_ENV_KEYS = (
    "HME_OPENAI_ACCESS_TOKEN",
    "HME_CARD_LINK_CREATE_PROXY_URL",
    "HME_CARD_LINK_PROMO_PROXY_URL",
)


@dataclass(frozen=True, slots=True)
class CardLinkBridgeCommand:
    """Model for one isolated card-link request sent to the shared worker."""

    method: str
    access_token: str = field(repr=False)
    country: str = "US"
    currency: str = "USD"
    locale: str = "en-US"
    account_email: str = field(default="", repr=False)
    create_proxy_url: str = field(default="", repr=False)
    promotion_proxy_url: str = field(default="", repr=False)
    target_amount: str = ""
    sentinel_so_enabled: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "method": str(self.method or "").strip(),
            "access_token": str(self.access_token or "").strip(),
            "country": str(self.country or "").strip().upper(),
            "currency": str(self.currency or "").strip().upper(),
            "locale": str(self.locale or "").strip(),
            "account_email": str(self.account_email or "").strip(),
            "create_proxy_url": str(self.create_proxy_url or "").strip(),
            "promotion_proxy_url": str(self.promotion_proxy_url or "").strip(),
            "target_amount": str(self.target_amount or "").strip(),
            "sentinel_so_enabled": bool(self.sentinel_so_enabled),
        }


@dataclass(frozen=True, slots=True)
class CardLinkBridgeResult:
    """Model returned by the worker after one complete request."""

    event: dict[str, Any]
    logs: tuple[str, ...] = ()


class CardLinkBridgeServiceError(RuntimeError):
    def __init__(
        self,
        detail: str,
        *,
        logs: list[str] | tuple[str, ...] | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(detail)
        self.logs = list(logs or ())
        self.retryable = retryable


class _WorkerProtocolError(RuntimeError):
    pass


class _WorkerRemoteError(RuntimeError):
    def __init__(
        self,
        detail: str,
        *,
        logs: list[str],
        retryable: bool | None,
    ) -> None:
        super().__init__(detail)
        self.logs = list(logs)
        self.retryable = retryable


def _redact(value: object, secrets: tuple[str, ...]) -> str:
    text = str(value or "").strip()
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def _payload_secrets(payload: dict[str, str]) -> tuple[str, ...]:
    candidates = [
        payload.get("access_token", ""),
        payload.get("account_email", ""),
    ]
    for key in ("create_proxy_url", "promotion_proxy_url"):
        proxy_url = payload.get(key, "")
        candidates.append(proxy_url)
        if not proxy_url:
            continue
        try:
            parsed = urlsplit(proxy_url)
        except ValueError:
            continue
        for component in (parsed.username, parsed.password):
            raw = str(component or "").strip()
            if raw:
                candidates.extend((raw, unquote(raw)))
    return tuple(
        sorted(
            {candidate for candidate in candidates if candidate},
            key=len,
            reverse=True,
        )
    )


class CardLinkBridgeView(Protocol):
    @property
    def worker_pid(self) -> int | None: ...

    @property
    def spawn_count(self) -> int: ...

    async def exchange(
        self,
        request_id: str,
        payload: dict[str, str],
        *,
        timeout_seconds: float,
        on_log: Callable[[str], None] | None = None,
    ) -> CardLinkBridgeResult: ...

    async def abort(self) -> None: ...

    async def close(self) -> None: ...


class CardLinkBridgeProcessView:
    """View/adapter that owns one hidden worker and its anonymous pipes."""

    def __init__(
        self,
        *,
        python_executable: Path,
        bridge_file: Path,
        working_directory: Path,
        startup_timeout_seconds: float = 15.0,
        shutdown_timeout_seconds: float = 2.0,
    ) -> None:
        self.python_executable = Path(python_executable).resolve()
        self.bridge_file = Path(bridge_file).resolve()
        self.working_directory = Path(working_directory).resolve()
        self.startup_timeout_seconds = max(0.1, startup_timeout_seconds)
        self.shutdown_timeout_seconds = max(0.1, shutdown_timeout_seconds)
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._spawn_count = 0

    @property
    def worker_pid(self) -> int | None:
        process = self._process
        if process is None or process.returncode is not None:
            return None
        return process.pid

    @property
    def spawn_count(self) -> int:
        return self._spawn_count

    async def _discard_dead_process(self) -> None:
        process = self._process
        if process is None or process.returncode is None:
            return
        await process.wait()
        self._process = None
        await self._stop_stderr_task()

    async def _stop_stderr_task(self) -> None:
        task = self._stderr_task
        self._stderr_task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    @staticmethod
    async def _drain_stderr(reader: asyncio.StreamReader | None) -> None:
        if reader is None:
            return
        try:
            while await reader.read(64 * 1024):
                pass
        except (asyncio.CancelledError, Exception):
            pass

    async def _read_protocol_message(self) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdout is None:
            raise _WorkerProtocolError("worker stdout is unavailable")
        while True:
            try:
                raw_line = await process.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError) as error:
                raise _WorkerProtocolError(
                    "worker protocol line is too long"
                ) from error
            if not raw_line:
                raise _WorkerProtocolError("worker closed its output pipe")
            if len(raw_line) > MAX_PROTOCOL_LINE_BYTES:
                raise _WorkerProtocolError("worker protocol line is too long")
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line.startswith(WORKER_MESSAGE_PREFIX):
                continue
            try:
                message = json.loads(line[len(WORKER_MESSAGE_PREFIX) :])
            except json.JSONDecodeError as error:
                raise _WorkerProtocolError("worker returned malformed JSON") from error
            if not isinstance(message, dict):
                raise _WorkerProtocolError("worker returned a non-object message")
            if message.get("v") != WORKER_PROTOCOL_VERSION:
                raise _WorkerProtocolError("worker protocol version mismatch")
            return message

    async def start(self) -> None:
        await self._discard_dead_process()
        if self.worker_pid is not None:
            return
        env = os.environ.copy()
        for key in _SENSITIVE_ENV_KEYS:
            env.pop(key, None)
        env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self._process = await asyncio.create_subprocess_exec(
            str(self.python_executable),
            str(self.bridge_file),
            "--worker",
            cwd=str(self.working_directory),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
            limit=MAX_PROTOCOL_LINE_BYTES,
        )
        self._spawn_count += 1
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(self._process.stderr)
        )
        try:
            ready = await asyncio.wait_for(
                self._read_protocol_message(),
                timeout=self.startup_timeout_seconds,
            )
            if ready.get("type") != "ready":
                raise _WorkerProtocolError("worker did not send ready")
        except BaseException:
            await self.abort()
            raise

    async def exchange(
        self,
        request_id: str,
        payload: dict[str, str],
        *,
        timeout_seconds: float,
        on_log: Callable[[str], None] | None = None,
    ) -> CardLinkBridgeResult:
        await self.start()
        process = self._process
        if process is None or process.stdin is None:
            raise _WorkerProtocolError("worker stdin is unavailable")
        request = {
            "v": WORKER_PROTOCOL_VERSION,
            "id": request_id,
            "op": "generate",
            "payload": payload,
        }
        encoded = (
            json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        if len(encoded) > MAX_PROTOCOL_LINE_BYTES:
            raise CardLinkBridgeServiceError(
                "提链请求数据过长",
                retryable=False,
            )
        secrets = _payload_secrets(payload)
        logs: list[str] = []
        async with asyncio.timeout(max(0.1, timeout_seconds)):
            process.stdin.write(encoded)
            await process.stdin.drain()
            while True:
                message = await self._read_protocol_message()
                if message.get("id") != request_id:
                    raise _WorkerProtocolError("worker response id mismatch")
                message_type = str(message.get("type") or "")
                if message_type == "log":
                    detail = _redact(message.get("message"), secrets)
                    if detail and len(logs) < MAX_PROGRESS_LOGS:
                        logs.append(detail[:500])
                        if on_log is not None:
                            try:
                                on_log(detail[:500])
                            except Exception:
                                pass
                    continue
                if message_type == "error":
                    detail = _redact(message.get("detail"), secrets)
                    retryable = message.get("retryable")
                    raise _WorkerRemoteError(
                        (detail or "直卡支付链接生成失败")[:1000],
                        logs=logs,
                        retryable=retryable if isinstance(retryable, bool) else None,
                    )
                if message_type != "result":
                    raise _WorkerProtocolError("worker response type is invalid")
                event = message.get("event")
                if not isinstance(event, dict):
                    raise _WorkerProtocolError("worker result event is invalid")
                return CardLinkBridgeResult(dict(event), tuple(logs))

    async def _terminate(self, *, graceful: bool) -> None:
        process = self._process
        if process is None:
            await self._stop_stderr_task()
            return
        if graceful and process.returncode is None and process.stdin is not None:
            shutdown = {
                "v": WORKER_PROTOCOL_VERSION,
                "id": uuid.uuid4().hex,
                "op": "shutdown",
            }
            try:
                async with asyncio.timeout(self.shutdown_timeout_seconds):
                    process.stdin.write(
                        (json.dumps(shutdown, separators=(",", ":")) + "\n").encode(
                            "utf-8"
                        )
                    )
                    await process.stdin.drain()
                    await process.wait()
            except (
                BrokenPipeError,
                ConnectionError,
                RuntimeError,
                asyncio.TimeoutError,
            ):
                pass
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
        self._process = None
        await self._stop_stderr_task()

    async def abort(self) -> None:
        await self._terminate(graceful=False)

    async def close(self) -> None:
        await self._terminate(graceful=True)


class SharedCardLinkBridgePresenter:
    """Presenter that serializes requests through one reusable process view."""

    def __init__(
        self,
        view: CardLinkBridgeView,
        *,
        request_timeout_seconds: float = 240.0,
    ) -> None:
        self._view = view
        self.request_timeout_seconds = max(0.1, request_timeout_seconds)
        self._lock = asyncio.Lock()
        self._closed = False

    @classmethod
    def from_paths(
        cls,
        *,
        python_executable: Path | None = None,
        bridge_file: Path,
        working_directory: Path,
        request_timeout_seconds: float = 240.0,
    ) -> SharedCardLinkBridgePresenter:
        view = CardLinkBridgeProcessView(
            python_executable=python_executable or Path(sys.executable),
            bridge_file=bridge_file,
            working_directory=working_directory,
        )
        return cls(view, request_timeout_seconds=request_timeout_seconds)

    @property
    def worker_pid(self) -> int | None:
        return self._view.worker_pid

    @property
    def spawn_count(self) -> int:
        return self._view.spawn_count

    async def generate(
        self,
        command: CardLinkBridgeCommand,
        *,
        on_log: Callable[[str], None] | None = None,
    ) -> CardLinkBridgeResult:
        if self._closed:
            raise CardLinkBridgeServiceError(
                "共享提链服务已关闭",
                retryable=False,
            )
        payload = command.to_payload()
        if payload["method"] not in SHARED_CARD_LINK_METHODS:
            raise CardLinkBridgeServiceError(
                f"共享提链服务不支持方法：{payload['method'] or 'empty'}",
                retryable=False,
            )
        if not payload["access_token"]:
            raise CardLinkBridgeServiceError("Access Token 为空", retryable=False)
        request_id = uuid.uuid4().hex
        async with self._lock:
            if self._closed:
                raise CardLinkBridgeServiceError(
                    "共享提链服务已关闭",
                    retryable=False,
                )
            try:
                return await self._view.exchange(
                    request_id,
                    payload,
                    timeout_seconds=self.request_timeout_seconds,
                    on_log=on_log,
                )
            except _WorkerRemoteError as error:
                raise CardLinkBridgeServiceError(
                    str(error),
                    logs=error.logs,
                    retryable=error.retryable,
                ) from error
            except asyncio.TimeoutError as error:
                await self._view.abort()
                raise CardLinkBridgeServiceError(
                    "生成直卡支付链接超时，请稍后重试",
                    retryable=False,
                ) from error
            except asyncio.CancelledError:
                await asyncio.shield(self._view.abort())
                raise
            except CardLinkBridgeServiceError:
                raise
            except Exception as error:
                await self._view.abort()
                raise CardLinkBridgeServiceError(
                    "共享提链服务连接中断，请重试",
                    retryable=True,
                ) from error

    async def close(self) -> None:
        if self._closed:
            return
        async with self._lock:
            if self._closed:
                return
            await self._view.close()
            self._closed = True
