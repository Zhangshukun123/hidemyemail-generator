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
            if error.code not in {404, 408, 425, 429, 503}:
                raise
        except (OSError, TimeoutError, URLError):
            pass
        time.sleep(1.2)
    return None


def run(payload: dict[str, Any]) -> dict[str, Any]:
    source_root = Path(str(payload.get("source_root") or "")).resolve()
    core_root = Path(str(payload.get("gptfree_root") or "")).resolve() / "core"
    for path in (str(source_root), str(core_root)):
        if path and path not in sys.path:
            sys.path.insert(0, path)

    from gpt_trial_protocol.codex_oauth import run_codex_oauth_protocol
    from hidemyemail_generator.plus_sms import PlusSmsProviderFactory

    provider = PlusSmsProviderFactory(Path(str(payload.get("db_file") or ""))).create(
        str(payload.get("sms_provider") or "")
    )

    def log_fn(message: str) -> None:
        _emit("protocol_log", {"message": _safe_text(message)})

    result = run_codex_oauth_protocol(
        email=str(payload.get("email") or ""),
        password=str(payload.get("password") or ""),
        # The protocol currently requires a non-empty relay credential even
        # when a custom fetcher is supplied.  The signed local URL satisfies
        # that contract and is consumed only by _email_code_fetcher.
        outlook_refresh_token=str(payload.get("code_url") or ""),
        outlook_client_id="",
        sms_provider=provider,
        proxy=str(payload.get("proxy_url") or ""),
        impersonate=str(payload.get("impersonate") or "firefox144"),
        email_otp_timeout=180,
        sms_otp_timeout=60,
        # One lease keeps the entire post-payment phone budget at $0.10.
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
