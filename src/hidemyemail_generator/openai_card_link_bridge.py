from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hidemyemail_generator.openai_browser_bridge import ensure_tkinter_importable


EVENT_PREFIX = "HME_CARD_LINK_EVENT:"


def emit(payload: dict) -> None:
    print(EVENT_PREFIX + json.dumps(payload, ensure_ascii=False), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create one ChatGPT card checkout link")
    parser.add_argument("--source-dir", required=True)
    parser.add_argument(
        "--method", choices=("standard", "ph_hosted"), default="standard"
    )
    parser.add_argument("--country", default="US")
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--locale", default="en-US")
    args = parser.parse_args()

    token = str(os.environ.get("HME_OPENAI_ACCESS_TOKEN") or "").strip()
    create_proxy_url = str(
        os.environ.get("HME_CARD_LINK_CREATE_PROXY_URL") or ""
    ).strip()
    promotion_proxy_url = str(
        os.environ.get("HME_CARD_LINK_PROMO_PROXY_URL") or ""
    ).strip()
    if not token:
        emit({"status": "error", "detail": "Access Token 为空"})
        return 2

    source_dir = Path(args.source_dir).resolve()
    sys.path.insert(0, str(source_dir))
    try:
        ensure_tkinter_importable()
        import app_backend

        if args.method == "ph_hosted":
            checkout = app_backend.generate_opll_philippines_short_link(
                token,
                create_proxy_url,
                promotion_proxy_url,
                "0",
            )
            link = str(
                checkout.get("chatgpt_checkout_url")
                or checkout.get("checkout_url")
                or checkout.get("long_url")
                or ""
            ).strip()
            if "/oaics_" not in link:
                raise RuntimeError("菲律宾 hosted 严格零元流程没有返回 oaics_ Checkout")
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
        if not link or not app_backend.opll_is_chatgpt_checkout_page_url(link):
            raise RuntimeError("Checkout 已创建，但未生成有效的 ChatGPT 支付地址")
    except Exception as error:
        detail = str(error or "直卡支付链接生成失败").replace(token, "[REDACTED]")
        for proxy_url in (create_proxy_url, promotion_proxy_url):
            if proxy_url:
                detail = detail.replace(proxy_url, "[REDACTED_PROXY]")
        emit({"status": "error", "detail": detail[:1000]})
        return 1

    event = {
        "status": "success",
        "url": link,
        "method": args.method,
        "country": str(checkout.get("billing_country") or args.country).upper(),
        "currency": str(checkout.get("currency") or args.currency).upper(),
        "payment_link_type": str(checkout.get("payment_link_type") or ""),
        "checkout_ui_mode": str(checkout.get("checkout_ui_mode") or "custom"),
    }
    if args.method == "ph_hosted":
        event.update(
            {
                "amount": str(checkout.get("stripe_amount") or ""),
                "amount_currency": str(
                    checkout.get("amount_currency") or "PHP"
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
