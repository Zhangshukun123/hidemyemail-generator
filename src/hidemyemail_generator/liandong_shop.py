"""Liandong shop inventory upload integration.

The merchant token is intentionally kept server-side.  Public projections only
report whether it is configured, and account upload markers never contain card
contents or credentials.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

from .account_actions import account_phone_binding_state
from .browser_tasks import load_account_record
from .inbox import connect_db


LIANDONG_SHOP_API_URL = "https://pay.ldxp.cn/merchantApi/GoodsCardStorage/add"
LIANDONG_SHOP_TOKEN_ENV = "LIANDONG_SHOP_MERCHANT_TOKEN"
LIANDONG_SHOP_CONFIG_KEY = "liandong_shop:config"


@dataclass(frozen=True)
class LiandongShopGoods:
    """One fixed destination in the merchant's Liandong shop."""

    goods_id: int
    key: str
    name: str
    short_label: str


UNBOUND_GOODS = LiandongShopGoods(
    goods_id=698207,
    key="plus_unbound",
    name="PLUS--质保首登--未接码",
    short_label="未接码商品",
)
BOUND_GOODS = LiandongShopGoods(
    goods_id=685418,
    key="plus_bound",
    name="PLUS--质保首登--已接码",
    short_label="已接码商品",
)


class LiandongShopError(RuntimeError):
    """A safe user-facing configuration, validation, or upload failure."""


class LiandongShopConfigStore:
    """Persist the merchant token without exposing it through HTTP responses."""

    def __init__(self, db_file: Path) -> None:
        self.db_file = Path(db_file)

    def _stored_token(self) -> str:
        conn = connect_db(str(self.db_file))
        try:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?",
                (LIANDONG_SHOP_CONFIG_KEY,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return ""
        raw = str(row["value"] or "")
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return raw.strip()
        if not isinstance(payload, dict):
            return ""
        return str(payload.get("merchant_token") or "").strip()

    def token(self) -> tuple[str, str]:
        environment_token = str(os.environ.get(LIANDONG_SHOP_TOKEN_ENV) or "").strip()
        if environment_token:
            return environment_token, "environment"
        stored_token = self._stored_token()
        return stored_token, "database" if stored_token else ""

    def save(self, token: str) -> None:
        normalized = str(token or "").strip()
        if not normalized:
            raise LiandongShopError("请输入联动小铺 Merchant-Token")
        if len(normalized) > 4096 or "\r" in normalized or "\n" in normalized:
            raise LiandongShopError("Merchant-Token 格式无效")
        conn = connect_db(str(self.db_file))
        try:
            conn.execute(
                """
                INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (
                    LIANDONG_SHOP_CONFIG_KEY,
                    json.dumps({"merchant_token": normalized}, ensure_ascii=False),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def clear(self) -> None:
        conn = connect_db(str(self.db_file))
        try:
            conn.execute(
                "DELETE FROM settings WHERE key = ?", (LIANDONG_SHOP_CONFIG_KEY,)
            )
            conn.commit()
        finally:
            conn.close()

    def public_status(self) -> dict[str, Any]:
        token, source = self.token()
        return {
            "ok": True,
            "configured": bool(token),
            "source": source,
            "products": {
                "unbound": _goods_projection(UNBOUND_GOODS),
                "bound": _goods_projection(BOUND_GOODS),
            },
        }


class LiandongShopClient:
    """Upload one card to the merchant API."""

    def __init__(self, api_url: str = LIANDONG_SHOP_API_URL) -> None:
        self.api_url = str(api_url).strip() or LIANDONG_SHOP_API_URL

    async def upload_card(
        self, *, token: str, goods: LiandongShopGoods, content: str
    ) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=30)
        headers = {
            "Merchant-Token": token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        body = {
            "goods_id": goods.goods_id,
            "content": content,
            "first": 0,
            "remove_repeat": 1,
        }
        try:
            async with aiohttp.ClientSession(
                timeout=timeout, trust_env=False
            ) as session:
                async with session.post(
                    self.api_url, json=body, headers=headers
                ) as response:
                    try:
                        payload = await response.json(content_type=None)
                    except (aiohttp.ContentTypeError, json.JSONDecodeError, ValueError):
                        payload = {}
                    if response.status < 200 or response.status >= 300:
                        raise LiandongShopError(
                            f"联动小铺请求失败（HTTP {response.status}）"
                        )
        except LiandongShopError:
            raise
        except (aiohttp.ClientError, TimeoutError) as error:
            raise LiandongShopError("无法连接联动小铺，请稍后重试") from error

        if not isinstance(payload, dict):
            raise LiandongShopError("联动小铺响应格式无效")
        code = payload.get("code")
        if code is None or code is False or str(code).strip() == "0":
            message = _safe_response_message(payload)
            raise LiandongShopError(message or "联动小铺拒绝添加库存")
        return {
            "code": code,
            "message": _safe_response_message(payload),
        }


def _goods_projection(goods: LiandongShopGoods) -> dict[str, Any]:
    return {
        "goodsId": goods.goods_id,
        "key": goods.key,
        "name": goods.name,
        "label": goods.short_label,
    }


def _safe_response_message(payload: dict[str, Any]) -> str:
    value = payload.get("msg") or payload.get("message") or payload.get("error") or ""
    return str(value).replace("\r", " ").replace("\n", " ").strip()[:240]


def card_upload_for_account(
    email: str, record: dict[str, Any]
) -> tuple[LiandongShopGoods, str]:
    """Validate and serialize one Plus account for its fixed goods destination."""

    target = str(email or "").strip().lower()
    if not target or "\r" in target or "\n" in target:
        raise LiandongShopError("邮箱地址无效")
    if str(record.get("account_type") or "").strip().lower() != "plus":
        raise LiandongShopError("只有已确认的 Plus 账号可以上传到联动小铺")
    password = str(record.get("password") or "").strip()
    if not password or record.get("password_confirmed") is False:
        raise LiandongShopError("该账号尚未保存已确认的密码")
    two_factor = record.get("two_factor")
    two_factor = two_factor if isinstance(two_factor, dict) else {}
    secret = str(two_factor.get("secret") or "").strip()
    if not two_factor.get("enabled") or not secret:
        raise LiandongShopError("该账号尚未启用并保存 2FA 密钥")
    password = password.replace("\r", "").replace("\n", "")
    secret = secret.replace("\r", "").replace("\n", "")
    goods = (
        BOUND_GOODS if account_phone_binding_state(record)["bound"] else UNBOUND_GOODS
    )
    return goods, f"{target}----{password}----{secret}"


def uploaded_marker(record: dict[str, Any]) -> dict[str, Any]:
    marker = record.get("liandong_shop")
    return marker if isinstance(marker, dict) else {}


def persist_uploaded_account(
    db_file: Path,
    email: str,
    *,
    goods: LiandongShopGoods,
    response_code: Any,
) -> dict[str, Any]:
    """Persist a successful upload without storing card contents."""

    target = str(email or "").strip().lower()
    record = load_account_record(db_file, target)
    if not record:
        raise LiandongShopError("未找到账号记录")
    current = uploaded_marker(record)
    if current.get("uploaded") is True:
        return current
    now = datetime.now(timezone.utc).isoformat()
    marker = {
        "uploaded": True,
        "uploaded_at": now,
        "upload_method": "merchant_api",
        "goods_id": goods.goods_id,
        "goods_key": goods.key,
        "goods_name": goods.name,
        "goods_label": goods.short_label,
        "phone_bound": goods is BOUND_GOODS,
        "response_code": response_code,
    }
    record["liandong_shop"] = marker
    record["updated_at"] = now
    conn = connect_db(str(db_file))
    try:
        conn.execute(
            """
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (f"gpt_account:{target}", json.dumps(record, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()
    return marker


__all__ = [
    "BOUND_GOODS",
    "LIANDONG_SHOP_API_URL",
    "LIANDONG_SHOP_TOKEN_ENV",
    "LiandongShopClient",
    "LiandongShopConfigStore",
    "LiandongShopError",
    "UNBOUND_GOODS",
    "card_upload_for_account",
    "persist_uploaded_account",
    "uploaded_marker",
]
