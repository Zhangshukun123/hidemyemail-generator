from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hidemyemail_generator.account_plan import AccountPlanPresenter


EVENT_PREFIX = "HME_VERIFY_EVENT:"


def emit(payload: dict) -> None:
    print(EVENT_PREFIX + json.dumps(payload, ensure_ascii=False), flush=True)


def confirmed_invalid(detail: str) -> bool:
    value = str(detail or "")
    account_check_rejected = re.search(
        r"/backend-api/accounts/check[^:;]*:\s*HTTP\s+401\b", value
    )
    profile_rejected = re.search(r"/backend-api/me:\s*HTTP\s+401\b", value)
    return bool(account_check_rejected and profile_rejected)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate one saved OpenAI account")
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--locale", default="en-US")
    args = parser.parse_args()

    token = str(os.environ.get("HME_OPENAI_ACCESS_TOKEN") or "").strip()
    if not token:
        emit({"status": "error", "detail": "Access Token 为空"})
        return 2

    try:
        result = AccountPlanPresenter().check(
            token,
            proxy_url=str(os.environ.get("HME_OPENAI_PLAN_PROXY_URL") or ""),
            device_id=str(os.environ.get("HME_OPENAI_PLAN_DEVICE_ID") or ""),
            language=str(
                os.environ.get("HME_OPENAI_PLAN_LANGUAGE") or args.locale or "en-US"
            ),
            timezone_offset_min=str(
                os.environ.get("HME_OPENAI_PLAN_TIMEZONE_OFFSET_MIN") or "-"
            ),
        )
    except Exception as error:
        emit({"status": "error", "detail": str(error)})
        return 1

    payload = {
        "detail": str(result.detail or "AT 在线套餐查询完成"),
        "source": str(result.source or "accounts_check"),
        "http_status": result.http_status,
        "plan_type": str(result.plan_type or ""),
        "account_id": str(result.account_id or ""),
        "has_active_subscription": bool(result.has_active_subscription),
        "plus_trial_eligible": bool(result.plus_trial_eligible),
    }
    if result.status in {"plus", "free"}:
        emit({"status": result.status, **payload})
        return 0
    if result.needs_refresh:
        emit({"status": "invalid", "needs_refresh": True, **payload})
        return 0
    emit({"status": "error", "needs_refresh": False, **payload})
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
