from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hidemyemail_generator import card_link_runtime
from hidemyemail_generator.card_link_bridge_service import (
    MAX_PROTOCOL_LINE_BYTES,
    WORKER_MESSAGE_PREFIX,
    WORKER_PROTOCOL_VERSION,
)
from hidemyemail_generator.openai_browser_bridge import ensure_tkinter_importable


EVENT_PREFIX = "HME_CARD_LINK_EVENT:"
LOG_PREFIX = "HME_CARD_LINK_LOG:"
CS_LIVE_RE = re.compile(r"\bcs_live_[A-Za-z0-9_-]+")


def emit(payload: dict) -> None:
    print(EVENT_PREFIX + json.dumps(payload, ensure_ascii=False), flush=True)


def emit_progress(message: object) -> None:
    text = str(message or "").strip()
    if text:
        print(
            LOG_PREFIX + json.dumps({"message": text}, ensure_ascii=False),
            flush=True,
        )


def checkout_id_type(value: object) -> str:
    checkout_id = str(value or "").strip()
    if checkout_id.startswith("oaics_"):
        return "oaics"
    if checkout_id.startswith("cs_live_"):
        return "cs_live"
    if checkout_id.startswith("cs_"):
        return "cs"
    return "other"


def emit_checkout_classification(method: str, classification: str) -> None:
    emit(
        {
            "status": "classified",
            "classification": classification,
            "checkout_id_type": classification,
            "method": method,
        }
    )


def card_link_error_is_retryable(
    method: str,
    error: Exception,
    *,
    runtime=card_link_runtime,
) -> bool:
    """Return whether a fresh outer extraction attempt can help."""

    detail = str(error or "").lower()
    expected_country = {
        "paypal_us": "us",
        "paypal_gb": "gb",
    }.get(method)
    if expected_country and (
        "billing country must match request country" in detail
        or f"提链代理真实出口国家与 {expected_country}" in detail
    ):
        return True
    classifier = getattr(runtime, "opll_is_non_retryable_link_error", None)
    if callable(classifier):
        return not bool(classifier(error))
    return True


def detect_link_proxy_identity(app_backend, proxy_url: str) -> dict[str, str]:
    """Resolve one real proxy exit as a non-secret link identity."""

    proxy = str(proxy_url or "").strip()
    detector = getattr(app_backend, "detect_proxy_health", None)
    if not proxy or not callable(detector):
        return {}
    health = detector(
        proxy,
        check_stripe=False,
        check_chatgpt=False,
    )
    country = str(getattr(health, "country", "") or "").strip().upper()
    exit_ip = str(getattr(health, "ip", "") or "").strip()
    if not bool(getattr(health, "success", False)) or not country or not exit_ip:
        detail = str(
            getattr(health, "error", "")
            or getattr(health, "failed_stage", "")
            or "出口国家或 IP 缺失"
        ).strip()
        raise RuntimeError(f"提链代理真实出口解析失败：{detail}")
    return {
        "link_proxy_country": country,
        "link_proxy_ip": exit_ip,
    }


def detect_paypal_proxy_pair(
    app_backend,
    checkout_proxy_url: str,
    final_proxy_url: str,
    expected_country: str,
) -> dict[str, object]:
    """Validate the PAY153 two-proxy contract and expose safe identities only."""

    checkout_proxy = str(checkout_proxy_url or "").strip()
    final_proxy = str(final_proxy_url or "").strip()
    country = str(expected_country or "").strip().upper()
    if not checkout_proxy or not final_proxy or checkout_proxy == final_proxy:
        raise RuntimeError(
            f"PayPal {country}/PAY153 需要两条不同的 {country} 粘性代理"
        )

    checkout_identity = detect_link_proxy_identity(
        app_backend,
        checkout_proxy,
    )
    final_identity = detect_link_proxy_identity(
        app_backend,
        final_proxy,
    )
    checkout_country = str(
        checkout_identity.get("link_proxy_country") or ""
    ).upper()
    final_country = str(final_identity.get("link_proxy_country") or "").upper()
    checkout_ip = str(checkout_identity.get("link_proxy_ip") or "").strip()
    final_ip = str(final_identity.get("link_proxy_ip") or "").strip()
    if not checkout_country or not checkout_ip:
        raise RuntimeError("代理池 1 提链代理真实出口解析失败")
    if not final_country or not final_ip:
        raise RuntimeError("代理池 2 提链代理真实出口解析失败")
    if checkout_country != country:
        raise RuntimeError(
            f"代理池 1 提链代理真实出口国家与 {country} 不一致："
            f"当前={checkout_country}"
        )
    if final_country != country:
        raise RuntimeError(
            f"代理池 2 提链代理真实出口国家与 {country} 不一致："
            f"当前={final_country}"
        )
    if checkout_ip == final_ip:
        raise RuntimeError(
            f"PayPal {country}/PAY153 两条粘性代理解析到同一出口 IP"
        )
    return {
        "checkout_proxy_country": checkout_country,
        "checkout_proxy_ip": checkout_ip,
        "link_proxy_country": final_country,
        "link_proxy_ip": final_ip,
        "independent_proxy_pair": True,
    }


def paypal_two_proxy_event_metadata(
    checkout: dict,
    *,
    default_flow: str,
    expected_country: str,
) -> dict[str, object]:
    """Present verified, non-secret two-proxy protocol facts."""

    country = str(expected_country or "").strip().upper()
    update_count = int(checkout.get("promotion_update_count") or 0)
    update_country = str(
        checkout.get("promotion_update_country") or ""
    ).strip().upper()
    taxes_performed = bool(checkout.get("checkout_taxes_performed"))
    taxes_count = int(checkout.get("checkout_taxes_count") or 0)
    promotion_timing = str(checkout.get("promotion_timing") or "").strip()
    approval_before_promotion = bool(
        checkout.get("approval_completed_before_promotion")
    )
    same_checkout_promotion = bool(checkout.get("same_checkout_promotion"))
    if update_count != 1:
        raise RuntimeError("PayPal 双代理流程必须且只能完成一次 Checkout Update")
    if update_country != country:
        raise RuntimeError(
            "PayPal 双代理优惠 Update 国家不匹配："
            f"期望={country}，当前={update_country or '<missing>'}"
        )

    metadata = {
        "paypal_flow": str(checkout.get("paypal_flow") or default_flow),
        "variant": str(checkout.get("variant") or default_flow),
        "promotion_update_count": update_count,
        "promotion_update_country": update_country,
        "checkout_taxes_performed": taxes_performed,
        "checkout_taxes_count": taxes_count,
        "checkout_taxes_country": str(
            checkout.get("checkout_taxes_country") or ""
        ).upper(),
        "checkout_taxes_currency": str(
            checkout.get("checkout_taxes_currency") or ""
        ).upper(),
        "approval_strategy": str(checkout.get("approval_strategy") or ""),
        "promotion_timing": promotion_timing,
        "promotion_checkout_id": str(
            checkout.get("promotion_checkout_id") or ""
        ),
        "approval_completed_before_promotion": approval_before_promotion,
        "same_checkout_promotion": same_checkout_promotion,
        "session_proxy_consistent": bool(
            checkout.get("session_proxy_consistent")
        ),
        "stripe_context_consistent": bool(
            checkout.get("stripe_context_consistent")
        ),
        "browser_http_used": bool(checkout.get("browser_http_used")),
    }
    if country == "US":
        taxes_country = str(
            checkout.get("checkout_taxes_country") or ""
        ).strip().upper()
        taxes_currency = str(
            checkout.get("checkout_taxes_currency") or ""
        ).strip().upper()
        if (
            not taxes_performed
            or taxes_count != 1
            or taxes_country != "US"
            or taxes_currency != "USD"
        ):
            raise RuntimeError(
                "PayPal US/PAY153 必须完成一次 US/USD Checkout Taxes"
            )
        return metadata

    if country != "GB":
        raise RuntimeError(f"PayPal 双代理事件不支持国家：{country or '<missing>'}")

    checkout_id = str(checkout.get("cs_id") or "").strip()
    promotion_checkout_id = str(
        checkout.get("promotion_checkout_id") or ""
    ).strip()
    stripe_tax_region_count = int(
        checkout.get("stripe_tax_region_count") or 0
    )
    stripe_tax_region_country = str(
        checkout.get("stripe_tax_region_country") or ""
    ).strip().upper()
    snapshot_count = int(checkout.get("checkout_snapshot_count") or 0)
    update_attempts = int(checkout.get("promotion_update_attempts") or 0)
    post_init_count = int(checkout.get("post_approval_init_count") or 0)
    post_init_proxy = str(
        checkout.get("post_approval_init_proxy_used") or ""
    ).strip()
    promotion_proxy = str(checkout.get("promotion_proxy") or "").strip()
    amount = str(checkout.get("stripe_amount") or "").strip()
    amount_currency = str(
        checkout.get("amount_currency")
        or checkout.get("currency")
        or ""
    ).strip().upper()
    approval_state = str(checkout.get("approval_state") or "").strip().lower()
    paypal_ba_state = str(
        checkout.get("paypal_ba_state") or ""
    ).strip().lower()

    if promotion_timing != "post_approve":
        raise RuntimeError("PayPal GB 优惠时序必须为 post_approve")
    if not approval_before_promotion:
        raise RuntimeError("PayPal GB 优惠 Update 必须发生在 Approval 完成后")
    if (
        not checkout_id.startswith("cs_")
        or promotion_checkout_id != checkout_id
        or not same_checkout_promotion
        or not bool(checkout.get("checkout_identity_preserved"))
    ):
        raise RuntimeError("PayPal GB 后置优惠必须复用同一标准 Checkout")
    if not bool(checkout.get("stripe_context_consistent")):
        raise RuntimeError("PayPal GB 池 1/池 2 必须复用一致的 Stripe Context")
    if not bool(checkout.get("session_proxy_consistent")):
        raise RuntimeError("PayPal GB 会话与代理绑定不一致")
    if taxes_performed or taxes_count != 0:
        raise RuntimeError("PayPal GB 后置优惠流程不得使用 ChatGPT Checkout Taxes")
    if stripe_tax_region_count != 1 or stripe_tax_region_country != "GB":
        raise RuntimeError("PayPal GB 必须完成一次 Stripe tax_region")
    if snapshot_count != 1:
        raise RuntimeError("PayPal GB 必须尝试一次 Checkout snapshot")
    if not 1 <= update_attempts <= 3:
        raise RuntimeError("PayPal GB 优惠 Update 尝试次数必须在 1 到 3 次之间")
    if not 1 <= post_init_count <= 3:
        raise RuntimeError("PayPal GB 池 2 后置 Stripe Init 次数必须在 1 到 3 次之间")
    if not post_init_proxy or (
        post_init_proxy.casefold() != "pool2"
        and (not promotion_proxy or post_init_proxy != promotion_proxy)
    ):
        raise RuntimeError("PayPal GB 优惠后 Stripe Init 必须使用池 2")
    if approval_state != "approved" or paypal_ba_state != "approved":
        raise RuntimeError("PayPal GB 优惠后 Payment Page 与 PayPal BA 必须保持 approved")
    if not bool(checkout.get("ba_preserved_after_promotion")):
        raise RuntimeError("PayPal GB 优惠后必须保留已批准的 PayPal BA")
    if amount != "0" or amount_currency != "GBP":
        raise RuntimeError(
            "PayPal GB 优惠后 Payment Page 必须为 approved + GBP/0"
        )

    metadata.update(
        {
            "promotion_update_attempts": update_attempts,
            "post_approval_init_count": post_init_count,
            "post_approval_init_proxy_used": "pool2",
            "checkout_snapshot_performed": bool(
                checkout.get("checkout_snapshot_performed")
            ),
            "checkout_snapshot_count": snapshot_count,
            "stripe_tax_region_count": stripe_tax_region_count,
            "stripe_tax_region_country": stripe_tax_region_country,
            "approval_state": approval_state,
            "paypal_ba_state": paypal_ba_state,
            "ba_preserved_after_promotion": True,
            "checkout_identity_preserved": True,
        }
    )
    return metadata


def paypal_ba_approve_url(runtime, checkout: dict) -> str:
    """Return a validated PayPal Billing Agreement approval URL."""

    link = str(checkout.get("paypal_ba_approve_url") or "").strip()
    validator = getattr(runtime, "opll_is_paypal_ba_approve_url", None)
    if not link or not callable(validator) or not validator(link):
        raise RuntimeError(
            "PayPal Checkout 已创建，但未生成有效的 PayPal BA 授权地址"
        )
    return link


def generate_paypal_us_event(
    token: str,
    create_proxy_url: str,
    promotion_proxy_url: str,
    target_amount: str,
    *,
    account_email: str = "",
    sentinel_so_enabled: bool = False,
    session_context: dict | None = None,
    diagnostic_log=emit_progress,
    runtime=card_link_runtime,
) -> dict:
    """Run the embedded PayPal US/USD PAY153 two-proxy extractor."""

    proxy_pair = detect_paypal_proxy_pair(
        runtime,
        create_proxy_url,
        promotion_proxy_url,
        "US",
    )
    normalized_target = str(target_amount or "0").strip() or "0"
    checkout = runtime.generate_opll_paypal_pay153_long_link(
        token,
        create_proxy_url,
        promotion_proxy_url,
        normalized_target,
        account_email=str(account_email or "").strip(),
        diagnostic_log=diagnostic_log,
        sentinel_so_enabled=sentinel_so_enabled,
        session_context=session_context,
    )
    link = paypal_ba_approve_url(runtime, checkout)
    checkout_country = str(checkout.get("billing_country") or "US").upper()
    checkout_currency = str(checkout.get("currency") or "USD").upper()
    if (checkout_country, checkout_currency) != ("US", "USD"):
        raise RuntimeError(
            "PayPal 美国模式返回了错误的 Checkout 国家或币种："
            f"{checkout_country}/{checkout_currency}"
        )
    return {
        "status": "success",
        "url": link,
        "method": "paypal_us",
        "country": checkout_country,
        "currency": checkout_currency,
        "payment_link_type": str(checkout.get("payment_link_type") or ""),
        "checkout_ui_mode": str(checkout.get("checkout_ui_mode") or "custom"),
        "checkout_id_type": checkout_id_type(checkout.get("cs_id")),
        "amount": str(checkout.get("stripe_amount") or ""),
        "amount_currency": str(
            checkout.get("amount_currency")
            or checkout.get("currency")
            or "USD"
        ).upper(),
        "amount_verification": str(checkout.get("amount_verification") or ""),
        "promotion_applied": bool(checkout.get("promotion_applied")),
        "promotion_strategy": str(checkout.get("promotion_strategy") or ""),
        **paypal_two_proxy_event_metadata(
            checkout,
            default_flow="pay153_protocol",
            expected_country="US",
        ),
        **proxy_pair,
    }


def generate_paypal_gb_event(
    token: str,
    create_proxy_url: str,
    promotion_proxy_url: str,
    target_amount: str,
    *,
    account_email: str = "",
    sentinel_so_enabled: bool = False,
    session_context: dict | None = None,
    browser_runtime: dict | None = None,
    diagnostic_log=emit_progress,
    runtime=card_link_runtime,
) -> dict:
    """Run the embedded PayPal GB/GBP PAY153 two-proxy extractor."""

    create_proxy_url = str(create_proxy_url or "").strip()
    promotion_proxy_url = str(promotion_proxy_url or "").strip()
    proxy_pair = detect_paypal_proxy_pair(
        runtime,
        create_proxy_url,
        promotion_proxy_url,
        "GB",
    )
    normalized_target = str(target_amount or "0").strip() or "0"
    generator = getattr(
        runtime,
        "generate_opll_paypal_gb_two_proxy_long_link",
        None,
    )
    if not callable(generator):
        generator = getattr(
            runtime,
            "generate_opll_paypal_gb_pay153_long_link",
            None,
        )
    if not callable(generator):
        raise RuntimeError("PayPal GB 双代理运行时入口缺失")
    generator_kwargs = {
        "account_email": str(account_email or "").strip(),
        "diagnostic_log": diagnostic_log,
        "sentinel_so_enabled": sentinel_so_enabled,
        "session_context": session_context,
    }
    if isinstance(browser_runtime, dict) and browser_runtime:
        generator_kwargs["browser_runtime"] = browser_runtime
    checkout = generator(
        token,
        create_proxy_url,
        promotion_proxy_url,
        normalized_target,
        **generator_kwargs,
    )
    link = paypal_ba_approve_url(runtime, checkout)
    checkout_country = str(checkout.get("billing_country") or "GB").upper()
    checkout_currency = str(checkout.get("currency") or "GBP").upper()
    if (checkout_country, checkout_currency) != ("GB", "GBP"):
        raise RuntimeError(
            "PayPal 英国模式返回了错误的 Checkout 国家或币种："
            f"{checkout_country}/{checkout_currency}"
        )
    return {
        "status": "success",
        "url": link,
        "method": "paypal_gb",
        "country": checkout_country,
        "currency": checkout_currency,
        "payment_link_type": str(checkout.get("payment_link_type") or ""),
        "checkout_ui_mode": str(checkout.get("checkout_ui_mode") or "custom"),
        "checkout_id_type": checkout_id_type(checkout.get("cs_id")),
        "amount": str(checkout.get("stripe_amount") or ""),
        "amount_currency": str(
            checkout.get("amount_currency")
            or checkout.get("currency")
            or "GBP"
        ).upper(),
        "amount_verification": str(checkout.get("amount_verification") or ""),
        "promotion_applied": bool(checkout.get("promotion_applied")),
        "promotion_strategy": str(checkout.get("promotion_strategy") or ""),
        **paypal_two_proxy_event_metadata(
            checkout,
            default_flow="gb_two_proxy_promotion",
            expected_country="GB",
        ),
        **proxy_pair,
    }


def generate_paypal_de_event(
    token: str,
    create_proxy_url: str,
    promotion_proxy_url: str,
    target_amount: str,
    *,
    account_email: str = "",
    sentinel_so_enabled: bool = False,
    session_context: dict | None = None,
    diagnostic_log=emit_progress,
    runtime=card_link_runtime,
) -> dict:
    """Create a DE/EUR PayPal checkout with promotion attached at Create."""

    create_proxy_url = str(create_proxy_url or "").strip()
    link_proxy_identity = detect_link_proxy_identity(runtime, create_proxy_url)
    detected_country = str(
        link_proxy_identity.get("link_proxy_country") or ""
    ).upper()
    if detected_country and detected_country != "DE":
        raise RuntimeError(
            "提链代理真实出口国家与 DE 不一致："
            f"当前={detected_country}"
        )
    checkout = runtime.generate_opll_paypal_long_link(
        token,
        "DE",
        "EUR",
        create_proxy_url,
        create_proxy_url,
        create_proxy_url,
        "0",
        paypal_flow=runtime.PAYPAL_BR_DE_STRICT_ZERO_FLOW,
        account_email=str(account_email or "").strip(),
        checkout_includes_trial_promo=True,
        checkout_create_promotion_only=True,
        sentinel_so_enabled=sentinel_so_enabled,
        session_context=session_context,
        diagnostic_log=diagnostic_log,
    )
    link = str(
        checkout.get("paypal_ba_approve_url")
        or checkout.get("provider_redirect_url")
        or checkout.get("long_url")
        or ""
    ).strip()
    if not link or not runtime.opll_is_paypal_success_url(link):
        raise RuntimeError("PayPal Checkout 已创建，但未生成有效的 PayPal 授权地址")
    return {
        "status": "success",
        "url": link,
        "method": "de_oaics_paypal",
        "country": "DE",
        "currency": "EUR",
        "payment_link_type": str(checkout.get("payment_link_type") or ""),
        "checkout_ui_mode": str(checkout.get("checkout_ui_mode") or "custom"),
        "checkout_id_type": checkout_id_type(checkout.get("cs_id")),
        "amount": str(checkout.get("stripe_amount") or ""),
        "amount_currency": "EUR",
        "amount_verification": str(checkout.get("amount_verification") or ""),
        "promotion_applied": bool(checkout.get("promotion_applied")),
        "promotion_strategy": str(
            checkout.get("promotion_strategy") or "checkout_create"
        ),
        **link_proxy_identity,
    }


def emit_worker(payload: dict, *, output_stream=None) -> None:
    stream = output_stream or sys.stdout
    message = {"v": WORKER_PROTOCOL_VERSION, **payload}
    stream.write(
        WORKER_MESSAGE_PREFIX
        + json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        + "\n"
    )
    stream.flush()


def _worker_redact(value: object, secrets: tuple[str, ...]) -> str:
    text = str(value or "").strip()
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def _worker_request_secrets(payload: dict) -> tuple[str, ...]:
    candidates = [
        str(payload.get("access_token") or "").strip(),
        str(payload.get("account_email") or "").strip(),
    ]
    for key in ("create_proxy_url", "promotion_proxy_url"):
        proxy_url = str(payload.get(key) or "").strip()
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
    session_context = payload.get("session_context")
    if isinstance(session_context, dict):
        pending: list[object] = [session_context]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                pending.extend(item.values())
            elif isinstance(item, (list, tuple)):
                pending.extend(item)
            elif isinstance(item, str) and item.strip():
                candidates.append(item.strip())
    return tuple(
        sorted(
            {candidate for candidate in candidates if candidate},
            key=len,
            reverse=True,
        )
    )


def _worker_generate(
    request_id: str,
    payload: dict,
    *,
    output_stream=None,
) -> dict:
    method = str(payload.get("method") or "").strip()
    if method not in {"de_oaics_paypal", "paypal_us", "paypal_gb"}:
        raise ValueError(f"共享提链服务不支持方法：{method or 'empty'}")
    token = str(payload.get("access_token") or "").strip()
    if not token:
        raise ValueError("Access Token 为空")
    create_proxy_url = str(payload.get("create_proxy_url") or "").strip()
    promotion_proxy_url = str(
        payload.get("promotion_proxy_url") or ""
    ).strip()
    if method == "de_oaics_paypal":
        promotion_proxy_url = create_proxy_url
    account_email = str(payload.get("account_email") or "").strip()
    target_amount = str(payload.get("target_amount") or "").strip()
    sentinel_so_enabled = payload.get("sentinel_so_enabled") is True
    session_context = (
        dict(payload.get("session_context"))
        if isinstance(payload.get("session_context"), dict)
        else {}
    )
    secrets = _worker_request_secrets(payload)
    card_link_runtime.clear_proxy_exit_cache()

    def diagnostic_log(message: object) -> None:
        text = _worker_redact(message, secrets)
        if text:
            emit_worker(
                {
                    "id": request_id,
                    "type": "log",
                    "message": text[:500],
                },
                output_stream=output_stream,
            )

    generator = {
        "de_oaics_paypal": generate_paypal_de_event,
        "paypal_us": generate_paypal_us_event,
        "paypal_gb": generate_paypal_gb_event,
    }[method]
    options = {
        "account_email": account_email,
        "sentinel_so_enabled": sentinel_so_enabled,
        "diagnostic_log": diagnostic_log,
    }
    if session_context:
        options["session_context"] = session_context
    return generator(
        token,
        create_proxy_url,
        promotion_proxy_url,
        target_amount,
        **options,
    )


def _handle_worker_request(request: dict, *, output_stream=None) -> bool:
    """Handle one request and release all request-owned values before returning."""

    payload = None
    try:
        request_id = str(request.get("id") or "")[:128]
        if request.get("v") != WORKER_PROTOCOL_VERSION:
            emit_worker(
                {
                    "id": request_id,
                    "type": "error",
                    "detail": "提链服务协议版本不匹配",
                    "retryable": False,
                },
                output_stream=output_stream,
            )
            return False
        operation = str(request.get("op") or "")
        if operation == "shutdown":
            emit_worker(
                {"id": request_id, "type": "stopped"},
                output_stream=output_stream,
            )
            return True
        payload = request.get("payload")
        if operation != "generate" or not isinstance(payload, dict):
            emit_worker(
                {
                    "id": request_id,
                    "type": "error",
                    "detail": "提链请求操作无效",
                    "retryable": False,
                },
                output_stream=output_stream,
            )
            return False
        secrets = _worker_request_secrets(payload)
        method = str(payload.get("method") or "").strip()
        try:
            event = _worker_generate(
                request_id,
                payload,
                output_stream=output_stream,
            )
            emit_worker(
                {"id": request_id, "type": "result", "event": event},
                output_stream=output_stream,
            )
        except Exception as error:
            detail = _worker_redact(error, secrets)
            emit_worker(
                {
                    "id": request_id,
                    "type": "error",
                    "detail": (detail or "直卡支付链接生成失败")[:1000],
                    "retryable": card_link_error_is_retryable(
                        method,
                        error,
                    ),
                },
                output_stream=output_stream,
            )
        return False
    finally:
        if isinstance(payload, dict):
            payload.clear()
        request.clear()


def worker_main(*, input_stream=None, output_stream=None) -> int:
    """Serve PayPal US/GB requests over a bounded JSONL pipe."""

    source = input_stream or sys.stdin.buffer
    emit_worker({"type": "ready", "pid": os.getpid()}, output_stream=output_stream)
    while True:
        raw_line = source.readline(MAX_PROTOCOL_LINE_BYTES + 1)
        if not raw_line:
            return 0
        if isinstance(raw_line, bytes):
            oversized = len(raw_line) > MAX_PROTOCOL_LINE_BYTES
            line = raw_line.decode("utf-8", errors="replace")
        else:
            oversized = len(raw_line.encode("utf-8")) > MAX_PROTOCOL_LINE_BYTES
            line = raw_line
        if oversized:
            while raw_line and not raw_line.endswith(
                b"\n" if isinstance(raw_line, bytes) else "\n"
            ):
                raw_line = source.readline(MAX_PROTOCOL_LINE_BYTES + 1)
            emit_worker(
                {
                    "id": "",
                    "type": "error",
                    "detail": "提链请求数据过长",
                    "retryable": False,
                },
                output_stream=output_stream,
            )
            raw_line = None
            line = ""
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            emit_worker(
                {
                    "id": "",
                    "type": "error",
                    "detail": "提链请求格式无效",
                    "retryable": False,
                },
                output_stream=output_stream,
            )
            raw_line = None
            line = ""
            continue
        raw_line = None
        line = ""
        if not isinstance(request, dict):
            request = None
            continue
        should_stop = _handle_worker_request(
            request,
            output_stream=output_stream,
        )
        request = None
        if should_stop:
            return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Create one ChatGPT card checkout link")
    parser.add_argument(
        "--worker",
        action="store_true",
        help="Serve reusable PayPal US/GB requests over stdin/stdout",
    )
    parser.add_argument(
        "--source-dir",
        default="",
        help="Legacy runtime directory for non-US card-link modes",
    )
    parser.add_argument(
        "--method",
        choices=(
            "standard",
            "ph_hosted",
            "de_oaics_paypal",
            "paypal_us",
            "paypal_gb",
        ),
        default="standard",
    )
    parser.add_argument("--account-email", default="")
    parser.add_argument("--country", default="US")
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--locale", default="en-US")
    parser.add_argument("--target-amount", default="")
    args = parser.parse_args()

    if args.worker:
        return worker_main()

    token = str(os.environ.get("HME_OPENAI_ACCESS_TOKEN") or "").strip()
    create_proxy_url = str(
        os.environ.get("HME_CARD_LINK_CREATE_PROXY_URL") or ""
    ).strip()
    promotion_proxy_url = str(
        os.environ.get("HME_CARD_LINK_PROMO_PROXY_URL") or ""
    ).strip()
    if not token:
        emit(
            {
                "status": "error",
                "detail": "Access Token 为空",
                "retryable": False,
            }
        )
        return 2

    error_runtime = card_link_runtime
    try:
        if args.method == "paypal_us":
            emit(
                generate_paypal_us_event(
                    token,
                    create_proxy_url,
                    promotion_proxy_url,
                    args.target_amount,
                    account_email=args.account_email,
                )
            )
            return 0
        if args.method == "paypal_gb":
            emit(
                generate_paypal_gb_event(
                    token,
                    create_proxy_url,
                    promotion_proxy_url,
                    args.target_amount,
                    account_email=args.account_email,
                )
            )
            return 0

        source_dir = Path(args.source_dir).resolve()
        sys.path.insert(0, str(source_dir))
        ensure_tkinter_importable()
        import app_backend
        error_runtime = app_backend

        link_proxy_identity = (
            detect_link_proxy_identity(app_backend, create_proxy_url)
            if args.method in {"de_oaics_paypal", "paypal_us"}
            else {}
        )

        if args.method == "ph_hosted":
            checkout = app_backend.generate_opll_philippines_short_link(
                token,
                create_proxy_url,
                promotion_proxy_url,
                "0",
                diagnostic_log=emit_progress,
            )
            link = str(
                checkout.get("chatgpt_checkout_url")
                or checkout.get("checkout_url")
                or checkout.get("long_url")
                or ""
            ).strip()
            if "/oaics_" not in link:
                raise RuntimeError("菲律宾 hosted 严格零元流程没有返回 oaics_ Checkout")
        elif args.method == "de_oaics_paypal":
            checkout = app_backend.generate_opll_de_oaics_paypal_link(
                token,
                create_proxy_url,
                promotion_proxy_url,
                "0",
                account_email=str(args.account_email or "").strip(),
                diagnostic_log=emit_progress,
            )
            checkout_id = str(checkout.get("cs_id") or "").strip()
            link = str(
                checkout.get("paypal_ba_approve_url")
                or checkout.get("provider_redirect_url")
                or checkout.get("long_url")
                or ""
            ).strip()
            if not checkout_id.startswith("oaics_"):
                raise RuntimeError(
                    "PayPal DE/EUR 模式没有返回 oaics_ Checkout"
                )
            if str(checkout.get("billing_country") or "").upper() != "DE":
                raise RuntimeError("PayPal OAICS Checkout 国家不是 DE")
            if str(checkout.get("currency") or "").upper() != "EUR":
                raise RuntimeError("PayPal OAICS Checkout 币种不是 EUR")
            if str(checkout.get("stripe_amount") or "").strip() != "0":
                raise RuntimeError("PayPal OAICS Checkout 金额不是 0")
            if str(checkout.get("amount_currency") or "EUR").upper() != "EUR":
                raise RuntimeError("PayPal OAICS Checkout 金额币种不是 EUR")
            if not checkout.get("promotion_applied"):
                raise RuntimeError("PayPal OAICS Checkout 未确认优惠已生效")
        else:
            checkout = app_backend.opll_create_checkout(
                token,
                str(args.country or "US").upper(),
                str(args.currency or "USD").upper(),
                request_locale=str(args.locale or "en-US"),
                include_trial_promo=False,
                checkout_ui_mode="custom",
            )
            link = app_backend.opll_chatgpt_checkout_page_url(
                checkout.get("cs_id", ""),
                checkout.get("billing_country", args.country),
                checkout.get("processor_entity", ""),
            )
        if args.method in {"de_oaics_paypal", "paypal_us"}:
            if not link or not app_backend.opll_is_paypal_success_url(link):
                raise RuntimeError(
                    "PayPal Checkout 已创建，但未生成有效的 PayPal 授权地址"
                )
        elif not link or not app_backend.opll_is_chatgpt_checkout_page_url(link):
            raise RuntimeError("Checkout 已创建，但未生成有效的 ChatGPT 支付地址")
    except Exception as error:
        detail = str(error or "直卡支付链接生成失败").replace(token, "[REDACTED]")
        if args.method == "de_oaics_paypal" and CS_LIVE_RE.search(detail):
            emit_checkout_classification(args.method, "cs_live")
            return 0
        for proxy_url in (create_proxy_url, promotion_proxy_url):
            if proxy_url:
                detail = detail.replace(proxy_url, "[REDACTED_PROXY]")
        emit(
            {
                "status": "error",
                "detail": detail[:1000],
                "retryable": card_link_error_is_retryable(
                    args.method,
                    error,
                    runtime=error_runtime,
                ),
            }
        )
        return 1

    event = {
        "status": "success",
        "url": link,
        "method": args.method,
        "country": str(checkout.get("billing_country") or args.country).upper(),
        "currency": str(checkout.get("currency") or args.currency).upper(),
        "payment_link_type": str(checkout.get("payment_link_type") or ""),
        "checkout_ui_mode": str(checkout.get("checkout_ui_mode") or "custom"),
        "checkout_id_type": checkout_id_type(checkout.get("cs_id")),
        **link_proxy_identity,
    }
    if args.method in {
        "ph_hosted",
        "de_oaics_paypal",
        "paypal_us",
    }:
        event.update(
            {
                "amount": str(checkout.get("stripe_amount") or ""),
                "amount_currency": str(
                    checkout.get("amount_currency")
                    or checkout.get("currency")
                    or args.currency
                ).upper(),
                "amount_verification": str(
                    checkout.get("amount_verification") or ""
                ),
                "promotion_applied": bool(checkout.get("promotion_applied")),
                "promotion_strategy": str(
                    checkout.get("promotion_strategy") or ""
                ),
            }
        )
    emit(event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
