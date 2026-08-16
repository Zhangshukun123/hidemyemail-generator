from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hidemyemail_generator import card_link_runtime
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
    """Resolve the real create-proxy exit used as the payment identity country."""

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


def generate_paypal_us_event(
    token: str,
    create_proxy_url: str,
    promotion_proxy_url: str,
    target_amount: str,
    *,
    account_email: str = "",
    diagnostic_log=emit_progress,
    runtime=card_link_runtime,
) -> dict:
    """Run the embedded PayPal US/USD extractor without another project."""

    promotion_proxy_url = str(create_proxy_url or "").strip()
    link_proxy_identity = detect_link_proxy_identity(runtime, create_proxy_url)
    detected_country = str(
        link_proxy_identity.get("link_proxy_country") or ""
    ).upper()
    if detected_country and detected_country != "US":
        raise RuntimeError(
            "提链代理真实出口国家与 US 不一致："
            f"当前={detected_country}"
        )
    normalized_target = str(target_amount or "").strip()
    if normalized_target == "0":
        checkout = runtime.generate_opll_paypal_us_tr_long_link(
            token,
            create_proxy_url,
            promotion_proxy_url,
            normalized_target,
            account_email=str(account_email or "").strip(),
            diagnostic_log=diagnostic_log,
        )
    else:
        checkout = runtime.generate_opll_paypal_long_link(
            token,
            "US",
            "USD",
            create_proxy_url,
            promotion_proxy_url,
            create_proxy_url,
            normalized_target,
            account_email=str(account_email or "").strip(),
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
        "method": "paypal_us",
        "country": str(checkout.get("billing_country") or "US").upper(),
        "currency": str(checkout.get("currency") or "USD").upper(),
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
        **link_proxy_identity,
    }


def generate_paypal_gb_event(
    token: str,
    create_proxy_url: str,
    promotion_proxy_url: str,
    target_amount: str,
    *,
    account_email: str = "",
    diagnostic_log=emit_progress,
    runtime=card_link_runtime,
) -> dict:
    """Run the embedded PayPal GB/GBP zero-amount flow on one GB proxy."""

    create_proxy_url = str(create_proxy_url or "").strip()
    promotion_proxy_url = create_proxy_url
    link_proxy_identity = detect_link_proxy_identity(runtime, create_proxy_url)
    detected_country = str(
        link_proxy_identity.get("link_proxy_country") or ""
    ).upper()
    if detected_country and detected_country != "GB":
        raise RuntimeError(
            "提链代理真实出口国家与 GB 不一致："
            f"当前={detected_country}"
        )
    checkout = runtime.generate_opll_paypal_long_link(
        token,
        "GB",
        "GBP",
        create_proxy_url,
        promotion_proxy_url,
        create_proxy_url,
        "0",
        account_email=str(account_email or "").strip(),
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
        **link_proxy_identity,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Create one ChatGPT card checkout link")
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

    token = str(os.environ.get("HME_OPENAI_ACCESS_TOKEN") or "").strip()
    create_proxy_url = str(
        os.environ.get("HME_CARD_LINK_CREATE_PROXY_URL") or ""
    ).strip()
    promotion_proxy_url = str(
        os.environ.get("HME_CARD_LINK_PROMO_PROXY_URL") or ""
    ).strip()
    if args.method in {"paypal_us", "paypal_gb"}:
        # PayPal US and GB are intentionally single-proxy: Update, Stripe,
        # Confirm and Approve keep the same exit identity as Checkout creation.
        promotion_proxy_url = create_proxy_url
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
