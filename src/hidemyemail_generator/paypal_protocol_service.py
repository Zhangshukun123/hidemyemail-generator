"""Lifecycle management for the vendored PayPal protocol workspace."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

import aiohttp


class PayPalProtocolService:
    """Start, monitor, and stop the local PayPal protocol web service."""

    def __init__(
        self,
        *,
        project_dir: Path,
        runtime_dir: Path,
        config_db_file: Path | None = None,
        host: str = "127.0.0.1",
        port: int = 18097,
        python_executable: Path | None = None,
    ) -> None:
        self.project_dir = project_dir.resolve()
        self.runtime_dir = runtime_dir.resolve()
        self.config_db_file = (
            Path(config_db_file).resolve() if config_db_file is not None else None
        )
        self.host = host
        self.port = port
        self.python_executable = Path(python_executable or sys.executable).resolve()
        self.process: subprocess.Popen | None = None
        self.error = ""
        self.ready = False
        self._owns_process = False
        self._log_handle = None
        self._lock = asyncio.Lock()

    @property
    def upstream_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def entrypoint(self) -> Path:
        return self.project_dir / "web.py"

    async def _healthy(self) -> bool:
        timeout = aiohttp.ClientTimeout(total=0.8)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{self.upstream_url}/api/health") as response:
                    return response.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return False

    def _runtime_environment(self) -> dict[str, str]:
        runtime = self.runtime_dir
        environment = {
            **os.environ,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PAYPAL_WEB_METRICS_PATH": str(runtime / "protocol_metrics.json"),
            "PAYPAL_WEB_PAYMENT_AUDIT_PATH": str(runtime / "payment_audit.jsonl"),
            "PAYPAL_WEB_PAYMENT_AUDIT_KEY_PATH": str(runtime / ".payment_audit_hmac_key"),
            "PAYPAL_WEB_FULL_LOG_PATH": str(runtime / "protocol_full.log"),
        }
        if self.config_db_file is not None:
            environment["HME_DB_FILE"] = str(self.config_db_file)
        return environment

    async def ensure_running(self) -> bool:
        async with self._lock:
            if not self.entrypoint.is_file():
                self.ready = False
                self.error = f"PayPal 协议项目不存在：{self.project_dir}"
                return False
            if self.process is not None and self.process.poll() is not None:
                self.process = None
                self.ready = False
                self._owns_process = False
                self._close_log()
            if self.ready and (self.process is None or self.process.poll() is None):
                return True
            if await self._healthy():
                self.ready = True
                self.error = ""
                return True

            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            log_path = self.runtime_dir / "paypal-protocol.log"
            self._log_handle = log_path.open("ab", buffering=0)
            creation_flags = 0
            if os.name == "nt":
                creation_flags = subprocess.CREATE_NO_WINDOW
            try:
                self.process = subprocess.Popen(
                    [
                        str(self.python_executable),
                        str(self.entrypoint),
                        "--host",
                        self.host,
                        "--port",
                        str(self.port),
                    ],
                    cwd=str(self.project_dir),
                    env=self._runtime_environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=self._log_handle,
                    stderr=subprocess.STDOUT,
                    creationflags=creation_flags,
                )
                self._owns_process = True
            except OSError as error:
                self.error = f"PayPal 协议服务启动失败：{error}"
                self._close_log()
                return False

            for _ in range(40):
                if self.process.poll() is not None:
                    self.error = (
                        "PayPal 协议服务启动后立即退出，请查看 "
                        f"{log_path}"
                    )
                    self.ready = False
                    self._close_log()
                    return False
                if await self._healthy():
                    self.ready = True
                    self.error = ""
                    return True
                await asyncio.sleep(0.25)

            self.error = f"PayPal 协议服务启动超时，请查看 {log_path}"
            await self._stop_owned_process()
            return False

    async def snapshot(self, *, ensure: bool = False) -> dict[str, object]:
        if ensure:
            await self.ensure_running()
        elif self.ready and not await self._healthy():
            self.ready = False
            self.error = "PayPal 协议服务未响应"
        return {
            "available": self.entrypoint.is_file(),
            "running": self.ready,
            "error": self.error,
            "url": "/paypal-pay/",
            "upstream": self.upstream_url,
        }

    async def create_job(
        self, payload: dict[str, object], *, device_id: str
    ) -> tuple[int, dict[str, object]]:
        """Create a device-owned protocol job through the loopback API."""
        if not await self.ensure_running():
            return 503, {"error": self.error or "PayPal 协议服务暂不可用"}
        timeout = aiohttp.ClientTimeout(total=15)
        headers = {
            "Cookie": f"paypal_web_device_id={device_id}",
            "X-Internal-Auto-Channel": "1",
        }
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.upstream_url}/api/jobs", json=payload, headers=headers
                ) as response:
                    try:
                        data = await response.json()
                    except (aiohttp.ContentTypeError, ValueError):
                        data = {"error": (await response.text()).strip()}
                    return response.status, data if isinstance(data, dict) else {}
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
            self.ready = False
            self.error = f"PayPal 协议服务连接失败：{error}"
            return 502, {"error": self.error}

    async def close(self) -> None:
        async with self._lock:
            await self._stop_owned_process()
            self.ready = False

    async def _stop_owned_process(self) -> None:
        process = self.process
        if process is not None and self._owns_process and process.poll() is None:
            process.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                with suppress(Exception):
                    await asyncio.to_thread(process.wait)
        self.process = None
        self._owns_process = False
        self._close_log()

    def _close_log(self) -> None:
        if self._log_handle is not None:
            with suppress(OSError):
                self._log_handle.close()
            self._log_handle = None
