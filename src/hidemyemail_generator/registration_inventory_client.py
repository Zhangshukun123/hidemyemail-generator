from __future__ import annotations

import asyncio
import json
import socket
from typing import Any
from urllib.parse import urlsplit

import aiohttp


INVENTORY_STATUS_PATH = "/api/integrations/registration-inventory/status"
INVENTORY_LOGIN_PATH = "/api/integrations/registration-inventory/login"
INVENTORY_LEASE_PATH = "/api/integrations/registration-inventory/lease"
INVENTORY_RESULT_PATH = "/api/integrations/registration-inventory/result"
INVENTORY_SYNC_PATH = "/api/integrations/registration-inventory/sync"
INVENTORY_INTEGRATION_PATHS = {
    INVENTORY_STATUS_PATH,
    INVENTORY_LEASE_PATH,
    INVENTORY_RESULT_PATH,
    INVENTORY_SYNC_PATH,
}
MAX_SYNC_BATCH_RECORDS = 50
MAX_SYNC_BATCH_BYTES = 768 * 1024
DEFAULT_CONNECT_RETRY_DELAYS = (0.5, 1.5)


def _is_loopback_host(hostname: str | None) -> bool:
    return str(hostname or "").casefold() in {"localhost", "127.0.0.1", "::1"}


def _normalize_service_url(service_url: str) -> str:
    value = str(service_url or "").strip()
    if not value:
        return ""
    if value.startswith("//"):
        value = f"https:{value}"
    elif "://" not in value:
        value = f"https://{value}"
    parsed = urlsplit(value)
    if (
        parsed.scheme.casefold() == "http"
        and parsed.hostname
        and not _is_loopback_host(parsed.hostname)
    ):
        value = parsed._replace(scheme="https").geturl()
    return value.rstrip("/")


def _service_url_error(service_url: str) -> str:
    if not service_url:
        return ""
    parsed = urlsplit(service_url)
    if parsed.username or parsed.password:
        return "远程库存地址不能包含用户名或密码"
    if parsed.query or parsed.fragment:
        return "远程库存地址不能包含查询参数或片段"
    if parsed.scheme == "https" and parsed.hostname:
        return ""
    if parsed.scheme == "http" and _is_loopback_host(parsed.hostname):
        return ""
    return "远程库存服务必须使用 HTTPS；HTTP 仅允许本机回环地址"


class RemoteRegistrationInventoryClient:
    def __init__(
        self,
        *,
        service_url: str,
        token: str = "",
        username: str = "",
        password: str = "",
        client_id: str = "",
        timeout_seconds: float = 20,
        connect_retry_delays: tuple[float, ...] = DEFAULT_CONNECT_RETRY_DELAYS,
    ) -> None:
        self.service_url = _normalize_service_url(service_url)
        self.token = str(token or "").strip()
        self.username = str(username or "").strip()
        self.password = str(password or "")
        self._access_token = ""
        self.client_id = str(client_id or socket.gethostname() or "local-client")[:200]
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.connect_retry_delays = tuple(
            max(0.0, float(delay)) for delay in connect_retry_delays
        )
        self.configuration_error = _service_url_error(self.service_url)
        self._leases_by_email: dict[str, dict[str, Any]] = {}

    @property
    def configured(self) -> bool:
        has_login = bool(self.username and self.password)
        return bool(self.service_url and (has_login or self.token))

    async def _send_once(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                method,
                f"{self.service_url}{path}",
                json=payload,
                headers=headers,
            ) as response:
                try:
                    data = await response.json()
                except (aiohttp.ContentTypeError, ValueError):
                    detail = (await response.text())[:300]
                    raise RuntimeError(
                        f"Remote inventory returned an invalid response "
                        f"(HTTP {response.status}): {detail}"
                    )
                return response.status, data if isinstance(data, dict) else {}

    @staticmethod
    def _is_retryable_connect_error(error: BaseException) -> bool:
        if isinstance(error, aiohttp.ClientConnectorError):
            return True
        connection_timeout = getattr(aiohttp, "ConnectionTimeoutError", None)
        return bool(
            isinstance(connection_timeout, type)
            and isinstance(error, connection_timeout)
        )

    def _connection_error_message(
        self, error: BaseException, *, attempts: int, retryable: bool
    ) -> str:
        parsed = urlsplit(self.service_url)
        endpoint = parsed.hostname or self.service_url
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if retryable:
            return (
                f"无法连接远端 HTTPS 库存接口（已尝试 {attempts} 次）："
                f"{endpoint}:{port}；{error}"
            )
        return f"远端 HTTPS 库存请求失败：{endpoint}:{port}；{error}"

    async def _send(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        attempts = len(self.connect_retry_delays) + 1
        for attempt_index in range(attempts):
            try:
                return await self._send_once(
                    method, path, payload=payload, headers=headers
                )
            except (aiohttp.ClientError, TimeoutError) as error:
                retryable = self._is_retryable_connect_error(error)
                if not retryable or attempt_index >= attempts - 1:
                    raise RuntimeError(
                        self._connection_error_message(
                            error,
                            attempts=attempt_index + 1,
                            retryable=retryable,
                        )
                    ) from error
                delay = self.connect_retry_delays[attempt_index]
                if delay:
                    await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def _login(self) -> None:
        status, data = await self._send(
            "POST",
            INVENTORY_LOGIN_PATH,
            payload={"username": self.username, "password": self.password},
        )
        access_token = str(data.get("accessToken") or "").strip()
        if status != 200 or not data.get("ok") or not access_token:
            raise RuntimeError(
                str(data.get("error") or f"Remote inventory login failed (HTTP {status})")
            )
        self._access_token = access_token

    async def _request(
        self, method: str, path: str, *, payload: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        if self.configuration_error:
            raise RuntimeError(self.configuration_error)
        if not self.configured:
            raise RuntimeError("远端邮箱库存服务未配置")
        use_login = bool(self.username and self.password)
        if use_login and not self._access_token:
            await self._login()
        headers = (
            {"Authorization": f"Bearer {self._access_token}"}
            if use_login
            else {"X-HME-Import-Token": self.token}
        )
        status, data = await self._send(
            method, path, payload=payload, headers=headers
        )
        if use_login and status == 401:
            self._access_token = ""
            await self._login()
            status, data = await self._send(
                method,
                path,
                payload=payload,
                headers={"Authorization": f"Bearer {self._access_token}"},
            )
        return status, data

    async def acquire_email(self, label: str) -> str:
        status, data = await self._request(
            "POST",
            INVENTORY_LEASE_PATH,
            payload={"clientId": self.client_id, "label": str(label or "")[:200]},
        )
        if status == 409 and data.get("code") == "inventory_empty":
            return ""
        if status != 200 or not data.get("ok"):
            raise RuntimeError(str(data.get("error") or f"领取邮箱失败（HTTP {status}）"))
        lease = data.get("lease")
        if not isinstance(lease, dict):
            raise RuntimeError("远端邮箱库存服务未返回有效租约")
        email = str(lease.get("email") or "").strip().lower()
        lease_id = str(lease.get("leaseId") or "").strip()
        if not email or not lease_id:
            raise RuntimeError("远端邮箱库存租约缺少邮箱或 leaseId")
        self._leases_by_email[email] = dict(lease)
        return email

    def leased_record(self, email: str) -> dict[str, Any] | None:
        lease = self._leases_by_email.get(str(email or "").strip().lower())
        record = lease.get("record") if isinstance(lease, dict) else None
        return dict(record) if isinstance(record, dict) else None

    async def complete_email(
        self,
        email: str,
        success: bool,
        message: str = "",
        *,
        record: dict[str, Any] | None = None,
    ) -> None:
        target = str(email or "").strip().lower()
        lease = self._leases_by_email.get(target)
        if not lease:
            raise RuntimeError(f"找不到邮箱 {target} 的远端库存租约")
        payload: dict[str, Any] = {
            "leaseId": lease["leaseId"],
            "email": target,
            "success": bool(success),
            "message": str(message or "")[:1000],
        }
        if record is not None:
            payload["record"] = record
        status, data = await self._request(
            "POST",
            INVENTORY_RESULT_PATH,
            payload=payload,
        )
        if status != 200 or not data.get("ok"):
            raise RuntimeError(str(data.get("error") or f"提交注册回执失败（HTTP {status}）"))
        self._leases_by_email.pop(target, None)

    @staticmethod
    def _sync_batches(
        records: list[dict[str, Any]],
    ) -> list[list[dict[str, Any]]]:
        batches: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        for record in records:
            candidate = [*current, record]
            encoded_size = len(
                json.dumps(
                    {"schemaVersion": 1, "records": candidate},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            if current and (
                len(candidate) > MAX_SYNC_BATCH_RECORDS
                or encoded_size > MAX_SYNC_BATCH_BYTES
            ):
                batches.append(current)
                current = [record]
            else:
                current = candidate
        if current:
            batches.append(current)
        return batches

    async def sync_records(
        self, records: list[dict[str, Any]]
    ) -> dict[str, int]:
        totals = {"records": 0, "addresses": 0, "accounts": 0, "batches": 0}
        for batch in self._sync_batches(list(records)):
            status, data = await self._request(
                "POST",
                INVENTORY_SYNC_PATH,
                payload={"schemaVersion": 1, "records": batch},
            )
            if status != 200 or not data.get("ok"):
                raise RuntimeError(
                    str(data.get("error") or f"同步远程邮箱失败（HTTP {status}）")
                )
            for key in ("records", "addresses", "accounts"):
                totals[key] += max(0, int(data.get(key) or 0))
            totals["batches"] += 1
        return totals

    async def status(self) -> dict[str, Any]:
        status, data = await self._request("GET", INVENTORY_STATUS_PATH)
        if status != 200 or not data.get("ok"):
            raise RuntimeError(str(data.get("error") or f"读取库存状态失败（HTTP {status}）"))
        return data


__all__ = [
    "INVENTORY_INTEGRATION_PATHS",
    "INVENTORY_LOGIN_PATH",
    "INVENTORY_LEASE_PATH",
    "INVENTORY_RESULT_PATH",
    "INVENTORY_SYNC_PATH",
    "INVENTORY_STATUS_PATH",
    "MAX_SYNC_BATCH_BYTES",
    "MAX_SYNC_BATCH_RECORDS",
    "RemoteRegistrationInventoryClient",
]
