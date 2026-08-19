"""Embedded direct-card payment-link runtime.

Generated from openai-register-paylink/app_backend.py at revision working-tree.
Only the transitive code required by the PayPal US/USD card-link mode is included.
This module is self-contained inside hidemyemail-generator at runtime.
"""

from __future__ import annotations

# The source backend keeps broad shared import groups and a few branch-local
# compatibility variables.  They are intentionally retained in this snapshot.
# ruff: noqa: F401,F841

try:
    from curl_cffi.requests import Session as CurlCffiSession
    from curl_cffi.const import CurlOpt as CurlCffiOpt
except ImportError:
    CurlCffiSession = None
    CurlCffiOpt = None
import dataclasses

import base64

import hashlib

import json

import random

import re

import secrets

import threading

import time

import unicodedata

import uuid

from urllib.parse import parse_qs, parse_qsl, quote, urlencode, unquote, urljoin, urlparse, urlsplit, urlunsplit

from curl_cffi import requests

from ._card_link_browser_identity import (
    DeviceFingerprint,
    country_browser_locale,
    country_code_upper,
    country_payment_locale,
    country_request_locale,
    country_timezone,
    generate_fingerprint,
    generate_fingerprint_for_exit,
    generate_payment_fingerprint,
    generate_register_fingerprint,
    generate_team_fingerprint,
    locale_language_list,
    locale_parts,
    opll_accept_language_for_locale,
    opll_locale_context_for_country,
)

from ._card_link_country_profiles import (
    AU_BILLING_NAMES,
    AU_BILLING_STREETS,
    BILLING_PROFILE_BY_COUNTRY,
    COUNTRY_CURRENCY,
    COUNTRY_PHONE_PREFIX,
    DE_BILLING_NAMES,
    DE_BILLING_STREETS,
    EXTRA_BILLING_NAMES,
    EXTRA_BILLING_STREETS,
    GB_BILLING_NAMES,
    GB_BILLING_STREETS,
    IN_BILLING_NAMES,
    IN_BILLING_STREETS,
    KR_BILLING_NAMES,
    KR_BILLING_STREETS,
    NL_BILLING_NAMES,
    NL_BILLING_STREETS,
    OPENAI_SUPPORTED_COUNTRY_CODES,
    QUICK_PROXY_COUNTRY_CHOICES,
    QUICK_PROXY_COUNTRY_DISPLAY_CHOICES,
    QUICK_PROXY_COUNTRY_NAMES,
    US_BILLING_NAMES,
    US_BILLING_STREETS,
    VN_BILLING_NAMES,
    VN_BILLING_STREETS,
    quick_proxy_country_code,
    quick_proxy_country_display,
)

from ._card_link_payment_modes import (
    GCASH_PH_MODE,
    IDEAL_TEMPORARY_MODE,
    KAKAO_STRICT_ZERO_MODE,
    MOMO_VN_MODE,
    PAYMENT_MODE_ALIASES,
    PAYMENT_MODE_DISPLAY_LABELS,
    PAYMENT_MODE_MENU_GROUPS,
    PAYMENT_MODES,
    PHILIPPINES_GPT_LINK_FLOW,
    PHILIPPINES_PAYPAL_CHECK_FLOW,
    PHILIPPINES_PAYPAL_CHECK_MODE,
    PHILIPPINES_SHORT_LINK_MODE,
    PAYPAL_BR_DE_STRICT_ZERO_FLOW,
    PAYPAL_DE_NATIVE_PROMO_FLOW,
    PAYPAL_DE_OAICS_FLOW,
    PAYPAL_EUR_JP_STYLE_ZERO_FLOW,
    PAYPAL_EUR_JP_STYLE_ZERO_MODE,
    PAYPAL_FR_IDEAL_STYLE_ZERO_FLOW,
    PAYPAL_JP_STRICT_ZERO_FLOW,
    PAYPAL_JP_STRICT_ZERO_MODE,
    PAYPAL_NATIVE_PROMO_MODE,
    PAYPAL_BILLING_COUNTRY_CHOICES,
    PAYPAL_BILLING_COUNTRY_CURRENCY,
    PAYPAL_BILLING_COUNTRY_IP,
    PAYPAL_STRICT_ZERO_MODE,
    PAYPAL_US_TR_FLOW,
    PAYPAL_US_TR_MODE,
    UPI_ACTIVITY_STRICT_ZERO_MODE,
    UPI_STRICT_ZERO_MODE,
    normalize_payment_mode_name,
    normalize_paypal_billing_country_mode,
    payment_mode_paypal_flow,
    payment_mode_target_amount,
)

CHATGPT_BASE_URL = "https://chatgpt.com"

OPLL_HTTP_IMPERSONATE = "chrome136"

OPLL_BROWSER_MAJOR = "136"

OPLL_BROWSER_FULL_VERSION = f"{OPLL_BROWSER_MAJOR}.0.0.0"

OPLL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/{OPLL_BROWSER_FULL_VERSION} Safari/537.36"
)

DEFAULT_STRIPE_PK = "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n"

STRIPE_VERSION_FULL = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"

DEFAULT_STRIPE_RUNTIME_VERSION = "6f8494a281"

PIX_TRIAL_PROMOTION_ID = "plus-1-month-free"

PAY_LONG_LINK_TIMEOUT = 30

PAYPAL_NATIVE_PROMO_SYNC_ATTEMPTS = 6

PAYPAL_NATIVE_PROMO_SYNC_INTERVAL_SECONDS = 2.0

PAYPAL_NATIVE_PROMO_IP_REFRESH_ATTEMPTS = 3

PAYPAL_DE_STRICT_ZERO_PROMO_IP_REFRESH_ATTEMPTS = 5

PAYPAL_NATIVE_POST_CONFIRM_SETTLE_SECONDS = 2.0

PAYPAL_NATIVE_APPROVE_EXTRACT_ATTEMPTS = 5

PAYPAL_NATIVE_APPROVE_CHECKOUT_IP_ATTEMPTS = 2

PAYPAL_NATIVE_POST_APPROVE_POLL_SECONDS = (8, 8, 8, 8, 20)

PAYPAL_NATIVE_APPROVE_RETRY_INTERVAL_SECONDS = 1.0


def opll_paypal_checkout_sentinel_headers(
    proxy_url: str,
    device_id: str,
    request_locale: str,
) -> dict[str, str]:
    """Generate the flow-bound SEN + SO headers for one checkout creation."""

    from .vendor.gptfree_register.core.gpt_trial_protocol.models import (
        BrowserProfile,
        ProtocolConfig,
    )
    from .vendor.gptfree_register.core.gpt_trial_protocol.sentinel_http import (
        SentinelHttpTokenProvider,
    )

    locale_context = opll_locale_context_for_country(
        str(request_locale or "en-US").split("-", 1)[-1]
    )
    profile = BrowserProfile(
        user_agent=OPLL_USER_AGENT,
        sec_ch_ua=(
            f'"Chromium";v="{OPLL_BROWSER_MAJOR}", '
            f'"Not=A?Brand";v="24", "Google Chrome";v="{OPLL_BROWSER_MAJOR}"'
        ),
        language=str(request_locale or "en-US"),
        timezone=str(locale_context.get("browser_timezone") or "America/New_York"),
        device_id=str(device_id or "").strip() or str(uuid.uuid4()),
    )
    provider = SentinelHttpTokenProvider(
        config=ProtocolConfig(
            timeout=float(PAY_LONG_LINK_TIMEOUT),
            profile=profile,
        ),
        proxy=str(proxy_url or "").strip() or None,
        device_id=profile.device_id,
    )
    bundle = provider.get_openai_sentinel(purpose="checkout")
    headers = bundle.sentinel.as_headers()
    if not headers.get("openai-sentinel-token"):
        raise OpllChatgptSentinelError("Checkout SEN 主令牌生成失败")
    if not headers.get("openai-sentinel-so-token"):
        raise OpllChatgptSentinelError("Checkout SO 附加令牌生成失败")
    return headers

@dataclasses.dataclass(frozen=True)
class ProxyHealthResult:
    success: bool
    ip: str = ""
    country: str = ""
    region: str = ""
    city: str = ""
    timezone: str = ""
    org: str = ""
    chatgpt_status: int = 0
    stripe_status: int = 0
    failed_stage: str = ""
    error: str = ""

    @property
    def location(self) -> str:
        return "/".join(part for part in (self.country, self.region, self.city) if part)

    @property
    def summary(self) -> str:
        if not self.success:
            detail = f": {self.error}" if self.error else ""
            return f"检测失败[{self.failed_stage or 'unknown'}]{detail}"
        return " ".join(
            part
            for part in (
                self.ip,
                self.location,
                self.timezone,
                self.org,
                f"ChatGPT={self.chatgpt_status}",
                f"Stripe={self.stripe_status}",
            )
            if part
        )

    @property
    def compact_summary(self) -> str:
        """日志用的精简出口信息；完整字段仍保留在对象中供指纹与诊断使用。"""
        if not self.success:
            detail = f"：{self.error}" if self.error else ""
            text = f"检测失败[{self.failed_stage or 'unknown'}]{detail}"
            return text if len(text) <= 140 else text[:137] + "..."
        location_parts: list[str] = []
        for part in (self.country, self.region, self.city):
            value = str(part or "").strip()
            if value and all(value.casefold() != existing.casefold() for existing in location_parts):
                location_parts.append(value)
        location = "/".join(location_parts)
        org = str(self.org or "").strip()
        if len(org) > 42:
            org = org[:39] + "..."
        return " · ".join(
            part
            for part in (
                str(self.ip or "").strip(),
                location,
                org,
                f"GPT {self.chatgpt_status}" if self.chatgpt_status else "",
                f"Stripe {self.stripe_status}" if self.stripe_status else "",
            )
            if part
        )

def _split_request_timeout(timeout, default: float = 15.0) -> tuple[float, float]:
    """把 timeout 规范为 (connect, read)。单数字时 connect 取较短值，坏节点更快失败。"""
    if isinstance(timeout, (tuple, list)) and len(timeout) >= 2:
        return max(0.5, float(timeout[0])), max(0.5, float(timeout[1]))
    try:
        value = float(timeout if timeout is not None else default)
    except Exception:
        value = float(default)
    value = max(1.0, value)
    connect = min(4.0, max(1.0, value * 0.35))
    return connect, value

_PROXY_EXIT_PROBES = (
    ("ipinfo", "https://ipinfo.io/json", 8.0),
    ("mayips", "https://mayips.com", 5.0),
    ("ipapi", "https://ipapi.co/json/", 6.0),
    ("ipwho", "https://ipwho.is/", 6.0),
    ("countryis", "https://api.country.is/", 6.0),
    ("ipsb", "https://api.ip.sb/geoip", 6.0),
)

_PROXY_EXIT_CACHE_TTL = 900.0

_PROXY_EXIT_CACHE_MAX_ENTRIES = 256

_PROXY_EXIT_CACHE: dict[str, tuple[float, dict[str, str]]] = {}

_PROXY_EXIT_CACHE_LOCK = threading.Lock()


def clear_proxy_exit_cache() -> None:
    """Clear request-scoped proxy identity state at a shared-worker boundary."""

    with _PROXY_EXIT_CACHE_LOCK:
        _PROXY_EXIT_CACHE.clear()


def _prune_proxy_exit_cache(now: float) -> None:
    expired = [
        key
        for key, (created_at, _payload) in _PROXY_EXIT_CACHE.items()
        if now - created_at > _PROXY_EXIT_CACHE_TTL
    ]
    for key in expired:
        _PROXY_EXIT_CACHE.pop(key, None)
    overflow = len(_PROXY_EXIT_CACHE) - _PROXY_EXIT_CACHE_MAX_ENTRIES
    if overflow > 0:
        oldest = sorted(
            _PROXY_EXIT_CACHE,
            key=lambda key: _PROXY_EXIT_CACHE[key][0],
        )[:overflow]
        for key in oldest:
            _PROXY_EXIT_CACHE.pop(key, None)


def _new_proxy_probe_session():
    """Create an isolated probe client with the payment stack's TLS profile."""
    if CurlCffiSession is not None:
        client = CurlCffiSession(impersonate=OPLL_HTTP_IMPERSONATE)
    else:
        client = requests.Session()
    if hasattr(client, "trust_env"):
        client.trust_env = False
    return client

def _proxy_exit_cache_key(proxy_url: str) -> str:
    normalized = normalize_proxy_url(proxy_url)
    return hashlib.sha256(normalized.encode("utf-8", errors="ignore")).hexdigest()

def _proxy_exit_payload(payload: dict, source: str) -> tuple[dict[str, str] | None, str]:
    if not isinstance(payload, dict):
        return None, f"{source}=响应格式无效"
    if payload.get("success") is False:
        return None, f"{source}=provider_failed"
    country = str(
        payload.get("country_code")
        or payload.get("countryCode")
        or payload.get("country_code2")
        or payload.get("country")
        or ""
    ).strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", country):
        return None, f"{source}=缺少国家代码"
    ip = str(payload.get("ip") or payload.get("query") or "").strip()
    if not ip:
        return None, f"{source}=缺少 IP"
    timezone_value = payload.get("timezone") or ""
    if isinstance(timezone_value, dict):
        timezone_value = timezone_value.get("id") or timezone_value.get("name") or ""
    org_value = (
        payload.get("org")
        or payload.get("organization")
        or payload.get("asn_organization")
        or payload.get("isp")
        or payload.get("asn")
        or ""
    )
    if isinstance(org_value, dict):
        org_value = org_value.get("name") or org_value.get("org") or org_value.get("asn") or ""
    return {
        "ip": ip,
        "country": country,
        "region": str(
            payload.get("region")
            or payload.get("region_name")
            or payload.get("regionName")
            or payload.get("state")
            or ""
        ).strip(),
        "city": str(payload.get("city") or "").strip(),
        "timezone": country_timezone(country, str(timezone_value).strip()),
        "org": str(org_value).strip(),
    }, ""

def detect_proxy_health(
    proxy_url: str,
    timeout: int = 15,
    session=None,
    *,
    check_stripe: bool = True,
    check_chatgpt: bool = True,
) -> ProxyHealthResult:
    normalized = normalize_proxy_url(proxy_url)
    connect_timeout, read_timeout = _split_request_timeout(timeout)
    full_timeout = (connect_timeout, read_timeout)
    request_kwargs = {"timeout": full_timeout}
    if normalized:
        explicit_proxies = {"http": normalized, "https": normalized}
        request_kwargs["proxies"] = explicit_proxies
    base: dict[str, str] | None = None
    exit_errors: list[str] = []
    cache_key = _proxy_exit_cache_key(normalized)
    if session is None:
        with _PROXY_EXIT_CACHE_LOCK:
            _prune_proxy_exit_cache(time.monotonic())
            cached = _PROXY_EXIT_CACHE.get(cache_key)
            if cached and time.monotonic() - cached[0] <= _PROXY_EXIT_CACHE_TTL:
                base = dict(cached[1])

    if base is None:
        for source, url, provider_read_timeout in _PROXY_EXIT_PROBES:
            # Match the actual checkout transport.  Some residential gateways
            # reset Requests/OpenSSL tunnels but work with the browser-like
            # curl_cffi TLS stack used by the payment flow.
            probe_client = session or _new_proxy_probe_session()
            owns_probe_client = session is None
            try:
                if hasattr(probe_client, "trust_env"):
                    probe_client.trust_env = False
                if normalized and hasattr(probe_client, "proxies"):
                    probe_client.proxies.update(request_kwargs["proxies"])
                response = probe_client.get(
                    url,
                    **{
                        **request_kwargs,
                        "timeout": (
                            connect_timeout,
                            min(read_timeout, provider_read_timeout),
                        ),
                    },
                )
                if response.status_code != 200:
                    exit_errors.append(f"{source}=HTTP {response.status_code}")
                    continue
                try:
                    payload = response.json() or {}
                except Exception as exc:
                    exit_errors.append(f"{source}=JSON异常({type(exc).__name__})")
                    continue
                base, payload_error = _proxy_exit_payload(payload, source)
                if base is not None:
                    break
                exit_errors.append(payload_error)
            except Exception as exc:
                exit_errors.append(f"{source}=请求异常({type(exc).__name__})")
            finally:
                if owns_probe_client:
                    try:
                        probe_client.close()
                    except Exception:
                        pass

    if base is None:
        return ProxyHealthResult(False, failed_stage="出口", error="；".join(exit_errors))
    if session is None:
        with _PROXY_EXIT_CACHE_LOCK:
            now = time.monotonic()
            _PROXY_EXIT_CACHE[cache_key] = (now, dict(base))
            _prune_proxy_exit_cache(now)

    if not check_chatgpt:
        return ProxyHealthResult(True, **base)

    client = session or _new_proxy_probe_session()
    if hasattr(client, "trust_env"):
        client.trust_env = False
    if normalized and hasattr(client, "proxies"):
        client.proxies.update(request_kwargs["proxies"])

    try:
        response = client.get(f"{CHATGPT_BASE_URL}/api/auth/csrf", **request_kwargs)
        chatgpt_status = int(response.status_code)
        if chatgpt_status not in (200, 403):
            return ProxyHealthResult(False, **base, chatgpt_status=chatgpt_status, failed_stage="ChatGPT", error=f"HTTP {chatgpt_status}")
    except Exception as exc:
        return ProxyHealthResult(False, **base, failed_stage="ChatGPT", error=str(exc))

    if not check_stripe:
        return ProxyHealthResult(
            True,
            **base,
            chatgpt_status=chatgpt_status,
        )

    try:
        response = client.get("https://api.stripe.com/v1/payment_pages/__connectivity_check__", **request_kwargs)
        stripe_status = int(response.status_code)
        if stripe_status == 407 or stripe_status == 429 or stripe_status >= 500:
            return ProxyHealthResult(
                False,
                **base,
                chatgpt_status=chatgpt_status,
                stripe_status=stripe_status,
                failed_stage="Stripe",
                error=f"HTTP {stripe_status}",
            )
    except Exception as exc:
        return ProxyHealthResult(False, **base, chatgpt_status=chatgpt_status, failed_stage="Stripe", error=str(exc))

    return ProxyHealthResult(True, **base, chatgpt_status=chatgpt_status, stripe_status=stripe_status)

def random_provider_sid() -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))

_PROVIDER_PROXY_SID_RE = re.compile(
    r"(-sid-)([A-Za-z0-9]{4,32})(-t-)",
    re.IGNORECASE,
)

def opll_proxy_has_rotatable_sid(proxy_url: str) -> bool:
    return bool(_PROVIDER_PROXY_SID_RE.search(str(proxy_url or "")))

def opll_with_fresh_provider_sid(proxy_url: str) -> str:
    """把代理 URL 里的 sid 换成新的 8 位，迫使住宅/提供商出口换 IP。"""
    text = str(proxy_url or "").strip()
    if not text or not opll_proxy_has_rotatable_sid(text):
        return text
    return _PROVIDER_PROXY_SID_RE.sub(
        lambda match: f"{match.group(1)}{random_provider_sid()}{match.group(3)}",
        text,
        count=1,
    )

_KOOKEEY_PROXY_ROUTE_RE = re.compile(
    r"^(?P<base>.+)-(?P<region>[A-Za-z]{2}|global)-"
    r"(?P<sid>[A-Za-z0-9]{8})-(?P<duration>\d+[mh])$",
    re.IGNORECASE,
)

def opll_proxy_has_refreshable_ip(proxy_url: str) -> bool:
    text = str(proxy_url or "").strip()
    if opll_proxy_has_rotatable_sid(text):
        return True
    try:
        parsed = urlsplit(text)
        password = unquote(parsed.password or "")
    except Exception:
        return False
    return bool(_KOOKEEY_PROXY_ROUTE_RE.fullmatch(password))

def opll_with_fresh_promotion_proxy_ip(proxy_url: str) -> str:
    """Refresh a standard provider SID or Kookeey password-route session."""
    text = str(proxy_url or "").strip()
    standard = opll_with_fresh_provider_sid(text)
    if standard != text:
        return standard
    try:
        parsed = urlsplit(text)
        username = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        matched = _KOOKEEY_PROXY_ROUTE_RE.fullmatch(password)
        hostname = parsed.hostname or ""
    except Exception:
        return text
    if not matched or not username or not hostname:
        return text
    refreshed_password = (
        f"{matched.group('base')}-{matched.group('region')}-"
        f"{random_provider_sid()}-{matched.group('duration')}"
    )
    host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    netloc = (
        f"{quote(username, safe='-._~')}:"
        f"{quote(refreshed_password, safe='-._~')}@{host}"
    )
    return urlunsplit((parsed.scheme or "http", netloc, parsed.path, parsed.query, parsed.fragment))

def opll_normalize_approve_proxy_candidates(proxy_url: str | list[str] | tuple[str, ...] | None = None) -> list[str]:
    if proxy_url is None:
        return []
    if isinstance(proxy_url, (list, tuple)):
        items = [str(item or "").strip() for item in proxy_url]
    else:
        text = str(proxy_url or "").strip()
        if not text:
            return []
        # 支持多行 / 逗号分隔的手工 Approve 池
        if "\n" in text or "," in text:
            parts = re.split(r"[\n,]+", text)
            items = [str(item or "").strip() for item in parts]
        else:
            items = [text]
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result

def opll_pick_approve_proxy_for_attempt(
    proxy_candidates: list[str] | str | tuple[str, ...] | None,
    attempt_index: int,
    *,
    force_new_sid: bool = True,
) -> str:
    """每次 approve 选代理：多条轮询；单条带 sid 则每次换 sid 换 IP。"""
    candidates = opll_normalize_approve_proxy_candidates(proxy_candidates)
    if not candidates:
        return ""
    index = max(0, int(attempt_index or 0)) % len(candidates)
    selected = candidates[index]
    if force_new_sid and opll_proxy_has_rotatable_sid(selected):
        # 多条池：每次 attempt 也刷 sid，避免同一条被 sticky 粘住
        return opll_with_fresh_provider_sid(selected)
    return selected

def opll_proxy_is_loopback(proxy_url: str) -> bool:
    try:
        host = (urlsplit(str(proxy_url or "").strip()).hostname or "").lower()
    except Exception:
        return False
    return host in {"127.0.0.1", "localhost", "::1"}

def opll_describe_proxy_endpoint(proxy_url: str) -> str:
    """把代理 URL 收成不含凭据的 region/sid/host:port 出口标签。"""
    text = str(proxy_url or "").strip()
    if not text:
        return "直连"
    try:
        parsed = urlsplit(text if "://" in text else f"http://{text}")
    except Exception:
        return text[:80]
    host = (parsed.hostname or "").strip() or "?"
    port = parsed.port
    host_port = f"{host}:{port}" if port else host
    username = ""
    try:
        username = unquote(parsed.username or "")
    except Exception:
        username = str(parsed.username or "")
    region = ""
    sid = ""
    duration = ""
    if username:
        region_match = re.search(r"(?i)-region-([A-Za-z]{2})(?:-|$)", username)
        sid_match = re.search(r"(?i)-sid-([A-Za-z0-9]{8})(?:-|$)", username)
        t_match = re.search(r"(?i)-t-(\d+)(?:-|$|:)", username)
        if region_match:
            region = region_match.group(1).upper()
        if sid_match:
            sid = sid_match.group(1)
        if t_match:
            duration = t_match.group(1)
        # 兼容 query 里的 sid
    if not sid:
        try:
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            sid = str(query.get("sid") or query.get("SID") or "").strip()
        except Exception:
            sid = ""
    parts: list[str] = []
    if opll_proxy_is_loopback(text):
        parts.append(f"本地链 {host_port}")
    else:
        parts.append(f"host={host_port}")
    if region:
        parts.append(f"region={region}")
    if sid:
        parts.append(f"sid={sid}")
    if duration:
        parts.append(f"t={duration}")
    return " ".join(parts)

def opll_format_approve_proxy_fingerprint(
    proxy_url: str,
    *,
    upstream_proxy_url: str = "",
    proxy_exit: str = "",
) -> str:
    """approve 日志用：本地链 + 上游动态代理 + 可选出口探测结果。"""
    request_text = str(proxy_url or "").strip()
    upstream_text = str(upstream_proxy_url or "").strip()
    exit_text = str(proxy_exit or "").strip()
    if not request_text and not upstream_text:
        label = "直连"
    elif opll_proxy_is_loopback(request_text) and upstream_text:
        label = f"{opll_describe_proxy_endpoint(request_text)} → 上游 {opll_describe_proxy_endpoint(upstream_text)}"
    elif upstream_text and upstream_text != request_text and not opll_proxy_is_loopback(upstream_text):
        # 请求走 A、展示上游 B（例如 chain 与 dynamic 不同）
        label = f"{opll_describe_proxy_endpoint(request_text)} | 上游 {opll_describe_proxy_endpoint(upstream_text)}"
    else:
        label = opll_describe_proxy_endpoint(request_text or upstream_text)
    if exit_text:
        label = f"{label} | 出口={exit_text}"
    return label

def normalize_proxy_url(value: str, default_scheme: str = "http") -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"{default_scheme}://{text}"
    return text

def find_access_token(value) -> str:
    if isinstance(value, dict):
        for key in ("accessToken", "access_token", "token"):
            token = str(value.get(key) or "").strip()
            if token:
                return token
        for item in value.values():
            token = find_access_token(item)
            if token:
                return token
    if isinstance(value, list):
        for item in value:
            token = find_access_token(item)
            if token:
                return token
    return ""

def extract_access_token_from_session_text(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    if raw.startswith("Bearer "):
        return raw.split(None, 1)[1].strip()
    try:
        return find_access_token(json.loads(raw))
    except Exception:
        pass
    match = re.search(r'"(?:accessToken|access_token|token)"\s*:\s*"([^"]+)"', raw)
    if match:
        return match.group(1).strip()
    return raw if raw.count(".") >= 2 and len(raw) > 80 else ""

def account_name_from_access_token(token: str) -> str:
    payload = decode_jwt_payload(token)
    if not isinstance(payload, dict):
        return ""

    def first_text(container: dict, keys: tuple[str, ...]) -> str:
        for key in keys:
            value = container.get(key)
            if not isinstance(value, (str, int, float)):
                continue
            text_value = str(value).strip()
            if text_value:
                return re.sub(r"\s+", " ", text_value)
        return ""

    email_keys = (
        "email",
        "account_email",
        "preferred_username",
        "upn",
        "unique_name",
    )
    account_name = first_text(payload, email_keys)
    if account_name:
        return account_name

    # Current ChatGPT access tokens put the login email in this namespaced claim.
    profile = payload.get("https://api.openai.com/profile")
    if isinstance(profile, dict):
        account_name = first_text(profile, email_keys)
        if account_name:
            return account_name

    # Keep compatibility with providers that use a different namespaced profile.
    for value in payload.values():
        if not isinstance(value, dict) or value is profile:
            continue
        account_name = first_text(value, email_keys)
        if account_name:
            return account_name

    account_name = first_text(
        payload,
        ("username", "account_name", "sub", "account_id"),
    )
    if account_name:
        return account_name
    return ""

def decode_jwt_payload(token: str) -> dict:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    try:
        payload = parts[1].replace("-", "+").replace("_", "/")
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.b64decode(payload).decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def currency_for_country(country: str) -> str:
    return COUNTRY_CURRENCY.get(str(country or "").upper(), "USD")

def normalize_opll_country(country: str) -> str:
    country = str(country or "").strip().upper()
    return country if country in OPENAI_SUPPORTED_COUNTRY_CODES else "US"

def opll_extract_processor_entity(data) -> str:
    if not isinstance(data, dict):
        return ""
    direct = data.get("processor_entity") or data.get("processorEntity")
    if direct:
        return str(direct).strip()
    for key in ("checkout_session", "session", "checkout", "data"):
        nested = data.get(key)
        if isinstance(nested, dict):
            found = opll_extract_processor_entity(nested)
            if found:
                return found
    return ""

def opll_extract_checkout_session_id(data) -> str:
    """Extract a ChatGPT/Stripe Checkout ID from common response wrappers."""
    if isinstance(data, dict):
        for key in ("checkout_session_id", "session_id", "id"):
            value = str(data.get(key) or "").strip()
            if opll_is_checkout_session_id(value):
                return value
        for key in ("checkout_session", "checkout", "session", "data", "raw"):
            found = opll_extract_checkout_session_id(data.get(key))
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = opll_extract_checkout_session_id(item)
            if found:
                return found
    return ""

def opll_extract_stripe_payment_page_id(*payloads) -> str:
    """Extract the real Stripe ``cs_`` ID behind an OpenAI ``oaics_`` checkout.

    OpenAI custom Checkout responses can expose their own ``oaics_`` identifier
    together with a Stripe Checkout client secret. Stripe ``payment_pages``
    endpoints accept only the Stripe ``cs_`` portion, never the ``oaics_`` ID.
    """

    preferred_keys = (
        "stripe_checkout_session_id",
        "stripe_session_id",
        "payment_page_id",
        "checkout_session_client_secret",
        "stripe_client_secret",
        "client_secret",
    )
    visited: set[int] = set()

    def normalize(value) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if not text.startswith("cs_"):
            match = re.search(r"(?<![A-Za-z0-9])(cs_(?:test|live)_[A-Za-z0-9]+)", text)
            if not match:
                return ""
            text = match.group(1)
        if "_secret_" in text:
            text = text.split("_secret_", 1)[0]
        return text if text.startswith("cs_") else ""

    def walk(value) -> str:
        if isinstance(value, str):
            return normalize(value)
        if not isinstance(value, (dict, list)):
            return ""
        identity = id(value)
        if identity in visited:
            return ""
        visited.add(identity)
        if isinstance(value, dict):
            for key in preferred_keys:
                if key not in value:
                    continue
                found = walk(value.get(key))
                if found:
                    return found
            for nested in value.values():
                found = walk(nested)
                if found:
                    return found
        else:
            for nested in value:
                found = walk(nested)
                if found:
                    return found
        return ""

    for payload in payloads:
        found = walk(payload)
        if found:
            return found
    return ""

def opll_extract_stripe_publishable_key(data) -> str:
    if isinstance(data, str):
        match = re.search(r"pk_live_[A-Za-z0-9]+", data)
        return match.group(0) if match else ""
    if isinstance(data, dict):
        for key in ("stripe_publishable_key", "publishable_key", "publishableKey", "stripePublishableKey", "key"):
            found = opll_extract_stripe_publishable_key(data.get(key))
            if found:
                return found
        for item in data.values():
            found = opll_extract_stripe_publishable_key(item)
            if found:
                return found
    if isinstance(data, list):
        for item in data:
            found = opll_extract_stripe_publishable_key(item)
            if found:
                return found
    return ""

def opll_processor_entity_for_country(country: str, processor_entity: str = "") -> str:
    entity = str(processor_entity or "").strip()
    if entity:
        return entity
    return "openai_llc" if str(country or "").upper() == "US" else "openai_ie"

CHECKOUT_SESSION_ID_PREFIXES = ("cs_", "oaics_")

def opll_is_checkout_session_id(value: str) -> bool:
    """Return whether *value* is an accepted ChatGPT Checkout Session ID.

    Stripe-backed Checkout responses historically used ``cs_`` IDs.  The
    custom ChatGPT Checkout endpoint can also return ``oaics_`` IDs.  An
    ``oaics_`` value is valid for ChatGPT Checkout routes and update calls,
    but must not be sent to Stripe ``payment_pages`` endpoints.
    """
    return str(value or "").strip().startswith(CHECKOUT_SESSION_ID_PREFIXES)

def opll_chatgpt_success_return_url(cs_id: str, country: str, processor_entity: str = "") -> str:
    entity = opll_processor_entity_for_country(country, processor_entity)
    return f"https://chatgpt.com/checkout/verify?stripe_session_id={cs_id}&processor_entity={entity}&plan_type=plus"

def opll_to_openai_pay_url(stripe_hosted_url: str) -> str:
    url = str(stripe_hosted_url or "").strip()
    if not url:
        return ""
    if url.startswith("https://checkout.stripe.com"):
        return "https://pay.openai.com" + url[len("https://checkout.stripe.com"):]
    parsed = urlsplit(url)
    if parsed.netloc.lower() == "checkout.stripe.com":
        return urlunsplit((parsed.scheme or "https", "pay.openai.com", parsed.path, parsed.query, parsed.fragment))
    return url

def opll_chatgpt_checkout_page_url(
    cs_id: str,
    country: str,
    processor_entity: str = "",
) -> str:
    """Return the ChatGPT custom Checkout route used by the MoMo-capable UI."""
    checkout_id = str(cs_id or "").strip()
    if not opll_is_checkout_session_id(checkout_id):
        return ""
    entity = opll_processor_entity_for_country(country, processor_entity)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", entity):
        return ""
    return f"https://chatgpt.com/checkout/{entity}/{checkout_id}"

def opll_is_chatgpt_checkout_page_url(value: str) -> bool:
    """只接受 ChatGPT 原生 Checkout 短链，不把普通站内 URL 当成支付结果。"""
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
    except Exception:
        return False
    if (
        parsed.scheme.lower() != "https"
        or parsed.netloc.lower() != "chatgpt.com"
        or parsed.username
        or parsed.password
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
    ):
        return False
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 3 or parts[0] != "checkout":
        return False
    entity, checkout_id = parts[1], parts[2]
    return bool(
        re.fullmatch(r"[A-Za-z0-9_-]+", entity)
        and opll_is_checkout_session_id(checkout_id)
    )

def opll_stripe_checkout_long_url(cs_id: str, country: str, processor_entity: str = "") -> str:
    return (
        f"https://checkout.stripe.com/c/pay/{cs_id}"
        f"?returned_from_redirect=true&ui_mode=custom&return_url="
        f"{quote(opll_chatgpt_success_return_url(cs_id, country, processor_entity), safe='')}"
    )

def opll_stripe_confirm_return_url(cs_id: str, checkout: dict, stripe_hosted_url: str) -> str:
    hosted_url = opll_to_openai_pay_url(stripe_hosted_url) or opll_stripe_checkout_long_url(
        cs_id,
        checkout["billing_country"],
        checkout.get("processor_entity", ""),
    )
    if "pay.openai.com/" in hosted_url or "checkout.stripe.com/" in hosted_url:
        parsed = urlsplit(hosted_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query.setdefault(
            "success_return_url",
            opll_chatgpt_success_return_url(
                cs_id,
                checkout["billing_country"],
                checkout.get("processor_entity", ""),
            ),
        )
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
    return hosted_url

def opll_new_http_session(*, fresh_connections: bool = False) -> requests.Session:
    if CurlCffiSession is not None:
        session_options = {"impersonate": OPLL_HTTP_IMPERSONATE}
        if fresh_connections and CurlCffiOpt is not None:
            # Long multi-step payment flows can leave an HTTP/2/TLS connection idle
            # long enough for an upstream proxy to close it.  Keep the session cookie
            # jar and browser identity, but open a fresh transport for every request.
            session_options["curl_options"] = {
                CurlCffiOpt.FRESH_CONNECT: 1,
                CurlCffiOpt.FORBID_REUSE: 1,
            }
        session = CurlCffiSession(**session_options)  # type: ignore[assignment]
    else:
        session = requests.Session()
    if hasattr(session, "trust_env"):
        session.trust_env = False
    return session

class OpllBrowserFetchResponse:
    """Small requests.Response-compatible wrapper for a page-context fetch."""

    def __init__(self, payload: dict):
        self.status_code = int((payload or {}).get("status") or 0)
        self.text = str((payload or {}).get("text") or "")
        self.url = str((payload or {}).get("url") or "")
        raw_headers = (payload or {}).get("headers") or {}
        self.headers: dict[str, str] = {}
        if isinstance(raw_headers, dict):
            for key, value in raw_headers.items():
                text_key = str(key or "")
                text_value = str(value or "")
                self.headers[text_key] = text_value
                self.headers.setdefault(text_key.lower(), text_value)

    def json(self):
        return json.loads(self.text) if self.text else {}

class OpllBrowserFetchSession:
    """Issue HTTP requests from inside a real Camoufox page via window.fetch."""

    _FORBIDDEN_HEADER_NAMES = {
        "accept-encoding",
        "accept-language",
        "connection",
        "content-length",
        "cookie",
        "host",
        "origin",
        "referer",
        "user-agent",
    }

    def __init__(self, page, log=None, label: str = ""):
        self.page = page
        self.log = log
        self.label = str(label or "").strip() or "Camoufox"
        self.headers: dict[str, str] = {}
        self.proxies: dict[str, str] = {}
        self.trust_env = False
        self.opll_oai_device_id = ""

    def close(self) -> None:
        return None

    def _request_headers(self, headers: dict | None = None) -> dict[str, str]:
        merged = {
            str(key): str(value)
            for key, value in self.headers.items()
            if value is not None
        }
        merged.update({
            str(key): str(value)
            for key, value in (headers or {}).items()
            if value is not None
        })
        return {
            key: value
            for key, value in merged.items()
            if key.lower() not in self._FORBIDDEN_HEADER_NAMES
            and not key.lower().startswith(("proxy-", "sec-"))
        }

    def _sync_cookie_header(self) -> None:
        cookie_header = next(
            (
                str(value or "")
                for key, value in self.headers.items()
                if str(key or "").lower() == "cookie"
            ),
            "",
        )
        if not cookie_header or self.page.is_closed():
            return
        cookies = []
        for part in cookie_header.split(";"):
            name, separator, value = part.strip().partition("=")
            if not separator or not name:
                continue
            cookies.append({
                "name": name,
                "value": value,
                "url": "https://chatgpt.com/",
            })
        if cookies:
            self.page.context.add_cookies(cookies)

    @staticmethod
    def _timeout_milliseconds(timeout) -> int:
        if isinstance(timeout, (tuple, list)):
            values = [float(item) for item in timeout if item is not None]
            timeout = max(values) if values else PAY_LONG_LINK_TIMEOUT
        try:
            seconds = float(timeout)
        except Exception:
            seconds = float(PAY_LONG_LINK_TIMEOUT or 30)
        return max(1000, int(seconds * 1000))

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        data=None,
        json_body=None,
        headers: dict | None = None,
        timeout=None,
        allow_redirects: bool = True,
    ) -> OpllBrowserFetchResponse:
        if self.page.is_closed():
            raise RuntimeError(f"{self.label} 页面已关闭，无法发送浏览器内请求")
        request_url = str(url or "").strip()
        if params:
            query = urlencode(params, doseq=True)
            request_url += ("&" if "?" in request_url else "?") + query
        request_headers = self._request_headers(headers)
        body = None
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False, separators=(",", ":"))
            request_headers.setdefault("Content-Type", "application/json")
        elif data is not None:
            if isinstance(data, dict):
                body = urlencode(data, doseq=True)
                request_headers.setdefault(
                    "Content-Type",
                    "application/x-www-form-urlencoded;charset=UTF-8",
                )
            elif isinstance(data, bytes):
                body = data.decode("utf-8", errors="replace")
            else:
                body = str(data)
        self._sync_cookie_header()
        payload = {
            "url": request_url,
            "method": str(method or "GET").upper(),
            "headers": request_headers,
            "body": body,
            "timeoutMs": self._timeout_milliseconds(timeout),
            "redirect": "follow" if allow_redirects else "manual",
        }
        try:
            result = self.page.evaluate(
                """async (request) => {
                    const controller = new AbortController();
                    const timer = setTimeout(() => controller.abort(), request.timeoutMs);
                    try {
                        const options = {
                            method: request.method,
                            headers: request.headers,
                            credentials: 'include',
                            cache: 'no-store',
                            redirect: request.redirect,
                            signal: controller.signal,
                        };
                        if (request.body !== null && request.method !== 'GET' && request.method !== 'HEAD') {
                            options.body = request.body;
                        }
                        const response = await fetch(request.url, options);
                        const responseHeaders = {};
                        response.headers.forEach((value, key) => {
                            responseHeaders[key] = value;
                        });
                        return {
                            status: response.status,
                            text: await response.text(),
                            headers: responseHeaders,
                            url: response.url,
                            type: response.type,
                        };
                    } catch (error) {
                        return {
                            fetchError: String(error && (error.stack || error.message) || error),
                        };
                    } finally {
                        clearTimeout(timer);
                    }
                }""",
                payload,
            )
        except Exception as exc:
            raise RuntimeError(
                f"{self.label} 页面内请求执行失败: {opll_short_error(str(exc), 260)}"
            ) from exc
        if not isinstance(result, dict):
            raise RuntimeError(f"{self.label} 页面内请求没有返回有效结果")
        fetch_error = str(result.get("fetchError") or "").strip()
        if fetch_error:
            raise RuntimeError(
                f"{self.label} 页面内 fetch 失败: {opll_short_error(fetch_error, 260)}"
            )
        return OpllBrowserFetchResponse(result)

    def get(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        timeout=None,
        allow_redirects: bool = True,
        **_kwargs,
    ) -> OpllBrowserFetchResponse:
        return self.request(
            "GET",
            url,
            params=params,
            headers=headers,
            timeout=timeout,
            allow_redirects=allow_redirects,
        )

    def post(
        self,
        url: str,
        *,
        data=None,
        json=None,
        headers: dict | None = None,
        timeout=None,
        allow_redirects: bool = True,
        **_kwargs,
    ) -> OpllBrowserFetchResponse:
        return self.request(
            "POST",
            url,
            data=data,
            json_body=json,
            headers=headers,
            timeout=timeout,
            allow_redirects=allow_redirects,
        )

def opll_chrome_identity_headers() -> dict[str, str]:
    """Chrome identity headers aligned with the curl_cffi TLS impersonation."""
    return {
        "User-Agent": OPLL_USER_AGENT,
        "sec-ch-ua": (
            f'"Google Chrome";v="{OPLL_BROWSER_MAJOR}", '
            f'"Chromium";v="{OPLL_BROWSER_MAJOR}", "Not.A/Brand";v="24"'
        ),
        "sec-ch-ua-full-version-list": (
            f'"Google Chrome";v="{OPLL_BROWSER_FULL_VERSION}", '
            f'"Chromium";v="{OPLL_BROWSER_FULL_VERSION}", "Not.A/Brand";v="24.0.0.0"'
        ),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-platform-version": '"15.0.0"',
    }

def opll_new_oai_device_id() -> str:
    """生成 ChatGPT oai-did / oai-device-id（UUID 字符串）。"""
    return str(uuid.uuid4())

def opll_resolve_oai_device_id(*candidates) -> str:
    """从候选里取第一个非空 device id；都空则新建。

    用途：同一笔 checkout（Create → Update → Approve）固定同一设备指纹，
    避免每次 build session 都换 oai-did 触发风控 blocked。
    """
    for item in candidates:
        if isinstance(item, dict):
            for key in ("oai_device_id", "device_id", "oai-did", "oai_did"):
                text = str(item.get(key) or "").strip()
                if text:
                    return text
            continue
        text = str(item or "").strip()
        if text:
            return text
    return opll_new_oai_device_id()

OPLL_CHATGPT_IDENTITY_COOKIE_NAMES = frozenset({
    "__Secure-next-auth.session-token",
    "__Secure-authjs.session-token",
    "next-auth.session-token",
    "authjs.session-token",
    "oai-did",
    "oai-sc",
})

OPLL_CHATGPT_AUTH_COOKIE_NAMES = frozenset({
    "__Secure-next-auth.session-token",
    "__Secure-authjs.session-token",
    "next-auth.session-token",
    "authjs.session-token",
})

def opll_is_chatgpt_identity_cookie_name(name: str) -> bool:
    """Accept normal and chunked ChatGPT identity cookie names."""
    text = str(name or "").strip()
    return bool(
        text in OPLL_CHATGPT_IDENTITY_COOKIE_NAMES
        or any(text.startswith(f"{base}.") for base in OPLL_CHATGPT_AUTH_COOKIE_NAMES)
    )

def opll_chatgpt_session_context_details(
    session_context: dict | None,
) -> tuple[dict[str, str], str]:
    """提取可跨 API 请求复用的 ChatGPT 登录/设备身份。

    Cloudflare 等边缘 Cookie 可能绑定获取时的出口 IP，因此不会从保存的浏览器
    storage_state 跨代理注入；Sentinel 在当前 HTTP session 新下发的 Cookie 仍由
    cookie jar 自然保留并用于紧随其后的 Approve。
    """
    context = dict(session_context) if isinstance(session_context, dict) else {}
    cookies: dict[str, str] = {}
    device_id = str(
        context.get("device_id")
        or context.get("oai_device_id")
        or ""
    ).strip()

    session_payload = context.get("session_json")
    if isinstance(session_payload, str):
        try:
            session_payload = json.loads(session_payload)
        except Exception:
            session_payload = {}
    if isinstance(session_payload, dict):
        session_token = str(
            context.get("session_token")
            or session_payload.get("sessionToken")
            or session_payload.get("session_token")
            or ""
        ).strip()
    else:
        session_token = str(context.get("session_token") or "").strip()
    if session_token:
        cookies["__Secure-next-auth.session-token"] = session_token

    storage_state = context.get("storage_state_json") or context.get("storage_state")
    if isinstance(storage_state, str):
        try:
            storage_state = json.loads(storage_state)
        except Exception:
            storage_state = {}
    if isinstance(storage_state, dict):
        for cookie in storage_state.get("cookies") or []:
            if not isinstance(cookie, dict):
                continue
            name = str(cookie.get("name") or "").strip()
            value = str(cookie.get("value") or "").strip()
            domain = str(cookie.get("domain") or "").strip().lower().lstrip(".")
            if (
                not opll_is_chatgpt_identity_cookie_name(name)
                or not value
                or (
                    domain
                    and domain not in {"chatgpt.com", "openai.com"}
                    and not domain.endswith(".chatgpt.com")
                )
            ):
                continue
            # 兼容旧协议记录中误写为 .openai.com 的 oai-did；其它 cookie 不跨域。
            if domain == "openai.com" and name != "oai-did":
                continue
            cookies[name] = value
            if name == "oai-did" and not device_id:
                device_id = value

    if device_id:
        cookies["oai-did"] = device_id
    return cookies, device_id

def opll_build_chatgpt_session(
    access_token: str,
    proxy_url: str = "",
    request_locale: str = "en-US",
    *,
    device_id: str = "",
    session: requests.Session | OpllBrowserFetchSession | None = None,
    session_context: dict | None = None,
) -> requests.Session:
    token = extract_access_token_from_session_text(access_token) or str(access_token or "").strip()
    if not token:
        raise RuntimeError("当前账户没有 Access Token，请先注册并获取 Session 信息")
    identity_cookies, context_device_id = opll_chatgpt_session_context_details(
        session_context
    )
    # 优先复用已登录浏览器/协议会话的设备 ID；整笔 checkout 再固定此值。
    resolved_device_id = opll_resolve_oai_device_id(device_id, context_device_id)
    identity_cookies["oai-did"] = resolved_device_id
    accept_language, oai_language = opll_accept_language_for_locale(request_locale)
    session = session or opll_new_http_session()
    session.headers.update({
        **opll_chrome_identity_headers(),
        "Accept": "*/*",
        "Accept-Language": accept_language,
        "Authorization": f"Bearer {token}",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "Content-Type": "application/json",
        "oai-device-id": resolved_device_id,
        "oai-language": oai_language,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    })
    cookie_jar = getattr(session, "cookies", None)
    if cookie_jar is None:
        cookie_jar = requests.cookies.RequestsCookieJar()
        session.cookies = cookie_jar  # type: ignore[attr-defined]
    for name, value in identity_cookies.items():
        cookie_jar.set(name, value, domain="chatgpt.com", path="/")
    # 供调用方读取本 session 实际使用的 device id（尤其是未传入时的新生成值）
    session.opll_oai_device_id = resolved_device_id  # type: ignore[attr-defined]
    session.opll_auth_cookie_names = tuple(sorted(identity_cookies))  # type: ignore[attr-defined]
    if proxy_url:
        session.proxies.update({"http": proxy_url, "https": proxy_url})
    return session

def opll_is_non_retryable_link_error(exc: Exception | str) -> bool:
    explicit_retryable = getattr(exc, "retryable", None)
    if explicit_retryable is True:
        return False
    if isinstance(exc, AmountMismatchError):
        return True
    if explicit_retryable is False:
        return True
    text = str(exc or "").lower()
    non_retryable_markers = (
        "billing country must match request country",
        "当前 stripe checkout 不支持 ",
        "confirm_error_reason=payment_method_types_mismatch",
        "token_invalidated",
        "authentication token has been invalidated",
        "token 验证失败",
        "access token 为空",
        "user is already paid",
    )
    return any(marker in text for marker in non_retryable_markers)

def opll_validate_access_token(
    access_token: str,
    proxy_url: str = "",
    request_locale: str = "en-US",
    *,
    session: requests.Session | OpllBrowserFetchSession | None = None,
    chatgpt_session: requests.Session | None = None,
    device_id: str = "",
    session_context: dict | None = None,
) -> dict:
    if not str(access_token or "").strip():
        raise RuntimeError("Access Token 为空")
    session = chatgpt_session or opll_build_chatgpt_session(
        access_token,
        proxy_url,
        request_locale=request_locale,
        device_id=device_id,
        **({"session": session} if session is not None else {}),
        **({"session_context": session_context} if session_context else {}),
    )
    response = session.get(
        "https://chatgpt.com/backend-api/me",
        headers={
            "Referer": "https://chatgpt.com/",
            "x-openai-target-path": "/backend-api/me",
            "x-openai-target-route": "/backend-api/me",
        },
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Token 验证失败: HTTP {response.status_code}")
    try:
        data = response.json() or {}
    except Exception as exc:
        raise RuntimeError("Token 验证响应不是有效 JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Token 验证响应格式无效")
    return data

def opll_create_checkout(
    access_token: str,
    country: str,
    currency: str,
    proxy_url: str = "",
    *,
    request_locale: str = "en-US",
    include_trial_promo: bool = True,
    checkout_ui_mode: str = "custom",
    cancel_url: str = "",
    device_id: str = "",
    session: requests.Session | OpllBrowserFetchSession | None = None,
    session_context: dict | None = None,
    chatgpt_session: requests.Session | None = None,
    return_raw_payload: bool = False,
    request_timeout: float = PAY_LONG_LINK_TIMEOUT,
    sentinel_so_enabled: bool = False,
    sentinel_header_provider=None,
    diagnostic_log=None,
) -> dict:
    country = normalize_opll_country(country)
    currency = currency_for_country(country)
    # 本笔 checkout 的设备指纹：Create 生成一次，后续 Update/Approve 原样复用
    _identity_cookies, context_device_id = opll_chatgpt_session_context_details(
        session_context
    )
    session_device_id = str(
        getattr(chatgpt_session, "opll_oai_device_id", "") or ""
    ).strip()
    oai_device_id = opll_resolve_oai_device_id(
        device_id,
        session_device_id,
        context_device_id,
    )
    session = chatgpt_session or opll_build_chatgpt_session(
        access_token,
        proxy_url,
        request_locale=request_locale,
        device_id=oai_device_id,
        **({"session": session} if session is not None else {}),
        **({"session_context": session_context} if session_context else {}),
    )
    payload = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": country, "currency": currency},
        "checkout_ui_mode": str(checkout_ui_mode or "custom").strip() or "custom",
    }
    if str(cancel_url or "").strip():
        payload["cancel_url"] = str(cancel_url).strip()
    if include_trial_promo:
        payload["promo_campaign"] = {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        }
    checkout_headers = {
        "Referer": "https://chatgpt.com/",
        "x-openai-target-path": "/backend-api/payments/checkout",
        "x-openai-target-route": "/backend-api/payments/checkout",
    }
    if sentinel_so_enabled:
        provider = sentinel_header_provider or opll_paypal_checkout_sentinel_headers
        checkout_headers.update(
            provider(proxy_url, oai_device_id, request_locale)
        )
        opll_emit_diagnostic(
            diagnostic_log,
            "[PayPal] Checkout 已生成并提交 SEN + SO 双令牌",
        )
    response = session.post(
        "https://chatgpt.com/backend-api/payments/checkout",
        json=payload,
        headers=checkout_headers,
        timeout=max(1.0, float(request_timeout or PAY_LONG_LINK_TIMEOUT)),
    )
    if response.status_code >= 400:
        raise RuntimeError(f"checkout create failed: HTTP {response.status_code} {opll_complete_response_body(response)}")
    data = response.json() or {}
    cs_id = data.get("checkout_session_id") or data.get("session_id") or data.get("id")
    if not opll_is_checkout_session_id(cs_id):
        raise RuntimeError(f"checkout response missing cs_id: {str(data)[:500]}")
    checkout = {
        "cs_id": str(cs_id),
        "processor_entity": opll_extract_processor_entity(data),
        "stripe_publishable_key": opll_extract_stripe_publishable_key(data),
        "billing_country": country,
        "currency": currency,
        "oai_device_id": oai_device_id,
    }
    if return_raw_payload:
        checkout["_checkout_payload"] = data
    return checkout

def opll_stripe_key_for_checkout(checkout: dict | None = None) -> str:
    return str((checkout or {}).get("stripe_publishable_key") or "").strip() or DEFAULT_STRIPE_PK

def opll_stripe_init(
    cs_id: str,
    country: str,
    currency: str,
    proxy_url: str = "",
    payment_locale: str = "",
    stripe: requests.Session | None = None,
    ctx: dict | None = None,
    checkout: dict | None = None,
    browser_timezone: str = "",
    stripe_version: str = "",
    redirect_type: str = "",
    eid: str = "",
    saved_payment_method_mode: str = "never",
) -> dict:
    cs_id = str(cs_id or "").strip()
    if not cs_id.startswith("cs_"):
        raise RuntimeError(
            "stripe init requires a Stripe cs_ payment_page ID; "
            f"received {cs_id[:24] or '<missing>'}"
        )
    locale_ctx = opll_locale_context_for_country(country)
    resolved_payment_locale = str(payment_locale or "").strip() or locale_ctx["payment_locale"]
    resolved_timezone = str(browser_timezone or "").strip() or locale_ctx["browser_timezone"]
    browser_locale, elements_locale = locale_parts(resolved_payment_locale)
    stripe_pk = opll_stripe_key_for_checkout(checkout)
    stripe_session = stripe or opll_new_http_session()
    if stripe is None:
        accept_language, _oai = opll_accept_language_for_locale(browser_locale)
        stripe_session.headers.update({
            **opll_chrome_identity_headers(),
            "Accept-Language": accept_language,
        })
        if proxy_url:
            stripe_session.proxies.update({"http": proxy_url, "https": proxy_url})
    saved_mode = str(saved_payment_method_mode or "never").strip().lower() or "never"
    request_data = {
        "browser_locale": browser_locale,
        "browser_timezone": resolved_timezone,
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": str((ctx or {}).get("stripe_js_id") or uuid.uuid4()),
        "elements_session_client[locale]": elements_locale,
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": saved_mode,
        "elements_options_client[saved_payment_method][enable_redisplay]": saved_mode,
        "key": stripe_pk,
        "_stripe_version": str(stripe_version or STRIPE_VERSION_FULL),
    }
    if str(redirect_type or "").strip():
        request_data["redirect_type"] = str(redirect_type).strip()
    if str(eid or "").strip():
        request_data["eid"] = str(eid).strip()
    response = stripe_session.post(
        f"https://api.stripe.com/v1/payment_pages/{cs_id}/init",
        data=request_data,
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"stripe init failed: HTTP {response.status_code} {opll_complete_response_body(response)}")
    return response.json() or {}

def opll_build_stripe_session(
    proxy_url: str = "",
    request_locale: str = "en-US",
    *,
    fresh_connections: bool = False,
) -> requests.Session:
    session = (
        opll_new_http_session(fresh_connections=True)
        if fresh_connections
        else opll_new_http_session()
    )
    accept_language, _oai = opll_accept_language_for_locale(request_locale)
    session.headers.update({
        **opll_chrome_identity_headers(),
        "Accept-Language": accept_language,
    })
    if proxy_url:
        session.proxies.update({"http": proxy_url, "https": proxy_url})
    return session

def opll_stripe_context(init_payload: dict, payment_locale: str = "en", ctx: dict | None = None) -> dict:
    """从 Init 响应构建 Stripe 上下文；尽量复用已有 stripe_js_id / elements_session_id。"""
    _browser_locale, elements_locale = locale_parts(payment_locale)
    base = ctx or {}
    payload = init_payload if isinstance(init_payload, dict) else {}
    # 客户端指纹优先沿用上一跳 ctx，避免二次 Init 换新会话；服务端若回传 id 也可补上
    stripe_js_id = str(
        base.get("stripe_js_id")
        or payload.get("stripe_js_id")
        or uuid.uuid4()
    )
    elements_session_id = str(
        base.get("elements_session_id")
        or payload.get("elements_session_id")
        or f"elements_session_{uuid.uuid4().hex[:11]}"
    )
    # Stripe.js keeps these browser identifiers stable from Init through
    # PaymentMethod, Confirm, and Payment Page polling.  Generating fresh
    # values at Confirm time breaks that continuity and can make ChatGPT's
    # manual approval endpoint reject an otherwise valid submission.
    guid = str(base.get("guid") or payload.get("guid") or uuid.uuid4().hex)
    muid = str(base.get("muid") or payload.get("muid") or uuid.uuid4().hex)
    sid = str(base.get("sid") or payload.get("sid") or uuid.uuid4().hex)
    return {
        "guid": guid,
        "muid": muid,
        "sid": sid,
        "stripe_js_id": stripe_js_id,
        "elements_session_id": elements_session_id,
        "elements_session_config_id": str(
            payload.get("config_id")
            or base.get("elements_session_config_id")
            or uuid.uuid4()
        ),
        "config_id": str(payload.get("config_id") or base.get("config_id") or ""),
        "init_checksum": str(payload.get("init_checksum") or base.get("init_checksum") or ""),
        "checkout_amount": str(opll_expected_amount(payload)),
        "currency": str(payload.get("currency") or base.get("currency") or "").lower(),
        "locale": elements_locale,
        "runtime_version": str(base.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION),
        "stripe_version": str(base.get("stripe_version") or STRIPE_VERSION_FULL),
    }

def opll_expected_amount(init_payload: dict) -> str:
    return opll_stripe_amount_info(init_payload)[0]

def opll_stripe_amount_info(init_payload) -> tuple[str, str]:
    if not isinstance(init_payload, dict):
        return "", "missing_payload"
    total_summary = init_payload.get("total_summary") if isinstance(init_payload, dict) else None
    if isinstance(total_summary, dict) and total_summary.get("due") is not None:
        return str(total_summary.get("due")), "total_summary.due"
    invoice = init_payload.get("invoice") if isinstance(init_payload, dict) else None
    if isinstance(invoice, dict) and invoice.get("amount_due") is not None:
        return str(invoice.get("amount_due")), "invoice.amount_due"
    line_items = init_payload.get("line_items") if isinstance(init_payload, dict) else None
    if isinstance(line_items, list):
        total = 0
        found = False
        for item in line_items:
            if not isinstance(item, dict) or "amount" not in item:
                return "", "invalid_line_items"
            found = True
            amount = item.get("amount")
            if isinstance(amount, bool):
                return "", "invalid_line_items"
            if isinstance(amount, int):
                parsed_amount = amount
            elif isinstance(amount, str) and re.fullmatch(r"[+-]?\d+", amount.strip()):
                parsed_amount = int(amount.strip())
            else:
                return "", "invalid_line_items"
            total += parsed_amount
        if found:
            return str(total), "line_items.amount"
    elif line_items is not None:
        return "", "invalid_line_items"
    return "", "fallback_zero"

def opll_chatgpt_checkout_amount_info(payload) -> tuple[str, str]:
    """Read minor-unit Checkout amounts from ChatGPT create/update/page payloads."""

    wrapper_keys = (
        "checkout_session",
        "checkoutSession",
        "session",
        "checkout",
        "data",
        "result",
        "payload",
        "response",
        "checkout_state",
        "checkoutState",
        "checkout_snapshot",
        "checkoutSnapshot",
    )
    amount_paths = (
        ("checkout_amount_minor",),
        ("total_summary", "due"),
        ("totalSummary", "due"),
        ("invoice", "amount_due"),
        ("invoice", "amountDue"),
        ("amount_due",),
        ("amountDue",),
        ("amount_total",),
        ("amountTotal",),
        ("total", "total"),
        ("total", "due"),
        ("total", "taxInclusive"),
        ("total", "taxInclusiveAmount"),
    )
    visited: set[int] = set()

    def minor_units(value) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, dict):
            for key in ("minorUnitsAmount", "minor_units_amount", "amount"):
                if value.get(key) is not None:
                    return minor_units(value.get(key))
            return None
        if isinstance(value, int):
            return value
        text = str(value or "").strip()
        return int(text) if re.fullmatch(r"-?\d+", text) else None

    def nested_value(value, path: tuple[str, ...]):
        current = value
        for key in path:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current

    def find(value, prefix: str = "") -> tuple[str, str]:
        if not isinstance(value, dict) or id(value) in visited:
            return "", "missing_payload"
        visited.add(id(value))
        for path in amount_paths:
            amount = minor_units(nested_value(value, path))
            if amount is not None:
                source = ".".join(path)
                return str(amount), f"{prefix}.{source}" if prefix else source
        line_items = value.get("lineItems") or value.get("line_items")
        if isinstance(line_items, list):
            total = 0
            found = False
            for item in line_items:
                if not isinstance(item, dict):
                    continue
                for key in ("total", "subtotal", "unitAmount", "unit_amount", "amount"):
                    amount = minor_units(item.get(key))
                    if amount is not None:
                        total += amount
                        found = True
                        break
            if found:
                source = "lineItems" if "lineItems" in value else "line_items"
                return str(total), f"{prefix}.{source}" if prefix else source
        for key in wrapper_keys:
            amount, source = find(value.get(key), f"{prefix}.{key}" if prefix else key)
            if amount:
                return amount, source
        return "", "missing_payload"

    return find(payload)

def opll_chatgpt_checkout_currency(payload) -> str:
    """Return the first declared three-letter currency in a ChatGPT payload."""

    wrapper_keys = (
        "checkout_state",
        "checkoutState",
        "checkout_snapshot",
        "checkoutSnapshot",
        "checkout_session",
        "checkoutSession",
        "session",
        "checkout",
        "data",
        "result",
        "payload",
        "response",
        "total",
        "total_summary",
        "totalSummary",
        "invoice",
    )
    visited: set[int] = set()

    def find(value) -> str:
        if not isinstance(value, dict) or id(value) in visited:
            return ""
        visited.add(id(value))
        for key in ("currency", "currency_code", "currencyCode"):
            currency = str(value.get(key) or "").strip().upper()
            if re.fullmatch(r"[A-Z]{3}", currency):
                return currency
        for key in wrapper_keys:
            currency = find(value.get(key))
            if currency:
                return currency
        return ""

    return find(payload)

def opll_payment_method_types(init_payload) -> list[str]:
    if not isinstance(init_payload, dict):
        return []
    raw = init_payload.get("payment_method_types")
    if not isinstance(raw, list):
        return []
    return [str(item).strip().lower() for item in raw if str(item).strip()]

CHECKOUT_PAYMENT_METHOD_CONTAINER_KEYS = {
    "custom_payment_methods",
    "custom_payment_method_types",
    "external_payment_methods",
    "external_payment_method_types",
    "payment_methods",
    "payment_method_types",
}

CHECKOUT_PAYMENT_METHOD_VALUE_KEYS = {
    "code",
    "display_label",
    "display_name",
    "external_payment_method_type",
    "label",
    "localized_name",
    "name",
    "payment_method_type",
    "title",
    "type",
}

CHECKOUT_PAYMENT_METHOD_ALIASES = {
    "card": {"card", "cards", "creditcard", "debitcard"},
    "gcash": {"gcash", "gcashpay", "externalgcash", "customgcash"},
}

def opll_checkout_declares_payment_method(payload, expected: str) -> bool:
    """Return whether Checkout explicitly declares a requested payment method."""
    expected_key = re.sub(r"[^a-z0-9]+", "", str(expected or "").strip().lower())
    aliases = CHECKOUT_PAYMENT_METHOD_ALIASES.get(expected_key, {expected_key})
    visited: set[int] = set()

    def matches(value) -> bool:
        normalized = re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())
        return normalized in aliases

    def walk(value, *, in_method_container: bool = False) -> bool:
        if isinstance(value, (dict, list)):
            value_id = id(value)
            if value_id in visited:
                return False
            visited.add(value_id)
        if isinstance(value, dict):
            for key, nested in value.items():
                normalized_key = str(key or "").strip().lower()
                child_is_method_container = (
                    in_method_container
                    or normalized_key in CHECKOUT_PAYMENT_METHOD_CONTAINER_KEYS
                )
                if child_is_method_container and matches(key):
                    return True
                if (
                    child_is_method_container
                    and normalized_key in CHECKOUT_PAYMENT_METHOD_VALUE_KEYS
                    and matches(nested)
                ):
                    return True
                if walk(nested, in_method_container=child_is_method_container):
                    return True
        elif isinstance(value, list):
            for item in value:
                if walk(item, in_method_container=in_method_container):
                    return True
        elif in_method_container and matches(value):
            return True
        return False

    return bool(expected_key) and walk(payload)

def opll_checkout_declared_payment_method_types(payload) -> list[str]:
    """Collect the methods needed to validate PH_SHORT Checkout parity."""
    methods = list(opll_payment_method_types(payload))
    for expected in ("card", "gcash", "paypal"):
        if expected not in methods and opll_checkout_declares_payment_method(payload, expected):
            methods.append(expected)
    return methods

def opll_emit_diagnostic(log, message: str) -> None:
    if not callable(log):
        return
    try:
        log(message)
    except Exception:
        pass

def opll_stripe_payment_page_request_data(stripe_pk: str, ctx: dict) -> dict:
    return {
        "elements_session_client[session_id]": str(ctx.get("elements_session_id") or ""),
        "elements_session_client[locale]": str(ctx.get("locale") or "en"),
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[stripe_js_id]": str(ctx.get("stripe_js_id") or ""),
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "key": str(stripe_pk or "").strip() or DEFAULT_STRIPE_PK,
        "_stripe_version": str(ctx.get("stripe_version") or STRIPE_VERSION_FULL),
    }

def opll_merge_payment_page_payload(payload: dict) -> dict:
    """合并 Stripe 顶层 envelope 与内层 payment_page，顶层状态/next_action 优先。"""
    if not isinstance(payload, dict):
        return {}
    payment_page = payload.get("payment_page")
    if not isinstance(payment_page, dict) or not payment_page:
        return payload
    merged = dict(payment_page)
    for key, value in payload.items():
        if key == "payment_page":
            continue
        if key not in merged or merged.get(key) in (None, "", {}, []):
            merged[key] = value
        elif key in {"next_action", "submission_attempt", "setup_intent", "payment_intent"}:
            merged[key] = value
    return merged

def opll_stripe_payment_page_response(
    response,
    prefix: str,
    *,
    preserve_envelope: bool = False,
) -> dict:
    try:
        payload = response.json() or {}
    except Exception as exc:
        if response.status_code >= 400:
            raise RuntimeError(f"{prefix}: HTTP {response.status_code}") from exc
        raise RuntimeError(f"{prefix}: invalid JSON response") from exc
    if not isinstance(payload, dict):
        if response.status_code >= 400:
            raise RuntimeError(f"{prefix}: HTTP {response.status_code}")
        raise RuntimeError(f"{prefix}: invalid response type={type(payload).__name__}")
    error = payload.get("error")
    if response.status_code >= 400:
        if isinstance(error, dict):
            code = opll_short_error(str(error.get("code") or error.get("type") or "unknown"), 120)
            raise RuntimeError(f"{prefix}: HTTP {response.status_code}, code={code}")
        raise RuntimeError(f"{prefix}: HTTP {response.status_code}")
    if isinstance(error, dict):
        code = opll_short_error(str(error.get("code") or error.get("type") or "unknown"), 120)
        raise RuntimeError(f"{prefix}: code={code}")
    if preserve_envelope:
        return opll_merge_payment_page_payload(payload)
    payment_page = payload.get("payment_page")
    return payment_page if isinstance(payment_page, dict) else payload

class OpllPromotionUpdateError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 0,
        error_code: str = "",
        category: str = "",
        retryable: bool = False,
    ):
        self.status_code = int(status_code or 0)
        self.error_code = str(error_code or "").strip()
        self.category = str(category or "").strip()
        self.retryable = bool(retryable)
        super().__init__(message)

def opll_classify_promotion_update_error(
    status_code: int,
    payload,
    response_text: str = "",
    headers: dict | None = None,
) -> tuple[str, bool]:
    """区分可换 IP 的边缘拒绝与换 IP 无法解决的活动/账户业务拒绝。"""
    try:
        payload_text = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        payload_text = str(payload or "")
    evidence = f"{payload_text} {str(response_text or '')[:2000]}".lower()
    business_markers = (
        "not_eligible", "not eligible", "ineligible", "eligibility",
        "campaign_expired", "promotion_expired", "promo_expired",
        "already_redeemed", "already redeemed", "already_applied",
        "invalid_promo", "invalid_campaign", "promotion_not_available",
        "campaign_not_available", "user is already paid", "already_paid",
        "checkout_session_expired", "invalid_checkout", "token_invalidated",
    )
    if any(marker in evidence for marker in business_markers):
        return "promotion_or_account_ineligible", False
    if int(status_code or 0) in {401, 404}:
        return "authentication_or_checkout_invalid", False
    if int(status_code or 0) in {408, 409, 425, 429} or int(status_code or 0) >= 500:
        return "transient_http", True
    if int(status_code or 0) == 403:
        response_headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        edge_markers = (
            "cloudflare", "cf-ray", "access denied", "request blocked",
            "ip blocked", "geo_blocked", "country_not_supported",
        )
        if "cf-ray" in response_headers or any(marker in evidence for marker in edge_markers):
            return "edge_or_ip_blocked", True
        return "forbidden_unknown", True
    return "business_rejection", False

def opll_should_update_checkout_promotion(
    *,
    apply_trial_promotion: bool,
    checkout_includes_trial_promo: bool,
    target_amount: str,
    actual_amount: str,
) -> bool:
    """Apply at most one same-Checkout fallback when Create did not reach target."""

    if not apply_trial_promotion:
        return False
    expected = str(target_amount or "").strip()
    actual = str(actual_amount or "").strip()
    if checkout_includes_trial_promo and expected and actual == expected:
        return False
    return not expected or actual != expected


def opll_chatgpt_update_checkout_promotion(
    access_token: str,
    checkout: dict,
    proxy_url: str = "",
    *,
    request_locale: str = "en-US",
    device_id: str = "",
    session: requests.Session | OpllBrowserFetchSession | None = None,
    session_context: dict | None = None,
    include_checkout_context: bool = False,
) -> dict:
    cs_id = str(checkout.get("cs_id") or "").strip()
    if not opll_is_checkout_session_id(cs_id):
        raise RuntimeError("Checkout 活动更新缺少有效 Checkout Session ID")
    processor_entity = opll_processor_entity_for_country(
        str(checkout.get("billing_country") or "BR"),
        str(checkout.get("processor_entity") or ""),
    )
    request_path = "/backend-api/payments/checkout/update"
    # 优先显式 device_id，否则复用 Create 写进 checkout 的 oai_device_id
    oai_device_id = opll_resolve_oai_device_id(device_id, checkout)
    session = opll_build_chatgpt_session(
        access_token,
        proxy_url,
        request_locale=request_locale,
        device_id=oai_device_id,
        **({"session": session} if session is not None else {}),
        **({"session_context": session_context} if session_context else {}),
    )
    request_payload = {
        "checkout_session_id": cs_id,
        "processor_entity": processor_entity,
        "plan_name": "chatgptplusplan",
        "price_interval": "month",
        "seat_quantity": 1,
        "promo_campaign": {
            "promo_campaign_id": PIX_TRIAL_PROMOTION_ID,
            "is_coupon_from_query_param": False,
        },
    }
    if include_checkout_context:
        request_payload.update({
            "billing_details": {
                "country": str(checkout.get("billing_country") or "PH").strip().upper(),
                "currency": str(checkout.get("currency") or "PHP").strip().upper(),
            },
            "checkout_ui_mode": "custom",
        })
    request_headers = {
        "Referer": f"https://chatgpt.com/checkout/{processor_entity}/{cs_id}",
        "x-openai-target-path": request_path,
        "x-openai-target-route": request_path,
    }
    if include_checkout_context:
        request_headers["Origin"] = "https://chatgpt.com"
    response = session.post(
        f"https://chatgpt.com{request_path}",
        json=request_payload,
        headers=request_headers,
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    payload = None
    try:
        payload = response.json() or {}
    except Exception as exc:
        if response.status_code >= 400:
            category, retryable = opll_classify_promotion_update_error(
                response.status_code,
                None,
                str(getattr(response, "text", "") or ""),
                getattr(response, "headers", {}) or {},
            )
            request_id = str(
                (getattr(response, "headers", {}) or {}).get("x-request-id")
                or (getattr(response, "headers", {}) or {}).get("openai-request-id")
                or (getattr(response, "headers", {}) or {}).get("cf-ray")
                or ""
            ).strip()
            raise OpllPromotionUpdateError(
                f"{PIX_TRIAL_PROMOTION_ID} 活动更新失败: HTTP {response.status_code}, "
                f"category={category}, retryable={'yes' if retryable else 'no'}"
                + (f", request_id={opll_short_error(request_id, 120)}" if request_id else ""),
                status_code=response.status_code,
                category=category,
                retryable=retryable,
            ) from exc
        raise RuntimeError(f"{PIX_TRIAL_PROMOTION_ID} 活动更新响应不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{PIX_TRIAL_PROMOTION_ID} 活动更新响应格式无效")
    error = payload.get("error")
    detail = payload.get("detail")
    error_data = error if isinstance(error, dict) else (detail if isinstance(detail, dict) else payload)
    error_code = opll_short_error(
        str(error_data.get("code") or error_data.get("error_code") or error_data.get("type") or ""),
        120,
    )
    if response.status_code >= 400:
        code_text = f", code={error_code}" if error_code else ""
        category, retryable = opll_classify_promotion_update_error(
            response.status_code,
            payload,
            str(getattr(response, "text", "") or ""),
            getattr(response, "headers", {}) or {},
        )
        request_id = str(
            (getattr(response, "headers", {}) or {}).get("x-request-id")
            or (getattr(response, "headers", {}) or {}).get("openai-request-id")
            or (getattr(response, "headers", {}) or {}).get("cf-ray")
            or ""
        ).strip()
        raise OpllPromotionUpdateError(
            f"{PIX_TRIAL_PROMOTION_ID} 活动更新失败: HTTP {response.status_code}{code_text}, "
            f"category={category}, retryable={'yes' if retryable else 'no'}"
            + (f", request_id={opll_short_error(request_id, 120)}" if request_id else ""),
            status_code=response.status_code,
            error_code=error_code,
            category=category,
            retryable=retryable,
        )
    if isinstance(error, dict):
        raise RuntimeError(
            f"{PIX_TRIAL_PROMOTION_ID} 活动更新失败"
            + (f": code={error_code}" if error_code else "")
        )
    return payload

def opll_stripe_retrieve_payment_page(
    stripe: requests.Session,
    cs_id: str,
    stripe_pk: str,
    ctx: dict,
    *,
    preserve_envelope: bool = False,
) -> dict:
    response = stripe.get(
        f"https://api.stripe.com/v1/payment_pages/{cs_id}",
        params=opll_stripe_payment_page_request_data(stripe_pk, ctx),
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    return opll_stripe_payment_page_response(
        response,
        "优惠后的 Payment Page 读取失败",
        preserve_envelope=preserve_envelope,
    )

def opll_pix_payload_has_promotion_id(payload, promotion_id: str = PIX_TRIAL_PROMOTION_ID) -> bool:
    expected = str(promotion_id or "").strip().lower()
    if not expected:
        return False

    def walk(value, promotion_scope: bool = False) -> bool:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized_key = str(key or "").strip().lower()
                scoped = promotion_scope or any(word in normalized_key for word in ("promo", "discount", "coupon"))
                if scoped and isinstance(item, (str, int, float)) and str(item).strip().lower() == expected:
                    return True
                if walk(item, scoped):
                    return True
        elif isinstance(value, list):
            return any(walk(item, promotion_scope) for item in value)
        return False

    return walk(payload)

def opll_pix_discount_amounts(payload) -> list[int]:
    amounts: list[int] = []

    def number(value) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value or "").strip()
        return int(text) if re.fullmatch(r"-?\d+", text) else None

    def walk(value, discount_scope: bool = False) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized_key = str(key or "").strip().lower()
                scoped = discount_scope or "discount" in normalized_key
                if scoped and normalized_key in {"amount", "value", "discount", "discount_amount", "amount_discount"}:
                    found = number(item)
                    if found is not None:
                        amounts.append(found)
                walk(item, scoped)
        elif isinstance(value, list):
            for item in value:
                walk(item, discount_scope)

    walk(payload)
    return amounts

def opll_pix_promotion_applied(before_payload, after_payload) -> bool:
    if opll_pix_payload_has_promotion_id(after_payload):
        return True
    before_amount, before_source = opll_stripe_amount_info(before_payload)
    after_amount, after_source = opll_stripe_amount_info(after_payload)
    if before_source == "fallback_zero" or after_source == "fallback_zero":
        return False
    try:
        amount_reduced = int(after_amount) < int(before_amount)
    except (TypeError, ValueError):
        return False
    return amount_reduced and any(amount > 0 for amount in opll_pix_discount_amounts(after_payload))

def opll_strict_zero_promotion_applied(before_payload, after_payload) -> bool:
    if opll_pix_promotion_applied(before_payload, after_payload):
        return True
    before_amount, before_source = opll_stripe_amount_info(before_payload)
    after_amount, after_source = opll_stripe_amount_info(after_payload)
    invalid_sources = {"fallback_zero", "missing_payload", "invalid_line_items"}
    if before_source in invalid_sources or after_source in invalid_sources:
        return False
    try:
        return int(before_amount) > 0 and int(after_amount) == 0
    except (TypeError, ValueError):
        return False

def opll_promotion_proof_diagnostics(before_payload, after_payload) -> str:
    before_amount, before_source = opll_stripe_amount_info(before_payload)
    after_amount, after_source = opll_stripe_amount_info(after_payload)
    methods = ",".join(opll_payment_method_types(after_payload)) or "<missing>"
    return (
        f"初始金额={before_amount or '<missing>'}({before_source}), "
        f"更新后金额={after_amount or '<missing>'}({after_source}), "
        f"payment_method_types={methods}"
    )

def opll_apply_checkout_trial_promotion(
    stripe: requests.Session,
    cs_id: str,
    stripe_pk: str,
    before_payload: dict,
    ctx: dict,
    *,
    access_token: str,
    checkout: dict,
    chatgpt_proxy_url: str = "",
    request_locale: str = "en-US",
    allow_pending: bool = False,
    chatgpt_session: requests.Session | OpllBrowserFetchSession | None = None,
    session_context: dict | None = None,
) -> dict:
    update_payload = opll_chatgpt_update_checkout_promotion(
        access_token,
        checkout,
        chatgpt_proxy_url,
        request_locale=request_locale,
        **({"session": chatgpt_session} if chatgpt_session is not None else {}),
        **({"session_context": session_context} if session_context else {}),
    )
    payment_page = opll_stripe_retrieve_payment_page(stripe, cs_id, stripe_pk, ctx)
    promotion_applied = bool(
        opll_pix_promotion_applied(before_payload, update_payload)
        or opll_pix_promotion_applied(before_payload, payment_page)
    )
    if not promotion_applied and not allow_pending:
        raise RuntimeError("活动更新响应未证明优惠已生效")
    amount, source = opll_stripe_amount_info(payment_page)
    return {
        "promotion_id": PIX_TRIAL_PROMOTION_ID,
        "promotion_applied": promotion_applied,
        "payment_page": payment_page,
        "amount": amount,
        "amount_source": source,
    }

def opll_apply_pix_trial_promotion(*args, **kwargs) -> dict:
    return opll_apply_checkout_trial_promotion(*args, **kwargs)

def opll_wait_for_us_tr_promoted_payment_page(
    stripe: requests.Session,
    cs_id: str,
    stripe_pk: str,
    ctx: dict,
    initial_payment_page: dict,
    *,
    before_payload: dict | None = None,
    accept_strict_zero_amount_drop: bool = False,
    promotion_already_proven: bool = False,
    required_amount: str = "",
    required_payment_method_type: str = "paypal",
    attempts: int = 6,
    interval_seconds: float = 1.0,
) -> dict:
    latest = initial_payment_page if isinstance(initial_payment_page, dict) else {}
    last_ready_signature = None
    stable_ready_count = 0
    total_attempts = max(1, int(attempts or 1))
    expected_payment_method = str(required_payment_method_type or "").strip().lower()
    for attempt in range(total_attempts):
        amount, amount_source = opll_stripe_amount_info(latest)
        methods = opll_payment_method_types(latest)
        amount_known = bool(amount) and amount_source not in {
            "fallback_zero",
            "missing_payload",
            "invalid_line_items",
        }
        promotion_proven = bool(isinstance(before_payload, dict) and (
            opll_pix_promotion_applied(before_payload, latest)
            or (
                accept_strict_zero_amount_drop
                and opll_strict_zero_promotion_applied(before_payload, latest)
            )
        )) or bool(promotion_already_proven)
        amount_matches = not str(required_amount or "").strip() or amount == str(required_amount).strip()
        payment_method_ready = not expected_payment_method or expected_payment_method in methods
        ready = promotion_proven and payment_method_ready and amount_known and amount_matches
        signature = (
            tuple(methods),
            amount,
            amount_source,
        )
        if ready:
            stable_ready_count = stable_ready_count + 1 if signature == last_ready_signature else 1
            last_ready_signature = signature
            if stable_ready_count >= 2:
                return latest
        else:
            stable_ready_count = 0
            last_ready_signature = None
        if attempt + 1 >= total_attempts:
            if ready:
                raise RuntimeError("活动更新后的 Payment Page 状态未稳定")
            return latest
        if interval_seconds > 0:
            time.sleep(interval_seconds)
        latest = opll_stripe_retrieve_payment_page(stripe, cs_id, stripe_pk, ctx)
    return latest

def opll_require_payment_method_type(
    init_payload,
    payment_method_type: str,
    require_declared: bool = False,
) -> None:
    expected = str(payment_method_type or "").strip().lower()
    available = opll_payment_method_types(init_payload)
    unavailable = expected and expected not in available and (require_declared or bool(available))
    if unavailable:
        actual = ",".join(available) or "<missing>"
        raise RuntimeError(
            f"当前 Stripe Checkout 不支持 {expected}；payment_method_types={actual}。"
            f"需要 OpenAI/Stripe 创建的 Checkout Session 本身包含 {expected}，否则 confirm 会返回 payment_method_types_mismatch。"
        )

class AmountMismatchError(RuntimeError):
    def __init__(self, target_amount: str, actual_amount: str, stripe_amount_source: str):
        self.target_amount = target_amount
        self.actual_amount = actual_amount
        self.stripe_amount_source = stripe_amount_source
        super().__init__(f"金额不匹配: 目标 {target_amount}, 实际 {actual_amount}")

class NonZeroAmountError(AmountMismatchError):
    """The promotion completed, but the checkout still has a payable balance."""

    retryable = False

    def __init__(
        self,
        actual_amount: str,
        stripe_amount_source: str,
        currency: str = "",
    ) -> None:
        self.currency = str(currency or "").strip().upper()
        super().__init__("0", str(actual_amount or ""), stripe_amount_source)

class RetryablePromotionAmountMismatchError(AmountMismatchError):
    """The promotion IPs were exhausted; rebuild Checkout with a new proxy group."""

    retryable = True

class PayPalMethodUnavailableError(RuntimeError):
    """当前 Checkout 未声明 PayPal；换一组地区/IP 后可以重试。"""

    retryable = True

    def __init__(self, available_methods) -> None:
        available = [str(item).strip().lower() for item in (available_methods or []) if str(item).strip()]
        self.available_methods = available
        super().__init__(
            "当前支付线路未开放 PayPal，可用方式："
            + (", ".join(available) if available else "<missing>")
        )

def opll_sync_native_paypal_promotion(
    stripe: requests.Session,
    checkout: dict,
    proxy_url: str,
    initial_payload: dict,
    ctx: dict,
    *,
    payment_locale: str = "de",
    browser_timezone: str = "Europe/Berlin",
    diagnostic_log=None,
    attempts: int = PAYPAL_NATIVE_PROMO_SYNC_ATTEMPTS,
    interval_seconds: float = PAYPAL_NATIVE_PROMO_SYNC_INTERVAL_SECONDS,
) -> tuple[dict, dict]:
    """原生优惠最多检查 6 轮；可信金额连续两轮为 0 且 PayPal 可用时提前结束。"""
    latest = initial_payload if isinstance(initial_payload, dict) else {}
    current_ctx = dict(ctx or {})
    total_attempts = max(1, int(attempts or 1))
    invalid_amount_sources = {"fallback_zero", "missing_payload", "invalid_line_items"}
    stable_zero_checks = 0

    for check_no in range(1, total_attempts + 1):
        if check_no > 1:
            if interval_seconds > 0:
                time.sleep(interval_seconds)
            refreshed = opll_stripe_init(
                checkout["cs_id"],
                checkout["billing_country"],
                checkout["currency"],
                proxy_url,
                payment_locale=payment_locale,
                stripe=stripe,
                ctx=current_ctx,
                checkout=checkout,
                browser_timezone=browser_timezone,
            )
            if not isinstance(refreshed, dict):
                raise RuntimeError("PayPal 优惠同步检查返回了无效 Stripe Payment Page")
            latest = {**latest, **refreshed}

        amount, amount_source = opll_stripe_amount_info(latest)
        methods = opll_payment_method_types(latest)
        currency = str(latest.get("currency") or checkout.get("currency") or "").strip().lower()
        stripe_version = str(
            latest.get("stripe_version")
            or current_ctx.get("stripe_version")
            or STRIPE_VERSION_FULL
        ).split(";", 1)[0].strip()
        opll_emit_diagnostic(
            diagnostic_log,
            f"[stripe] init ok version={stripe_version} amount={amount or '<missing>'} "
            f"currency={currency or '<missing>'} pm={methods!r}",
        )
        if currency != "eur":
            raise RuntimeError(
                "PayPal 原生优惠流程要求 Stripe Payment Page 币种为 EUR；"
                f"当前为 {currency.upper() or '<missing>'}"
            )
        if "paypal" not in methods:
            raise PayPalMethodUnavailableError(methods)
        if (
            not re.fullmatch(r"\d+", str(amount or "").strip())
            or amount_source in invalid_amount_sources
        ):
            raise AmountMismatchError("可信 Stripe 金额", amount, amount_source)

        opll_emit_diagnostic(
            diagnostic_log,
            f"[promo] PayPal 优惠同步检查 {check_no}/{total_attempts}: amount={amount}",
        )
        current_ctx = opll_stripe_context(latest, payment_locale, ctx=current_ctx)
        if int(amount) == 0:
            stable_zero_checks += 1
            if stable_zero_checks < 2 and check_no < total_attempts:
                opll_emit_diagnostic(
                    diagnostic_log,
                    "[promo] Stripe 金额首次为 0 且 PayPal 可用，等待下一轮确认状态稳定",
                )
                continue
            if stable_zero_checks >= 2:
                remaining_checks = total_attempts - check_no
                if remaining_checks > 0:
                    opll_emit_diagnostic(
                        diagnostic_log,
                        f"[promo] Stripe 金额已连续 2 轮为 0 且 PayPal 可用，跳过剩余 {remaining_checks} 轮优惠同步检查",
                    )
                break
        else:
            stable_zero_checks = 0

    if str(amount or "").strip() == "0" and stable_zero_checks < 2:
        raise RuntimeError("PayPal 优惠同步结束时零金额尚未连续稳定 2 轮")

    return latest, current_ctx

def opll_apply_native_paypal_promotion_with_ip_rotation(
    stripe: requests.Session,
    checkout: dict,
    stripe_pk: str,
    initial_payload: dict,
    ctx: dict,
    *,
    access_token: str,
    request_proxy_url: str,
    stripe_proxy_url: str,
    rotation_proxy_url: str = "",
    target_amount: str = "",
    request_locale: str = "de-DE",
    payment_locale: str = "de",
    browser_timezone: str = "Europe/Berlin",
    diagnostic_log=None,
    chatgpt_session: requests.Session | OpllBrowserFetchSession | None = None,
    chatgpt_session_factory=None,
    max_ip_refreshes: int = PAYPAL_NATIVE_PROMO_IP_REFRESH_ATTEMPTS,
) -> tuple[dict, dict, dict, str]:
    """Apply once, then refresh the promotion IP up to three times."""
    request_proxy_url = str(request_proxy_url or "").strip()
    rotation_seed = str(rotation_proxy_url or "").strip() or request_proxy_url
    expected_amount = str(target_amount or "").strip()
    total_refreshes = max(0, int(max_ip_refreshes or 0))
    total_attempts = 1 + total_refreshes
    can_rotate = opll_proxy_has_refreshable_ip(rotation_seed)
    current_proxy = request_proxy_url
    last_actual = ""
    last_source = "missing_payload"
    last_error: Exception | None = None
    current_ctx = dict(ctx or {})

    for attempt_no in range(1, total_attempts + 1):
        if attempt_no > 1:
            if can_rotate:
                current_proxy = opll_with_fresh_promotion_proxy_ip(rotation_seed)
            else:
                current_proxy = rotation_seed
            opll_emit_diagnostic(
                diagnostic_log,
                f"[PayPal 原生优惠] 金额未匹配，切换刷新优惠 IP "
                f"{attempt_no - 1}/{total_refreshes}: "
                f"{opll_describe_proxy_endpoint(current_proxy)}",
            )

        attempt_session = chatgpt_session if attempt_no == 1 else None
        if attempt_no > 1 and callable(chatgpt_session_factory):
            attempt_session = chatgpt_session_factory(current_proxy)

        try:
            promotion = opll_apply_checkout_trial_promotion(
                stripe,
                checkout["cs_id"],
                stripe_pk,
                initial_payload,
                ctx,
                access_token=access_token,
                checkout=checkout,
                chatgpt_proxy_url=current_proxy,
                request_locale=request_locale,
                allow_pending=True,
                **(
                    {"chatgpt_session": attempt_session}
                    if attempt_session is not None
                    else {}
                ),
            )
            payment_page = promotion.get("payment_page")
            if not isinstance(payment_page, dict):
                raise RuntimeError("更新优惠后的 Stripe Payment Page 状态无效")
            effective_payload = {**initial_payload, **payment_page}
            effective_payload, current_ctx = opll_sync_native_paypal_promotion(
                stripe,
                checkout,
                stripe_proxy_url,
                effective_payload,
                current_ctx,
                payment_locale=payment_locale,
                browser_timezone=browser_timezone,
                diagnostic_log=diagnostic_log,
            )
            last_actual, last_source = opll_stripe_amount_info(effective_payload)
            promotion_applied = bool(
                promotion.get("promotion_applied") is True
                or opll_pix_payload_has_promotion_id(effective_payload)
                or opll_strict_zero_promotion_applied(
                    initial_payload,
                    effective_payload,
                )
            )
            amount_matches = not expected_amount or last_actual == expected_amount
            if promotion_applied and amount_matches:
                promotion["promotion_applied"] = True
                reported_proxy = (
                    rotation_seed
                    if attempt_no == 1 and rotation_proxy_url
                    else current_proxy
                )
                return promotion, effective_payload, current_ctx, reported_proxy

            reason = (
                f"目标金额={expected_amount or '<未设置>'}, "
                f"实际金额={last_actual or '<未知>'}, 金额来源={last_source}; "
                + (
                    "优惠已生效但金额不匹配"
                    if promotion_applied
                    else "优惠 Update 未证明优惠已生效"
                )
            )
            last_error = RetryablePromotionAmountMismatchError(
                expected_amount or "优惠后金额",
                last_actual,
                last_source,
            )
            opll_emit_diagnostic(
                diagnostic_log,
                f"[PayPal 原生优惠] 刷新优惠结果未匹配（{attempt_no}/{total_attempts}）: {reason}",
            )
        except PayPalMethodUnavailableError:
            raise
        except Exception as exc:
            if getattr(exc, "retryable", None) is False:
                raise
            last_error = exc
            if isinstance(exc, AmountMismatchError):
                last_actual = str(exc.actual_amount or "")
                last_source = str(exc.stripe_amount_source or "")
            opll_emit_diagnostic(
                diagnostic_log,
                f"[PayPal 原生优惠] 刷新优惠失败（{attempt_no}/{total_attempts}）: "
                f"{opll_short_error(str(exc), 220)}",
            )

        if attempt_no >= total_attempts:
            break

    exhausted = RetryablePromotionAmountMismatchError(
        expected_amount or "优惠后金额",
        last_actual,
        last_source,
    )
    opll_emit_diagnostic(
        diagnostic_log,
        f"[PayPal 原生优惠] 已刷新优惠 IP {total_refreshes} 次仍未匹配，"
        "退出本次执行并交由外层更换代理组后重新尝试",
    )
    raise exhausted from last_error

def opll_apply_de_strict_zero_promotion_with_ip_rotation(
    stripe: requests.Session,
    checkout: dict,
    stripe_pk: str,
    initial_payload: dict,
    ctx: dict,
    *,
    access_token: str,
    request_proxy_url: str,
    rotation_proxy_url: str = "",
    target_amount: str = "0",
    request_locale: str = "de-DE",
    diagnostic_log=None,
    max_ip_refreshes: int = PAYPAL_DE_STRICT_ZERO_PROMO_IP_REFRESH_ATTEMPTS,
) -> tuple[dict, dict, str]:
    """Retry only promotion Update on fresh IPs while preserving one Checkout."""
    request_proxy_url = str(request_proxy_url or "").strip()
    rotation_seed = str(rotation_proxy_url or "").strip() or request_proxy_url
    expected_amount = str(target_amount or "0").strip() or "0"
    can_rotate = opll_proxy_has_refreshable_ip(rotation_seed)
    total_refreshes = max(0, int(max_ip_refreshes or 0)) if can_rotate else 0
    total_attempts = 1 + total_refreshes
    current_proxy = request_proxy_url
    last_actual = ""
    last_source = "missing_payload"
    last_error: Exception | None = None

    for attempt_no in range(1, total_attempts + 1):
        if attempt_no > 1:
            current_proxy = opll_with_fresh_promotion_proxy_ip(rotation_seed)
            opll_emit_diagnostic(
                diagnostic_log,
                "[PayPal] 优惠后金额仍非 0；保留当前 Checkout，不重新执行 "
                "Create / Stripe init / 初始支付方式 Check，直接刷新第二代理 "
                f"SID/IP 并重提 Update（{attempt_no - 1}/{total_refreshes}）: "
                f"{opll_describe_proxy_endpoint(current_proxy)}",
            )

        try:
            promotion = opll_apply_checkout_trial_promotion(
                stripe,
                checkout["cs_id"],
                stripe_pk,
                initial_payload,
                ctx,
                access_token=access_token,
                checkout=checkout,
                chatgpt_proxy_url=current_proxy,
                request_locale=request_locale,
                allow_pending=True,
            )
            payment_page = promotion.get("payment_page")
            if not isinstance(payment_page, dict):
                raise RuntimeError("应用优惠后的 Payment Page 状态无效")
            opll_emit_diagnostic(
                diagnostic_log,
                f"[PayPal] 优惠 Update 第 {attempt_no}/{total_attempts} 次已提交，"
                "正在轮询并检测金额（同一 Checkout）",
            )
            payment_page = opll_wait_for_us_tr_promoted_payment_page(
                stripe,
                checkout["cs_id"],
                stripe_pk,
                ctx,
                payment_page,
                before_payload=initial_payload,
                accept_strict_zero_amount_drop=True,
                promotion_already_proven=promotion.get("promotion_applied") is True,
                required_amount=expected_amount,
                required_payment_method_type="paypal",
                attempts=6,
            )
            last_actual, last_source = opll_stripe_amount_info(payment_page)
            promotion_applied = bool(
                promotion.get("promotion_applied") is True
                or opll_strict_zero_promotion_applied(
                    initial_payload,
                    payment_page,
                )
            )
            if promotion_applied and last_actual == expected_amount:
                promotion["promotion_applied"] = True
                promotion["payment_page"] = payment_page
                used_proxy = (
                    rotation_seed
                    if attempt_no == 1 and rotation_proxy_url
                    else current_proxy
                )
                return promotion, payment_page, used_proxy

            last_error = NonZeroAmountError(
                last_actual,
                f"promotion_update.{last_source or 'unknown'}",
                str(payment_page.get("currency") or checkout.get("currency") or "EUR"),
            )
            opll_emit_diagnostic(
                diagnostic_log,
                f"[PayPal] 同一 Checkout 优惠结果未免费（{attempt_no}/{total_attempts}）："
                f"目标={expected_amount}，实际={last_actual or '<未知>'}，"
                f"来源={last_source or '<未知>'}",
            )
        except Exception as exc:
            if getattr(exc, "retryable", None) is False and not isinstance(
                exc,
                NonZeroAmountError,
            ):
                raise
            last_error = exc
            if isinstance(exc, AmountMismatchError):
                last_actual = str(exc.actual_amount or "")
                last_source = str(exc.stripe_amount_source or "")
            opll_emit_diagnostic(
                diagnostic_log,
                f"[PayPal] 同一 Checkout 优惠 Update 失败（{attempt_no}/{total_attempts}）："
                f"{opll_short_error(str(exc), 220)}",
            )

        if attempt_no >= total_attempts:
            break

    if last_actual and last_actual != expected_amount:
        exhausted = NonZeroAmountError(
            last_actual,
            last_source or "promotion_update.unknown",
            str(checkout.get("currency") or "EUR"),
        )
        opll_emit_diagnostic(
            diagnostic_log,
            f"[PayPal] 已刷新第二代理 IP {total_refreshes} 次，金额仍为 "
            f"{last_actual}；直接退出当前账号，不重建 Checkout、不回到前置 Check",
        )
        raise exhausted from last_error

    raise RuntimeError(
        "PayPal 优惠 Update 刷新结束后仍无法确认免费金额；"
        "已退出当前 Checkout，不重新执行前置 Check"
    ) from last_error

def opll_apply_amount_check(result: dict, target_amount: str = "") -> dict:
    target = str(target_amount).strip()
    actual = str(result.get("stripe_amount") or "").strip()
    source = str(result.get("stripe_amount_source") or "").strip()
    result["target_amount"] = target
    if not target:
        result["amount_check"] = "skipped"
        return result
    if actual != target:
        result["amount_check"] = "failed"
        raise AmountMismatchError(target, actual, source)
    result["amount_check"] = "passed"
    return result

def opll_random_postal_code(pattern: str) -> str:
    result = []
    for char in str(pattern or "#####"):
        if char == "#":
            result.append(str(random.randint(0, 9)))
        elif char == "A":
            result.append(chr(random.randint(ord("A"), ord("Z"))))
        else:
            result.append(char)
    return "".join(result)

def generate_brazil_cpf() -> str:
    def check_digit(digits: list[int]) -> int:
        weight = len(digits) + 1
        value = (sum(digit * (weight - index) for index, digit in enumerate(digits)) * 10) % 11
        return 0 if value == 10 else value

    while True:
        digits = [random.randint(0, 9) for _ in range(9)]
        if len(set(digits)) == 1:
            continue
        digits.append(check_digit(digits))
        digits.append(check_digit(digits))
        return "".join(str(digit) for digit in digits)

def opll_india_mobile_phone() -> str:
    """印度手机号：+91 + 首位 6–9 + 9 位数字（共 10 位国内号）。"""
    return f"+91{random.randint(6, 9)}{random.randint(100000000, 999999999)}"

def opll_netherlands_mobile_phone() -> str:
    """荷兰手机号：国际格式 +31 6 + 8 位号码，不携带国内拨号前缀 0。"""
    return f"+316{random.randint(0, 99999999):08d}"

def opll_korea_mobile_phone() -> str:
    """韩国手机号：国际格式 +82 10 + 8 位号码。"""
    return f"+8210{random.randint(0, 99999999):08d}"

def opll_vietnam_mobile_phone() -> str:
    """越南手机号：国际格式 +84 + 常用移动号段首位 + 8 位号码。"""
    return f"+84{random.choice((3, 5, 7, 8, 9))}{random.randint(0, 99999999):08d}"

_BILLING_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_SYNTHETIC_BILLING_EMAIL_DOMAINS = (
    "gmail.com",
    "outlook.com",
    "hotmail.com",
    "yahoo.com",
    "icloud.com",
    "live.com",
)

def opll_looks_like_email(value: str) -> bool:
    text = str(value or "").strip()
    return bool(text and _BILLING_EMAIL_RE.fullmatch(text))

def opll_account_email_from_access_token(access_token: str) -> str:
    """从 ChatGPT access token / session 中解析登录邮箱，供账单 email 与账号对齐。"""
    token = extract_access_token_from_session_text(access_token) or str(access_token or "").strip()
    if not token:
        return ""
    payload = decode_jwt_payload(token)
    for key in (
        "email",
        "account_email",
        "preferred_username",
        "upn",
        "unique_name",
        "username",
    ):
        value = payload.get(key) if isinstance(payload, dict) else ""
        text = str(value or "").strip()
        if opll_looks_like_email(text):
            return text
    # account_name_from_access_token 可能返回 email 或 sub；仅接受邮箱形态
    fallback = account_name_from_access_token(token)
    return fallback if opll_looks_like_email(fallback) else ""

def opll_resolve_billing_email(
    *,
    account_email: str = "",
    access_token: str = "",
    first: str = "",
    last: str = "",
) -> str:
    """
    账单邮箱优先级：
    1) 显式账号邮箱（列表里的登录邮箱）
    2) access token 中的登录邮箱
    3) 合成邮箱（真实消费邮箱域名，绝不使用 @example.com）
    """
    for candidate in (
        str(account_email or "").strip(),
        opll_account_email_from_access_token(access_token),
    ):
        if opll_looks_like_email(candidate):
            return candidate
    local_first = re.sub(r"[^a-z0-9]", "", str(first or "").lower())
    local_last = re.sub(r"[^a-z0-9]", "", str(last or "").lower())
    if local_first and local_last:
        local = f"{local_first}.{local_last}{random.randint(10, 99)}"
    elif local_first or local_last:
        local = f"{local_first or local_last}{random.randint(100, 999)}"
    else:
        local = f"user{random.randint(10000, 99999)}"
    domain = random.choice(_SYNTHETIC_BILLING_EMAIL_DOMAINS)
    return f"{local}@{domain}"

_NL_CITY_ALIASES = {
    "thehague": "denhaag",
    "hague": "denhaag",
    "sgravenhage": "denhaag",
}

def opll_normalize_nl_city(city: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(city or "").strip().casefold())
    compact = "".join(
        char for char in normalized
        if not unicodedata.combining(char) and char.isalnum()
    )
    return _NL_CITY_ALIASES.get(compact, compact)

def opll_billing_for_country(
    country: str,
    *,
    account_email: str = "",
    access_token: str = "",
    city_hint: str = "",
    state_hint: str = "",
) -> dict:
    country = normalize_opll_country(country)
    if country == "KR":
        family, given = random.choice(KR_BILLING_NAMES)
        line1, city, state, postal = random.choice(KR_BILLING_STREETS)
        return {
            "name": f"{family}{given}",
            "email": opll_resolve_billing_email(
                account_email=account_email,
                access_token=access_token,
            ),
            "phone": opll_korea_mobile_phone(),
            "country": "KR",
            "line1": line1,
            "city": city,
            "state": state,
            "postal_code": postal,
        }
    if country == "IN":
        first, last = random.choice(IN_BILLING_NAMES)
        line1, city, state, postal = random.choice(IN_BILLING_STREETS)
        return {
            "name": f"{first} {last}",
            "email": opll_resolve_billing_email(
                account_email=account_email,
                access_token=access_token,
                first=first,
                last=last,
            ),
            "phone": opll_india_mobile_phone(),
            "country": "IN",
            "line1": line1,
            "city": city,
            "state": state,
            "postal_code": postal,
        }
    if country == "VN":
        # 姓名拆开组合，地址只随机门牌并保留城市/省市/邮编成套关系。
        # 每次完整 MoMo 提取只调用一次本函数，随后 taxes、tax_region 和
        # PaymentMethod 全程复用同一个 billing，既有随机性又不会前后不一致。
        first = random.choice([item[0] for item in VN_BILLING_NAMES])
        last = random.choice([item[1] for item in VN_BILLING_NAMES])
        line1_template, city, state, postal = random.choice(VN_BILLING_STREETS)
        street_name = re.sub(r"^\s*\d+[A-Za-z]?\s+", "", line1_template).strip()
        line1 = f"{random.randint(1, 999)} {street_name or line1_template}"
        return {
            "name": f"{first} {last}",
            "email": opll_resolve_billing_email(
                account_email=account_email,
                access_token=access_token,
                first=first,
                last=last,
            ),
            "phone": opll_vietnam_mobile_phone(),
            "country": "VN",
            "line1": line1,
            "city": city,
            "state": state,
            "postal_code": postal,
        }
    if country == "NL":
        first, last = random.choice(NL_BILLING_NAMES)
        requested_city = str(city_hint or "").strip()
        address_pool = list(NL_BILLING_STREETS)
        if requested_city:
            normalized_city = opll_normalize_nl_city(requested_city)
            address_pool = [
                address
                for address in NL_BILLING_STREETS
                if opll_normalize_nl_city(address[1]) == normalized_city
            ]
            if not address_pool:
                # GeoIP may return any Dutch municipality. Keep the parsed city
                # instead of blocking on the finite local fixture table.
                line1 = f"{random.randint(1, 199)} {random.choice(('Kerkstraat', 'Dorpsstraat', 'Molenweg', 'Stationsweg', 'Schoolstraat'))}"
                city = requested_city
                state = str(state_hint or "Nederland").strip()
                postal = (
                    f"{random.randint(1000, 9999)} "
                    f"{random.choice('ABCDEFGHJKLMNPRSTUVWXYZ')}"
                    f"{random.choice('ABCDEFGHJKLMNPRSTUVWXYZ')}"
                )
            else:
                line1, city, state, postal = random.choice(address_pool)
        else:
            line1, city, state, postal = random.choice(address_pool)
    elif country == "DE":
        first, last = random.choice(DE_BILLING_NAMES)
        line1, city, state, postal = random.choice(DE_BILLING_STREETS)
    elif country == "GB":
        first, last = random.choice(GB_BILLING_NAMES)
        line1, city, state, postal = random.choice(GB_BILLING_STREETS)
    elif country == "AU":
        first, last = random.choice(AU_BILLING_NAMES)
        line1, city, state, postal = random.choice(AU_BILLING_STREETS)
    elif country == "US":
        first, last = random.choice(US_BILLING_NAMES)
        address_pool = [
            item
            for item in US_BILLING_STREETS
            if (
                not str(city_hint or "").strip()
                or item[1].casefold() == str(city_hint).strip().casefold()
            )
            and (
                not str(state_hint or "").strip()
                or item[2].casefold() == str(state_hint).strip().casefold()
            )
        ]
        line1, city, state, postal = random.choice(
            address_pool or US_BILLING_STREETS
        )
    elif country in EXTRA_BILLING_STREETS:
        first, last = random.choice(EXTRA_BILLING_NAMES)
        line1, city, state, postal = random.choice(EXTRA_BILLING_STREETS[country])
    elif country in OPENAI_SUPPORTED_COUNTRY_CODES:
        profile = BILLING_PROFILE_BY_COUNTRY[country]
        first, last = random.choice(EXTRA_BILLING_NAMES)
        line1 = f"{random.randint(10, 999)} {random.choice(profile['street_pool'])}"
        city = random.choice(profile["city_pool"])
        state = country
        postal = opll_random_postal_code(str(profile.get("postal_pattern") or "#####"))
    else:
        raise RuntimeError(f"不支持的账单资料地区: {country}")
    if country == "NL":
        phone = opll_netherlands_mobile_phone()
    else:
        phone_prefix = str(BILLING_PROFILE_BY_COUNTRY.get(country, {}).get("phone_prefix") or COUNTRY_PHONE_PREFIX.get(country, "+1"))
        phone = f"{phone_prefix}{random.randint(100000000, 999999999)}"
    billing = {
        "name": f"{first} {last}",
        "email": opll_resolve_billing_email(
            account_email=account_email,
            access_token=access_token,
            first=first,
            last=last,
        ),
        "phone": phone,
        "country": country,
        "line1": line1,
        "city": city,
        "state": state,
        "postal_code": postal,
    }
    if country == "BR":
        billing["tax_id"] = generate_brazil_cpf()
    return billing

def opll_require_ideal_billing_details(billing: dict) -> None:
    """iDEAL 资料必须完整且为一致的 NL 格式，禁止静默混入美国兜底字段。"""
    if not isinstance(billing, dict):
        raise RuntimeError("iDEAL 账单资料格式无效")
    required_fields = ("name", "email", "phone", "country", "line1", "city", "state", "postal_code")
    missing = [key for key in required_fields if not str(billing.get(key) or "").strip()]
    if missing:
        raise RuntimeError(f"iDEAL NL 账单资料缺少字段: {','.join(missing)}")
    if str(billing.get("country") or "").strip().upper() != "NL":
        raise RuntimeError(
            f"iDEAL 账单国家必须为 NL，当前={str(billing.get('country') or '<missing>').strip()}"
        )
    if not opll_looks_like_email(str(billing.get("email") or "")):
        raise RuntimeError("iDEAL NL 账单 email 格式无效")
    if not re.fullmatch(r"\+316\d{8}", str(billing.get("phone") or "").strip()):
        raise RuntimeError("iDEAL NL 账单 phone 必须为 +316xxxxxxxx")
    if not re.fullmatch(r"[1-9]\d{3} [A-Z]{2}", str(billing.get("postal_code") or "").strip().upper()):
        raise RuntimeError("iDEAL NL 账单 postal_code 格式无效，应为 1234 AB")

def opll_stripe_create_paypal_method(stripe: requests.Session, cs_id: str, ctx: dict, billing: dict, stripe_pk: str = "", payment_method_type: str = "paypal") -> str:
    runtime_version = str(ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION)
    payment_method_type = str(payment_method_type or "paypal").strip().lower()
    if payment_method_type == "ideal":
        opll_require_ideal_billing_details(billing)
    body = {
        "guid": str(ctx.get("guid") or ""),
        "muid": str(ctx.get("muid") or ""),
        "sid": str(ctx.get("sid") or ""),
        "billing_details[name]": billing.get("name") or "John Doe",
        "billing_details[email]": billing.get("email") or "",
        "billing_details[phone]": billing.get("phone") or "",
        "billing_details[address][country]": billing.get("country") or "US",
        "billing_details[address][line1]": billing.get("line1") or "3110 Sunset Boulevard",
        "billing_details[address][city]": billing.get("city") or "Los Angeles",
        "billing_details[address][postal_code]": billing.get("postal_code") or "90026",
        "billing_details[address][state]": billing.get("state") or "CA",
        "type": payment_method_type,
        "payment_user_agent": f"stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; payment-element; deferred-intent",
        "referrer": "https://chatgpt.com",
        "time_on_page": str(random.randint(25000, 55000)),
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[client_session_id]": ctx["stripe_js_id"],
        "client_attribution_metadata[checkout_config_id]": ctx.get("config_id") or "",
        "client_attribution_metadata[elements_session_id]": ctx["elements_session_id"],
        "client_attribution_metadata[elements_session_config_id]": ctx["elements_session_config_id"],
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "key": stripe_pk or DEFAULT_STRIPE_PK,
        "_stripe_version": str(ctx.get("stripe_version") or STRIPE_VERSION_FULL),
    }
    if payment_method_type == "pix":
        tax_id = str(billing.get("tax_id") or "").strip()
        if not re.fullmatch(r"\d{11}", tax_id):
            raise RuntimeError("PIX 账单缺少有效的 11 位 CPF 税号")
        body["billing_details[tax_id]"] = tax_id
    response = stripe.post("https://api.stripe.com/v1/payment_methods", data=body, timeout=PAY_LONG_LINK_TIMEOUT)
    if response.status_code >= 400:
        raise RuntimeError(f"stripe payment_methods failed: HTTP {response.status_code} {opll_complete_response_body(response)}")
    pm_id = str((response.json() or {}).get("id") or "")
    if not pm_id.startswith("pm_"):
        raise RuntimeError(f"stripe payment_methods bad response: {opll_complete_response_body(response)}")
    return pm_id

def opll_stripe_create_paypal_confirmation_token(
    stripe: requests.Session,
    billing: dict,
    stripe_pk: str,
    *,
    return_url: str = "",
    stripe_version: str = STRIPE_VERSION_FULL,
) -> str:
    """Mirror Stripe.js ``createConfirmationToken`` for OAICS PayPal setup."""
    body = {
        "payment_method_data[type]": "paypal",
        "payment_method_data[allow_redisplay]": "unspecified",
        "payment_method_data[billing_details][name]": str(billing.get("name") or ""),
        "payment_method_data[billing_details][email]": str(billing.get("email") or ""),
        "payment_method_data[billing_details][phone]": str(billing.get("phone") or ""),
        "payment_method_data[billing_details][address][line1]": str(billing.get("line1") or ""),
        "payment_method_data[billing_details][address][city]": str(billing.get("city") or ""),
        "payment_method_data[billing_details][address][state]": str(billing.get("state") or ""),
        "payment_method_data[billing_details][address][postal_code]": str(
            billing.get("postal_code") or ""
        ),
        "payment_method_data[billing_details][address][country]": str(
            billing.get("country") or "DE"
        ).upper(),
        "setup_future_usage": "off_session",
        "mandate_data[customer_acceptance][type]": "online",
        "mandate_data[customer_acceptance][online][infer_from_client]": "true",
        "key": str(stripe_pk or "").strip() or DEFAULT_STRIPE_PK,
        "_stripe_version": str(stripe_version or STRIPE_VERSION_FULL),
    }
    if str(return_url or "").strip():
        body["return_url"] = str(return_url).strip()
    response = stripe.post(
        "https://api.stripe.com/v1/confirmation_tokens",
        data=body,
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            opll_stripe_error_summary("PayPal confirmation token failed", response)
        )
    confirmation_token = str((response.json() or {}).get("id") or "").strip()
    if not confirmation_token.startswith("ctoken_"):
        raise RuntimeError("PayPal confirmation token 响应缺少 ctoken_ ID")
    return confirmation_token

def opll_chatgpt_checkout_taxes(
    access_token: str,
    checkout: dict,
    billing: dict,
    proxy_url: str = "",
    *,
    request_locale: str = "en-IN",
    device_id: str = "",
    diagnostic_log=None,
    require_success: bool = False,
    flow_label: str = "UPI",
    session_context: dict | None = None,
    chatgpt_session: requests.Session | None = None,
    provider_session_request: bool = False,
) -> dict:
    """先调 ChatGPT /checkout/taxes，再走 Stripe tax_region。

    require_success=False（默认）：失败返回 {} 并继续 Stripe tax_region。
    require_success=True：网络/HTTP/JSON 失败直接抛错（硬失败）。
    """
    cs_id = str(checkout.get("cs_id") or "").strip()
    if not opll_is_checkout_session_id(cs_id):
        raise RuntimeError("Checkout taxes 缺少有效 Checkout Session ID")
    flow_label = str(flow_label or "UPI").strip() or "UPI"
    entity = opll_processor_entity_for_country(
        str(checkout.get("billing_country") or "IN"),
        str(checkout.get("processor_entity") or ""),
    )
    oai_device_id = opll_resolve_oai_device_id(device_id, checkout)
    session = chatgpt_session or opll_build_chatgpt_session(
        access_token,
        proxy_url,
        request_locale=request_locale,
        device_id=oai_device_id,
        **({"session_context": session_context} if session_context else {}),
    )
    request_path = "/backend-api/payments/checkout/taxes"
    checkout_country = str(checkout.get("billing_country") or "").strip().upper()
    billing_country = str(
        billing.get("country")
        or checkout.get("billing_country")
        or "IN"
    ).upper()
    if checkout_country and billing_country != checkout_country:
        raise RuntimeError(
            f"{flow_label} taxes 账单国家与 Checkout 不一致: "
            f"billing={billing_country}, checkout={checkout_country}"
        )
    if checkout_country == "NL" or billing_country == "NL":
        opll_require_ideal_billing_details(billing)
    currency = str(checkout.get("currency") or currency_for_country(billing_country) or "INR").upper()
    # 422 对照：body 需要顶层 billing_country / currency，不只是 billing_details
    body = {
        "checkout_session_id": cs_id,
        "processor_entity": entity,
        "checkout_email": str(billing.get("email") or ""),
        "billing_country": billing_country,
        "billing_name": str(billing.get("name") or ""),
        "currency": currency,
        "tax_id": billing.get("tax_id"),
        "billing_address": {
            "line1": str(billing.get("line1") or ""),
            "city": str(billing.get("city") or ""),
            "country": billing_country,
            "postal_code": str(billing.get("postal_code") or ""),
            "state": str(billing.get("state") or ""),
        },
        "billing_details": {
            "name": str(billing.get("name") or ""),
            "email": str(billing.get("email") or ""),
            "phone": str(billing.get("phone") or ""),
            "country": billing_country,
            "line1": str(billing.get("line1") or ""),
            "city": str(billing.get("city") or ""),
            "state": str(billing.get("state") or ""),
            "postal_code": str(billing.get("postal_code") or ""),
        },
    }
    if provider_session_request:
        token = (
            extract_access_token_from_session_text(access_token)
            or str(access_token or "").strip()
        )
        if not token:
            raise RuntimeError(f"{flow_label} Provider taxes 缺少 Access Token")
        request_headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "oai-language": "ko-KR",
            "User-Agent": OPLL_USER_AGENT,
            "Referer": f"https://chatgpt.com/checkout/{entity}/{cs_id}",
            "x-openai-target-path": request_path,
            "x-openai-target-route": request_path,
        }
    else:
        request_headers = {
            "Referer": f"https://chatgpt.com/checkout/{entity}/{cs_id}",
            "x-openai-target-path": request_path,
            "x-openai-target-route": request_path,
        }
    try:
        response = session.post(
            f"https://chatgpt.com{request_path}",
            json=body,
            headers=request_headers,
            timeout=PAY_LONG_LINK_TIMEOUT,
        )
    except Exception as exc:
        message = f"ChatGPT /checkout/taxes 请求失败: {opll_short_error(str(exc), 160)}"
        if require_success:
            raise RuntimeError(message) from exc
        opll_emit_diagnostic(
            diagnostic_log,
            f"[{flow_label}] {message}（软失败，继续 Stripe tax_region）",
        )
        return {}
    if response.status_code >= 400:
        message = (
            f"ChatGPT /checkout/taxes HTTP {response.status_code}: "
            f"{opll_complete_response_body(response)}"
        )
        if require_success:
            raise RuntimeError(message)
        opll_emit_diagnostic(
            diagnostic_log,
            f"[{flow_label}] {message}（软失败，继续 Stripe tax_region）",
        )
        return {}
    try:
        payload = response.json() or {}
    except Exception as exc:
        message = f"ChatGPT /checkout/taxes 响应不是有效 JSON: {opll_short_error(str(exc), 120)}"
        if require_success:
            raise RuntimeError(message) from exc
        opll_emit_diagnostic(diagnostic_log, f"[{flow_label}] {message}（软失败，继续 Stripe tax_region）")
        return {}
    if not isinstance(payload, dict):
        message = "ChatGPT /checkout/taxes 响应格式无效"
        if require_success:
            raise RuntimeError(message)
        opll_emit_diagnostic(diagnostic_log, f"[{flow_label}] {message}（软失败，继续 Stripe tax_region）")
        return {}
    opll_emit_diagnostic(diagnostic_log, f"[{flow_label}] ChatGPT /checkout/taxes 成功")
    return payload

def opll_is_retryable_http_transport_error(exc: Exception | str) -> bool:
    """Return True only for transport failures that are safe to retry in-place."""
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    detail = str(exc or "").strip().lower()
    if re.search(r"curl:\s*\((?:28|35|52|55|56|92)\)", detail):
        return True
    return any(
        marker in detail
        for marker in (
            "connection closed abruptly",
            "connection aborted",
            "connection broken",
            "connection reset",
            "connection refused",
            "remote disconnected",
            "remote end closed connection",
            "failed to establish a new connection",
            "connect timeout",
            "read timeout",
            "timed out",
            "tls handshake",
            "ssl handshake",
        )
    )

def opll_short_error(detail: str, limit: int = 260) -> str:
    """Return complete diagnostic text; ``limit`` remains API-compatible only."""
    return str(detail or "").strip()

def opll_complete_response_body(value) -> str:
    """Serialize a complete response body while protecting secret material."""
    raw_text = str(getattr(value, "text", value) or "")
    try:
        payload = json.loads(raw_text)
    except Exception:
        text = raw_text
        # Keep the body complete, but never emit authentication or card secrets.
        replacements = (
            (r'(?i)("?(?:access_token|refresh_token|id_token|client_secret|password|cookie|authorization|cvc|cvv|card_number)"?\s*[:=]\s*"?)([^"\s,}]+)', r'\1***'),
            (r'(?i)(Bearer\s+)[A-Za-z0-9._~+/-]+', r'\1***'),
            (r'(?i)(Basic\s+)[A-Za-z0-9+/=]+', r'\1***'),
            (r'(?<!\d)(?:\d[ -]?){12,19}(?!\d)', '***CARD***'),
        )
        for pattern, replacement in replacements:
            text = re.sub(pattern, replacement, text)
        return text.strip()
    return opll_redact_approve_payload(payload)

def opll_stripe_error_summary(prefix: str, response) -> str:
    try:
        payload = response.json() or {}
    except Exception:
        payload = {}
    error = payload.get("error") if isinstance(payload, dict) else {}
    if not isinstance(error, dict):
        error = {}
    extra_fields = error.get("extra_fields") if isinstance(error.get("extra_fields"), dict) else {}
    parts = []
    for label, value in (
        ("code", error.get("code")),
        ("decline_code", error.get("decline_code")),
        ("type", error.get("type")),
        ("message", error.get("message")),
        ("payment_method_type", extra_fields.get("payment_method_type")),
        ("confirm_error_reason", extra_fields.get("confirm_error_reason")),
        ("confirm_error_code", extra_fields.get("confirm_error_code")),
        ("confirm_error_message", extra_fields.get("confirm_error_message")),
    ):
        if value is not None and value != "":
            parts.append(f"{label}={opll_short_error(str(value), 180)}")
    if parts:
        return f"{prefix}: " + ", ".join(parts)
    return f"{prefix}: {opll_complete_response_body(response)}"

def opll_is_external_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except Exception:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)

def opll_is_paypal_url(value: str) -> bool:
    host = (urlsplit(value).netloc or "").lower()
    return host == "paypal.com" or host.endswith(".paypal.com") or host == "paypalobjects.com" or host.endswith(".paypalobjects.com")

def opll_is_paypal_ba_approve_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    if not (host == "paypal.com" or host.endswith(".paypal.com")):
        return False
    path = parsed.path.rstrip("/").lower()
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    return path == "/agreements/approve" and bool(str(query.get("ba_token") or "").strip())

def opll_is_stripe_pm_redirect_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except Exception:
        return False
    return parsed.scheme.lower() == "https" and (parsed.hostname or "").lower() == "pm-redirects.stripe.com"

def opll_is_paypal_success_url(value: str) -> bool:
    if opll_is_paypal_ba_approve_url(value):
        return True
    return opll_is_stripe_pm_redirect_url(value)

def opll_chatgpt_checkout_api_context(checkout: dict) -> tuple[str, str, str]:
    cs_id = str((checkout or {}).get("cs_id") or "").strip()
    if not opll_is_checkout_session_id(cs_id):
        raise RuntimeError("自定义 Checkout API 缺少有效 Checkout Session ID")
    country = str((checkout or {}).get("billing_country") or "VN").strip().upper() or "VN"
    entity = opll_processor_entity_for_country(
        country,
        str((checkout or {}).get("processor_entity") or ""),
    )
    if not re.fullmatch(r"[A-Za-z0-9_-]+", entity):
        raise RuntimeError("自定义 Checkout API 的 processor_entity 无效")
    return cs_id, entity, f"https://chatgpt.com/checkout/{entity}/{cs_id}"

def opll_chatgpt_checkout_json_response(response, operation: str) -> dict:
    raw_text = str(getattr(response, "text", "") or "")
    try:
        payload = response.json() or {}
    except Exception as exc:
        if response.status_code >= 400:
            raise RuntimeError(
                f"{operation}失败: HTTP {response.status_code} {opll_complete_response_body(raw_text)}"
            ) from exc
        raise RuntimeError(f"{operation}响应不是有效 JSON") from exc
    if response.status_code >= 400:
        detail = opll_redact_approve_payload(payload) if payload else opll_complete_response_body(raw_text)
        raise RuntimeError(f"{operation}失败: HTTP {response.status_code} {detail}")
    if not isinstance(payload, dict):
        raise RuntimeError(f"{operation}响应格式无效")
    return payload

def opll_chatgpt_fetch_checkout(
    access_token: str,
    checkout: dict,
    proxy_url: str = "",
    *,
    request_locale: str = "vi-VN",
    chatgpt: requests.Session | None = None,
) -> dict:
    """Fetch the Checkout object that owns ChatGPT custom payment methods."""
    cs_id, entity, referer = opll_chatgpt_checkout_api_context(checkout)
    request_path = f"/backend-api/payments/checkout/{entity}/{cs_id}"
    session = chatgpt or opll_build_chatgpt_session(
        access_token,
        proxy_url,
        request_locale=request_locale,
        device_id=opll_resolve_oai_device_id(checkout),
    )
    response = session.get(
        f"https://chatgpt.com{request_path}",
        headers={
            "Referer": referer,
            "x-openai-target-path": request_path,
            "x-openai-target-route": request_path,
        },
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    return opll_chatgpt_checkout_json_response(response, "读取 ChatGPT Checkout")

def opll_chatgpt_confirm_custom_payment_method(
    access_token: str,
    checkout: dict,
    custom_payment_method_id: str,
    proxy_url: str = "",
    *,
    request_locale: str = "vi-VN",
    chatgpt: requests.Session | None = None,
    method_name: str = "MoMo",
    sentinel_required: bool = False,
    diagnostic_log=None,
    allow_builtin: bool = False,
    confirmation_token: str = "",
) -> dict:
    cs_id, _entity, referer = opll_chatgpt_checkout_api_context(checkout)
    method_id = str(custom_payment_method_id or "").strip()
    method_name = str(method_name or "自定义").strip() or "自定义"
    builtin_method = method_id.lower() in {"paypal"}
    if not method_id.startswith("cpmt_") and not (allow_builtin and builtin_method):
        raise RuntimeError(f"确认 {method_name} 支付方法缺少有效 cpmt_ ID")
    confirm_token = str(confirmation_token or "").strip()
    if builtin_method and not confirm_token.startswith("ctoken_"):
        raise RuntimeError(f"确认 {method_name} 支付方法缺少 Stripe ctoken_ ConfirmationToken")
    request_path = "/backend-api/payments/checkout/confirm"
    session = chatgpt or opll_build_chatgpt_session(
        access_token,
        proxy_url,
        request_locale=request_locale,
        device_id=opll_resolve_oai_device_id(checkout),
    )
    if sentinel_required:
        opll_chatgpt_sentinel_ping(
            session,
            diagnostic_log=diagnostic_log,
            diagnostic_prefix=f"[{method_name}] ",
        )
    request_body = {
        "checkout_session_id": cs_id,
        "selected_payment_method_type": method_id,
    }
    if builtin_method:
        # ChatGPT Checkout frontend uses ``confirm_token`` (not
        # ``confirmation_token``) for non-custom payment methods.
        request_body["confirm_token"] = confirm_token
    response = session.post(
        f"https://chatgpt.com{request_path}",
        json=request_body,
        headers={
            "Referer": referer,
            "x-openai-target-path": request_path,
            "x-openai-target-route": request_path,
        },
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    payload = opll_chatgpt_checkout_json_response(response, f"确认 {method_name} 支付方法")
    result = str(payload.get("result") or payload.get("status") or "").strip().lower()
    if result == "blocked":
        raise RuntimeError(f"CUSTOM_CONFIRM_BLOCKED: {method_name} 支付方式确认被上游拦截")
    if result in {"exception", "failed", "failure", "error", "canceled", "cancelled"}:
        raise RuntimeError(f"确认 {method_name} 支付方法失败: result={result}")
    if payload.get("error"):
        raise RuntimeError(
            f"确认 {method_name} 支付方法失败: "
            + opll_redact_approve_payload({"error": payload.get("error")})
        )
    return payload

def opll_is_openai_return_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    return host in ("openai.com", "chatgpt.com") or host.endswith(".openai.com") or host.endswith(".chatgpt.com")

def opll_is_ignored_resource_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    full_url = str(value or "").lower()
    # CloudFront 可能既承载静态资源，也可能作为支付跳转中间层；不能按整个域名封杀。
    # 静态文件仍由 ignored_suffixes 拦截，非支付 200 页面最终也过不了银行授权门禁。
    ignored_hosts = {
        "stripe-camo.global.ssl.fastly.net",
        "files.stripe.com",
        "q.stripe.com",
        "js.stripe.com",
        "m.stripe.network",
    }
    ignored_suffixes = (".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif", ".ico", ".css", ".js", ".woff", ".woff2")
    if host in ignored_hosts or any(host.endswith(f".{item}") for item in ignored_hosts):
        return True
    if "files.stripe.com" in full_url or "68747470733a2f2f66696c65732e7374726970652e636f6d" in full_url or "/files/mdb8" in full_url:
        return True
    if "cloudfront.net" in host and ("merchant_id=" in full_url or "/files/" in path or "stripe.com" in full_url or "68747470733a2f2f" in full_url):
        return True
    return path.endswith(ignored_suffixes)

def opll_collect_urls(payload, urls: list[str] | None = None) -> list[str]:
    found = urls if urls is not None else []
    if isinstance(payload, str):
        for match in re.findall(r"https?://[^\s\"'<>]+", payload):
            found.append(match.rstrip("),.;]"))
    elif isinstance(payload, dict):
        for key, value in payload.items():
            if key in ("url", "return_url", "redirect_url", "redirect_to_url") and isinstance(value, str) and opll_is_external_url(value):
                found.append(value)
            else:
                opll_collect_urls(value, found)
    elif isinstance(payload, list):
        for item in payload:
            opll_collect_urls(item, found)
    return found

def opll_extract_redirect_to_url(payload) -> str:
    if not isinstance(payload, dict):
        urls = opll_collect_urls(payload)
        return next(
            (item for item in urls if opll_is_paypal_ba_approve_url(item)),
            next((item for item in urls if opll_is_paypal_url(item) and not opll_is_ignored_resource_url(item) and not opll_is_openai_return_url(item)), ""),
        )
    next_action = payload.get("next_action")
    if isinstance(next_action, dict) and next_action.get("type") == "redirect_to_url":
        redirect_to_url = next_action.get("redirect_to_url") or {}
        if isinstance(redirect_to_url, dict):
            url = str(redirect_to_url.get("url") or "").strip()
            if url and not opll_is_ignored_resource_url(url) and not opll_is_openai_return_url(url):
                return url
    for key in ("setup_intent", "payment_intent"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            found = opll_extract_redirect_to_url(nested)
            if found:
                return found
    urls = opll_collect_urls(payload)
    return next(
        (item for item in urls if opll_is_paypal_ba_approve_url(item)),
        next((item for item in urls if opll_is_paypal_url(item) and not opll_is_ignored_resource_url(item) and not opll_is_openai_return_url(item)), ""),
    )

def opll_first_non_empty(values: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = str(values.get(key) or "").strip()
        if value:
            return value
    return ""

def opll_submission_attempt_failure_fields(submission) -> dict[str, str]:
    wanted = {"error", "code", "message", "reason", "failure_reason", "decline_code", "failure_code", "failure_message"}
    found: dict[str, str] = {}

    def walk(value) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key or "").strip()
                if normalized in wanted and normalized not in found:
                    if isinstance(item, (str, int, float, bool)):
                        text = str(item).strip()
                    elif isinstance(item, dict):
                        text = str(item.get("message") or item.get("code") or item.get("reason") or item.get("type") or "").strip()
                    else:
                        text = ""
                    if text:
                        found[normalized] = text[:240]
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    if isinstance(submission, dict):
        walk(submission)
    return found

def opll_find_submission_attempt(payload) -> dict:
    if isinstance(payload, dict):
        item = payload.get("submission_attempt")
        if isinstance(item, dict):
            return item
        for value in payload.values():
            found = opll_find_submission_attempt(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = opll_find_submission_attempt(value)
            if found:
                return found
    return {}

def opll_stripe_payload_diagnostics(payload, ctx: dict) -> str:
    if not isinstance(payload, dict):
        return f"payload_type={type(payload).__name__}"
    keys = ",".join(sorted(payload.keys()))
    urls = opll_collect_urls(payload)
    paypal_count = sum(1 for item in urls if opll_is_paypal_url(item))
    ba_count = sum(1 for item in urls if opll_is_paypal_ba_approve_url(item))
    ignored_count = sum(1 for item in urls if opll_is_ignored_resource_url(item))
    submission = opll_find_submission_attempt(payload)
    submission_state = str(submission.get("state") or "") if isinstance(submission, dict) else ""
    submission_fields = opll_submission_attempt_failure_fields(submission)
    submission_reason = opll_first_non_empty(submission_fields, "reason", "failure_reason", "decline_code", "failure_code", "code")
    submission_code = opll_first_non_empty(submission_fields, "code", "decline_code", "failure_code")
    submission_message = opll_first_non_empty(submission_fields, "message", "failure_message", "error")
    return (
        f"submission_reason={submission_reason or '无'}, submission_code={submission_code or '无'}, "
        f"submission_message={submission_message or '无'}, submission_state={submission_state or '未知'}, "
        f"submission_attempt={bool(submission)}, urls={len(urls)}, paypal_urls={paypal_count}, "
        f"ba_approve_urls={ba_count}, ignored_resource_urls={ignored_count}, "
        f"ctx_session={ctx.get('elements_session_id') or ''}, keys=[{keys}]"
    )

class OpllStripeRequiresApproval(Exception):
    pass

class OpllChatgptApproveBlocked(RuntimeError):
    pass

class OpllChatgptApproveException(Exception):
    pass

class OpllChatgptSentinelError(Exception):
    pass

OPLL_APPROVE_BLOCKED_EARLY_STOP = 3

OPLL_APPROVE_BLOCKED_BACKOFF_SECONDS = (1.5, 3.0, 5.0, 8.0)

OPLL_APPROVE_EXCEPTION_BACKOFF_SECONDS = (2.0, 4.0)

def opll_redact_approve_payload(payload, *, limit: int = 700) -> str:
    """Serialize the complete payload; only mandatory secret fields are hidden."""
    sensitive_key_parts = (
        "token",
        "authorization",
        "cookie",
        "secret",
        "password",
        "access",
        "client_secret",
        "card_number",
        "cvc",
        "cvv",
    )

    def scrub(value, key_hint: str = ""):
        key_l = str(key_hint or "").lower()
        if any(part in key_l for part in sensitive_key_parts):
            text = str(value or "")
            if not text:
                return ""
            if len(text) <= 8:
                return "***"
            return f"{text[:4]}…{text[-2:]}(len={len(text)})"
        if isinstance(value, dict):
            return {str(k): scrub(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(item, key_hint) for item in value]
        return value

    try:
        cleaned = scrub(payload if isinstance(payload, (dict, list)) else {"raw": payload})
        text = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        text = str(payload)
    return text

def opll_response_request_id(response) -> str:
    headers = getattr(response, "headers", {}) or {}
    for key in ("x-request-id", "request-id", "cf-ray", "x-amzn-trace-id"):
        value = str(headers.get(key) or headers.get(key.title()) or "").strip()
        if value:
            return opll_short_error(value, 120)
    return ""

def opll_chatgpt_checkout_warmup(
    chatgpt: requests.Session,
    cs_id: str,
    checkout: dict,
    *,
    diagnostic_log=None,
    diagnostic_prefix: str = "",
) -> None:
    """Warm the exact Checkout page inside one clean approval session."""

    entity = opll_processor_entity_for_country(
        checkout["billing_country"], checkout.get("processor_entity", "")
    )
    checkout_url = opll_chatgpt_checkout_page_url(
        str(cs_id or "").strip(),
        str(checkout.get("billing_country") or "US"),
        entity,
    )
    response = chatgpt.get(
        checkout_url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://chatgpt.com/",
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "same-origin",
            "upgrade-insecure-requests": "1",
        },
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    prefix = str(diagnostic_prefix or "")
    opll_emit_diagnostic(
        diagnostic_log,
        f"{prefix}Checkout 页面预热: HTTP {response.status_code} "
        f"request_id={opll_response_request_id(response) or '<missing>'}",
    )
    if response.status_code >= 400:
        raise OpllChatgptSentinelError(
            "Checkout 页面预热失败: "
            f"HTTP {response.status_code} {opll_complete_response_body(response)}"
        )

def opll_chatgpt_sentinel_ping(
    chatgpt: requests.Session,
    *,
    diagnostic_log=None,
    diagnostic_prefix: str = "",
    network_attempts: int = 1,
    network_retry_delays: tuple[float, ...] = (),
) -> dict:
    """Approve 前确认 Sentinel 可用；失败时禁止继续盲目提交审批。"""
    prefix = str(diagnostic_prefix or "")
    total_attempts = max(1, int(network_attempts or 1))
    response = None
    for attempt_index in range(total_attempts):
        attempt_no = attempt_index + 1
        try:
            response = chatgpt.post(
                "https://chatgpt.com/backend-api/sentinel/ping",
                json={},
                headers={
                    "Referer": "https://chatgpt.com/",
                    "x-openai-target-path": "/backend-api/sentinel/ping",
                    "x-openai-target-route": "/backend-api/sentinel/ping",
                },
                timeout=min(8.0, float(PAY_LONG_LINK_TIMEOUT or 30)),
            )
            break
        except Exception as exc:
            if (
                attempt_no >= total_attempts
                or not opll_is_retryable_http_transport_error(exc)
            ):
                raise OpllChatgptSentinelError(
                    f"sentinel ping network error: {opll_short_error(str(exc), 180)}"
                ) from exc
            delay = (
                float(network_retry_delays[attempt_index])
                if attempt_index < len(network_retry_delays)
                else 0.0
            )
            opll_emit_diagnostic(
                diagnostic_log,
                f"{prefix}sentinel ping 连接异常，第 {attempt_no}/{total_attempts} 次失败；"
                "Approve 尚未提交，保持原 Checkout 与原代理重试: "
                f"{opll_short_error(str(exc), 140)}",
            )
            if delay > 0:
                time.sleep(delay)
    if response is None:  # pragma: no cover - loop always returns or raises
        raise OpllChatgptSentinelError("sentinel ping ended without a response")
    raw_text = str(getattr(response, "text", "") or "")
    try:
        payload = response.json() or {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {"_non_object": payload}
    body_log = (
        opll_redact_approve_payload(payload)
        if payload
        else (opll_complete_response_body(raw_text) or "<empty>")
    )
    request_id = opll_response_request_id(response)
    opll_emit_diagnostic(
        diagnostic_log,
        f"{prefix}sentinel ping: HTTP {response.status_code}"
        f" request_id={request_id or '<missing>'} body={body_log}",
    )
    if response.status_code >= 400:
        raise OpllChatgptSentinelError(
            f"sentinel ping failed: HTTP {response.status_code}, request_id={request_id or '<missing>'}"
        )
    result = str(payload.get("result") or "").strip().lower()
    if result == "blocked":
        raise OpllChatgptApproveBlocked("sentinel ping retryable result: 'blocked'")
    if result == "exception" or isinstance(payload.get("error"), dict):
        raise OpllChatgptSentinelError(
            f"sentinel ping backend exception, request_id={request_id or '<missing>'}"
        )
    return payload

def opll_chatgpt_approve(
    chatgpt: requests.Session,
    cs_id: str,
    checkout: dict,
    *,
    diagnostic_log=None,
    diagnostic_prefix: str = "",
    sentinel_network_attempts: int = 1,
    sentinel_network_retry_delays: tuple[float, ...] = (),
    request_access_token: str = "",
    sentinel_required: bool = True,
    provider_session_request: bool = False,
    warm_checkout_page: bool = False,
) -> dict:
    entity = opll_processor_entity_for_country(checkout["billing_country"], checkout.get("processor_entity", ""))
    prefix = str(diagnostic_prefix or "")
    if warm_checkout_page:
        opll_chatgpt_checkout_warmup(
            chatgpt,
            cs_id,
            checkout,
            diagnostic_log=diagnostic_log,
            diagnostic_prefix=prefix,
        )
    if sentinel_required:
        opll_chatgpt_sentinel_ping(
            chatgpt,
            diagnostic_log=diagnostic_log,
            diagnostic_prefix=prefix,
            network_attempts=sentinel_network_attempts,
            network_retry_delays=sentinel_network_retry_delays,
        )
    if provider_session_request:
        token = (
            extract_access_token_from_session_text(request_access_token)
            or str(request_access_token or "").strip()
        )
        if not token:
            raise RuntimeError("Kakao Provider Approve 缺少 Access Token")
        approve_headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "oai-language": "ko-KR",
            "User-Agent": OPLL_USER_AGENT,
            "Referer": f"https://chatgpt.com/checkout/{entity}/{cs_id}",
        }
    else:
        approve_headers = {
            "Referer": f"https://chatgpt.com/checkout/{entity}/{cs_id}",
            "x-openai-target-path": "/backend-api/payments/checkout/approve",
            "x-openai-target-route": "/backend-api/payments/checkout/approve",
        }
    response = chatgpt.post(
        "https://chatgpt.com/backend-api/payments/checkout/approve",
        json={"checkout_session_id": cs_id, "processor_entity": entity},
        headers=approve_headers,
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    raw_text = str(response.text or "")
    try:
        payload = response.json() or {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {"_non_object": payload}
    # 始终记录完整响应；仅凭据、Token、Cookie、卡数据等秘密字段保留遮蔽。
    if payload:
        body_log = opll_redact_approve_payload(payload)
    else:
        body_log = opll_complete_response_body(raw_text) or "<empty>"
    opll_emit_diagnostic(
        diagnostic_log,
        f"{prefix}approve 响应: HTTP {response.status_code} entity={entity} "
        f"request_id={opll_response_request_id(response) or '<missing>'} body={body_log}",
    )
    if response.status_code >= 400:
        raise RuntimeError(f"chatgpt approve failed: HTTP {response.status_code} {opll_complete_response_body(raw_text)}")
    result = payload.get("result") if isinstance(payload, dict) else ""
    normalized_result = str(result or "").strip().lower()
    if normalized_result == "blocked":
        request_id = opll_response_request_id(response)
        detail_parts = []
        for key in ("reason", "code", "detail", "message"):
            value = payload.get(key)
            if isinstance(value, dict):
                value = (
                    value.get("code")
                    or value.get("reason")
                    or value.get("message")
                    or value.get("type")
                )
            text = opll_short_error(str(value or "").strip(), 160)
            if text:
                detail_parts.append(f"{key}={text}")
        if request_id:
            detail_parts.append(f"request_id={request_id}")
        suffix = f" ({', '.join(detail_parts)})" if detail_parts else ""
        blocked_error = OpllChatgptApproveBlocked(
            f"chatgpt approve result: 'blocked'{suffix}"
        )
        blocked_error.request_id = request_id  # type: ignore[attr-defined]
        blocked_error.response_payload = dict(payload)  # type: ignore[attr-defined]
        blocked_evidence = json.dumps(payload, ensure_ascii=False).lower()
        edge_markers = (
            "edge",
            "cloudflare",
            "cf-ray",
            "ip_blocked",
            "ip blocked",
            "geo_blocked",
            "proxy_blocked",
        )
        edge_or_ip_blocked = any(marker in blocked_evidence for marker in edge_markers)
        blocked_error.category = (  # type: ignore[attr-defined]
            "edge_or_ip_blocked" if edge_or_ip_blocked else "account_or_session_blocked"
        )
        # 只有明确的边缘/IP 证据才允许外层重新创建整笔 checkout 后换代理。
        blocked_error.retryable = edge_or_ip_blocked  # type: ignore[attr-defined]
        raise blocked_error
    if normalized_result == "exception":
        raise OpllChatgptApproveException("chatgpt approve backend result: 'exception'")
    if result not in {"approved", None, ""}:
        raise RuntimeError(f"chatgpt approve unexpected result: {result!r}")
    opll_emit_diagnostic(
        diagnostic_log,
        f"{prefix}ChatGPT approve 已受理: HTTP {response.status_code}, "
        f"result={normalized_result or 'accepted_without_result'}",
    )
    # result 为空 / None 只代表已受理；最终成功仍由 Stripe 跳转状态证明。
    return payload if isinstance(payload, dict) else {}

def opll_chatgpt_approve_with_retry(
    access_token: str,
    cs_id: str,
    checkout: dict,
    proxy_url: str | list[str] | tuple[str, ...] = "",
    request_locale: str = "en-US",
    attempts: int = 3,
    interval_seconds: float = 1.0,
    diagnostic_log=None,
    diagnostic_prefix: str = "[UPI] ",
    rotate_ip_each_attempt: bool = True,
    upstream_proxy_url: str | list[str] | tuple[str, ...] = "",
    proxy_exit: str = "",
    device_id: str = "",
    session_context: dict | None = None,
) -> requests.Session:
    """ChatGPT approve 重试。

    rotate_ip_each_attempt=True（默认）：
    - 提供商代理（用户名含 -sid-XXXXXXXX-t-）每次尝试刷新 sid → 换出口 IP
    - 传入多条 Approve 代理时按 attempt 轮询，并尽量刷 sid

    device_id：
    - 与 Create/Update 共用同一 oai-did；优先显式参数，否则读 checkout["oai_device_id"]
    - 换出口时只换 IP，不换设备指纹

    日志：
    - proxy_url 若是本地链 127.0.0.1，会配合 upstream_proxy_url 打印真实上游
      （host/region/sid），避免只看到 host=127.0.0.1
    - 本地链 + 可轮换 sid 的上游：请求直接走刷 sid 后的上游，保证每次真换出口
    """
    last_error = ""
    total_attempts = max(1, int(attempts or 1))
    prefix = str(diagnostic_prefix or "")
    consecutive_blocked = 0
    consecutive_exceptions = 0
    clean_session_blocked_retry_used = False
    # 整单固定设备指纹：Create 写入 checkout 后，Approve 全程复用
    oai_device_id = opll_resolve_oai_device_id(device_id, checkout)
    proxy_candidates = opll_normalize_approve_proxy_candidates(proxy_url)
    upstream_candidates = opll_normalize_approve_proxy_candidates(upstream_proxy_url)
    # 请求侧若是 loopback 链且没传上游，无法展示/轮换真实出口
    base_request = proxy_candidates[0] if proxy_candidates else ""
    rotate_pool = upstream_candidates or proxy_candidates
    used_proxy_fingerprints: list[str] = []
    previous_attempt_proxy = ""
    reuse_previous_proxy = False
    reuse_previous_reason = ""
    proxy_rotation_index = 0
    for attempt in range(total_attempts):
        attempt_no = attempt + 1
        reused_previous_proxy = bool(reuse_previous_proxy and previous_attempt_proxy)
        attempt_reuse_reason = reuse_previous_reason
        if reused_previous_proxy:
            # backend/Sentinel 异常或首次 blocked 都只重建干净会话，
            # 保留原 Checkout、原 sticky 出口和原设备指纹。
            attempt_proxy = previous_attempt_proxy
            log_request = attempt_proxy
            log_upstream = ""
            reuse_previous_proxy = False
            reuse_previous_reason = ""
        else:
            selection_index = proxy_rotation_index
            proxy_rotation_index += 1
            # 第一个候选也刷新原 sticky sid；后续候选继续换 sid/IP。
            force_new_sid = bool(rotate_ip_each_attempt) and (
                selection_index > 0
                or any(opll_proxy_has_rotatable_sid(item) for item in rotate_pool)
            )
            attempt_upstream = opll_pick_approve_proxy_for_attempt(
                rotate_pool,
                attempt_index=selection_index,
                force_new_sid=force_new_sid,
            )
            # 本地链 127.0.0.1 本身没有 sid；有可轮换上游时请求直接走上游，才能换出口 IP
            if (
                attempt_upstream
                and base_request
                and opll_proxy_is_loopback(base_request)
                and (
                    opll_proxy_has_rotatable_sid(attempt_upstream)
                    or not opll_proxy_is_loopback(attempt_upstream)
                )
            ):
                attempt_proxy = attempt_upstream
                log_request = base_request
                log_upstream = attempt_upstream
            elif attempt_upstream and not proxy_candidates:
                attempt_proxy = attempt_upstream
                log_request = attempt_upstream
                log_upstream = ""
            else:
                # 无独立上游池：在 request 候选上轮询/刷 sid
                attempt_proxy = opll_pick_approve_proxy_for_attempt(
                    proxy_candidates,
                    attempt_index=selection_index,
                    force_new_sid=force_new_sid,
                ) if proxy_candidates else attempt_upstream
                log_request = attempt_proxy
                log_upstream = (
                    attempt_upstream
                    if attempt_upstream and attempt_upstream != attempt_proxy
                    else ""
                )
        previous_attempt_proxy = attempt_proxy
        proxy_fp = opll_format_approve_proxy_fingerprint(
            log_request,
            upstream_proxy_url=log_upstream,
            proxy_exit=proxy_exit if attempt_no == 1 else "",
        )
        used_proxy_fingerprints.append(proxy_fp)
        opll_emit_diagnostic(
            diagnostic_log,
            f"{prefix}approve {attempt_no}/{total_attempts}"
            f"（{attempt_reuse_reason if reused_previous_proxy else '固定粘性出口'}: {proxy_fp}）",
        )
        chatgpt = None
        attempt_succeeded = False
        try:
            chatgpt = opll_build_chatgpt_session(
                access_token,
                attempt_proxy,
                request_locale=request_locale,
                device_id=oai_device_id,
                **({"session_context": session_context} if session_context else {}),
            )
            opll_chatgpt_approve(
                chatgpt,
                cs_id,
                checkout,
                diagnostic_log=diagnostic_log,
                diagnostic_prefix=prefix,
                warm_checkout_page=True,
            )
            opll_emit_diagnostic(diagnostic_log, f"{prefix}→ approve 成功（{proxy_fp}）")
            attempt_succeeded = True
            return chatgpt
        except (OpllChatgptApproveException, OpllChatgptSentinelError) as exc:
            consecutive_blocked = 0
            consecutive_exceptions += 1
            last_error = str(exc)
            if attempt_no < total_attempts:
                reuse_previous_proxy = consecutive_exceptions == 1
                reuse_previous_reason = (
                    "exception 后同代理干净会话"
                    if reuse_previous_proxy
                    else "异常后下一候选出口"
                )
                wait = OPLL_APPROVE_EXCEPTION_BACKOFF_SECONDS[
                    min(consecutive_exceptions - 1, len(OPLL_APPROVE_EXCEPTION_BACKOFF_SECONDS) - 1)
                ]
                opll_emit_diagnostic(
                    diagnostic_log,
                    f"{prefix}approve backend/sentinel exception: "
                    f"{opll_short_error(last_error, 180)}；退避 {wait:.1f}s 后"
                    f"{'同 IP 重试一次' if reuse_previous_proxy else '换 IP 重试'}",
                )
                time.sleep(wait)
                continue
        except OpllChatgptApproveBlocked as exc:
            consecutive_exceptions = 0
            consecutive_blocked += 1
            last_error = str(exc)
            opll_emit_diagnostic(
                diagnostic_log,
                f"{prefix}approve 可重试: {opll_short_error(last_error, 180)} "
                f"(连续 blocked {consecutive_blocked}/{OPLL_APPROVE_BLOCKED_EARLY_STOP}，本轮 {proxy_fp})",
            )
            if getattr(exc, "retryable", None) is False:
                if (
                    not clean_session_blocked_retry_used
                    and attempt_no < total_attempts
                ):
                    clean_session_blocked_retry_used = True
                    reuse_previous_proxy = True
                    reuse_previous_reason = "blocked 后同代理干净会话"
                    opll_emit_diagnostic(
                        diagnostic_log,
                        f"{prefix}首次 approve blocked；关闭当前会话，保持同一 Checkout、"
                        "同一粘性代理和同一设备 ID，仅隔离重试 Approve 一次",
                    )
                    continue
                opll_emit_diagnostic(
                    diagnostic_log,
                    f"{prefix}approve blocked 分类={getattr(exc, 'category', 'account_or_session_blocked')}；"
                    "停止当前 checkout，不在 Approve 阶段换 IP",
                )
                raise
            # 已换 IP 仍连续 blocked：多半是账号/session 被拦，再刷无益
            if consecutive_blocked >= OPLL_APPROVE_BLOCKED_EARLY_STOP:
                opll_emit_diagnostic(
                    diagnostic_log,
                    f"{prefix}approve 连续 blocked（已换 IP: {', '.join(used_proxy_fingerprints)}），"
                    "停止连刷，交由 Stripe poll 等待异步结果",
                )
                raise RuntimeError(
                    f"ChatGPT approve 连续 blocked({consecutive_blocked}): {last_error}"
                ) from exc
            if attempt_no < total_attempts:
                wait = OPLL_APPROVE_BLOCKED_BACKOFF_SECONDS[
                    min(consecutive_blocked - 1, len(OPLL_APPROVE_BLOCKED_BACKOFF_SECONDS) - 1)
                ]
                opll_emit_diagnostic(
                    diagnostic_log,
                    f"{prefix}approve blocked 退避 {wait:.1f}s 后换 IP 再试",
                )
                time.sleep(wait)
                continue
            raise
        except Exception as exc:
            consecutive_blocked = 0
            consecutive_exceptions = 0
            last_error = str(exc)
            # 对齐外部成功日志里常见的 confirm/approve 失败摘要
            opll_emit_diagnostic(
                diagnostic_log,
                f"{prefix}chatgpt approve failed: {opll_short_error(last_error, 220)}（{proxy_fp}）",
            )
            if attempt_no < total_attempts and interval_seconds > 0:
                time.sleep(interval_seconds)
            continue
        finally:
            if chatgpt is not None and not attempt_succeeded:
                try:
                    close_session = getattr(chatgpt, "close", None)
                    if callable(close_session):
                        close_session()
                except Exception:
                    pass
        if attempt_no < total_attempts and interval_seconds > 0:
            time.sleep(interval_seconds)
    raise RuntimeError(f"ChatGPT approve 连续失败: {last_error}")

def opll_stripe_payment_page_redirect_url(stripe: requests.Session, cs_id: str, stripe_pk: str, payment_locale: str = "en", timeout_seconds: int = 45, ctx: dict | None = None) -> str:
    deadline = time.time() + max(1, timeout_seconds)
    _browser_locale, elements_locale = locale_parts(payment_locale)
    ctx = ctx or {}
    params = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[session_id]": str(ctx.get("elements_session_id") or f"elements_session_{uuid.uuid4().hex[:11]}"),
        "elements_session_client[stripe_js_id]": str(ctx.get("stripe_js_id") or uuid.uuid4()),
        "elements_session_client[locale]": elements_locale,
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "key": stripe_pk,
        "_stripe_version": str(ctx.get("stripe_version") or STRIPE_VERSION_FULL),
    }
    last_err = ""
    while time.time() < deadline:
        response = stripe.get(
            f"https://api.stripe.com/v1/payment_pages/{cs_id}",
            params=params,
            timeout=PAY_LONG_LINK_TIMEOUT,
        )
        if response.status_code == 200:
            payload = response.json() or {}
            redirect_url = opll_extract_redirect_to_url(payload)
            if redirect_url:
                return redirect_url
            submission = opll_find_submission_attempt(payload)
            if submission.get("state") == "requires_approval":
                raise OpllStripeRequiresApproval("payment page requires ChatGPT approval")
            if submission.get("state") == "failed":
                raise RuntimeError(f"stripe submission failed: {opll_stripe_payload_diagnostics(payload, ctx)}")
            last_err = opll_stripe_payload_diagnostics(payload, ctx)
        else:
            last_err = f"HTTP {response.status_code} {opll_complete_response_body(response)}"
        time.sleep(1)
    raise RuntimeError(f"redirect url resolution timeout: {last_err}")

def opll_resolve_paypal_redirect_result(stripe: requests.Session, redirect_url: str, max_hops: int = 8) -> dict[str, str]:
    original = str(redirect_url or "").strip()
    current = original
    paypal_ba_approve_url = ""
    stripe_pm_redirect_url = ""
    last_url = ""

    for _ in range(max(1, max_hops)):
        current = str(current or "").strip()
        if not current:
            break
        last_url = current
        if opll_is_paypal_ba_approve_url(current):
            paypal_ba_approve_url = current
            break
        if opll_is_stripe_pm_redirect_url(current) and not stripe_pm_redirect_url:
            stripe_pm_redirect_url = current
        if opll_is_ignored_resource_url(current):
            break
        try:
            response = stripe.get(current, allow_redirects=False, timeout=PAY_LONG_LINK_TIMEOUT)
        except Exception:
            break
        if response.status_code not in (301, 302, 303, 307, 308):
            break
        headers = getattr(response, "headers", {}) or {}
        location = str(headers.get("Location") or headers.get("location") or "").strip()
        if not location:
            break
        current = urljoin(current, location)

    selected_url = paypal_ba_approve_url or stripe_pm_redirect_url
    payment_link_type = "paypal_approve" if paypal_ba_approve_url else ("paypal_stripe_redirect" if stripe_pm_redirect_url else "")
    return {
        "selected_url": selected_url,
        "payment_link_type": payment_link_type,
        "paypal_ba_approve_url": paypal_ba_approve_url,
        "stripe_pm_redirect_url": stripe_pm_redirect_url,
        "stripe_redirect_url": original,
        "provider_redirect_url": selected_url,
        "redirect_last_url": last_url,
    }

def opll_stripe_confirm(stripe: requests.Session, cs_id: str, pm_id: str, stripe_pk: str, init_payload: dict, ctx: dict, checkout: dict, stripe_hosted_url: str, payment_method_type: str = "paypal") -> dict:
    return_url = opll_stripe_confirm_return_url(cs_id, checkout, stripe_hosted_url)
    runtime_version = str(ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION)
    payment_method_type = str(payment_method_type or "paypal").strip().lower()
    response = stripe.post(
        f"https://api.stripe.com/v1/payment_pages/{cs_id}/confirm",
        data={
            "guid": str(ctx.get("guid") or ""),
            "muid": str(ctx.get("muid") or ""),
            "sid": str(ctx.get("sid") or ""),
            "payment_method": pm_id,
            "init_checksum": str(init_payload.get("init_checksum") or ctx.get("init_checksum") or ""),
            "version": runtime_version,
            "expected_amount": str(ctx.get("checkout_amount") or opll_expected_amount(init_payload)),
            "expected_payment_method_type": payment_method_type,
            "return_url": return_url,
            "elements_session_client[session_id]": ctx["elements_session_id"],
            "elements_session_client[locale]": str(ctx.get("locale") or "en"),
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[is_aggregation_expected]": "false",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[stripe_js_id]": ctx["stripe_js_id"],
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_options_client[saved_payment_method][enable_save]": "never",
            "elements_options_client[saved_payment_method][enable_redisplay]": "never",
            "client_attribution_metadata[client_session_id]": ctx["stripe_js_id"],
            "client_attribution_metadata[checkout_session_id]": cs_id,
            "client_attribution_metadata[checkout_config_id]": ctx.get("config_id") or "",
            "client_attribution_metadata[elements_session_id]": ctx["elements_session_id"],
            "client_attribution_metadata[elements_session_config_id]": ctx["elements_session_config_id"],
            "client_attribution_metadata[merchant_integration_source]": "checkout",
            "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
            "client_attribution_metadata[merchant_integration_version]": "custom",
            "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
            "client_attribution_metadata[payment_method_selection_flow]": "automatic",
            "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
            "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
            "consent[terms_of_service]": "accepted",
            "key": stripe_pk,
            "_stripe_version": str(ctx.get("stripe_version") or STRIPE_VERSION_FULL),
        },
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError(opll_stripe_error_summary("stripe confirm failed", response))
    return response.json() or {}

def opll_stripe_confirm_setup_intent_with_confirmation_token(
    stripe: requests.Session,
    client_secret: str,
    confirmation_token: str,
    return_url: str,
    stripe_pk: str,
    *,
    stripe_version: str = STRIPE_VERSION_FULL,
) -> dict:
    """Mirror Stripe.js ``confirmSetup`` for an OAICS PayPal SetupIntent."""
    client_secret = str(client_secret or "").strip()
    setup_intent_id = client_secret.split("_secret_", 1)[0]
    if not setup_intent_id.startswith("seti_") or "_secret_" not in client_secret:
        raise RuntimeError("PayPal OAICS SetupIntent 缺少有效 seti_ client_secret")
    confirmation_token = str(confirmation_token or "").strip()
    if not confirmation_token.startswith("ctoken_"):
        raise RuntimeError("PayPal OAICS SetupIntent 缺少有效 ctoken_ ConfirmationToken")
    return_url = str(return_url or "").strip()
    if not opll_is_external_url(return_url):
        raise RuntimeError("PayPal OAICS SetupIntent 缺少有效 confirm_return_url")
    response = stripe.post(
        f"https://api.stripe.com/v1/setup_intents/{setup_intent_id}/confirm",
        data={
            "client_secret": client_secret,
            "confirmation_token": confirmation_token,
            "return_url": return_url,
            "use_stripe_sdk": "true",
            "key": str(stripe_pk or "").strip() or DEFAULT_STRIPE_PK,
            "_stripe_version": str(stripe_version or STRIPE_VERSION_FULL),
        },
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            opll_stripe_error_summary("PayPal SetupIntent confirm failed", response)
        )
    payload = response.json() or {}
    if not isinstance(payload, dict):
        raise RuntimeError("PayPal SetupIntent confirm 响应格式无效")
    return payload

def opll_redirect_url_after_confirm(
    access_token: str,
    stripe: requests.Session,
    confirm_payload: dict,
    cs_id: str,
    stripe_pk: str,
    ctx: dict,
    checkout: dict,
    approve_proxy_url: str = "",
    *,
    stabilize_before_approve: bool = False,
    settle_seconds: float = PAYPAL_NATIVE_POST_CONFIRM_SETTLE_SECONDS,
    diagnostic_log=None,
    approve_upstream_proxy_url: str = "",
    chatgpt_session: requests.Session | OpllBrowserFetchSession | None = None,
    approve_checkout_id: str = "",
    session_context: dict | None = None,
) -> str:
    redirect_url = opll_extract_redirect_to_url(confirm_payload)
    if redirect_url:
        return redirect_url
    approve_country = str(
        checkout.get("payment_method_country")
        or checkout.get("billing_country")
        or ""
    ).upper()
    approve_request_locale = country_browser_locale(approve_country)
    approval_cs_id = str(approve_checkout_id or "").strip() or cs_id
    submission = opll_find_submission_attempt(confirm_payload)

    def approve_and_poll() -> str:
        if not stabilize_before_approve:
            if chatgpt_session is not None:
                opll_chatgpt_approve(
                    chatgpt_session,
                    approval_cs_id,
                    checkout,
                    diagnostic_log=diagnostic_log,
                    diagnostic_prefix="[PayPal 页面内] ",
                    warm_checkout_page=True,
                )
            else:
                opll_chatgpt_approve_with_retry(
                    access_token,
                    approval_cs_id,
                    checkout,
                    approve_proxy_url,
                    request_locale=approve_request_locale,
                    rotate_ip_each_attempt=False,
                    session_context=session_context,
                )
            return opll_stripe_payment_page_redirect_url(
                stripe,
                cs_id,
                stripe_pk,
                ctx=ctx,
                timeout_seconds=45,
            )

        total_attempts = min(
            PAYPAL_NATIVE_APPROVE_EXTRACT_ATTEMPTS,
            PAYPAL_NATIVE_APPROVE_CHECKOUT_IP_ATTEMPTS,
        )
        checkout_ip_attempts = min(
            total_attempts,
            PAYPAL_NATIVE_APPROVE_CHECKOUT_IP_ATTEMPTS,
        )
        rotation_candidates = (
            opll_normalize_approve_proxy_candidates(approve_upstream_proxy_url)
            or opll_normalize_approve_proxy_candidates(approve_proxy_url)
        )
        shared_device_id = opll_resolve_oai_device_id(checkout)
        last_error = ""
        force_rotate_next_attempt = False
        rotation_attempt_index = 0
        opll_emit_diagnostic(
            diagnostic_log,
            (
                "[PayPal] Camoufox 页面内启动 5 次提取：固定复用第一代理浏览器出口"
                if chatgpt_session is not None
                else (
                    "[PayPal] 提取重试固定复用本轮已占用的 Checkout IP；"
                    "失败后交由外层用从未使用的 IP 重建 Checkout"
                )
            ),
        )
        for attempt_idx in range(total_attempts):
            attempt_no = attempt_idx + 1
            rotate_after_submission_failure = force_rotate_next_attempt
            force_rotate_next_attempt = False
            use_checkout_ip = True
            if chatgpt_session is not None:
                attempt_proxy = approve_proxy_url
                proxy_fingerprint = "Camoufox 第一代理页面"
                ip_strategy = "页面内固定出口"
            elif use_checkout_ip:
                attempt_proxy = approve_proxy_url
                proxy_fingerprint = opll_format_approve_proxy_fingerprint(
                    attempt_proxy,
                    upstream_proxy_url=approve_upstream_proxy_url,
                )
                ip_strategy = "Checkout IP"
            else:
                attempt_proxy = opll_pick_approve_proxy_for_attempt(
                    rotation_candidates,
                    attempt_index=rotation_attempt_index,
                    force_new_sid=True,
                ) or approve_proxy_url
                rotation_attempt_index += 1
                proxy_fingerprint = opll_format_approve_proxy_fingerprint(attempt_proxy)
                ip_strategy = (
                    "submission 失败后立即刷新 IP"
                    if rotate_after_submission_failure
                    else "刷新 IP"
                )
            poll_timeout = PAYPAL_NATIVE_POST_APPROVE_POLL_SECONDS[
                min(attempt_idx, len(PAYPAL_NATIVE_POST_APPROVE_POLL_SECONDS) - 1)
            ]
            opll_emit_diagnostic(
                diagnostic_log,
                f"[PayPal] 提取尝试 {attempt_no}/{total_attempts}（{ip_strategy}: {proxy_fingerprint}）",
            )
            try:
                if chatgpt_session is not None:
                    opll_chatgpt_approve(
                        chatgpt_session,
                        approval_cs_id,
                        checkout,
                        diagnostic_log=diagnostic_log,
                        diagnostic_prefix=f"[PayPal 页面内] [{attempt_no}/{total_attempts}] ",
                        warm_checkout_page=True,
                    )
                else:
                    opll_chatgpt_approve_with_retry(
                        access_token,
                        approval_cs_id,
                        checkout,
                        attempt_proxy,
                        request_locale=approve_request_locale,
                        # Allow the first ordinary blocked response exactly one
                        # clean-session retry on the same Checkout and proxy.
                        attempts=2,
                        interval_seconds=0,
                        diagnostic_log=diagnostic_log,
                        diagnostic_prefix=f"[PayPal] [{attempt_no}/{total_attempts}] ",
                        rotate_ip_each_attempt=False,
                        upstream_proxy_url="",
                        device_id=shared_device_id,
                        session_context=session_context,
                    )
                return opll_stripe_payment_page_redirect_url(
                    stripe,
                    cs_id,
                    stripe_pk,
                    ctx=ctx,
                    timeout_seconds=poll_timeout,
                )
            except Exception as exc:
                last_error = str(exc)
                submission_requires_ip_rotation = (
                    opll_paypal_submission_requires_ip_rotation(
                        last_error
                    )
                )
                retryable = (
                    isinstance(exc, OpllStripeRequiresApproval)
                    or submission_requires_ip_rotation
                    or opll_paypal_extract_retryable(last_error)
                ) and not opll_is_non_retryable_link_error(exc)
                opll_emit_diagnostic(
                    diagnostic_log,
                    f"[PayPal] 提取尝试 {attempt_no}/{total_attempts} 未出链: "
                    f"{opll_short_error(last_error, 220)}",
                )
                if submission_requires_ip_rotation and attempt_no < total_attempts:
                    opll_emit_diagnostic(
                        diagnostic_log,
                        "[PayPal] Stripe submission 失败；本轮只在已占用的 Checkout IP "
                        "重建 Session 再试一次，仍失败则由外层换未使用 IP 并重建 Checkout",
                    )
                if attempt_no >= total_attempts or not retryable:
                    raise
                if PAYPAL_NATIVE_APPROVE_RETRY_INTERVAL_SECONDS > 0:
                    time.sleep(PAYPAL_NATIVE_APPROVE_RETRY_INTERVAL_SECONDS)
        raise RuntimeError(
            f"PayPal 连续提取 {total_attempts} 次仍未得到跳转链接: "
            f"{opll_short_error(last_error, 240)}"
        )

    def stabilize_and_approve() -> str:
        if not stabilize_before_approve:
            return approve_and_poll()
        settle_delay = max(0.0, float(settle_seconds or 0.0))
        if settle_delay > 0:
            opll_emit_diagnostic(
                diagnostic_log,
                f"[PayPal] Confirm 后等待 {settle_delay:g}s，再预检 Stripe 状态并执行 Approve",
            )
            time.sleep(settle_delay)
        try:
            settled_page = opll_stripe_retrieve_payment_page(
                stripe,
                cs_id,
                stripe_pk,
                ctx,
                preserve_envelope=True,
            )
        except Exception as exc:
            opll_emit_diagnostic(
                diagnostic_log,
                "[PayPal] Approve 前 Stripe 状态预检失败，继续使用 Confirm 状态审批: "
                f"{opll_short_error(str(exc), 180)}",
            )
        else:
            settled_redirect = opll_extract_redirect_to_url(settled_page)
            if settled_redirect:
                opll_emit_diagnostic(
                    diagnostic_log,
                    "[PayPal] Approve 前预检已得到跳转链接，跳过重复 Approve",
                )
                return settled_redirect
            settled_submission = opll_find_submission_attempt(settled_page)
            settled_state = str(settled_submission.get("state") or "").strip().lower()
            opll_emit_diagnostic(
                diagnostic_log,
                f"[PayPal] Approve 前 Stripe 状态={settled_state or '<missing>'}",
            )
            if settled_state == "failed":
                raise RuntimeError(
                    "stripe submission failed before approve: "
                    f"{opll_stripe_payload_diagnostics(settled_page, ctx)}"
                )
            if settled_state and settled_state != "requires_approval":
                try:
                    return opll_stripe_payment_page_redirect_url(
                        stripe,
                        cs_id,
                        stripe_pk,
                        ctx=ctx,
                        timeout_seconds=8,
                    )
                except OpllStripeRequiresApproval:
                    pass
        return approve_and_poll()

    if submission.get("state") == "requires_approval":
        return stabilize_and_approve()
    if submission.get("state") == "failed":
        raise RuntimeError(f"stripe submission failed: {opll_stripe_payload_diagnostics(confirm_payload, ctx)}")
    try:
        return opll_stripe_payment_page_redirect_url(stripe, cs_id, stripe_pk, ctx=ctx, timeout_seconds=30)
    except OpllStripeRequiresApproval:
        return stabilize_and_approve()

def opll_paypal_extract_retryable(detail: str) -> bool:
    """原生 PayPal Approve/Poll 未出链时可在同一 Checkout 内继续下一次提取。"""
    text = str(detail or "").lower()
    markers = (
        "requires_approval",
        "approval required",
        "redirect url resolution timeout",
        "timeout",
        "未提取到可用的 paypal",
    )
    return any(marker in text for marker in markers)

def opll_paypal_submission_requires_ip_rotation(detail: str) -> bool:
    """Stripe submission 失败时保留 Checkout，并要求下一次 Approve 立即换 IP。"""
    text = str(detail or "").lower()
    markers = (
        "checkout_approval_payment_failure",
        "generic_decline",
        "payment_failure",
        "submission_state=failed",
        "stripe submission failed",
    )
    return any(marker in text for marker in markers)

def opll_combo_attempt_order(country: str) -> list[tuple[str, str]]:
    requested = normalize_opll_country(country)
    ordered = [(requested, requested)]
    if requested == "DE":
        ordered.extend([("US", "US"), ("DE", "US"), ("US", "DE")])
    result = []
    seen = set()
    for item in ordered:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result

def opll_validate_extracted_paypal_link(
    provider_url: str,
    stripe_amount: str = "",
    target_amount: str = "",
    stripe_amount_source: str = "",
) -> None:
    """最终门禁：链接必须是可用的 PayPal/Stripe 成功跳转，且（若有目标金额）金额匹配。"""
    provider_url = str(provider_url or "").strip()
    if not provider_url:
        raise RuntimeError("提取结果为空，未得到可用支付链接")
    if not opll_is_paypal_success_url(provider_url):
        resource_hint = "仅发现 Stripe 资源 URL，未发现 PayPal BA approve 链；" if opll_is_ignored_resource_url(provider_url) else ""
        raise RuntimeError(f"{resource_hint}提取的链接不可用；当前结果: {provider_url}")
    if str(target_amount or "").strip():
        opll_apply_amount_check(
            {
                "stripe_amount": str(stripe_amount or "").strip(),
                "stripe_amount_source": str(stripe_amount_source or "").strip() or "final_validation",
            },
            target_amount,
        )

def opll_extract_paypal_from_oaics_checkout(
    access_token: str,
    checkout: dict,
    create_proxy_url: str = "",
    promotion_proxy_url: str = "",
    approve_proxy_url: str = "",
    target_amount: str = "",
    *,
    payment_method_country: str = "",
    apply_trial_promotion: bool = False,
    checkout_includes_trial_promo: bool = False,
    return_checkout_short: bool = False,
    paypal_flow: str = "",
    diagnostic_log=None,
    account_email: str = "",
    create_upstream_proxy_url: str = "",
    promotion_upstream_proxy_url: str = "",
    session_context: dict | None = None,
) -> dict:
    """Continue a PayPal extraction when Create returns an ``oaics_`` ID.

    ChatGPT Checkout requests keep the OpenAI ``oaics_`` identifier.  Stripe
    requests use a separately materialized ``cs_`` payment-page identifier; if
    ChatGPT instead returns a SetupIntent, the PayPal redirect is obtained from
    that intent without ever sending ``oaics_`` to a Stripe payment-page route.
    """

    checkout = dict(checkout or {})
    checkout_id = str(checkout.get("cs_id") or "").strip()
    if not checkout_id.startswith("oaics_"):
        raise RuntimeError(
            "OAICS PayPal 提链要求 Checkout 返回 oaics_；"
            f"当前为 {checkout_id[:24] or '<missing>'}"
        )

    create_proxy_url = str(create_proxy_url or "").strip()
    promotion_proxy_url = (
        str(promotion_proxy_url or "").strip() or create_proxy_url
    )
    approve_proxy_url = str(approve_proxy_url or "").strip() or create_proxy_url
    target_amount = str(target_amount or "").strip()
    checkout_country = normalize_opll_country(
        str(checkout.get("billing_country") or payment_method_country or "US")
    )
    checkout_currency = str(
        checkout.get("currency") or currency_for_country(checkout_country)
    ).strip().upper()
    pm_country = normalize_opll_country(
        str(payment_method_country or checkout_country)
    )
    locale_ctx = opll_locale_context_for_country(checkout_country)
    checkout["billing_country"] = checkout_country
    checkout["currency"] = checkout_currency
    checkout["processor_entity"] = opll_processor_entity_for_country(
        checkout_country,
        str(checkout.get("processor_entity") or ""),
    )

    raw_create_payload = checkout.get("_checkout_payload")
    if not isinstance(raw_create_payload, dict):
        raw_create_payload = {}
    public_checkout = {
        key: value
        for key, value in checkout.items()
        if key != "_checkout_payload"
    }
    primary_chatgpt = opll_build_chatgpt_session(
        access_token,
        create_proxy_url,
        request_locale=locale_ctx["request_locale"],
        device_id=str(checkout.get("oai_device_id") or ""),
        session_context=session_context,
    )

    promotion_payload: dict = {}
    promotion_requested = bool(
        apply_trial_promotion or checkout_includes_trial_promo
    )
    promotion_applied = False
    promotion_strategy = ""
    promotion_proof = ""
    promotion_proxy_used = ""

    checkout_payload = opll_chatgpt_fetch_checkout(
        access_token,
        checkout,
        create_proxy_url,
        request_locale=locale_ctx["request_locale"],
        chatgpt=primary_chatgpt,
    )
    materialized_payloads: list[tuple[str, dict]] = [
        ("checkout_create", raw_create_payload),
        ("checkout_update", promotion_payload),
        ("checkout_fetch", checkout_payload),
    ]

    amount = ""
    amount_source = "missing_payload"
    amount_stage = "checkout_fetch"
    amount_currency = ""
    declared_methods: list[str] = []

    def accept_payload(stage: str, payload: dict) -> None:
        nonlocal amount, amount_source, amount_stage, amount_currency
        if not isinstance(payload, dict) or not payload:
            return
        candidate_amount, candidate_source = opll_chatgpt_checkout_amount_info(
            payload
        )
        candidate_currency = opll_chatgpt_checkout_currency(payload)
        if candidate_amount:
            amount = candidate_amount
            amount_source = candidate_source
            amount_stage = stage
        if candidate_currency:
            normalized_currency = str(candidate_currency).strip().upper()
            if normalized_currency != checkout_currency:
                raise RuntimeError(
                    f"PayPal OAICS {stage} 币种必须为 {checkout_currency}；"
                    f"当前为 {normalized_currency}"
                )
            amount_currency = normalized_currency
        for method_type in opll_checkout_declared_payment_method_types(payload):
            if method_type not in declared_methods:
                declared_methods.append(method_type)

    for payload_stage, payload in materialized_payloads:
        accept_payload(payload_stage, payload)

    taxes_billing = opll_billing_for_country(
        checkout_country,
        account_email=account_email,
        access_token=access_token,
    )
    payment_billing = (
        taxes_billing
        if pm_country == checkout_country
        else opll_billing_for_country(
            pm_country,
            account_email=account_email,
            access_token=access_token,
        )
    )
    stripe_checkout_id = opll_extract_stripe_payment_page_id(
        *(payload for _stage, payload in materialized_payloads)
    )
    if not stripe_checkout_id:
        opll_emit_diagnostic(
            diagnostic_log,
        "[PayPal OAICS] Create/Fetch 尚无 Stripe cs_；"
            "第一代理使用 oaics_ 提交 Checkout Taxes 物化支付会话",
        )
        taxes_payload = opll_chatgpt_checkout_taxes(
            access_token,
            checkout,
            taxes_billing,
            create_proxy_url,
            request_locale=locale_ctx["request_locale"],
            diagnostic_log=diagnostic_log,
            require_success=True,
            flow_label="PayPal OAICS",
            chatgpt_session=primary_chatgpt,
        )
        accept_payload("checkout_taxes", taxes_payload)
        materialized_payloads.append(("checkout_taxes", taxes_payload))
        checkout_payload = opll_chatgpt_fetch_checkout(
            access_token,
            checkout,
            create_proxy_url,
            request_locale=locale_ctx["request_locale"],
            chatgpt=primary_chatgpt,
        )
        accept_payload("checkout_fetch_after_taxes", checkout_payload)
        materialized_payloads.append(
            ("checkout_fetch_after_taxes", checkout_payload)
        )
        stripe_checkout_id = opll_extract_stripe_payment_page_id(
            *(payload for _stage, payload in materialized_payloads)
        )

    needs_promotion_update = opll_should_update_checkout_promotion(
        apply_trial_promotion=apply_trial_promotion,
        checkout_includes_trial_promo=checkout_includes_trial_promo,
        target_amount=target_amount,
        actual_amount=amount,
    )
    if needs_promotion_update:
        opll_emit_diagnostic(
            diagnostic_log,
            "[PayPal OAICS] Taxes/Fetch 物化后金额仍未达到目标："
            f"目标={target_amount or '<未设置>'}，当前={amount or '<未知>'}；"
            "现在使用优惠代理对同一 oaics_ 执行 checkout/update",
        )
        promotion_payload = opll_chatgpt_update_checkout_promotion(
            access_token,
            checkout,
            promotion_proxy_url,
            request_locale=locale_ctx["request_locale"],
            device_id=str(checkout.get("oai_device_id") or ""),
            session=primary_chatgpt,
            session_context=session_context,
            include_checkout_context=True,
        )
        materialized_payloads.append(("checkout_update", promotion_payload))
        accept_payload("checkout_update", promotion_payload)
        updated_checkout_id = opll_extract_checkout_session_id(promotion_payload)
        if updated_checkout_id.startswith("oaics_"):
            checkout["cs_id"] = updated_checkout_id
            public_checkout["cs_id"] = updated_checkout_id
            checkout_id = updated_checkout_id
        updated_entity = opll_extract_processor_entity(promotion_payload)
        if updated_entity:
            checkout["processor_entity"] = updated_entity
            public_checkout["processor_entity"] = updated_entity
        promotion_proxy_used = (
            str(promotion_upstream_proxy_url or "").strip()
            or promotion_proxy_url
        )
        promotion_strategy = (
            "oaics_checkout_create_then_single_update"
            if checkout_includes_trial_promo
            else "oaics_checkout_update"
        )
        promotion_proof = "chatgpt_checkout_update_then_amount_validation"
        opll_emit_diagnostic(
            diagnostic_log,
            "[PayPal OAICS] checkout/update 已提交；第一代理重新读取同一 oaics_ 的金额状态",
        )
        checkout_payload = opll_chatgpt_fetch_checkout(
            access_token,
            checkout,
            create_proxy_url,
            request_locale=locale_ctx["request_locale"],
            chatgpt=primary_chatgpt,
        )
        accept_payload("checkout_fetch_after_update", checkout_payload)
        materialized_payloads.append(
            ("checkout_fetch_after_update", checkout_payload)
        )
        if target_amount and amount != target_amount and not stripe_checkout_id:
            opll_emit_diagnostic(
                diagnostic_log,
                "[PayPal OAICS] Update 后 Fetch 金额尚未同步；"
                "第一代理再次提交 Taxes 并复读 Checkout",
            )
            taxes_payload = opll_chatgpt_checkout_taxes(
                access_token,
                checkout,
                taxes_billing,
                create_proxy_url,
                request_locale=locale_ctx["request_locale"],
                diagnostic_log=diagnostic_log,
                require_success=True,
                flow_label="PayPal OAICS Update 后",
                chatgpt_session=primary_chatgpt,
            )
            accept_payload("checkout_taxes_after_update", taxes_payload)
            materialized_payloads.append(
                ("checkout_taxes_after_update", taxes_payload)
            )
            checkout_payload = opll_chatgpt_fetch_checkout(
                access_token,
                checkout,
                create_proxy_url,
                request_locale=locale_ctx["request_locale"],
                chatgpt=primary_chatgpt,
            )
            accept_payload(
                "checkout_fetch_after_update_taxes",
                checkout_payload,
            )
            materialized_payloads.append(
                ("checkout_fetch_after_update_taxes", checkout_payload)
            )
        stripe_checkout_id = opll_extract_stripe_payment_page_id(
            *(payload for _stage, payload in materialized_payloads)
        )

    if promotion_requested:
        if target_amount:
            promotion_applied = amount == target_amount
        elif needs_promotion_update:
            promotion_applied = True
        elif checkout_includes_trial_promo:
            promotion_applied = True
        if promotion_applied and not promotion_strategy:
            promotion_strategy = "checkout_create"
            promotion_proof = "oaics_checkout_create_amount_verified"

    if target_amount and not amount:
        raise AmountMismatchError(target_amount, "", f"{amount_stage}.missing_payload")
    amount_result = {
        "stripe_amount": amount,
        "stripe_amount_source": f"{amount_stage}.{amount_source}",
    }
    if not stripe_checkout_id:
        opll_apply_amount_check(amount_result, target_amount)
    if declared_methods and "paypal" not in declared_methods:
        raise PayPalMethodUnavailableError(declared_methods)

    chatgpt_checkout_url = opll_chatgpt_checkout_page_url(
        checkout_id,
        checkout_country,
        str(checkout.get("processor_entity") or ""),
    )
    if return_checkout_short:
        if not opll_is_chatgpt_checkout_page_url(chatgpt_checkout_url):
            raise RuntimeError("PayPal OAICS 未生成有效 ChatGPT Checkout 短链")
        short_result = {
            **public_checkout,
            "chatgpt_checkout_url": chatgpt_checkout_url,
            "checkout_url": chatgpt_checkout_url,
            "long_url": chatgpt_checkout_url,
            "payment_link_type": "chatgpt_checkout_short",
            **amount_result,
            "amount_currency": amount_currency or checkout_currency,
            "checkout_ui_mode": "custom",
            "checkout_payment_method_types": declared_methods,
            "paypal_available": True,
            "paypal_availability_status": "available",
            "paypal_flow": str(paypal_flow or "oaics_checkout"),
            "promotion_id": PIX_TRIAL_PROMOTION_ID if promotion_applied else "",
            "promotion_requested": promotion_requested,
            "promotion_applied": promotion_applied,
            "promotion_required": promotion_requested,
            "promotion_strategy": promotion_strategy,
            "promotion_proof": promotion_proof,
            "promotion_proxy": promotion_proxy_used,
        }
        return opll_apply_amount_check(short_result, target_amount)

    stripe = opll_build_stripe_session(
        create_proxy_url,
        request_locale=locale_ctx["request_locale"],
    )
    stripe_pk = opll_stripe_key_for_checkout(checkout)
    selection_payload: dict = {}
    if not stripe_checkout_id:
        opll_emit_diagnostic(
            diagnostic_log,
            "[PayPal OAICS] Taxes 后仍无 Stripe cs_；第一代理创建 PayPal "
            "ConfirmationToken，并使用 oaics_ 提交 ChatGPT checkout/confirm",
        )
        confirmation_token = opll_stripe_create_paypal_confirmation_token(
            stripe,
            payment_billing,
            stripe_pk,
            return_url=chatgpt_checkout_url,
        )
        selection_payload = opll_chatgpt_confirm_custom_payment_method(
            access_token,
            checkout,
            "paypal",
            create_proxy_url,
            request_locale=locale_ctx["request_locale"],
            chatgpt=primary_chatgpt,
            method_name="PayPal",
            sentinel_required=True,
            diagnostic_log=diagnostic_log,
            allow_builtin=True,
            confirmation_token=confirmation_token,
        )
        accept_payload("checkout_confirm_paypal", selection_payload)
        setup_client_secret = str(
            selection_payload.get("client_secret") or ""
        ).strip()
        setup_type = str(selection_payload.get("type") or "").strip().lower()
        if setup_type == "setup_intent" or setup_client_secret.startswith("seti_"):
            setup_confirm_payload = (
                opll_stripe_confirm_setup_intent_with_confirmation_token(
                    stripe,
                    setup_client_secret,
                    confirmation_token,
                    str(selection_payload.get("confirm_return_url") or "").strip(),
                    stripe_pk,
                )
            )
            stripe_redirect_url = opll_extract_redirect_to_url(
                setup_confirm_payload
            )
            if not stripe_redirect_url:
                raise RuntimeError(
                    "OAICS_PAYPAL_REDIRECT_MISSING: SetupIntent confirm "
                    "未返回 PayPal redirect_to_url"
                )
            redirect_result = opll_resolve_paypal_redirect_result(
                stripe,
                stripe_redirect_url,
            )
            provider_url = str(redirect_result.get("selected_url") or "").strip()
            opll_validate_extracted_paypal_link(
                provider_url,
                amount,
                target_amount,
                amount_result["stripe_amount_source"],
            )
            payment_proxy_used = (
                str(create_upstream_proxy_url or "").strip() or create_proxy_url
            )
            direct_result = {
                **public_checkout,
                "chatgpt_checkout_url": chatgpt_checkout_url,
                "checkout_url": provider_url,
                "long_url": provider_url,
                "payment_method_country": pm_country,
                "payment_method_id": str(
                    setup_confirm_payload.get("payment_method") or ""
                ),
                "stripe_payment_page_id": "",
                "stripe_hosted_url": "",
                "stripe_redirect_url": stripe_redirect_url,
                "stripe_pm_redirect_url": str(
                    redirect_result.get("stripe_pm_redirect_url") or ""
                ),
                "paypal_ba_approve_url": str(
                    redirect_result.get("paypal_ba_approve_url") or ""
                ),
                "provider_redirect_url": provider_url,
                "payment_link_type": str(
                    redirect_result.get("payment_link_type")
                    or "paypal_stripe_redirect"
                ),
                **amount_result,
                "amount_currency": amount_currency or checkout_currency,
                "checkout_ui_mode": "custom",
                "checkout_payment_method_types": declared_methods,
                "paypal_available": True,
                "paypal_availability_status": "available",
                "paypal_payment_method_types": ",".join(declared_methods),
                "paypal_flow": str(paypal_flow or "oaics_checkout"),
                "promotion_id": PIX_TRIAL_PROMOTION_ID if promotion_applied else "",
                "promotion_requested": promotion_requested,
                "promotion_applied": promotion_applied,
                "promotion_required": promotion_requested,
                "promotion_strategy": promotion_strategy,
                "promotion_proof": promotion_proof,
                "promotion_proxy": promotion_proxy_used,
                "create_proxy_used": payment_proxy_used,
                "payment_proxy_used": payment_proxy_used,
                "approve_proxy_used": payment_proxy_used,
                "request_execution": "python_http",
            }
            opll_emit_diagnostic(
                diagnostic_log,
                "[PayPal OAICS] 已使用 oaics_ 完成 Checkout Confirm，"
                "PayPal 支付链接提取完成",
            )
            return opll_apply_amount_check(direct_result, target_amount)

        stripe_checkout_id = opll_extract_stripe_payment_page_id(
            selection_payload
        )
        if not stripe_checkout_id:
            checkout_payload = opll_chatgpt_fetch_checkout(
                access_token,
                checkout,
                create_proxy_url,
                request_locale=locale_ctx["request_locale"],
                chatgpt=primary_chatgpt,
            )
            accept_payload("checkout_fetch_after_confirm", checkout_payload)
            stripe_checkout_id = opll_extract_stripe_payment_page_id(
                selection_payload,
                checkout_payload,
            )
    if not stripe_checkout_id.startswith("cs_"):
        raise RuntimeError(
            "OAICS_PAYMENT_ROUTE_MISSING: oaics_ Checkout 的 Create、Update、Fetch、"
            "Taxes 与 PayPal Confirm 均未返回 Stripe cs_/client_secret；"
            "已停止，未向 Stripe 发送 oaics_ ID"
        )

    stripe_checkout = {**checkout, "cs_id": stripe_checkout_id}
    opll_emit_diagnostic(
        diagnostic_log,
        "[PayPal OAICS] 已从 oaics_ 解析真实 Stripe Payment Page："
        f"{stripe_checkout_id[:20]}...；Stripe 请求仅使用 cs_，"
        "ChatGPT Approve 继续使用 oaics_",
    )
    ctx = opll_stripe_context({}, locale_ctx["payment_locale"])
    init_payload = opll_stripe_init(
        stripe_checkout_id,
        checkout_country,
        checkout_currency,
        create_proxy_url,
        payment_locale=locale_ctx["payment_locale"],
        stripe=stripe,
        ctx=ctx,
        checkout=stripe_checkout,
        browser_timezone=locale_ctx["browser_timezone"],
    )
    init_payload = opll_merge_payment_page_payload(init_payload)
    stripe_hosted_url = str(init_payload.get("stripe_hosted_url") or "").strip()
    if not stripe_hosted_url:
        raise RuntimeError(
            "PayPal OAICS Stripe init 未返回 hosted URL；"
            f"keys={sorted(init_payload.keys())}"
        )
    ctx = opll_stripe_context(
        init_payload,
        locale_ctx["payment_locale"],
        ctx=ctx,
    )
    if not ctx.get("currency"):
        ctx["currency"] = checkout_currency.lower()
    if promotion_requested and (promotion_applied or needs_promotion_update):
        opll_emit_diagnostic(
            diagnostic_log,
            "[PayPal OAICS] 优惠 Update 已完成；第一代理正在等待 Stripe 金额与 PayPal 状态稳定",
        )
        init_payload = opll_wait_for_us_tr_promoted_payment_page(
            stripe,
            stripe_checkout_id,
            stripe_pk,
            ctx,
            init_payload,
            promotion_already_proven=bool(
                promotion_applied or needs_promotion_update
            ),
            required_amount=target_amount,
            required_payment_method_type="paypal",
        )
    stripe_methods = opll_payment_method_types(init_payload)
    if "paypal" not in stripe_methods:
        raise PayPalMethodUnavailableError(stripe_methods)
    opll_require_payment_method_type(
        init_payload,
        "paypal",
        require_declared=True,
    )
    for method_type in stripe_methods:
        if method_type not in declared_methods:
            declared_methods.append(method_type)
    stripe_amount, stripe_amount_source = opll_stripe_amount_info(init_payload)
    opll_apply_amount_check(
        {
            "stripe_amount": stripe_amount,
            "stripe_amount_source": stripe_amount_source,
        },
        target_amount,
    )
    if promotion_requested and (
        not target_amount or stripe_amount == target_amount
    ):
        promotion_applied = True
        if needs_promotion_update:
            promotion_proof = "chatgpt_checkout_update_and_stripe_amount_verified"
    ctx = opll_stripe_context(
        init_payload,
        locale_ctx["payment_locale"],
        ctx=ctx,
    )
    pm_id = opll_stripe_create_paypal_method(
        stripe,
        stripe_checkout_id,
        ctx,
        payment_billing,
        stripe_pk,
    )
    confirm_payload = opll_stripe_confirm(
        stripe,
        stripe_checkout_id,
        pm_id,
        stripe_pk,
        init_payload,
        ctx,
        stripe_checkout,
        stripe_hosted_url,
    )
    stripe_redirect_url = opll_redirect_url_after_confirm(
        access_token,
        stripe,
        confirm_payload,
        stripe_checkout_id,
        stripe_pk,
        ctx,
        checkout,
        approve_proxy_url,
        stabilize_before_approve=True,
        diagnostic_log=diagnostic_log,
        approve_upstream_proxy_url=(
            str(create_upstream_proxy_url or "").strip() or create_proxy_url
        ),
        approve_checkout_id=checkout_id,
        session_context=session_context,
    )
    redirect_result = opll_resolve_paypal_redirect_result(
        stripe,
        stripe_redirect_url,
    )
    provider_url = str(redirect_result.get("selected_url") or "").strip()
    opll_validate_extracted_paypal_link(
        provider_url,
        stripe_amount,
        target_amount,
        stripe_amount_source,
    )
    payment_proxy_used = (
        str(create_upstream_proxy_url or "").strip() or create_proxy_url
    )
    result = {
        **public_checkout,
        "chatgpt_checkout_url": chatgpt_checkout_url,
        "checkout_url": provider_url,
        "long_url": provider_url,
        "payment_method_country": pm_country,
        "payment_method_id": pm_id,
        "stripe_payment_page_id": stripe_checkout_id,
        "stripe_hosted_url": stripe_hosted_url,
        "stripe_redirect_url": stripe_redirect_url,
        "stripe_pm_redirect_url": str(
            redirect_result.get("stripe_pm_redirect_url") or ""
        ),
        "paypal_ba_approve_url": str(
            redirect_result.get("paypal_ba_approve_url") or ""
        ),
        "provider_redirect_url": provider_url,
        "payment_link_type": str(
            redirect_result.get("payment_link_type") or "paypal_stripe_redirect"
        ),
        "stripe_amount": stripe_amount,
        "stripe_amount_source": stripe_amount_source,
        "amount_currency": checkout_currency,
        "checkout_ui_mode": "custom",
        "checkout_payment_method_types": declared_methods,
        "paypal_available": True,
        "paypal_availability_status": "available",
        "paypal_payment_method_types": ",".join(declared_methods),
        "paypal_flow": str(paypal_flow or "oaics_checkout"),
        "promotion_id": PIX_TRIAL_PROMOTION_ID if promotion_applied else "",
        "promotion_requested": promotion_requested,
        "promotion_applied": promotion_applied,
        "promotion_required": promotion_requested,
        "promotion_strategy": promotion_strategy,
        "promotion_proof": promotion_proof,
        "promotion_proxy": promotion_proxy_used,
        "create_proxy_used": payment_proxy_used,
        "payment_proxy_used": payment_proxy_used,
        "approve_proxy_used": payment_proxy_used,
        "request_execution": "python_http",
    }
    opll_emit_diagnostic(
        diagnostic_log,
        "[PayPal OAICS] oaics_ Checkout 的 PayPal 支付链接提取完成",
    )
    return opll_apply_amount_check(result, target_amount)

def generate_opll_paypal_long_link(
    access_token: str,
    country: str,
    currency: str,
    create_proxy_url: str = "",
    followup_proxy_url: str = "",
    approve_proxy_url: str = "",
    target_amount: str = "",
    *,
    paypal_flow: str = "",
    diagnostic_log=None,
    account_email: str = "",
    create_upstream_proxy_url: str = "",
    promotion_upstream_proxy_url: str = "",
    browser_runtime: dict | None = None,
    return_checkout_short: bool = False,
    payment_method_country: str = "",
    checkout_includes_trial_promo: bool = False,
    checkout_create_promotion_only: bool = False,
    sentinel_so_enabled: bool = False,
    session_context: dict | None = None,
) -> dict:
    create_proxy_url = str(create_proxy_url or "").strip()
    configured_followup_proxy_url = str(followup_proxy_url or "").strip()
    followup_proxy_url = configured_followup_proxy_url or create_proxy_url
    approve_proxy_url = str(approve_proxy_url or "").strip() or followup_proxy_url
    create_upstream_proxy_url = str(create_upstream_proxy_url or "").strip()
    promotion_upstream_proxy_url = str(promotion_upstream_proxy_url or "").strip()
    account_email = str(account_email or "").strip()
    failures: list[str] = []
    requested_country = normalize_opll_country(country)
    currency_country = requested_country
    normalized_target_amount = str(target_amount or "").strip()
    normalized_paypal_flow = str(paypal_flow or "").strip().lower()
    checkout_create_promotion_only = bool(checkout_create_promotion_only)
    return_checkout_short = bool(return_checkout_short)
    browser_runtime = browser_runtime if isinstance(browser_runtime, dict) else {}
    is_br_de_strict_zero = normalized_paypal_flow == PAYPAL_BR_DE_STRICT_ZERO_FLOW
    strict_zero_checkout_includes_trial_promo = bool(
        is_br_de_strict_zero
        and (checkout_includes_trial_promo or checkout_create_promotion_only)
    )
    strict_zero_payment_method_country = ""
    # JP 严格零：与 DE 相同后置活动门禁；沿用界面默认三段代理，不强制合并/不校验出口。
    is_jp_strict_zero = (
        normalized_paypal_flow == PAYPAL_JP_STRICT_ZERO_FLOW
        or (
            not normalized_paypal_flow
            and requested_country == "JP"
            and normalized_target_amount == "0"
        )
    )
    is_eur_jp_style_zero = normalized_paypal_flow == PAYPAL_EUR_JP_STYLE_ZERO_FLOW
    is_fr_ideal_style_zero = normalized_paypal_flow == PAYPAL_FR_IDEAL_STYLE_ZERO_FLOW
    is_de_native_promotion = normalized_paypal_flow == PAYPAL_DE_NATIVE_PROMO_FLOW
    is_gb_zero_post_promotion = (
        requested_country == "GB"
        and normalized_target_amount == "0"
        and not normalized_paypal_flow
    )
    if is_gb_zero_post_promotion:
        # GB/GBP 的支付链始终跟随第一代理。调用方传入同一代理时，优惠
        # Update 也复用第一代理；旧双代理调用仍只允许第二代理提交 Update。
        approve_proxy_url = create_proxy_url
        if configured_followup_proxy_url == create_proxy_url:
            opll_emit_diagnostic(
                diagnostic_log,
                "[PayPal GB] Checkout 创建时直接携带优惠；Stripe、Confirm、Approve 和轮询"
                "全程复用英国第一代理，不提交优惠 Update",
            )
        else:
            opll_emit_diagnostic(
                diagnostic_log,
                "[PayPal GB] 第一代理负责 Checkout、Stripe、Confirm、Approve 和轮询；"
                "第二代理仅负责优惠 Update",
            )
    if return_checkout_short and not is_de_native_promotion:
        raise RuntimeError("PayPal Checkout 短链仅支持 DE/EUR 原生优惠流程")
    native_primary_proxy_url = create_proxy_url
    native_promotion_proxy_url = configured_followup_proxy_url or create_proxy_url
    if is_br_de_strict_zero:
        requested_country = "DE"
        target_amount = "0"
        normalized_target_amount = "0"
        if not create_proxy_url:
            raise RuntimeError("PayPal DE/EUR 严格 0 流程需要配置第一代理作为主链")
        if (
            not strict_zero_checkout_includes_trial_promo
            and not configured_followup_proxy_url
        ):
            raise RuntimeError("PayPal DE/EUR 严格 0 流程需要配置第二代理用于优惠 Update")
        # Checkout 始终保持 DE/EUR；PayPal Payment Method 的账单地址可由界面
        # 选择跟随支付模式的货币国家，或跟随第一代理真实出口国家。
        strict_zero_payment_method_country = str(
            payment_method_country or currency_country
        ).strip().upper()
        if strict_zero_payment_method_country not in OPENAI_SUPPORTED_COUNTRY_CODES:
            raise RuntimeError(
                "PayPal DE/EUR 严格 0 流程无法使用所选账单国家："
                f"{strict_zero_payment_method_country or '<missing>'}"
            )
        followup_proxy_url = (
            create_proxy_url
            if strict_zero_checkout_includes_trial_promo
            else configured_followup_proxy_url
        )
        approve_proxy_url = create_proxy_url
        billing_strategy = (
            f"账单与 Payment Method 跟随第一代理 IP 国家 {strict_zero_payment_method_country}"
            if payment_method_country
            else f"账单与 Payment Method 跟随货币国家 {currency_country}"
        )
        proxy_strategy = (
                "Checkout 创建时直接携带优惠；后续仅校验金额，不提交优惠 Update"
            if strict_zero_checkout_includes_trial_promo
            else "第二代理仅负责优惠 Update"
        )
        opll_emit_diagnostic(
            diagnostic_log,
            "[PayPal] 步骤 1/7：准备 DE/EUR 严格 0 流程；Checkout 固定 DE/EUR，"
            f"{billing_strategy}，{proxy_strategy}",
        )
    elif is_jp_strict_zero:
        requested_country = "JP"
        target_amount = "0"
        normalized_target_amount = "0"
        opll_emit_diagnostic(diagnostic_log, "[PayPal] 步骤 1/7：正在创建支付连接，准备 JP/JPY 严格 0 流程（不校验代理出口）")
    elif is_eur_jp_style_zero:
        requested_country = "DE"
        target_amount = "0"
        normalized_target_amount = "0"
        opll_emit_diagnostic(
            diagnostic_log,
            "[PayPal] 步骤 1/7：正在创建支付连接，准备 DE/EUR JP 逻辑严格 0 流程（不校验代理出口）",
        )
    elif is_fr_ideal_style_zero:
        requested_country = "FR"
        target_amount = "0"
        normalized_target_amount = "0"
        if not create_proxy_url:
            raise RuntimeError("PayPal FR PIX式流程需要配置第一代理")
        if not configured_followup_proxy_url:
            raise RuntimeError("PayPal FR PIX式流程需要配置第二代理")
        strict_zero_payment_method_country = str(
            payment_method_country or ""
        ).strip().upper()
        if strict_zero_payment_method_country not in OPENAI_SUPPORTED_COUNTRY_CODES:
            raise RuntimeError(
                "PayPal FR PIX式流程无法使用第一代理出口国家作为账单国家："
                f"{strict_zero_payment_method_country or '<missing>'}"
            )
        approve_proxy_url = create_proxy_url
        opll_emit_diagnostic(
            diagnostic_log,
            "[PayPal FR] 步骤 1/7：准备 FR/EUR 严格 0 PIX式流程；"
            f"第一代理出口={strict_zero_payment_method_country}，账单国家跟随第一代理",
        )
    elif is_de_native_promotion:
        requested_country = "DE"
        if not native_primary_proxy_url:
            raise RuntimeError("PayPal 原生优惠流程需要配置第一代理或本地代理")
        if not native_promotion_proxy_url:
            raise RuntimeError("PayPal 原生优惠流程需要配置第二代理用于更新优惠")
        approve_proxy_url = native_primary_proxy_url
        opll_emit_diagnostic(
            diagnostic_log,
            "[PayPal 原生优惠] 步骤 1/7：第一代理使用 DE 出口负责 Token、Checkout、Stripe、Confirm、Approve；"
            "第二代理仅负责更新优惠",
        )
        opll_validate_access_token(
            access_token,
            native_primary_proxy_url,
            request_locale="de-DE",
            **(
                {"session": browser_runtime.get("primary_chatgpt_session")}
                if browser_runtime.get("primary_chatgpt_session") is not None
                else {}
            ),
        )
        if browser_runtime:
            opll_emit_diagnostic(
                diagnostic_log,
                "[PayPal 原生优惠] Token 验证已从第一代理 Camoufox 页面内部发出",
            )
    else:
        opll_emit_diagnostic(diagnostic_log, "[PayPal] 步骤 1/7：正在创建支付连接")
    is_br_post_promotion = requested_country == "BR"
    is_strict_zero_post_promotion = (
        is_br_de_strict_zero
        or is_jp_strict_zero
        or is_eur_jp_style_zero
        or is_fr_ideal_style_zero
        or (
            requested_country == "FR"
            and normalized_target_amount == "0"
        )
        or is_gb_zero_post_promotion
    )
    is_post_checkout_promotion = is_br_post_promotion or is_strict_zero_post_promotion
    if is_br_de_strict_zero:
        strict_proxy_locale_ctx = opll_locale_context_for_country(
            strict_zero_payment_method_country
        )
        post_request_locale = strict_proxy_locale_ctx["request_locale"]
        post_payment_locale = strict_proxy_locale_ctx["payment_locale"]
        post_timezone = strict_proxy_locale_ctx["browser_timezone"]
    elif is_br_post_promotion:
        post_request_locale, post_payment_locale, post_timezone = "pt-BR", "pt-BR", "America/Sao_Paulo"
    elif is_jp_strict_zero:
        post_request_locale, post_payment_locale, post_timezone = "ja-JP", "ja", "Asia/Tokyo"
    elif is_eur_jp_style_zero:
        post_request_locale, post_payment_locale, post_timezone = "de-DE", "de", "Europe/Berlin"
    elif is_fr_ideal_style_zero:
        fr_proxy_locale_ctx = opll_locale_context_for_country(
            strict_zero_payment_method_country
        )
        post_request_locale = fr_proxy_locale_ctx["request_locale"]
        post_payment_locale = fr_proxy_locale_ctx["payment_locale"]
        post_timezone = fr_proxy_locale_ctx["browser_timezone"]
    elif is_de_native_promotion:
        post_request_locale, post_payment_locale, post_timezone = "de-DE", "de", "Europe/Berlin"
    else:
        # 普通/严格零 FR·GB 等：按账单国家匹配 locale/timezone，不再默认 Asia/Shanghai
        locale_ctx = opll_locale_context_for_country(requested_country)
        post_request_locale = locale_ctx["request_locale"]
        post_payment_locale = locale_ctx["payment_locale"]
        post_timezone = locale_ctx["browser_timezone"]
    if is_br_de_strict_zero:
        attempt_order = [("DE", strict_zero_payment_method_country)]
    elif is_jp_strict_zero:
        attempt_order = [("JP", "JP")]
    elif is_eur_jp_style_zero:
        attempt_order = [("DE", "DE")]
    elif is_fr_ideal_style_zero:
        attempt_order = [("FR", strict_zero_payment_method_country)]
    elif is_de_native_promotion:
        attempt_order = [("DE", "DE")]
    else:
        attempt_order = opll_combo_attempt_order(requested_country)
    create_proxy_rotated = False
    promotion_used_proxy = ""
    for checkout_country, pm_country in attempt_order:
        try:
            checkout_promo_text = (
                "第一代理创建并直接带优惠"
                if (strict_zero_checkout_includes_trial_promo or checkout_create_promotion_only)
                else
                "第一代理创建，暂不带优惠"
                if is_de_native_promotion
                else "不带优惠" if is_post_checkout_promotion
                else "按当前配置"
            )
            opll_emit_diagnostic(
                diagnostic_log,
                f"[PayPal] 步骤 2/7：创建 checkout；正在创建 {checkout_country}/{currency_for_country(checkout_country)} Checkout（{checkout_promo_text}）",
            )
            if is_fr_ideal_style_zero:
                checkout = opll_create_checkout(
                    access_token,
                    "FR",
                    "EUR",
                    create_proxy_url,
                    request_locale=post_request_locale,
                    include_trial_promo=checkout_create_promotion_only,
                    sentinel_so_enabled=sentinel_so_enabled,
                    diagnostic_log=diagnostic_log,
                    session_context=session_context,
                )
            elif is_de_native_promotion:
                checkout = opll_create_checkout(
                    access_token,
                    "DE",
                    "EUR",
                    native_primary_proxy_url,
                    request_locale=post_request_locale,
                    include_trial_promo=checkout_create_promotion_only,
                    checkout_ui_mode="hosted",
                    sentinel_so_enabled=sentinel_so_enabled,
                    diagnostic_log=diagnostic_log,
                    session_context=session_context,
                    **(
                        {"session": browser_runtime.get("primary_chatgpt_session")}
                        if browser_runtime.get("primary_chatgpt_session") is not None
                        else {}
                    ),
                )
            elif is_post_checkout_promotion:
                checkout = opll_create_checkout(
                    access_token,
                    checkout_country,
                    currency_for_country(checkout_country),
                    create_proxy_url,
                    request_locale=post_request_locale,
                    include_trial_promo=(
                        strict_zero_checkout_includes_trial_promo
                        or checkout_create_promotion_only
                    ),
                    sentinel_so_enabled=sentinel_so_enabled,
                    diagnostic_log=diagnostic_log,
                    session_context=session_context,
                )
            else:
                checkout = opll_create_checkout(
                    access_token,
                    checkout_country,
                    currency_for_country(checkout_country),
                    create_proxy_url,
                    request_locale=post_request_locale,
                    include_trial_promo=True,
                    sentinel_so_enabled=sentinel_so_enabled,
                    diagnostic_log=diagnostic_log,
                    session_context=session_context,
                )
            if is_br_de_strict_zero:
                checkout_billing_country = str(checkout.get("billing_country") or "").upper()
                checkout_currency = str(checkout.get("currency") or "").upper()
                checkout_entity = opll_processor_entity_for_country(
                    checkout_billing_country,
                    str(checkout.get("processor_entity") or ""),
                )
                if (checkout_billing_country, checkout_currency) != ("DE", "EUR"):
                    raise RuntimeError(
                        "DE 严格零流程要求 Checkout 固定为 DE/EUR；"
                        f"当前为 {checkout_billing_country or '<missing>'}/{checkout_currency or '<missing>'}"
                    )
                if checkout_entity != "openai_ie":
                    raise RuntimeError(
                        "DE 严格零流程要求欧洲 OpenAI 处理实体 openai_ie；"
                        f"当前为 {checkout_entity or '<missing>'}"
                    )
            elif is_jp_strict_zero:
                checkout_billing_country = str(checkout.get("billing_country") or "").upper()
                checkout_currency = str(checkout.get("currency") or "").upper()
                if (checkout_billing_country, checkout_currency) != ("JP", "JPY"):
                    raise RuntimeError(
                        "JP 严格零流程要求 Checkout 固定为 JP/JPY；"
                        f"当前为 {checkout_billing_country or '<missing>'}/{checkout_currency or '<missing>'}"
                    )
            elif is_eur_jp_style_zero:
                checkout_billing_country = str(checkout.get("billing_country") or "").upper()
                checkout_currency = str(checkout.get("currency") or "").upper()
                checkout_entity = opll_processor_entity_for_country(
                    checkout_billing_country,
                    str(checkout.get("processor_entity") or ""),
                )
                if (checkout_billing_country, checkout_currency) != ("DE", "EUR"):
                    raise RuntimeError(
                        "DE/EUR JP 逻辑流程要求 Checkout 固定为 DE/EUR；"
                        f"当前为 {checkout_billing_country or '<missing>'}/{checkout_currency or '<missing>'}"
                    )
                if checkout_entity != "openai_ie":
                    raise RuntimeError(
                        "DE/EUR JP 逻辑流程要求欧洲 OpenAI 处理实体 openai_ie；"
                        f"当前为 {checkout_entity or '<missing>'}"
                    )
            elif is_fr_ideal_style_zero:
                checkout_billing_country = str(checkout.get("billing_country") or "").upper()
                checkout_currency = str(checkout.get("currency") or "").upper()
                if (checkout_billing_country, checkout_currency) != ("FR", "EUR"):
                    raise RuntimeError(
                        "PayPal FR PIX式流程要求 Checkout 固定为 FR/EUR；"
                        f"当前为 {checkout_billing_country or '<missing>'}/{checkout_currency or '<missing>'}"
                    )
            elif is_de_native_promotion:
                checkout_billing_country = str(checkout.get("billing_country") or "").upper()
                checkout_currency = str(checkout.get("currency") or "").upper()
                if (checkout_billing_country, checkout_currency) != ("DE", "EUR"):
                    raise RuntimeError(
                        "PayPal 原生优惠流程要求 Checkout 固定为 DE/EUR；"
                        f"当前为 {checkout_billing_country or '<missing>'}/{checkout_currency or '<missing>'}"
                    )
            checkout_id = str(checkout.get("cs_id") or "").strip()
            if checkout_id.startswith("oaics_"):
                opll_emit_diagnostic(
                    diagnostic_log,
                    "[PayPal] Checkout 返回 oaics_；切换到 OAICS 提链，"
                    "不再把该 ID 发送给 Stripe Payment Page API",
                )
                oaics_promotion_required = bool(
                    is_post_checkout_promotion or is_de_native_promotion
                )
                return opll_extract_paypal_from_oaics_checkout(
                    access_token,
                    checkout,
                    create_proxy_url=(
                        native_primary_proxy_url
                        if is_de_native_promotion
                        else create_proxy_url
                    ),
                    promotion_proxy_url=(
                        native_promotion_proxy_url
                        if is_de_native_promotion
                        else followup_proxy_url
                    ),
                    approve_proxy_url=approve_proxy_url,
                    target_amount=target_amount,
                    payment_method_country=pm_country,
                    # Create-time promotion is still only a request.  Keep one
                    # bounded Update available when the materialized amount
                    # proves that request did not take effect.
                    apply_trial_promotion=bool(
                        oaics_promotion_required
                        or strict_zero_checkout_includes_trial_promo
                        or checkout_create_promotion_only
                    ),
                    checkout_includes_trial_promo=(
                        strict_zero_checkout_includes_trial_promo
                        or checkout_create_promotion_only
                    ),
                    return_checkout_short=return_checkout_short,
                    paypal_flow=normalized_paypal_flow,
                    diagnostic_log=diagnostic_log,
                    account_email=account_email,
                    create_upstream_proxy_url=create_upstream_proxy_url,
                    promotion_upstream_proxy_url=promotion_upstream_proxy_url,
                    session_context=session_context,
                )
            opll_emit_diagnostic(diagnostic_log, "[PayPal] 步骤 3/7：stripe init；Checkout 已创建，正在初始化 Stripe Payment Page")
            billing_locale_ctx = opll_locale_context_for_country(checkout.get("billing_country") or checkout_country)
            stripe_request_locale = (
                post_request_locale
                if (is_br_post_promotion or is_jp_strict_zero or is_eur_jp_style_zero or is_br_de_strict_zero or is_strict_zero_post_promotion)
                else billing_locale_ctx["request_locale"]
            )
            stripe_proxy_url = (
                native_primary_proxy_url
                if is_de_native_promotion
                else create_proxy_url
                if (
                    is_br_de_strict_zero
                    or is_fr_ideal_style_zero
                    or is_gb_zero_post_promotion
                )
                else followup_proxy_url
            )
            stripe_session_factory = browser_runtime.get("stripe_session_factory")
            stripe = (
                stripe_session_factory(checkout)
                if is_de_native_promotion and callable(stripe_session_factory)
                else opll_build_stripe_session(
                    stripe_proxy_url,
                    request_locale=stripe_request_locale,
                )
            )
            if is_de_native_promotion and browser_runtime:
                opll_emit_diagnostic(
                    diagnostic_log,
                    "[PayPal 原生优惠] Checkout、Stripe init、Payment Method、Confirm、Approve "
                    "均由第一代理 Camoufox 页面内部发出",
                )
            if is_br_post_promotion or is_jp_strict_zero or is_eur_jp_style_zero or is_br_de_strict_zero or is_strict_zero_post_promotion:
                init_payment_locale = post_payment_locale
                init_timezone = post_timezone
            else:
                init_payment_locale = billing_locale_ctx["payment_locale"]
                init_timezone = billing_locale_ctx["browser_timezone"]
            # Build the client context before Init so the exact same Stripe.js,
            # Elements, guid, muid, and sid identifiers survive every stage.
            ctx = opll_stripe_context({}, init_payment_locale)
            init_payload = opll_stripe_init(
                checkout["cs_id"],
                checkout["billing_country"],
                checkout["currency"],
                stripe_proxy_url,
                payment_locale=init_payment_locale,
                stripe=stripe,
                ctx=ctx,
                checkout=checkout,
                browser_timezone=init_timezone,
            )
            stripe_hosted_url = str(init_payload.get("stripe_hosted_url") or "").strip()
            if not stripe_hosted_url:
                raise RuntimeError(f"stripe init response missing stripe_hosted_url, keys={sorted(init_payload.keys())}")
            if is_br_de_strict_zero:
                initial_currency = str(init_payload.get("currency") or "").upper()
                if initial_currency != "EUR":
                    raise RuntimeError(
                        "DE 严格零流程要求 Stripe Payment Page 币种为 EUR；"
                        f"当前为 {initial_currency or '<missing>'}"
                    )
            elif is_jp_strict_zero:
                initial_currency = str(init_payload.get("currency") or "").upper()
                if initial_currency != "JPY":
                    raise RuntimeError(
                        "JP 严格零流程要求 Stripe Payment Page 币种为 JPY；"
                        f"当前为 {initial_currency or '<missing>'}"
                    )
            elif is_eur_jp_style_zero:
                initial_currency = str(init_payload.get("currency") or "").upper()
                if initial_currency != "EUR":
                    raise RuntimeError(
                        "DE/EUR JP 逻辑流程要求 Stripe Payment Page 币种为 EUR；"
                        f"当前为 {initial_currency or '<missing>'}"
                    )
            elif is_fr_ideal_style_zero:
                initial_currency = str(init_payload.get("currency") or "").upper()
                if initial_currency != "EUR":
                    raise RuntimeError(
                        "PayPal FR PIX式流程要求 Stripe Payment Page 币种为 EUR；"
                        f"当前为 {initial_currency or '<missing>'}"
                    )
            hosted_long_url = opll_to_openai_pay_url(stripe_hosted_url)
            opll_emit_diagnostic(diagnostic_log, "[PayPal] Stripe 连接已建立，正在检测初始支付方式")
            if is_de_native_promotion:
                initial_methods = opll_payment_method_types(init_payload)
                if "paypal" not in initial_methods:
                    raise PayPalMethodUnavailableError(initial_methods)
                opll_emit_diagnostic(
                    diagnostic_log,
                    "[PayPal] 第一代理支付方式检测通过："
                    + (",".join(initial_methods) or "<missing>"),
                )
            # BR->DE / JP / DE-EUR JP逻辑允许初始只有 card；应用优惠后再继续后续门禁。
            if is_post_checkout_promotion:
                if is_br_de_strict_zero or is_jp_strict_zero or is_eur_jp_style_zero:
                    opll_require_payment_method_type(init_payload, "card", require_declared=True)
                else:
                    opll_require_payment_method_type(init_payload, "paypal", require_declared=True)
                initial_methods = ",".join(opll_payment_method_types(init_payload)) or "<missing>"
                opll_emit_diagnostic(
                    diagnostic_log,
                    f"[PayPal] 初始支付方式检测通过：{initial_methods}",
                )
                if (is_br_de_strict_zero or is_jp_strict_zero or is_eur_jp_style_zero) and "paypal" not in opll_payment_method_types(init_payload):
                    if is_br_de_strict_zero:
                        missing_paypal_message = (
                            "[PayPal] 初始暂未出现 paypal，继续应用优惠；将在金额轮询阶段再次检查"
                        )
                    else:
                        flow_label = "JP" if is_jp_strict_zero else "DE/EUR JP逻辑"
                        missing_paypal_message = (
                            "[PayPal] 初始暂未出现 paypal，继续应用优惠；"
                            f"{flow_label} 将轮询金额，最终由 Confirm 和实际跳转判定"
                        )
                    opll_emit_diagnostic(
                        diagnostic_log,
                        missing_paypal_message,
                    )
            stripe_pk = opll_stripe_key_for_checkout(checkout)
            payment_locale = post_payment_locale if is_post_checkout_promotion else init_payment_locale
            ctx = opll_stripe_context(init_payload, payment_locale, ctx=ctx)
            if not ctx.get("currency"):
                ctx["currency"] = str(checkout.get("currency") or "").lower()
            effective_payload = init_payload
            promotion: dict = {}
            if checkout_create_promotion_only:
                initial_amount, _initial_amount_source = opll_stripe_amount_info(
                    init_payload
                )
                needs_single_update = opll_should_update_checkout_promotion(
                    apply_trial_promotion=True,
                    checkout_includes_trial_promo=True,
                    target_amount=target_amount,
                    actual_amount=initial_amount,
                )
                if needs_single_update:
                    opll_emit_diagnostic(
                        diagnostic_log,
                        "[PayPal] Checkout 已携带优惠但金额尚未达到目标；"
                        "保留同一 Checkout，并用第一条粘性代理补做唯一一次优惠 Update",
                    )
                    promotion = opll_apply_checkout_trial_promotion(
                        stripe,
                        checkout["cs_id"],
                        stripe_pk,
                        init_payload,
                        ctx,
                        access_token=access_token,
                        checkout=checkout,
                        chatgpt_proxy_url=create_proxy_url,
                        request_locale=post_request_locale,
                        allow_pending=True,
                        session_context=session_context,
                    )
                    payment_page = promotion.get("payment_page")
                    if not isinstance(payment_page, dict):
                        raise RuntimeError("唯一一次优惠 Update 后 Payment Page 状态无效")
                    payment_page = opll_wait_for_us_tr_promoted_payment_page(
                        stripe,
                        checkout["cs_id"],
                        stripe_pk,
                        ctx,
                        payment_page,
                        before_payload=init_payload,
                        required_amount=target_amount,
                        required_payment_method_type="paypal",
                        attempts=6,
                    )
                    promotion = {
                        **promotion,
                        "promotion_id": PIX_TRIAL_PROMOTION_ID,
                        "promotion_applied": True,
                        "promotion_strategy": "checkout_create_then_single_update",
                        "promotion_proof": "same_checkout_same_proxy_update_amount_verified",
                    }
                    promotion_used_proxy = create_proxy_url
                else:
                    opll_emit_diagnostic(
                        diagnostic_log,
                        "[PayPal] 步骤 4/7：Checkout 创建时优惠金额已达到目标；"
                        "不提交 Update，正在轮询金额与 PayPal 支付方式",
                    )
                    payment_page = opll_wait_for_us_tr_promoted_payment_page(
                        stripe,
                        checkout["cs_id"],
                        stripe_pk,
                        ctx,
                        init_payload,
                        promotion_already_proven=True,
                        required_amount=target_amount,
                        required_payment_method_type="paypal",
                        attempts=6,
                    )
                    promotion = {
                        "promotion_id": PIX_TRIAL_PROMOTION_ID,
                        "promotion_applied": True,
                        "promotion_strategy": "checkout_create",
                        "promotion_proof": "checkout_create_amount_verified",
                    }
                effective_payload = {**init_payload, **payment_page}
                promotion_applied = True
                stripe_amount, stripe_amount_source = opll_stripe_amount_info(
                    effective_payload
                )
                ctx = opll_stripe_context(effective_payload, payment_locale, ctx=ctx)
                if not ctx.get("currency"):
                    ctx["currency"] = str(checkout.get("currency") or "").lower()
                stripe_hosted_url = str(
                    effective_payload.get("stripe_hosted_url") or stripe_hosted_url
                ).strip()
            elif is_de_native_promotion:
                opll_emit_diagnostic(
                    diagnostic_log,
                    "[PayPal] 步骤 4/7：第二代理仅提交优惠 Update；"
                    "后续金额轮询继续回到第一代理",
                )
                (
                    promotion,
                    effective_payload,
                    ctx,
                    promotion_used_proxy,
                ) = opll_apply_native_paypal_promotion_with_ip_rotation(
                    stripe,
                    checkout,
                    stripe_pk,
                    init_payload,
                    ctx,
                    access_token=access_token,
                    request_proxy_url=native_promotion_proxy_url,
                    stripe_proxy_url=stripe_proxy_url,
                    rotation_proxy_url=(
                        promotion_upstream_proxy_url
                        or configured_followup_proxy_url
                    ),
                    target_amount=target_amount,
                    request_locale=post_request_locale,
                    payment_locale=init_payment_locale,
                    browser_timezone=init_timezone,
                    diagnostic_log=diagnostic_log,
                    max_ip_refreshes=0,
                    **(
                        {
                            "chatgpt_session": browser_runtime.get(
                                "promotion_chatgpt_session"
                            )
                        }
                        if browser_runtime.get("promotion_chatgpt_session") is not None
                        else {}
                    ),
                    **(
                        {
                            "chatgpt_session_factory": browser_runtime.get(
                                "promotion_chatgpt_session_factory"
                            )
                        }
                        if callable(browser_runtime.get("promotion_chatgpt_session_factory"))
                        else {}
                    ),
                )
                stripe_hosted_url = str(
                    effective_payload.get("stripe_hosted_url") or stripe_hosted_url
                ).strip()
                hosted_long_url = opll_to_openai_pay_url(stripe_hosted_url)
            elif is_post_checkout_promotion:
                if strict_zero_checkout_includes_trial_promo:
                    opll_emit_diagnostic(
                        diagnostic_log,
                        "[PayPal] 步骤 4/7：Checkout 创建时已携带优惠；"
                        "仅校验真实金额，不提交优惠 Update",
                    )
                    initial_amount, initial_amount_source = opll_stripe_amount_info(
                        init_payload
                    )
                    try:
                        opll_apply_amount_check(
                            {
                                "stripe_amount": initial_amount,
                                "stripe_amount_source": initial_amount_source,
                            },
                            target_amount,
                        )
                    except AmountMismatchError:
                        raise AmountMismatchError(
                            str(target_amount or "").strip(),
                            initial_amount,
                            initial_amount_source,
                        )
                    else:
                        promotion = {
                            "promotion_id": PIX_TRIAL_PROMOTION_ID,
                            "promotion_applied": True,
                            "promotion_strategy": "checkout_create",
                            "promotion_proof": "checkout_create_amount_verified",
                        }
                        payment_page = init_payload
                        promotion_applied = True
                        promotion_used_proxy = ""
                else:
                    opll_emit_diagnostic(
                        diagnostic_log,
                        "[PayPal] 步骤 4/7：金额校验；先应用优惠，再轮询支付方式和真实金额",
                    )
                    opll_emit_diagnostic(
                        diagnostic_log,
                        f"[PayPal] 正在应用优惠 {PIX_TRIAL_PROMOTION_ID}",
                    )
                if strict_zero_checkout_includes_trial_promo:
                    pass
                elif is_fr_ideal_style_zero:
                    opll_emit_diagnostic(
                        diagnostic_log,
                        "[PayPal FR] PIX式提取：第一代理 Create/Stripe/Payment Method/Confirm/Approve；"
                        "第二代理仅负责优惠 Update；出口国家均不限",
                    )
                    promotion = opll_apply_pix_trial_promotion(
                        stripe,
                        checkout["cs_id"],
                        stripe_pk,
                        init_payload,
                        ctx,
                        access_token=access_token,
                        checkout=checkout,
                        chatgpt_proxy_url=configured_followup_proxy_url,
                        request_locale=post_request_locale,
                    )
                    payment_page = promotion.get("payment_page")
                    promotion_applied = promotion.get("promotion_applied") is True
                    promotion_used_proxy = (
                        promotion_upstream_proxy_url or configured_followup_proxy_url
                    )
                    promotion["promotion_proxy"] = promotion_used_proxy
                    promotion["promotion_proof"] = "pix_style_checkout_update"
                elif is_br_de_strict_zero:
                    opll_emit_diagnostic(
                        diagnostic_log,
                        "[PayPal] 优惠 Update 正在使用第二代理；若金额仍非 0，"
                        "废弃当前 Checkout，由外层分配从未使用的第二代理 IP 后重建",
                    )
                    (
                        promotion,
                        payment_page,
                        promotion_used_proxy,
                    ) = opll_apply_de_strict_zero_promotion_with_ip_rotation(
                        stripe,
                        checkout,
                        stripe_pk,
                        init_payload,
                        ctx,
                        access_token=access_token,
                        request_proxy_url=followup_proxy_url,
                        rotation_proxy_url=(
                            promotion_upstream_proxy_url
                            or configured_followup_proxy_url
                        ),
                        target_amount=target_amount,
                        request_locale=post_request_locale,
                        diagnostic_log=diagnostic_log,
                        max_ip_refreshes=0,
                    )
                else:
                    promotion_proxy_url = create_proxy_url if is_br_post_promotion else followup_proxy_url
                    promotion = opll_apply_checkout_trial_promotion(
                        stripe,
                        checkout["cs_id"],
                        stripe_pk,
                        init_payload,
                        ctx,
                        access_token=access_token,
                        checkout=checkout,
                        chatgpt_proxy_url=promotion_proxy_url,
                        request_locale=post_request_locale,
                        allow_pending=is_strict_zero_post_promotion,
                    )
                    payment_page = promotion.get("payment_page")
                if not isinstance(payment_page, dict):
                    raise RuntimeError("应用优惠后的 Payment Page 状态无效")
                if (
                    is_strict_zero_post_promotion
                    and not is_fr_ideal_style_zero
                    and not is_br_de_strict_zero
                ):
                    opll_emit_diagnostic(diagnostic_log, "[PayPal] 优惠已提交，正在轮询并检测金额")
                    payment_page = opll_wait_for_us_tr_promoted_payment_page(
                        stripe,
                        checkout["cs_id"],
                        stripe_pk,
                        ctx,
                        payment_page,
                        before_payload=init_payload,
                        accept_strict_zero_amount_drop=True,
                        promotion_already_proven=promotion.get("promotion_applied") is True,
                        required_amount=target_amount,
                        required_payment_method_type=(
                            "card"
                            if (is_jp_strict_zero or is_eur_jp_style_zero)
                            else "paypal"
                        ),
                        attempts=6,
                    )
                    promotion_applied = bool(
                        promotion.get("promotion_applied") is True
                        or opll_strict_zero_promotion_applied(init_payload, payment_page)
                    )
                elif not is_fr_ideal_style_zero and not is_br_de_strict_zero:
                    promotion_applied = promotion.get("promotion_applied") is True
                elif is_br_de_strict_zero:
                    promotion_applied = promotion.get("promotion_applied") is True
                if not promotion_applied:
                    raise RuntimeError(
                        "活动更新响应未证明优惠已生效；"
                        + opll_promotion_proof_diagnostics(init_payload, payment_page)
                    )
                effective_payload = {**init_payload, **payment_page}
                if not (is_jp_strict_zero or is_eur_jp_style_zero):
                    opll_require_payment_method_type(
                        effective_payload,
                        "paypal",
                        require_declared=True,
                    )
                elif "paypal" not in opll_payment_method_types(effective_payload):
                    flow_label = "JP" if is_jp_strict_zero else "DE/EUR JP逻辑"
                    opll_emit_diagnostic(
                        diagnostic_log,
                        f"[PayPal] {flow_label} 优惠后仍未声明 paypal，按参考流程继续创建 PM / Confirm / Approve / Poll",
                    )
                if is_br_de_strict_zero:
                    opll_require_payment_method_type(
                        effective_payload,
                        "card",
                        require_declared=True,
                    )
                    effective_currency = str(effective_payload.get("currency") or "").upper()
                    if effective_currency != "EUR":
                        raise RuntimeError(
                            "DE 严格零流程要求优惠后的 Stripe Payment Page 币种仍为 EUR；"
                            f"当前为 {effective_currency or '<missing>'}"
                        )
                elif is_jp_strict_zero:
                    opll_require_payment_method_type(
                        effective_payload,
                        "card",
                        require_declared=True,
                    )
                    effective_currency = str(effective_payload.get("currency") or "").upper()
                    if effective_currency != "JPY":
                        raise RuntimeError(
                            "JP 严格零流程要求优惠后的 Stripe Payment Page 币种仍为 JPY；"
                            f"当前为 {effective_currency or '<missing>'}"
                        )
                elif is_eur_jp_style_zero:
                    opll_require_payment_method_type(
                        effective_payload,
                        "card",
                        require_declared=True,
                    )
                    effective_currency = str(effective_payload.get("currency") or "").upper()
                    if effective_currency != "EUR":
                        raise RuntimeError(
                            "DE/EUR JP 逻辑流程要求优惠后的 Stripe Payment Page 币种仍为 EUR；"
                            f"当前为 {effective_currency or '<missing>'}"
                        )
                elif is_fr_ideal_style_zero:
                    effective_currency = str(effective_payload.get("currency") or "").upper()
                    if effective_currency != "EUR":
                        raise RuntimeError(
                            "PayPal FR PIX式流程要求优惠后的 Stripe Payment Page 币种仍为 EUR；"
                            f"当前为 {effective_currency or '<missing>'}"
                        )
                stripe_amount, stripe_amount_source = opll_stripe_amount_info(effective_payload)
                opll_emit_diagnostic(
                    diagnostic_log,
                    f"[PayPal] 正在检测金额：目标={str(target_amount or '<未设置>')}，当前={stripe_amount or '<未知>'}",
                )
                opll_apply_amount_check(
                    {
                        "stripe_amount": stripe_amount,
                        "stripe_amount_source": stripe_amount_source,
                    },
                    target_amount,
                )
                opll_emit_diagnostic(
                    diagnostic_log,
                    f"[PayPal] 金额检测通过：{stripe_amount} {str(checkout.get('currency') or '').upper()}",
                )
                ctx = opll_stripe_context(effective_payload, payment_locale, ctx=ctx)
                if not ctx.get("currency"):
                    ctx["currency"] = str(checkout.get("currency") or "").lower()
                stripe_hosted_url = str(
                    effective_payload.get("stripe_hosted_url") or stripe_hosted_url
                ).strip()
            else:
                stripe_amount, stripe_amount_source = opll_stripe_amount_info(effective_payload)
            if is_de_native_promotion:
                stripe_amount, stripe_amount_source = opll_stripe_amount_info(
                    effective_payload
                )
            if not is_post_checkout_promotion:
                opll_emit_diagnostic(diagnostic_log, "[PayPal] 步骤 4/7：金额校验")
                opll_emit_diagnostic(
                    diagnostic_log,
                    f"[PayPal] 正在检测金额：目标={str(target_amount or '<未设置>')}，当前={stripe_amount or '<未知>'}",
                )
            opll_apply_amount_check(
                {
                    "stripe_amount": stripe_amount,
                    "stripe_amount_source": stripe_amount_source,
                },
                target_amount,
            )
            if not is_post_checkout_promotion:
                opll_emit_diagnostic(
                    diagnostic_log,
                    f"[PayPal] 金额检测完成：{stripe_amount or '<未知>'} {str(checkout.get('currency') or '').upper()}",
                )
            if return_checkout_short:
                short_url = opll_chatgpt_checkout_page_url(
                    checkout["cs_id"],
                    str(checkout.get("billing_country") or "DE"),
                    str(checkout.get("processor_entity") or ""),
                )
                if not opll_is_chatgpt_checkout_page_url(short_url):
                    raise RuntimeError(
                        "DE/EUR PayPal 检测通过，但未生成有效 ChatGPT Checkout 短链"
                    )
                payment_proxy_used = create_upstream_proxy_url or create_proxy_url
                promotion_proxy_used = (
                    promotion_used_proxy
                    or promotion_upstream_proxy_url
                    or configured_followup_proxy_url
                    or create_proxy_url
                )
                short_result = {
                    **checkout,
                    "chatgpt_checkout_url": short_url,
                    "checkout_url": short_url,
                    "long_url": short_url,
                    "payment_link_type": "chatgpt_checkout_short",
                    "stripe_hosted_url": stripe_hosted_url,
                    "stripe_amount": stripe_amount,
                    "stripe_amount_source": stripe_amount_source,
                    "checkout_payment_method_types": opll_payment_method_types(
                        effective_payload
                    ),
                    "paypal_available": True,
                    "paypal_availability_status": "available",
                    "paypal_flow": PAYPAL_DE_NATIVE_PROMO_FLOW,
                    "promotion_id": PIX_TRIAL_PROMOTION_ID,
                    "promotion_requested": True,
                    "promotion_applied": promotion.get("promotion_applied") is True,
                    "promotion_required": True,
                    "promotion_strategy": str(
                        promotion.get("promotion_strategy") or "checkout_create"
                    ),
                    "promotion_proxy": promotion_proxy_used,
                    "create_proxy_used": payment_proxy_used,
                    "payment_proxy_used": payment_proxy_used,
                    "approve_proxy_used": "",
                    "request_execution": (
                        "camoufox_page" if browser_runtime else "python_http"
                    ),
                }
                opll_emit_diagnostic(
                    diagnostic_log,
                    f"[PayPal短链] Checkout 创建时已携带优惠；短链生成完成: {short_url}",
                )
                return opll_apply_amount_check(short_result, target_amount)
            checkout["payment_method_country"] = pm_country
            billing = opll_billing_for_country(
                pm_country,
                account_email=account_email,
                access_token=access_token,
            )
            opll_emit_diagnostic(diagnostic_log, f"[PayPal] 步骤 5/7：正在创建 {pm_country} PayPal Payment Method")
            pm_id = opll_stripe_create_paypal_method(
                stripe,
                checkout["cs_id"],
                ctx,
                billing,
                stripe_pk,
            )
            opll_emit_diagnostic(diagnostic_log, "[PayPal] 步骤 6/7：confirm → approve → poll；Payment Method 已创建，正在提交 Confirm")
            confirm_payload = opll_stripe_confirm(
                stripe,
                checkout["cs_id"],
                pm_id,
                stripe_pk,
                effective_payload,
                ctx,
                checkout,
                stripe_hosted_url,
            )
            opll_emit_diagnostic(diagnostic_log, "[PayPal] Confirm 已提交，正在轮询 PayPal 跳转链接")
            stripe_redirect_url = opll_redirect_url_after_confirm(
                access_token,
                stripe,
                confirm_payload,
                checkout["cs_id"],
                stripe_pk,
                ctx,
                checkout,
                approve_proxy_url,
                stabilize_before_approve=is_de_native_promotion,
                diagnostic_log=diagnostic_log,
                approve_upstream_proxy_url=(
                    create_upstream_proxy_url or create_proxy_url
                    if is_de_native_promotion
                    else ""
                ),
                session_context=session_context,
                **(
                    {
                        "chatgpt_session": browser_runtime.get(
                            "primary_chatgpt_session"
                        )
                    }
                    if (
                        is_de_native_promotion
                        and browser_runtime.get("primary_chatgpt_session") is not None
                    )
                    else {}
                ),
            )
            redirect_resolver = browser_runtime.get("redirect_resolver")
            redirect_result = (
                redirect_resolver(stripe_redirect_url)
                if is_de_native_promotion and callable(redirect_resolver)
                else opll_resolve_paypal_redirect_result(stripe, stripe_redirect_url)
            )
            provider_url = str(redirect_result.get("selected_url") or "").strip()
            result = {
                **checkout,
                "payment_method_country": pm_country,
                "payment_method_id": pm_id,
                "stripe_hosted_url": stripe_hosted_url,
                "stripe_redirect_url": stripe_redirect_url,
                "stripe_pm_redirect_url": str(redirect_result.get("stripe_pm_redirect_url") or ""),
                "paypal_ba_approve_url": str(redirect_result.get("paypal_ba_approve_url") or ""),
                "provider_redirect_url": provider_url,
                "payment_link_type": str(redirect_result.get("payment_link_type") or "paypal_stripe_redirect"),
                "fallback": False if (is_br_de_strict_zero or is_jp_strict_zero or is_eur_jp_style_zero or is_fr_ideal_style_zero or is_de_native_promotion) else (checkout_country, pm_country) != (requested_country, requested_country),
                "provider_error": "; ".join(failures),
                "long_url": provider_url or hosted_long_url,
                "stripe_amount": stripe_amount,
                "stripe_amount_source": stripe_amount_source,
            }
            if is_post_checkout_promotion:
                result.update({
                    "promotion_id": str(promotion.get("promotion_id") or PIX_TRIAL_PROMOTION_ID),
                    "promotion_applied": True,
                })
                if is_br_de_strict_zero:
                    payment_proxy_used = create_upstream_proxy_url or create_proxy_url
                    checkout_promotion_strategy = str(
                        promotion.get("promotion_strategy")
                        or (
                            "checkout_create"
                            if strict_zero_checkout_includes_trial_promo
                            else "post_init_update"
                        )
                    )
                    checkout_fallback_update_used = (
                        checkout_promotion_strategy
                        == "checkout_create_then_single_update"
                    )
                    result.update({
                        "paypal_flow": PAYPAL_BR_DE_STRICT_ZERO_FLOW,
                        "promotion_proxy": (
                            (
                                promotion_used_proxy
                                or create_upstream_proxy_url
                                or create_proxy_url
                            )
                            if checkout_fallback_update_used
                            else ""
                            if strict_zero_checkout_includes_trial_promo
                            else promotion_used_proxy
                            or promotion_upstream_proxy_url
                            or configured_followup_proxy_url
                        ),
                        "promotion_requested": True,
                        "promotion_required": True,
                        "promotion_strategy": checkout_promotion_strategy,
                        "promotion_proof": (
                            str(promotion.get("promotion_proof") or "")
                            if checkout_fallback_update_used
                            else "checkout_create_amount_verified"
                            if strict_zero_checkout_includes_trial_promo
                            else str(promotion.get("promotion_proof") or "checkout_update")
                        ),
                        "checkout_includes_trial_promo": strict_zero_checkout_includes_trial_promo,
                        "create_proxy_used": payment_proxy_used,
                        "payment_proxy_used": payment_proxy_used,
                        "approve_proxy_used": payment_proxy_used,
                        "billing_address_country": pm_country,
                    })
                elif is_jp_strict_zero:
                    result["paypal_flow"] = PAYPAL_JP_STRICT_ZERO_FLOW
                elif is_eur_jp_style_zero:
                    result["paypal_flow"] = PAYPAL_EUR_JP_STYLE_ZERO_FLOW
                elif is_fr_ideal_style_zero:
                    result.update({
                        "paypal_flow": PAYPAL_FR_IDEAL_STYLE_ZERO_FLOW,
                        "promotion_required": True,
                        "promotion_strategy": "pix_style_post_init_update",
                        "promotion_proof": str(promotion.get("promotion_proof") or ""),
                        "promotion_proxy": str(promotion.get("promotion_proxy") or promotion_used_proxy),
                        "create_proxy_used": create_upstream_proxy_url or create_proxy_url,
                        "payment_proxy_used": create_upstream_proxy_url or create_proxy_url,
                        "approve_proxy_used": create_upstream_proxy_url or create_proxy_url,
                        "create_proxy_rotated": False,
                    })
                elif is_gb_zero_post_promotion:
                    payment_proxy_used = create_upstream_proxy_url or create_proxy_url
                    result.update({
                        "promotion_required": True,
                        "promotion_strategy": str(
                            promotion.get("promotion_strategy") or "checkout_create"
                        ),
                        "promotion_proxy": "",
                        "create_proxy_used": payment_proxy_used,
                        "payment_proxy_used": payment_proxy_used,
                        "approve_proxy_used": payment_proxy_used,
                    })
            elif is_de_native_promotion:
                payment_proxy_used = create_upstream_proxy_url or create_proxy_url
                promotion_proxy_used = (
                    promotion_used_proxy
                    or promotion_upstream_proxy_url
                    or configured_followup_proxy_url
                    or create_proxy_url
                )
                result.update({
                    "paypal_flow": PAYPAL_DE_NATIVE_PROMO_FLOW,
                    "promotion_id": PIX_TRIAL_PROMOTION_ID,
                    "promotion_requested": True,
                    "promotion_applied": promotion.get("promotion_applied") is True,
                    "promotion_required": True,
                    "promotion_strategy": str(
                        promotion.get("promotion_strategy") or "checkout_create"
                    ),
                    "promotion_proxy": "" if checkout_create_promotion_only else promotion_proxy_used,
                    "create_proxy_used": payment_proxy_used,
                    "payment_proxy_used": payment_proxy_used,
                    "approve_proxy_used": payment_proxy_used,
                    "request_execution": (
                        "camoufox_page"
                        if browser_runtime
                        else "python_http"
                    ),
                })
            if not opll_is_paypal_success_url(provider_url):
                resource_hint = "仅发现 Stripe 资源 URL，未发现 PayPal BA approve 链；" if opll_is_ignored_resource_url(provider_url) else ""
                raise RuntimeError(
                    f"{resource_hint}未提取到可用的 PayPal 跳转链接；当前结果: {provider_url or stripe_redirect_url}"
                )
            opll_emit_diagnostic(diagnostic_log, "[PayPal] 步骤 7/7：PayPal 跳转链接提取完成")
            return opll_apply_amount_check(result, target_amount)
        except (AmountMismatchError, PayPalMethodUnavailableError):
            raise
        except Exception as exc:
            if is_fr_ideal_style_zero and opll_is_non_retryable_link_error(exc):
                raise
            failures.append(f"{checkout_country}+{pm_country}: {opll_short_error(str(exc))}")
    raise RuntimeError(f"所有组合均未提取到 PayPal BA approve 链；{'; '.join(failures)}")

def generate_opll_paypal_us_tr_long_link(
    access_token: str,
    us_proxy_url: str = "",
    tr_proxy_url: str = "",
    target_amount: str = "",
    *,
    account_email: str = "",
    diagnostic_log=None,
    sentinel_so_enabled: bool = False,
    session_context: dict | None = None,
) -> dict:
    us_proxy_url = str(us_proxy_url or "").strip()
    # The PayPal US flow keeps one stable exit identity for every stage.
    # Keep the legacy argument for callers, but never route Update through it.
    tr_proxy_url = us_proxy_url
    account_email = str(account_email or "").strip()
    opll_emit_diagnostic(
        diagnostic_log,
        "[PayPal US] 步骤 1/7：正在验证 Access Token，并准备 US/USD 零元优惠流程",
    )
    opll_validate_access_token(
        access_token,
        us_proxy_url,
        request_locale="en-US",
        session_context=session_context,
    )
    opll_emit_diagnostic(
        diagnostic_log,
        "[PayPal US] 步骤 2/7：第一代理创建 US/USD Checkout（直接携带优惠）",
    )
    checkout = opll_create_checkout(
        access_token,
        "US",
        "USD",
        us_proxy_url,
        request_locale="en-US",
        include_trial_promo=True,
        checkout_ui_mode="hosted",
        return_raw_payload=True,
        sentinel_so_enabled=sentinel_so_enabled,
        diagnostic_log=diagnostic_log,
        session_context=session_context,
    )
    raw_create_payload = checkout.pop("_checkout_payload", {})
    if not isinstance(raw_create_payload, dict):
        raw_create_payload = {}
    checkout_id = str(checkout.get("cs_id") or "").strip()
    is_oaics_checkout = checkout_id.startswith("oaics_")
    stripe_checkout_id = checkout_id
    oaics_update_payload: dict = {}
    oaics_fetch_payload: dict = {}
    oaics_taxes_payload: dict = {}
    us_chatgpt: requests.Session | None = None
    us_stripe: requests.Session | None = None
    billing: dict | None = None
    promotion_update_performed = False
    stripe_pk = opll_stripe_key_for_checkout(checkout)

    if is_oaics_checkout:
        opll_emit_diagnostic(
            diagnostic_log,
            "[PayPal US] Checkout 返回 oaics_；保留其作为 ChatGPT Checkout ID，"
            "优惠已在 Create 阶段携带，直接解析底层 Stripe cs_",
        )
        stripe_checkout_id = opll_extract_stripe_payment_page_id(
            raw_create_payload,
        )
        create_amount, _create_amount_source = opll_chatgpt_checkout_amount_info(
            raw_create_payload
        )
        if (
            not stripe_checkout_id
            and opll_should_update_checkout_promotion(
                apply_trial_promotion=True,
                checkout_includes_trial_promo=True,
                target_amount=target_amount,
                actual_amount=create_amount,
            )
        ):
            us_chatgpt = opll_build_chatgpt_session(
                access_token,
                us_proxy_url,
                request_locale="en-US",
                device_id=str(checkout.get("oai_device_id") or ""),
                session_context=session_context,
            )
            opll_emit_diagnostic(
                diagnostic_log,
                "[PayPal US] Create 优惠尚未返回目标金额或 Stripe cs_；"
                "保留原 oaics_ Checkout，并用同一条第一代理补做唯一一次 Update",
            )
            oaics_update_payload = opll_chatgpt_update_checkout_promotion(
                access_token,
                checkout,
                us_proxy_url,
                request_locale="en-US",
                device_id=str(checkout.get("oai_device_id") or ""),
                session=us_chatgpt,
                session_context=session_context,
                include_checkout_context=True,
            )
            promotion_update_performed = True
            updated_checkout_id = opll_extract_checkout_session_id(
                oaics_update_payload
            )
            if updated_checkout_id.startswith("oaics_"):
                checkout["cs_id"] = updated_checkout_id
                checkout_id = updated_checkout_id
            stripe_checkout_id = opll_extract_stripe_payment_page_id(
                raw_create_payload,
                oaics_update_payload,
            )
        if not stripe_checkout_id:
            us_chatgpt = us_chatgpt or opll_build_chatgpt_session(
                access_token,
                us_proxy_url,
                request_locale="en-US",
                device_id=str(checkout.get("oai_device_id") or ""),
                session_context=session_context,
            )
            oaics_fetch_payload = opll_chatgpt_fetch_checkout(
                access_token,
                checkout,
                us_proxy_url,
                request_locale="en-US",
                chatgpt=us_chatgpt,
            )
            stripe_checkout_id = opll_extract_stripe_payment_page_id(
                raw_create_payload,
                oaics_update_payload,
                oaics_fetch_payload,
            )
            if not stripe_checkout_id:
                billing = opll_billing_for_country(
                    "US",
                    account_email=account_email,
                    access_token=access_token,
                    city_hint="New York",
                    state_hint="NY",
                )
                opll_emit_diagnostic(
                    diagnostic_log,
                    "[PayPal US] oaics_ Create/Fetch 尚未返回 Stripe cs_；"
                    "第一代理调用 Checkout Taxes 物化底层支付会话",
                )
                oaics_taxes_payload = opll_chatgpt_checkout_taxes(
                    access_token,
                    checkout,
                    billing,
                    us_proxy_url,
                    request_locale="en-US",
                    diagnostic_log=diagnostic_log,
                    require_success=True,
                    flow_label="PayPal US OAICS",
                    chatgpt_session=us_chatgpt,
                )
                oaics_fetch_payload = opll_chatgpt_fetch_checkout(
                    access_token,
                    checkout,
                    us_proxy_url,
                    request_locale="en-US",
                    chatgpt=us_chatgpt,
                )
                stripe_checkout_id = opll_extract_stripe_payment_page_id(
                    raw_create_payload,
                    oaics_update_payload,
                    oaics_taxes_payload,
                    oaics_fetch_payload,
                )
        if not stripe_checkout_id:
            oaics_amount = ""
            oaics_amount_source = "missing_payload"
            oaics_amount_stage = "checkout_create"
            oaics_currency = ""
            declared_methods: list[str] = []
            for stage, payload in (
                ("checkout_create", raw_create_payload),
                ("checkout_update", oaics_update_payload),
                ("checkout_taxes", oaics_taxes_payload),
                ("checkout_fetch", oaics_fetch_payload),
            ):
                candidate_amount, candidate_source = (
                    opll_chatgpt_checkout_amount_info(payload)
                )
                candidate_currency = opll_chatgpt_checkout_currency(payload)
                if candidate_amount:
                    oaics_amount = candidate_amount
                    oaics_amount_source = candidate_source
                    oaics_amount_stage = stage
                if candidate_currency:
                    oaics_currency = str(candidate_currency).upper()
                for method_type in opll_checkout_declared_payment_method_types(
                    payload
                ):
                    if method_type not in declared_methods:
                        declared_methods.append(method_type)
            if not oaics_amount:
                raise AmountMismatchError(
                    str(target_amount or "").strip(),
                    "",
                    f"{oaics_amount_stage}.missing_payload",
                )
            if oaics_currency and oaics_currency != "USD":
                raise RuntimeError(
                    "PayPal US OAICS Checkout 币种必须为 USD；"
                    f"当前为 {oaics_currency}"
                )
            oaics_amount_result = {
                "stripe_amount": oaics_amount,
                "stripe_amount_source": (
                    f"{oaics_amount_stage}.{oaics_amount_source}"
                ),
            }
            opll_apply_amount_check(oaics_amount_result, target_amount)
            if "paypal" not in declared_methods:
                raise PayPalMethodUnavailableError(declared_methods)

            billing = billing or opll_billing_for_country(
                "US",
                account_email=account_email,
                access_token=access_token,
                city_hint="New York",
                state_hint="NY",
            )
            us_chatgpt = us_chatgpt or opll_build_chatgpt_session(
                access_token,
                us_proxy_url,
                request_locale="en-US",
                device_id=str(checkout.get("oai_device_id") or ""),
                session_context=session_context,
            )
            us_stripe = opll_build_stripe_session(
                us_proxy_url,
                request_locale="en-US",
            )
            chatgpt_checkout_url = opll_chatgpt_checkout_page_url(
                checkout_id,
                "US",
                str(checkout.get("processor_entity") or ""),
            )
            opll_emit_diagnostic(
                diagnostic_log,
                "[PayPal US] Taxes 后仍无 Stripe cs_；第一代理创建 PayPal "
                "ConfirmationToken，并按 OAICS 页面流程物化支付会话",
            )
            confirmation_token = opll_stripe_create_paypal_confirmation_token(
                us_stripe,
                billing,
                stripe_pk,
                return_url=chatgpt_checkout_url,
            )
            selection_payload = opll_chatgpt_confirm_custom_payment_method(
                access_token,
                checkout,
                "paypal",
                us_proxy_url,
                request_locale="en-US",
                chatgpt=us_chatgpt,
                method_name="PayPal",
                sentinel_required=True,
                diagnostic_log=diagnostic_log,
                allow_builtin=True,
                confirmation_token=confirmation_token,
            )
            setup_client_secret = str(
                selection_payload.get("client_secret") or ""
            ).strip()
            setup_type = str(selection_payload.get("type") or "").strip().lower()
            if setup_type == "setup_intent" or setup_client_secret.startswith(
                "seti_"
            ):
                setup_confirm_payload = (
                    opll_stripe_confirm_setup_intent_with_confirmation_token(
                        us_stripe,
                        setup_client_secret,
                        confirmation_token,
                        str(selection_payload.get("confirm_return_url") or "").strip(),
                        stripe_pk,
                    )
                )
                stripe_redirect_url = opll_extract_redirect_to_url(
                    setup_confirm_payload
                )
                if not stripe_redirect_url:
                    raise RuntimeError(
                        "OAICS_PAYPAL_REDIRECT_MISSING: PayPal US SetupIntent confirm "
                        "未返回 redirect_to_url"
                    )
                redirect_result = opll_resolve_paypal_redirect_result(
                    us_stripe,
                    stripe_redirect_url,
                )
                provider_url = str(
                    redirect_result.get("selected_url") or ""
                ).strip()
                opll_validate_extracted_paypal_link(
                    provider_url,
                    oaics_amount,
                    target_amount,
                    oaics_amount_result["stripe_amount_source"],
                )
                opll_emit_diagnostic(
                    diagnostic_log,
                    "[PayPal US] oaics_ SetupIntent 已确认，PayPal 跳转链接提取完成",
                )
                return opll_apply_amount_check(
                    {
                        **checkout,
                        "chatgpt_checkout_url": chatgpt_checkout_url,
                        "checkout_url": provider_url,
                        "long_url": provider_url,
                        "payment_method_country": "US",
                        "payment_method_id": str(
                            setup_confirm_payload.get("payment_method") or ""
                        ),
                        "stripe_payment_page_id": "",
                        "stripe_hosted_url": "",
                        "stripe_redirect_url": stripe_redirect_url,
                        "stripe_pm_redirect_url": str(
                            redirect_result.get("stripe_pm_redirect_url") or ""
                        ),
                        "paypal_ba_approve_url": str(
                            redirect_result.get("paypal_ba_approve_url") or ""
                        ),
                        "provider_redirect_url": provider_url,
                        "payment_link_type": str(
                            redirect_result.get("payment_link_type")
                            or "paypal_stripe_redirect"
                        ),
                        "stripe_amount": oaics_amount,
                        "stripe_amount_source": oaics_amount_result[
                            "stripe_amount_source"
                        ],
                        "checkout_ui_mode": "custom",
                        "checkout_payment_method_types": declared_methods,
                        "promotion_id": PIX_TRIAL_PROMOTION_ID,
                        "promotion_applied": True,
                        "promotion_strategy": (
                            "checkout_create_then_single_update"
                            if promotion_update_performed
                            else "checkout_create"
                        ),
                        "promotion_proof": (
                            "same_checkout_same_proxy_update_and_setup_intent_amount_verified"
                            if promotion_update_performed
                            else "checkout_create_and_setup_intent_amount_verified"
                        ),
                        "promotion_proxy": (
                            us_proxy_url if promotion_update_performed else ""
                        ),
                        "paypal_flow": PAYPAL_US_TR_FLOW,
                    },
                    target_amount,
                )

            stripe_checkout_id = opll_extract_stripe_payment_page_id(
                selection_payload
            )
            if not stripe_checkout_id:
                oaics_fetch_payload = opll_chatgpt_fetch_checkout(
                    access_token,
                    checkout,
                    us_proxy_url,
                    request_locale="en-US",
                    chatgpt=us_chatgpt,
                )
                stripe_checkout_id = opll_extract_stripe_payment_page_id(
                    selection_payload,
                    oaics_fetch_payload,
                )
            if not stripe_checkout_id:
                raise RuntimeError(
                    "OAICS_PAYMENT_ROUTE_MISSING: PayPal US 的 oaics_ Checkout 已在 Create 阶段携带优惠，"
                    "但 Create、Fetch、Taxes、PayPal Confirm 均未返回 "
                    "Stripe cs_/client_secret；已停止，未向 Stripe 发送 oaics_ ID"
                )

        if not stripe_checkout_id.startswith("cs_"):
            raise RuntimeError(
                "OAICS_PAYMENT_ROUTE_INVALID: PayPal US 未解析到有效 Stripe cs_"
            )
        opll_emit_diagnostic(
            diagnostic_log,
            "[PayPal US] oaics_ 已解析真实 Stripe Payment Page："
            f"{stripe_checkout_id[:20]}...；后续 Stripe 请求只使用 cs_",
        )

    stripe_checkout = (
        {**checkout, "cs_id": stripe_checkout_id}
        if is_oaics_checkout
        else checkout
    )

    opll_emit_diagnostic(
        diagnostic_log,
        "[PayPal US] 步骤 3/7：正在初始化 Stripe Payment Page",
    )
    us_stripe = us_stripe or opll_build_stripe_session(
        us_proxy_url,
        request_locale="en-US",
    )
    ctx = opll_stripe_context({}, "en")
    init_payload = opll_stripe_init(
        stripe_checkout_id,
        checkout["billing_country"],
        checkout["currency"],
        us_proxy_url,
        stripe=us_stripe,
        ctx=ctx,
        checkout=stripe_checkout,
        browser_timezone="America/New_York",
    )
    stripe_hosted_url = str(init_payload.get("stripe_hosted_url") or "").strip()
    if not stripe_hosted_url:
        raise RuntimeError(f"stripe init response missing stripe_hosted_url, keys={sorted(init_payload.keys())}")
    ctx = opll_stripe_context(init_payload, ctx=ctx)
    if not ctx.get("currency"):
        ctx["currency"] = str(checkout.get("currency") or "").lower()

    initial_amount, _initial_amount_source = opll_stripe_amount_info(init_payload)
    needs_single_update = (
        not promotion_update_performed
        and opll_should_update_checkout_promotion(
            apply_trial_promotion=True,
            checkout_includes_trial_promo=True,
            target_amount=target_amount,
            actual_amount=initial_amount,
        )
    )
    if needs_single_update:
        opll_emit_diagnostic(
            diagnostic_log,
            "[PayPal US] Checkout Create 已携带优惠但 Stripe 金额尚未达到目标；"
            "同一 oaics_/cs_、同一第一代理补做唯一一次优惠 Update",
        )
        promotion = opll_apply_checkout_trial_promotion(
            us_stripe,
            stripe_checkout_id,
            stripe_pk,
            init_payload,
            ctx,
            access_token=access_token,
            checkout=checkout,
            chatgpt_proxy_url=us_proxy_url,
            request_locale="en-US",
            allow_pending=True,
            chatgpt_session=us_chatgpt,
            session_context=session_context,
        )
        promotion_update_performed = True
        payment_page = promotion.get("payment_page")
        if not isinstance(payment_page, dict):
            raise RuntimeError("PayPal US 唯一一次优惠 Update 后状态无效")
        payment_page = opll_wait_for_us_tr_promoted_payment_page(
            us_stripe,
            stripe_checkout_id,
            stripe_pk,
            ctx,
            payment_page,
            before_payload=init_payload,
            required_amount=str(target_amount or "").strip(),
            required_payment_method_type="paypal",
        )
    else:
        opll_emit_diagnostic(
            diagnostic_log,
            "[PayPal US] 步骤 4/7：Checkout 优惠金额已达到目标；"
            "不提交额外 Update，正在轮询金额与 PayPal",
        )
        payment_page = opll_wait_for_us_tr_promoted_payment_page(
            us_stripe,
            stripe_checkout_id,
            stripe_pk,
            ctx,
            init_payload,
            promotion_already_proven=True,
            required_amount=str(target_amount or "").strip(),
            required_payment_method_type="paypal",
        )
        promotion = {}
    promotion = {
        **promotion,
        "promotion_id": PIX_TRIAL_PROMOTION_ID,
        "promotion_applied": True,
        "promotion_strategy": (
            "checkout_create_then_single_update"
            if promotion_update_performed
            else "checkout_create"
        ),
        "promotion_proof": (
            "same_checkout_same_proxy_update_amount_verified"
            if promotion_update_performed
            else "checkout_create_then_stripe_validation"
        ),
    }
    promotion_applied = True

    effective_payload = {**init_payload, **payment_page}
    opll_require_payment_method_type(effective_payload, "paypal", require_declared=True)
    stripe_amount, stripe_amount_source = opll_stripe_amount_info(effective_payload)
    amount_result = {
        "stripe_amount": stripe_amount,
        "stripe_amount_source": stripe_amount_source,
    }
    if not stripe_amount or stripe_amount_source in {"fallback_zero", "missing_payload", "invalid_line_items"}:
        raise AmountMismatchError(str(target_amount or "").strip(), "", stripe_amount_source)
    target_amount = str(target_amount or "").strip()
    if not re.fullmatch(r"\d+", target_amount):
        raise AmountMismatchError(target_amount, stripe_amount, stripe_amount_source)
    opll_apply_amount_check(amount_result, target_amount)
    opll_emit_diagnostic(
        diagnostic_log,
        f"[PayPal US] Checkout 优惠与金额校验通过：目标={target_amount}，当前={stripe_amount}",
    )

    ctx = opll_stripe_context(effective_payload, ctx=ctx)
    if not ctx.get("currency"):
        ctx["currency"] = str(checkout.get("currency") or "").lower()
    stripe_hosted_url = str(effective_payload.get("stripe_hosted_url") or stripe_hosted_url).strip()
    hosted_long_url = opll_to_openai_pay_url(stripe_hosted_url)

    opll_emit_diagnostic(
        diagnostic_log,
        "[PayPal US] 步骤 5/7：正在创建美国 PayPal Payment Method",
    )
    pm_id = opll_stripe_create_paypal_method(
        us_stripe,
        stripe_checkout_id,
        ctx,
        billing or opll_billing_for_country(
            "US",
            account_email=account_email,
            access_token=access_token,
            city_hint="New York",
            state_hint="NY",
        ),
        stripe_pk,
    )
    opll_emit_diagnostic(
        diagnostic_log,
        "[PayPal US] 步骤 6/7：正在提交 Confirm 并读取 PayPal Approve 跳转",
    )
    confirm_payload = opll_stripe_confirm(
        us_stripe,
        stripe_checkout_id,
        pm_id,
        stripe_pk,
        effective_payload,
        ctx,
        stripe_checkout,
        stripe_hosted_url,
    )
    stripe_redirect_url = opll_redirect_url_after_confirm(
        access_token,
        us_stripe,
        confirm_payload,
        stripe_checkout_id,
        stripe_pk,
        ctx,
        checkout,
        us_proxy_url,
        **(
            {"approve_checkout_id": checkout_id}
            if is_oaics_checkout
            else {}
        ),
        session_context=session_context,
    )
    redirect_result = opll_resolve_paypal_redirect_result(us_stripe, stripe_redirect_url)
    provider_url = str(redirect_result.get("selected_url") or "").strip()
    if not opll_is_paypal_success_url(provider_url):
        resource_hint = "仅发现 Stripe 资源 URL，未发现 PayPal BA approve 链；" if opll_is_ignored_resource_url(provider_url) else ""
        raise RuntimeError(
            f"{resource_hint}未提取到可用的 PayPal 跳转链接；当前结果: {provider_url or stripe_redirect_url}"
        )
    opll_emit_diagnostic(
        diagnostic_log,
        "[PayPal US] 步骤 7/7：PayPal 跳转链接提取完成",
    )

    result = {
        **checkout,
        "payment_method_country": "US",
        "payment_method_id": pm_id,
        "stripe_hosted_url": stripe_hosted_url,
        "stripe_redirect_url": stripe_redirect_url,
        "stripe_pm_redirect_url": str(redirect_result.get("stripe_pm_redirect_url") or ""),
        "paypal_ba_approve_url": str(redirect_result.get("paypal_ba_approve_url") or ""),
        "provider_redirect_url": provider_url,
        "payment_link_type": str(redirect_result.get("payment_link_type") or "paypal_stripe_redirect"),
        "fallback": False,
        "provider_error": "",
        "long_url": provider_url or hosted_long_url,
        "stripe_amount": stripe_amount,
        "stripe_amount_source": stripe_amount_source,
        "stripe_payment_page_id": (
            stripe_checkout_id if is_oaics_checkout else ""
        ),
        "checkout_ui_mode": "custom" if is_oaics_checkout else "hosted",
        "promotion_id": str(promotion.get("promotion_id") or PIX_TRIAL_PROMOTION_ID),
        "promotion_applied": promotion_applied,
        "promotion_strategy": str(
            promotion.get("promotion_strategy") or "checkout_create"
        ),
        "promotion_proof": str(promotion.get("promotion_proof") or "checkout_update"),
        "promotion_proxy": us_proxy_url if promotion_update_performed else "",
        "paypal_flow": PAYPAL_US_TR_FLOW,
    }
    return opll_apply_amount_check(result, target_amount)
