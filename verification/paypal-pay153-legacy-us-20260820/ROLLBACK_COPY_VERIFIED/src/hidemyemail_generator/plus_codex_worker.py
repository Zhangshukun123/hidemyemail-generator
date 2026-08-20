"""Isolated worker for post-payment Codex OAuth and add-phone."""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


EVENT_PREFIX = "HME_PLUS_CODEX_EVENT:"
RESULT_PREFIX = "HME_PLUS_CODEX_RESULT:"


def _configure_utf8() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def _safe_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(
        r"(?i)((?:access|refresh|id)[_-]?token|api[_-]?key|otp)\s*[=:]\s*\S+",
        r"\1=[REDACTED]",
        text,
    )
    text = re.sub(r"(?i)(https?://)([^/@\s]+)@", r"\1[REDACTED]@", text)
    return text[:800]


def _emit(event: str, payload: dict[str, Any] | None = None) -> None:
    safe: dict[str, Any] = {"event": str(event or "protocol")}
    for key, value in dict(payload or {}).items():
        if key.lower() in {
            "access_token",
            "refresh_token",
            "id_token",
            "api_key",
            "otp",
            "code",
        }:
            continue
        safe[str(key)] = _safe_text(value) if isinstance(value, str) else value
    print(EVENT_PREFIX + json.dumps(safe, ensure_ascii=False), flush=True)


def _email_code_fetcher(
    _email: str,
    relay_url: str,
    _client_id: str,
    timeout_seconds: int,
) -> str | None:
    """Poll the signed local code relay without exposing its token in logs."""

    deadline = time.monotonic() + max(5, int(timeout_seconds or 180))
    while time.monotonic() < deadline:
        try:
            request = Request(
                str(relay_url or ""),
                headers={"Accept": "text/plain", "User-Agent": "HME Plus Codex/1.0"},
                method="GET",
            )
            with urlopen(request, timeout=10) as response:  # nosec B310
                body = response.read().decode("utf-8", errors="replace").strip()
            code = "".join(character for character in body if character.isalnum())
            if 4 <= len(code) <= 10:
                return code
        except HTTPError as error:
            if error.code not in {404, 408, 425, 429, 502, 503}:
                raise
        except (OSError, TimeoutError, URLError):
            pass
        time.sleep(1.2)
    return None


def _browser_oauth_session(payload: dict[str, Any]) -> dict[str, Any]:
    """Authenticate OAuth using only code shipped in this project."""

    from hidemyemail_generator.plus_codex_browser import (
        run_browser_oauth_session,
    )

    return run_browser_oauth_session(
        payload,
        emit=_emit,
        email_code_fetcher=_email_code_fetcher,
    )


def run(payload: dict[str, Any]) -> dict[str, Any]:
    source_root = Path(str(payload.get("source_root") or "")).resolve()
    core_root = Path(str(payload.get("gptfree_root") or "")).resolve() / "core"
    for path in (str(source_root), str(core_root)):
        if path and path not in sys.path:
            sys.path.insert(0, path)

    from gpt_trial_protocol.codex_oauth import run_codex_oauth_protocol
    from hidemyemail_generator.browser_tasks import sync_account_browser_cookies
    from hidemyemail_generator.plus_sms import PlusSmsProviderFactory

    browser_auth = _browser_oauth_session(payload)
    browser_cookies = [
        dict(item)
        for item in (browser_auth.get("cookies") or [])
        if isinstance(item, dict)
    ]
    sync_account_browser_cookies(
        Path(str(payload.get("db_file") or "")),
        str(payload.get("email") or ""),
        browser_cookies,
    )
    _emit(
        "cookies_synced",
        {
            "message": "Roxy 登录 Cookie 已同步到当前账号；现在切换纯协议手机号接码",
            "stage": "cookie_sync",
            "level": "success",
            "cookie_count": len(browser_cookies),
        },
    )
    oauth_record = browser_auth.get("oauth_record")
    if isinstance(oauth_record, dict) and oauth_record:
        return {
            "ok": True,
            **oauth_record,
            "phone_bound": True,
            "phone": "",
            "activation_id": "",
            "sms_provider": "",
            "sms_country": "",
            "sms_max_price": 0,
            "phone_attempts": 0,
        }

    provider = PlusSmsProviderFactory(Path(str(payload.get("db_file") or ""))).create(
        str(payload.get("sms_provider") or ""),
        on_log=lambda event: _emit("sms_route", event),
        purpose="binding",
        countries=payload.get("sms_countries") or [],
        max_price=float(payload.get("sms_max_price") or 0.064),
    )

    def log_fn(message: str) -> None:
        _emit("protocol_log", {"message": _safe_text(message)})

    result = run_codex_oauth_protocol(
        email=str(payload.get("email") or ""),
        password=str(payload.get("password") or ""),
        # A signed local relay URL lets iCloud, Gmail, and zkgmail accounts use
        # the workspace's existing OTP readers when OAuth asks for confirmation.
        outlook_refresh_token=str(payload.get("code_url") or ""),
        outlook_client_id="",
        initial_session_token="",
        initial_session_cookies=browser_cookies,
        # Browser authentication owns email/MFA verification.  The HTTP phase
        # is intentionally limited to the phone challenge and token exchange.
        cookie_login_only=True,
        sms_provider=provider,
        proxy=str(payload.get("proxy_url") or ""),
        impersonate=str(payload.get("impersonate") or "firefox144"),
        email_otp_timeout=180,
        sms_otp_timeout=60,
        # One lease uses the global ordered country fallback route.
        sms_max_attempts=1,
        sms_max_otp_retries=1,
        log_fn=log_fn,
        on_event=_emit,
        email_code_fetcher=_email_code_fetcher,
    )
    if not isinstance(result, dict):
        raise RuntimeError("Codex OAuth 返回格式无效")
    activation = provider.last_activation
    result["sms_country"] = (
        str(activation.raw.get("country") or "") if activation is not None else ""
    )
    result["sms_max_price"] = (
        float(activation.raw.get("max_price") or 0) if activation is not None else 0
    )
    result["ok"] = bool(result.get("ok"))
    return result


def main() -> int:
    _configure_utf8()
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise ValueError("worker payload must be an object")
        result = run(payload)
        print(RESULT_PREFIX + json.dumps(result, ensure_ascii=False), flush=True)
        return 0 if result.get("ok") else 1
    except Exception as error:
        print(
            RESULT_PREFIX
            + json.dumps({"ok": False, "error": _safe_text(error)}, ensure_ascii=False),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
