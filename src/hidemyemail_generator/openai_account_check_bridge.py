from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hidemyemail_generator.openai_browser_bridge import ensure_tkinter_importable


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

    source_dir = Path(args.source_dir).resolve()
    sys.path.insert(0, str(source_dir))
    ensure_tkinter_importable()
    try:
        import app_backend
    except Exception as error:
        emit({"status": "error", "detail": f"账号验证模块加载失败：{error}"})
        return 2

    try:
        account_type, detail = app_backend.opll_detect_account_type(
            token, request_locale=args.locale
        )
    except app_backend.OpllAccountPlanUnknownError as error:
        # An authenticated response without a reliable plan field is not proof
        # of a Free account. Keep the existing classification so a Plus account
        # cannot later be deleted after being incorrectly downgraded to Free.
        emit({"status": "error", "detail": f"套餐不明确，账号已保留：{error}"})
        return 1
    except app_backend.OpllAccountInvalidError as error:
        detail = str(error)
        # Delete only when both independent account endpoints explicitly report
        # an unauthenticated (401) token. A 403 can be returned by account-plan
        # permissions, edge protection, or regional policy for a valid Plus AT.
        if confirmed_invalid(detail):
            emit({"status": "invalid", "detail": detail})
            return 0
        emit({"status": "error", "detail": f"验证结果不确定，已保留账号：{detail}"})
        return 1
    except Exception as error:
        emit({"status": "error", "detail": str(error)})
        return 1

    normalized = "plus" if account_type in {"plus", "team", "pro"} else "free"
    emit({"status": normalized, "detail": str(detail or "在线验证通过")})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
