from __future__ import annotations

import socket
from typing import Any

import aiohttp


INVENTORY_STATUS_PATH = "/api/integrations/registration-inventory/status"
INVENTORY_LEASE_PATH = "/api/integrations/registration-inventory/lease"
INVENTORY_RESULT_PATH = "/api/integrations/registration-inventory/result"
INVENTORY_INTEGRATION_PATHS = {
    INVENTORY_STATUS_PATH,
    INVENTORY_LEASE_PATH,
    INVENTORY_RESULT_PATH,
}


class RemoteRegistrationInventoryClient:
    def __init__(
        self,
        *,
        service_url: str,
        token: str,
        client_id: str = "",
        timeout_seconds: float = 20,
    ) -> None:
        self.service_url = str(service_url or "").strip().rstrip("/")
        self.token = str(token or "").strip()
        self.client_id = str(client_id or socket.gethostname() or "local-client")[:200]
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self._leases_by_email: dict[str, dict[str, Any]] = {}

    @property
    def configured(self) -> bool:
        return bool(self.service_url and self.token)

    async def _request(
        self, method: str, path: str, *, payload: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        if not self.configured:
            raise RuntimeError("远端邮箱库存服务未配置")
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        headers = {"X-HME-Import-Token": self.token}
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.request(
                    method, f"{self.service_url}{path}", json=payload
                ) as response:
                    try:
                        data = await response.json()
                    except (aiohttp.ContentTypeError, ValueError):
                        detail = (await response.text())[:300]
                        raise RuntimeError(
                            f"远端邮箱库存服务返回了无效响应（HTTP {response.status}）：{detail}"
                        )
                    return response.status, data if isinstance(data, dict) else {}
        except (aiohttp.ClientError, TimeoutError) as error:
            raise RuntimeError(f"无法连接远端邮箱库存服务：{error}") from error

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

    async def complete_email(self, email: str, success: bool, message: str = "") -> None:
        target = str(email or "").strip().lower()
        lease = self._leases_by_email.get(target)
        if not lease:
            raise RuntimeError(f"找不到邮箱 {target} 的远端库存租约")
        status, data = await self._request(
            "POST",
            INVENTORY_RESULT_PATH,
            payload={
                "leaseId": lease["leaseId"],
                "email": target,
                "success": bool(success),
                "message": str(message or "")[:1000],
            },
        )
        if status != 200 or not data.get("ok"):
            raise RuntimeError(str(data.get("error") or f"提交注册回执失败（HTTP {status}）"))
        self._leases_by_email.pop(target, None)

    async def status(self) -> dict[str, Any]:
        status, data = await self._request("GET", INVENTORY_STATUS_PATH)
        if status != 200 or not data.get("ok"):
            raise RuntimeError(str(data.get("error") or f"读取库存状态失败（HTTP {status}）"))
        return data


__all__ = [
    "INVENTORY_INTEGRATION_PATHS",
    "INVENTORY_LEASE_PATH",
    "INVENTORY_RESULT_PATH",
    "INVENTORY_STATUS_PATH",
    "RemoteRegistrationInventoryClient",
]
