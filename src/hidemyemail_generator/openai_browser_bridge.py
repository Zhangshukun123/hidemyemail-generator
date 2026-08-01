from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
import types
from datetime import datetime, timezone
from pathlib import Path

try:
    from .openai_mfa import enable_totp_mfa
except ImportError:
    from openai_mfa import enable_totp_mfa


EVENT_PREFIX = "HME_BROWSER_EVENT:"


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


def iso_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class ICloudOtpReader:
    def __init__(self, account, log, _proxy_url: str = "") -> None:
        import requests

        self.email = str(account.email or "").strip().lower()
        self.log = log
        self.service_url = os.environ.get(
            "HME_BROWSER_SERVICE_URL", "http://127.0.0.1:8765"
        ).rstrip("/")
        self.token = os.environ.get("HME_BROWSER_WORKER_TOKEN", "")
        self.session = requests.Session()
        self.session.trust_env = False

    def connect(self) -> None:
        if not self.token:
            raise RuntimeError("iCloud 浏览器工作器令牌未配置")
        try:
            response = self.session.get(self.service_url + "/healthz", timeout=5)
            response.raise_for_status()
        except Exception as error:
            raise RuntimeError(f"无法连接 iCloud 邮箱服务：{error}") from error
        self.log("iCloud 收码通道已连接")

    def wait_for_code(self, min_timestamp: float) -> str:
        deadline = time.time() + 240
        since = iso_timestamp(min_timestamp)
        last_error = ""
        while time.time() < deadline:
            try:
                response = self.session.post(
                    self.service_url + "/api/gpt-code",
                    headers={"X-Local-Token": self.token},
                    json={"email": self.email, "since": since},
                    timeout=40,
                )
                if response.status_code == 404:
                    time.sleep(5)
                    continue
                payload = response.json()
                if response.ok and payload.get("ok"):
                    code = re.sub(r"[^A-Za-z0-9]", "", str(payload.get("code") or ""))
                    if 4 <= len(code) <= 10:
                        self.log("已从 iCloud 转发收件箱获取对应邮箱的新验证码")
                        return code
                last_error = str(payload.get("error") or f"HTTP {response.status_code}")
            except Exception as error:
                last_error = str(error)
            time.sleep(5)
        detail = f"：{last_error}" if last_error else ""
        raise TimeoutError(f"iCloud 在 240 秒内未收到该邮箱的新验证码{detail}")

    def close(self) -> None:
        self.session.close()


def ensure_tkinter_importable() -> None:
    try:
        import tkinter  # noqa: F401

        return
    except ImportError:
        pass

    class DummyWidget:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

    class DummyModule(types.ModuleType):
        def __getattr__(self, _name: str):
            return DummyWidget

    tkinter = DummyModule("tkinter")
    ttk = DummyModule("tkinter.ttk")
    font = DummyModule("tkinter.font")
    tkinter.ttk = ttk
    tkinter.font = font
    tkinter.filedialog = DummyModule("tkinter.filedialog")
    tkinter.messagebox = DummyModule("tkinter.messagebox")
    tkinter.simpledialog = DummyModule("tkinter.simpledialog")
    sys.modules["tkinter"] = tkinter
    sys.modules["tkinter.ttk"] = ttk
    sys.modules["tkinter.font"] = font
    sys.modules["tkinter.filedialog"] = tkinter.filedialog
    sys.modules["tkinter.messagebox"] = tkinter.messagebox
    sys.modules["tkinter.simpledialog"] = tkinter.simpledialog


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="iCloud OpenAI Camoufox bridge")
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--headless", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    source_dir = Path(args.source_dir).resolve()
    if not (source_dir / "app_backend.py").is_file():
        emit("error", error=f"目标项目缺少 app_backend.py：{source_dir}")
        return 2
    sys.path.insert(0, str(source_dir))

    password = os.environ.get("HME_OPENAI_PASSWORD", "")
    enable_2fa = os.environ.get("HME_ENABLE_OPENAI_2FA", "") == "1"
    try:
        pending_2fa = json.loads(os.environ.get("HME_OPENAI_2FA_STATE", "{}"))
    except (json.JSONDecodeError, TypeError, ValueError):
        pending_2fa = {}
    account = None
    try:
        ensure_tkinter_importable()
        # Keep the temporary directory alive for the complete browser run.
        _camoufox_runtime = prepare_writable_camoufox_fontconfig()
        if _camoufox_runtime is not None:
            emit("log", message="[运行环境] Camoufox fontconfig 已切换到可写临时目录")
        import app_backend
        from account_models import MailAccount

        app_backend.HotmailOtpReader = ICloudOtpReader
        app_backend.OpenAIRegisterPayLinkWorker._force_fill_locator = (
            resilient_force_fill_locator
        )
        account = MailAccount(
            email=args.email.strip().lower(),
            password=password,
            client_id="icloud",
            refresh_token="icloud",
            raw="",
        )
        proxy = app_backend.ProxyConfig(
            local_proxy="", dynamic_proxy="", chain_url=""
        )

        def log(message: str) -> None:
            emit("log", message=safe_log_message(message))

        worker = app_backend.OpenAIRegisterPayLinkWorker(
            account,
            "",
            bool(args.headless),
            proxy,
            proxy,
            log,
            browser_engine="camoufox",
        )
        result = worker.run()
        if enable_2fa:
            emit(
                "account_registered",
                result=result,
                password=str(account.password or ""),
            )
            emit("two_factor_start")
            mfa_client = MfaHttpClient()
            try:
                two_factor = enable_totp_mfa(
                    mfa_client,
                    access_token=str(result.get("access_token") or ""),
                    email=str(account.email or ""),
                    pending=pending_2fa,
                    on_enrolled=lambda state: emit(
                        "two_factor_enrolled", two_factor=state
                    ),
                )
            finally:
                mfa_client.close()
            result["two_factor"] = two_factor
            emit("two_factor_enabled")
        emit(
            "result",
            result=result,
            password=str(account.password or ""),
        )
        return 0
    except KeyboardInterrupt:
        emit(
            "error",
            error="浏览器任务已停止",
            password=str(getattr(account, "password", "") or ""),
        )
        return 130
    except Exception as error:
        emit(
            "error",
            error=safe_log_message(str(error)),
            password=str(getattr(account, "password", "") or ""),
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
