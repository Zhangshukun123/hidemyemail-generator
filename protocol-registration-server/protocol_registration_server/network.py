from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Protocol

import aiohttp

from hidemyemail_generator.clash_proxy import detect_proxy_exit
from hidemyemail_generator.registration_proxy import (
    PROXY_MODE_KOOKEEY,
    PROXY_COUNTRIES,
    RegistrationProxyStore,
)


OFFER_PROXY_SETTING_KEY = "protocol_server_offer_proxy_v1"
REGISTRATION_PROXY_SETTING_KEY = "protocol_server_registration_proxy_v1"
OPENAI_CODE_PATH = "/api/integrations/workbench/openai-code"


class RegistrationProxyStrategy(Protocol):
    def next_proxy(self, *, force: bool = False) -> tuple[str, dict[str, Any]]: ...


class ServerAlternatingProxyStrategy:
    """Server-only Strategy: Clash rotation, then server direct, repeatedly."""

    def __init__(
        self,
        store: RegistrationProxyStore,
        *,
        exit_detector: Callable[[str], tuple[str, str]] = detect_proxy_exit,
    ) -> None:
        self.store = store
        self.exit_detector = exit_detector
        self._cursor = 0
        self._lock = threading.RLock()
        self._last_route = ""
        self._direct_exit: tuple[str, str, float] = ("", "", 0.0)

    def _direct_exit_state(self) -> tuple[str, str]:
        exit_ip, country, checked_at = self._direct_exit
        if exit_ip and time.monotonic() - checked_at < 300:
            return exit_ip, country
        try:
            exit_ip, country = self.exit_detector("")
        except Exception:
            return "", ""
        self._direct_exit = (exit_ip, country, time.monotonic())
        return exit_ip, country

    def next_proxy(self, *, force: bool = False) -> tuple[str, dict[str, Any]]:
        with self._lock:
            if force or self._cursor % 2 == 0:
                proxy_url, state = self.store.next_proxy(force=force)
                if not proxy_url:
                    raise RuntimeError("服务器 Clash 注册出口不可用")
                private_state = self.store.load()
                self._last_route = "clash"
                if not force:
                    self._cursor += 1
                return proxy_url, {
                    **state,
                    "strategy": "server_clash_direct",
                    "currentRoute": "clash",
                    "exitIp": str(private_state.get("lastExitIp") or ""),
                    "exitCountry": str(private_state.get("lastExitCountry") or ""),
                }
            self._cursor += 1
            self._last_route = "direct"
            state = self.store.public_state()
            exit_ip, exit_country = self._direct_exit_state()
            return "", {
                **state,
                "strategy": "server_clash_direct",
                "currentRoute": "direct",
                "country": "",
                "countryLabel": "服务器本机直连",
                "lastExitCountry": "",
                "exitIp": exit_ip,
                "exitCountry": exit_country,
            }

    def public_state(self) -> dict[str, Any]:
        state = self.store.public_state()
        return {
            "strategy": "server_clash_direct",
            "configured": bool(state.get("configured")),
            "enabled": bool(state.get("enabled")),
            "currentRoute": self._last_route,
            "nextRoute": "clash" if self._cursor % 2 == 0 else "direct",
            "controller": str(state.get("selector") or "自动选择"),
            "proxyEndpoint": str(state.get("normalEndpoint") or state.get("endpoint") or ""),
        }


class KookeeyRegistrationProxyStrategy:
    """Server-only Strategy that creates one Kookeey route per registration."""

    def __init__(
        self,
        store: RegistrationProxyStore,
        country: str,
        *,
        exit_detector: Callable[[str], tuple[str, str]] = detect_proxy_exit,
    ) -> None:
        selected = str(country or "").strip().upper()
        if selected not in PROXY_COUNTRIES:
            raise ValueError("Kookeey 注册出口国家无效")
        self.store = store
        self.country = selected
        self.exit_detector = exit_detector
        self._lock = threading.RLock()
        self._last_exit_country = ""
        self._last_exit_verified = False

    def next_proxy(self, *, force: bool = False) -> tuple[str, dict[str, Any]]:
        proxy_url, state = self.store.proxy_for_country(
            self.country, mode=PROXY_MODE_KOOKEEY
        )
        if not proxy_url:
            raise RuntimeError("Kookeey 注册代理不可用")
        exit_ip = ""
        exit_country = ""
        try:
            exit_ip, exit_country = self.exit_detector(proxy_url)
        except Exception:
            pass
        with self._lock:
            self._last_exit_country = exit_country
            self._last_exit_verified = bool(exit_ip)
        return proxy_url, {
            **state,
            "strategy": "server_kookeey",
            "currentRoute": "kookeey",
            "nextRoute": "kookeey",
            "mode": PROXY_MODE_KOOKEEY,
            "country": self.country,
            "countryLabel": PROXY_COUNTRIES[self.country],
            "exitIp": exit_ip,
            "exitCountry": exit_country,
        }

    def public_state(self) -> dict[str, Any]:
        state = self.store.public_state()
        configured = next(
            (
                bool(item.get("configured"))
                for item in state.get("modes", [])
                if isinstance(item, dict) and item.get("code") == PROXY_MODE_KOOKEEY
            ),
            False,
        )
        with self._lock:
            return {
                "strategy": "server_kookeey",
                "configured": configured,
                "enabled": configured,
                "currentRoute": "kookeey",
                "nextRoute": "kookeey",
                "country": self.country,
                "countryLabel": PROXY_COUNTRIES[self.country],
                "exitCountry": self._last_exit_country,
                "exitIpVerified": self._last_exit_verified,
                "proxyEndpoint": str(
                    state.get("dynamicEndpoint") or state.get("endpoint") or ""
                ),
            }


class OfferView(Protocol):
    def check_checkout(
        self,
        access_token: str,
        country: str,
    ) -> "CheckoutOfferProbe": ...


@dataclass(frozen=True, slots=True)
class CheckoutOfferProbe:
    exit_country: str
    checkout_country: str
    currency: str
    amount_minor: str
    amount_source: str
    paypal_available: bool
    checkout_url: str
    payment_methods: tuple[str, ...] = ()
    fallback_reason: str = ""
    requested_country: str = ""

    @property
    def eligible(self) -> bool:
        return self.paypal_available and self.amount_minor == "0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "requestedCountry": self.requested_country or self.exit_country,
            "exitCountry": self.exit_country,
            "checkoutCountry": self.checkout_country,
            "currency": self.currency,
            "amountMinor": self.amount_minor,
            "amountSource": self.amount_source,
            "paypalAvailable": self.paypal_available,
            "eligible": self.eligible,
            "checkoutUrl": self.checkout_url if self.eligible else "",
            "paymentMethods": list(self.payment_methods),
            "deFallback": bool(self.fallback_reason),
            "fallbackReason": self.fallback_reason,
        }


class KookeeyOfferView:
    """Network View used by the offer Presenter."""

    def __init__(
        self,
        store: RegistrationProxyStore,
    ) -> None:
        self.store = store

    def public_state(self) -> dict[str, Any]:
        state = self.store.public_state()
        mode = next(
            (
                item
                for item in state.get("modes", [])
                if isinstance(item, dict) and item.get("code") == PROXY_MODE_KOOKEEY
            ),
            {},
        )
        return {
            "mode": PROXY_MODE_KOOKEEY,
            "configured": bool(mode.get("configured")),
            "countries": ["US", "GB", "DE"],
            "endpoint": str(state.get("dynamicEndpoint") or state.get("endpoint") or ""),
        }

    def proxy_url(self, country: str) -> str:
        if not self.public_state()["configured"]:
            raise RuntimeError("Kookeey 优惠监测代理尚未配置")
        selected = str(country or "").strip().upper()
        if selected not in PROXY_COUNTRIES:
            raise RuntimeError("优惠检查国家无效")
        proxy_url, _ = self.store.proxy_for_country(
            selected,
            mode=PROXY_MODE_KOOKEEY,
        )
        if not proxy_url:
            raise RuntimeError("Kookeey 优惠监测代理不可用")
        return proxy_url

    def check_checkout(
        self,
        access_token: str,
        country: str,
    ) -> CheckoutOfferProbe:
        selected = str(country or "").strip().upper()
        try:
            primary = self._check_checkout_with_retries(
                access_token,
                exit_country=selected,
                checkout_country=selected,
            )
        except Exception as primary_error:
            if selected == "DE":
                raise
            try:
                fallback = self._check_checkout_with_retries(
                    access_token,
                    exit_country="DE",
                    checkout_country="DE",
                )
            except Exception as fallback_error:
                raise RuntimeError(
                    f"{selected} Checkout 失败：{primary_error}；"
                    f"DE 账单回退失败：{fallback_error}"
                ) from fallback_error
            return replace(
                fallback,
                fallback_reason=f"{selected} Checkout 不可用，回退 DE 账单",
                requested_country=selected,
            )
        if primary.paypal_available or selected == "DE":
            return primary
        try:
            fallback = self._check_checkout_with_retries(
                access_token,
                exit_country="DE",
                checkout_country="DE",
            )
        except Exception as fallback_error:
            raise RuntimeError(
                f"{selected} Checkout 未声明 PayPal；"
                f"DE 出口/账单回退失败：{fallback_error}"
            ) from fallback_error
        return replace(
            fallback,
            fallback_reason=f"{selected} Checkout 未声明 PayPal，回退 DE 账单",
            requested_country=selected,
        )

    def _check_checkout_with_retries(
        self,
        access_token: str,
        *,
        exit_country: str,
        checkout_country: str,
    ) -> CheckoutOfferProbe:
        errors: list[str] = []
        for _attempt in range(3):
            try:
                return self._check_checkout_once(
                    access_token,
                    exit_country=exit_country,
                    checkout_country=checkout_country,
                )
            except Exception as error:
                errors.append(str(error))
        raise RuntimeError(
            f"{exit_country}/{checkout_country} Checkout 连续 3 次失败："
            + errors[-1]
        )

    def _check_checkout_once(
        self,
        access_token: str,
        *,
        exit_country: str,
        checkout_country: str,
    ) -> CheckoutOfferProbe:
        from hidemyemail_generator.card_link_runtime import (
            currency_for_country,
            opll_chatgpt_checkout_page_url,
            opll_chatgpt_checkout_amount_info,
            opll_chatgpt_fetch_checkout,
            opll_checkout_declared_payment_method_types,
            opll_create_checkout,
            opll_extract_stripe_payment_page_id,
            opll_stripe_init,
        )

        proxy_url = self.proxy_url(exit_country)
        currency = currency_for_country(checkout_country)
        checkout = opll_create_checkout(
            access_token,
            checkout_country,
            currency,
            proxy_url,
            request_locale="en-US",
            include_trial_promo=True,
            checkout_ui_mode="custom",
            return_raw_payload=True,
        )
        raw_payload = checkout.get("_checkout_payload")
        payloads = [raw_payload] if isinstance(raw_payload, dict) else []
        checkout_id = str(checkout.get("cs_id") or "")
        if checkout_id.startswith("oaics_"):
            fetched = opll_chatgpt_fetch_checkout(
                access_token,
                checkout,
                proxy_url,
                request_locale="en-US",
            )
            payloads.append(fetched)
            stripe_id = opll_extract_stripe_payment_page_id(*payloads)
        else:
            stripe_id = checkout_id if checkout_id.startswith("cs_") else ""
        if stripe_id:
            stripe_checkout = {**checkout, "cs_id": stripe_id}
            payloads.append(
                opll_stripe_init(
                    stripe_id,
                    checkout_country,
                    currency,
                    proxy_url,
                    checkout=stripe_checkout,
                )
            )
        amount_minor = ""
        amount_source = "missing_payload"
        payment_methods: list[str] = []
        for payload in reversed(payloads):
            candidate_amount, candidate_source = opll_chatgpt_checkout_amount_info(
                payload
            )
            if not amount_minor and candidate_amount:
                amount_minor = candidate_amount
                amount_source = candidate_source
            for method in opll_checkout_declared_payment_method_types(payload):
                if method not in payment_methods:
                    payment_methods.append(method)
        if not amount_minor:
            raise RuntimeError(f"{checkout_country} Checkout 未返回可确认金额")
        url = opll_chatgpt_checkout_page_url(
            checkout_id,
            str(checkout.get("billing_country") or checkout_country),
            str(checkout.get("processor_entity") or ""),
        )
        if not url:
            raise RuntimeError("优惠 Checkout 已提交但未返回链接")
        return CheckoutOfferProbe(
            exit_country=exit_country,
            checkout_country=checkout_country,
            currency=currency,
            amount_minor=amount_minor,
            amount_source=amount_source,
            paypal_available="paypal" in payment_methods,
            checkout_url=url,
            payment_methods=tuple(payment_methods),
            requested_country=exit_country,
        )


@dataclass(slots=True)
class CodeServiceClient:
    base_url: str
    token: str
    timeout_seconds: float = 15.0

    async def fetch(self, email: str, since: str) -> tuple[int, str]:
        timeout = aiohttp.ClientTimeout(total=max(1.0, self.timeout_seconds))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{self.base_url.rstrip('/')}{OPENAI_CODE_PATH}",
                headers={"X-HME-Import-Token": self.token},
                json={"email": email, "since": since},
            ) as response:
                try:
                    payload = await response.json()
                except (aiohttp.ContentTypeError, ValueError):
                    payload = {}
                if response.status == 200 and payload.get("ok"):
                    code = str(payload.get("code") or "").strip()
                    return (200, code) if code else (502, "验证码服务返回空值")
                return response.status, str(
                    payload.get("error") or f"验证码服务 HTTP {response.status}"
                )[:500]

    async def wait_until_ready(self) -> bool:
        for _ in range(3):
            try:
                timeout = aiohttp.ClientTimeout(total=3)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(
                        f"{self.base_url.rstrip('/')}/healthz"
                    ) as response:
                        if response.status == 200:
                            return True
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(0.2)
        return False
