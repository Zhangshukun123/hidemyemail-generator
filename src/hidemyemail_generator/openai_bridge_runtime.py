"""Persistence, logging, runtime preparation, and low-level input helpers."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path

try:
    from .openai_browser_selectors import EVENT_PREFIX
    from .openai_mfa import generate_totp
except ImportError:
    from openai_browser_selectors import EVENT_PREFIX
    from openai_mfa import generate_totp


def load_saved_storage_state(db_file: str, email: str) -> dict:
    path_text = str(db_file or "").strip()
    target_email = str(email or "").strip().lower()
    if not path_text or not target_email:
        return {}
    try:
        connection = sqlite3.connect(path_text)
        try:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = ?",
                (f"gpt_account:{target_email}",),
            ).fetchone()
        finally:
            connection.close()
        if not row:
            return {}
        record = json.loads(str(row[0] or "{}"))
        if not isinstance(record, dict):
            return {}
        raw_state = record.get("storage_state_json")
        if isinstance(raw_state, str) and raw_state.strip():
            raw_state = json.loads(raw_state)
        if isinstance(raw_state, dict):
            state = dict(raw_state)
            if isinstance(state.get("cookies"), list) and state["cookies"]:
                state.setdefault("origins", [])
                return state

        for key in ("cookies", "cookies_json"):
            cookies = record.get(key)
            if isinstance(cookies, str) and cookies.strip():
                cookies = json.loads(cookies)
            if isinstance(cookies, list) and cookies:
                return {
                    "cookies": [
                        dict(item) for item in cookies if isinstance(item, dict)
                    ],
                    "origins": [],
                }
        return {}
    except (
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        sqlite3.Error,
    ):
        return {}


def configure_worker_login_totp(
    worker,
    two_factor: dict | None,
    *,
    generate_code=generate_totp,
) -> bool:
    state = two_factor if isinstance(two_factor, dict) else {}
    secret = str(state.get("secret") or "").strip()
    if not secret:
        return False

    def current_code() -> str:
        return generate_code(secret)

    worker.login_totp_provider = current_code
    return True


def configure_registration_profile_capture(app_backend, worker) -> bool:
    original = getattr(app_backend, "random_profile", None)
    if not callable(original):
        return False
    if getattr(original, "_hme_profile_capture", False):
        return True

    def captured_random_profile():
        result = original()
        if isinstance(result, (tuple, list)) and result:
            worker.registration_profile_name = str(result[0] or "").strip()
        return result

    captured_random_profile._hme_profile_capture = True
    app_backend.random_profile = captured_random_profile
    return True


def reusable_enabled_two_factor(two_factor: dict | None) -> dict:
    state = dict(two_factor) if isinstance(two_factor, dict) else {}
    if state.get("enabled") and str(state.get("secret") or "").strip():
        return state
    return {}


def _mfa_token_was_invalidated(error: Exception) -> bool:
    message = str(error or "").casefold()
    return "http 401" in message and any(
        marker in message
        for marker in (
            "token",
            "authentication",
            "invalidated",
            "signing in again",
            "re-authenticate",
            "recent_auth_required",
            "recent auth",
        )
    )


def _fontconfig_generator_with_home(generator, runtime_home: Path):
    """Run Camoufox's hard-coded fontconfig writer against a writable home."""

    def redirected(fontconfig_path: str) -> str:
        previous_home = os.environ.get("HOME")
        os.environ["HOME"] = str(runtime_home)
        try:
            return generator(fontconfig_path)
        finally:
            if previous_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = previous_home

    return redirected


def _configure_camoufox_runtime_cache(runtime_home: Path) -> Path:
    """Point Firefox's XDG font cache at the same writable runtime tree."""

    runtime_cache = runtime_home / ".cache"
    (runtime_cache / "camoufox" / "fontconfig").mkdir(
        parents=True,
        exist_ok=True,
    )
    (runtime_cache / "fontconfig").mkdir(parents=True, exist_ok=True)
    os.environ["XDG_CACHE_HOME"] = str(runtime_cache)
    return runtime_cache


def prepare_writable_camoufox_fontconfig():
    """Redirect Camoufox and Firefox fontconfig writes to writable /tmp."""

    if os.name != "posix":
        return None

    from camoufox import utils as camoufox_utils

    generator = getattr(camoufox_utils, "_generate_fontconfig", None)
    if not callable(generator):
        return None

    runtime_home = tempfile.TemporaryDirectory(
        prefix="hidemyemail-camoufox-",
        dir="/tmp",
    )
    runtime_root = Path(runtime_home.name)
    _configure_camoufox_runtime_cache(runtime_root)
    camoufox_utils._generate_fontconfig = _fontconfig_generator_with_home(
        generator,
        runtime_root,
    )
    return runtime_home


class MfaHttpClient:
    def __init__(self) -> None:
        import requests

        self.session = requests.Session()
        self.session.trust_env = False

    def post(self, url: str, **kwargs):
        kwargs["timeout"] = 60
        return self.session.post(url, **kwargs)

    def close(self) -> None:
        self.session.close()


def emit(kind: str, **payload) -> None:
    print(
        EVENT_PREFIX + json.dumps({"type": kind, **payload}, ensure_ascii=False),
        flush=True,
    )


def safe_log_message(message: str) -> str:
    text = str(message or "")
    text = re.sub(
        r"(已生成密码\s*[:：])\s*\S+",
        r"\1 [已安全保存]",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"(?im)(authorization\s*:\s*bearer\s+)[^\s]+",
        r"\1[已隐藏]",
        text,
    )
    text = re.sub(
        r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}",
        "Bearer [已隐藏]",
        text,
    )
    text = re.sub(
        r"(?im)(cookie\s*:\s*)[^\r\n]+",
        r"\1[已隐藏]",
        text,
    )
    return text[:1500]


def _locator_value_matches(locator, expected: str) -> bool:
    input_value = getattr(locator, "input_value", None)
    if not callable(input_value):
        return False
    try:
        actual = input_value(timeout=2000)
    except TypeError:
        try:
            actual = input_value()
        except Exception:
            return False
    except Exception:
        return False
    return str(actual or "") == expected


def resilient_force_fill_locator(worker, locator, value: str) -> bool:
    """Fill a React-controlled input and confirm that the value was applied."""
    expected = str(value)

    try:
        locator.click(timeout=3000, force=True)
    except TypeError:
        try:
            locator.click(timeout=3000)
        except Exception:
            pass
    except Exception:
        pass

    try:
        locator.fill(expected, timeout=7000, force=True)
    except TypeError:
        try:
            locator.fill(expected, timeout=7000)
        except Exception:
            pass
    except Exception:
        pass
    if _locator_value_matches(locator, expected):
        return True

    try:
        locator.evaluate(
            """(el, value) => {
                el.focus();
                const proto = el instanceof HTMLTextAreaElement
                    ? HTMLTextAreaElement.prototype
                    : HTMLInputElement.prototype;
                const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
                if (descriptor && descriptor.set) descriptor.set.call(el, value);
                else el.value = value;
                const inputEvent = typeof InputEvent === "function"
                    ? new InputEvent("input", {
                        bubbles: true,
                        composed: true,
                        inputType: "insertText",
                        data: value,
                    })
                    : new Event("input", { bubbles: true, composed: true });
                el.dispatchEvent(inputEvent);
                el.dispatchEvent(new Event("change", { bubbles: true }));
            }""",
            expected,
        )
    except Exception:
        pass
    if _locator_value_matches(locator, expected):
        worker.log("[认证] 已使用兼容输入方式填写密码")
        return True

    try:
        locator.click(timeout=3000, force=True)
        locator.press("Control+A", timeout=2000)
        locator.press("Backspace", timeout=2000)
        locator.type(expected, delay=25, timeout=10000)
    except TypeError:
        try:
            locator.click(timeout=3000)
            locator.press("Control+A")
            locator.press("Backspace")
            locator.type(expected, delay=25)
        except Exception:
            return False
    except Exception:
        return False
    if _locator_value_matches(locator, expected):
        worker.log("[认证] 已使用键盘输入方式填写密码")
        return True
    return False
