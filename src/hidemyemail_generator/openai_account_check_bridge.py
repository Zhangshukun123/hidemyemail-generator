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
    return len(re.findall(r"HTTP\s+(?:401|403)", str(detail or ""))) >= 2


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
        # The source detector only raises this after an authenticated endpoint
        # returned a valid payload. With no paid marker, classify it as Free.
        emit({"status": "free", "detail": str(error)})
        return 0
    except app_backend.OpllAccountInvalidError as error:
        detail = str(error)
        # Delete only when both independent account endpoints rejected the AT.
        # A single 403 can also be a transient edge/WAF response.
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
