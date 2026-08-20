import asyncio
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from hidemyemail_generator.browser_tasks import load_account_record
from hidemyemail_generator.inbox import connect_db
from hidemyemail_generator.plus_codex import (
    PlusCodexModel,
    PlusCodexPresenter,
    PlusCodexView,
    ProtocolPlusCodexRunner,
    direct_plus_phone_job_id,
)
from hidemyemail_generator.webapp import create_app


def save_plus_account(database: Path, email: str, job_id: str) -> None:
    confirmation = {
        "job_id": job_id,
        "email": email,
        "status": "plus",
        "payment_succeeded": True,
        "plus_confirmed": True,
        "account_type": "plus",
        "plan": "plus",
    }
    record = {
        "email": email,
        "password": "Confirmed!Password123",
        "password_confirmed": True,
        "registration_proxy_url": "http://proxy.test:8080",
        "account_type": "plus",
        "account_type_source": "payment_at_refresh",
        "session": {"sessionToken": "saved-chatgpt-session-token"},
        "storage_state_json": json.dumps(
            {
                "cookies": [
                    {
                        "name": "__Secure-next-auth.session-token",
                        "value": "saved-chatgpt-session-token",
                        "domain": ".chatgpt.com",
                        "path": "/",
                        "expires": -1,
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "Lax",
                    },
                    {
                        "name": "oai-did",
                        "value": "saved-device-id",
                        "domain": ".chatgpt.com",
                        "path": "/",
                        "expires": -1,
                        "secure": True,
                        "sameSite": "Lax",
                    },
                ]
            }
        ),
        "payment_confirmation": confirmation,
        "payment_confirmations": {job_id: confirmation},
    }
    connection = connect_db(str(database))
    try:
        connection.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?)",
            (f"gpt_account:{email}", json.dumps(record)),
        )
        connection.commit()
    finally:
        connection.close()


def payment_job(email: str, job_id: str) -> dict:
    return {
        "id": job_id,
        "status": "completed",
        "source_account_email": email,
        "sms_provider": "hero-sms",
        "result": {"status": "success", "settlement_status": "confirmed"},
    }


def confirmation(email: str, job_id: str) -> dict:
    return {
        "job_id": job_id,
        "email": email,
        "status": "plus",
        "payment_succeeded": True,
        "plus_confirmed": True,
        "account_type": "plus",
        "detail": "新 AT 已确认 Plus",
    }


class SuccessfulRunner:
    def __init__(self) -> None:
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def __call__(self, payload, on_event):
        self.calls.append(payload)
        self.started.set()
        on_event({"event": "phone_acquired"})
        on_event(
            {
                "event": "sms_route",
                "stage": "sms_route",
                "level": "warning",
                "message": "SMSBower 智利线路无库存；仅因此回退美国",
            }
        )
        await self.release.wait()
        on_event({"event": "phone_bound"})
        return {
            "ok": True,
            "access_token": "codex-access-token",
            "refresh_token": "codex-refresh-token",
            "id_token": "codex-id-token",
            "account_id": "acct-plus",
            "email": payload["email"],
            "expires_in": 3600,
            "phone_bound": True,
            "phone": "+15555550123",
            "activation_id": "activation-secret-id",
            "sms_provider": payload["sms_provider"],
            "sms_country": "CL",
            "sms_max_price": 0.054,
            "phone_attempts": 1,
        }

    async def close(self):
        self.closed = True


async def wait_for_status(model, email, job_id, expected):
    for _ in range(100):
        state = await asyncio.to_thread(model.current, email, job_id)
        if state and state["status"] == expected:
            return state
        await asyncio.sleep(0.01)
    raise AssertionError(f"Plus Codex state did not reach {expected}")


def test_presenter_runs_one_background_binding_for_concurrent_polls(tmp_path):
    asyncio.run(_presenter_runs_one_background_binding(tmp_path))


async def _presenter_runs_one_background_binding(tmp_path):
    database = tmp_path / "accounts.db"
    email = "paid@icloud.com"
    job_id = "payment-job-123456"
    save_plus_account(database, email, job_id)
    model = PlusCodexModel(database)
    runner = SuccessfulRunner()
    synced = []

    async def sync_account(value):
        synced.append(value)

    presenter = PlusCodexPresenter(model, runner=runner, on_account_saved=sync_account)
    job = payment_job(email, job_id)
    payment_confirmation = confirmation(email, job_id)

    first, second = await asyncio.gather(
        presenter.ensure(
            job=job,
            confirmation=payment_confirmation,
            base_url="http://127.0.0.1:8765",
            sms_provider="hero-sms",
        ),
        presenter.ensure(
            job=job,
            confirmation=payment_confirmation,
            base_url="http://127.0.0.1:8765",
            sms_provider="hero-sms",
        ),
    )
    await runner.started.wait()

    assert first["status"] == "running"
    assert second["status"] == "running"
    assert len(runner.calls) == 1
    payload = runner.calls[0]
    assert payload["sms_max_price"] == 0.064
    assert payload["sms_max_attempts"] == 1
    assert payload["sms_provider"] == "smsbower"
    assert payload["proxy_url"] == "http://proxy.test:8080"
    assert payload["cookie_login_only"] is True
    assert "initial_storage_state" not in payload
    assert payload["initial_session_token"] == ""
    assert payload["initial_session_cookies"] == []
    assert payload["password"] == "Confirmed!Password123"
    assert payload["code_url"].startswith(
        "http://127.0.0.1:8765/api/protocol-registration/code/"
    )
    assert "api_key" not in json.dumps(payload).lower()

    runner.release.set()
    state = await wait_for_status(model, email, job_id, "completed")

    assert state["export_ready"] is True
    assert state["sms_verified"] is True
    assert state["phone_masked"] == "+***0123"
    assert synced == [email]

    record = load_account_record(database, email)
    assert record["account_type"] == "plus"
    assert record["payment_confirmation"]["payment_succeeded"] is True
    assert record["codex_oauth"]["refresh_token"] == "codex-refresh-token"
    assert record["codex_oauth"]["id_token"] == "codex-id-token"
    assert record["plus_sms"]["max_price"] == 0.054
    assert record["plus_sms"]["service_code"] == "dr"
    assert record["plus_sms"]["phone_masked"] == "+***0123"
    assert "activation-secret-id" not in json.dumps(record["plus_sms"])
    assert any(
        item["stage"] == "sms_route"
        and item["level"] == "warning"
        and "回退美国" in item["message"]
        for item in state["logs"]
    )

    replay = await presenter.ensure(
        job=job,
        confirmation=payment_confirmation,
        base_url="http://127.0.0.1:8765",
        sms_provider="hero-sms",
    )
    assert replay["status"] == "completed"
    assert len(runner.calls) == 1
    await presenter.close()
    assert runner.closed is True


def test_failure_keeps_paid_plus_but_blocks_export(tmp_path):
    asyncio.run(_failure_keeps_paid_plus_but_blocks_export(tmp_path))


async def _failure_keeps_paid_plus_but_blocks_export(tmp_path):
    database = tmp_path / "accounts.db"
    email = "failed@gmail.com"
    job_id = "payment-job-failed"
    save_plus_account(database, email, job_id)

    async def failed_runner(_payload, _on_event):
        raise RuntimeError("provider rejected otp=123456 refresh_token=secret")

    model = PlusCodexModel(database)
    presenter = PlusCodexPresenter(model, runner=failed_runner)
    await presenter.ensure(
        job=payment_job(email, job_id),
        confirmation=confirmation(email, job_id),
        base_url="http://127.0.0.1:8765",
        sms_provider="smsbower",
    )
    state = await wait_for_status(model, email, job_id, "failed")

    assert state["export_ready"] is False
    assert state["sms_verified"] is False
    assert "123456" not in state["detail"]
    assert "secret" not in state["detail"]
    record = load_account_record(database, email)
    assert record["account_type"] == "plus"
    assert record["payment_confirmation"]["payment_succeeded"] is True
    assert "codex_oauth" not in record
    await presenter.close()


def test_failed_binding_can_be_retried_by_presenter(tmp_path):
    asyncio.run(_failed_binding_can_be_retried_by_presenter(tmp_path))


async def _failed_binding_can_be_retried_by_presenter(tmp_path):
    database = tmp_path / "accounts.db"
    email = "retry-failed@icloud.com"
    job_id = "payment-job-retry-failed"
    save_plus_account(database, email, job_id)
    calls = []

    async def retry_runner(payload, _on_event):
        calls.append(payload)
        if len(calls) == 1:
            raise RuntimeError("temporary phone provider failure")
        return {
            "access_token": "at-retry",
            "refresh_token": "rt-retry",
            "id_token": "idt-retry",
            "account_id": "acct-retry",
            "phone_bound": True,
            "phone": "+15555550124",
            "activation_id": "activation-retry",
            "sms_provider": payload["sms_provider"],
        }

    model = PlusCodexModel(database)
    presenter = PlusCodexPresenter(model, runner=retry_runner)
    job = payment_job(email, job_id)
    payment_confirmation = confirmation(email, job_id)

    await presenter.ensure(
        job=job,
        confirmation=payment_confirmation,
        base_url="http://127.0.0.1:8765",
        sms_provider="smsbower",
    )
    await wait_for_status(model, email, job_id, "failed")
    replayed = await presenter.ensure(
        job=job,
        confirmation=payment_confirmation,
        base_url="http://127.0.0.1:8765",
        sms_provider="smsbower",
    )
    assert replayed["status"] == "failed"
    assert len(calls) == 1
    retried = await presenter.ensure(
        job=job,
        confirmation=payment_confirmation,
        base_url="http://127.0.0.1:8765",
        sms_provider="smsbower",
        retry_failed=True,
    )
    completed = await wait_for_status(model, email, job_id, "completed")

    assert retried["status"] in {"running", "completed"}
    assert completed["attempt"] == 2
    assert len(calls) == 2
    await presenter.close()


def test_existing_plus_context_does_not_require_payment_history(tmp_path):
    database = tmp_path / "accounts.db"
    email = "existing-plus-no-payment@icloud.com"
    old_job_id = "payment-job-remove"
    save_plus_account(database, email, old_job_id)
    connection = connect_db(str(database))
    try:
        record = load_account_record(database, email)
        record.pop("payment_confirmation", None)
        record.pop("payment_confirmations", None)
        record.pop("password", None)
        record.pop("password_confirmed", None)
        connection.execute(
            "UPDATE settings SET value = ? WHERE key = ?",
            (json.dumps(record), f"gpt_account:{email}"),
        )
        connection.commit()
    finally:
        connection.close()

    context = PlusCodexModel(database).context(
        email,
        direct_plus_phone_job_id(email),
    )

    assert context["email"] == email
    assert context["cookie_login_only"] is True
    assert "initial_storage_state" not in context
    assert context["initial_session_token"] == ""
    assert context["initial_session_cookies"] == []
    assert context["password"] == ""


def test_existing_plus_context_does_not_require_saved_chatgpt_cookie(tmp_path):
    database = tmp_path / "accounts.db"
    email = "existing-plus-no-cookie@icloud.com"
    job_id = "payment-job-no-cookie"
    save_plus_account(database, email, job_id)
    record = load_account_record(database, email)
    record.pop("session", None)
    record.pop("storage_state_json", None)
    connection = connect_db(str(database))
    try:
        connection.execute(
            "UPDATE settings SET value = ? WHERE key = ?",
            (json.dumps(record), f"gpt_account:{email}"),
        )
        connection.commit()
    finally:
        connection.close()

    context = PlusCodexModel(database).context(email, job_id)

    assert context["initial_session_token"] == ""
    assert context["initial_session_cookies"] == []
    assert "initial_storage_state" not in context


def test_worker_uses_browser_oauth_cookies_for_protocol_phone_binding(
    monkeypatch, tmp_path
):
    from hidemyemail_generator import browser_tasks, plus_codex_worker
    from hidemyemail_generator import plus_sms

    captured = {}
    order = []

    def run_protocol(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    protocol_module = SimpleNamespace(run_codex_oauth_protocol=run_protocol)
    monkeypatch.setitem(sys.modules, "gpt_trial_protocol.codex_oauth", protocol_module)
    browser_cookies = [
        {
            "name": "oai-client-auth-session",
            "value": "browser-auth-session",
            "domain": "auth.openai.com",
            "path": "/",
        }
    ]
    monkeypatch.setattr(
        plus_codex_worker,
        "_browser_oauth_session",
        lambda _payload: {"cookies": browser_cookies, "oauth_record": {}},
    )

    provider = SimpleNamespace(last_activation=None)

    def create_provider(name, **_kwargs):
        order.append("provider_created")
        captured["selected_sms_provider"] = name
        return provider

    monkeypatch.setattr(
        plus_sms,
        "PlusSmsProviderFactory",
        lambda _path: SimpleNamespace(create=create_provider),
    )
    original_sync = browser_tasks.sync_account_browser_cookies

    def sync_cookies(db_file, email, cookies, **kwargs):
        order.append("cookies_synced")
        original_sync(db_file, email, cookies, **kwargs)

    monkeypatch.setattr(browser_tasks, "sync_account_browser_cookies", sync_cookies)

    result = plus_codex_worker.run(
        {
            "source_root": str(Path(__file__).parents[1] / "src"),
            "gptfree_root": str(
                Path(__file__).parents[1]
                / "src"
                / "hidemyemail_generator"
                / "vendor"
                / "gptfree_register"
            ),
            "db_file": str(tmp_path / "accounts.db"),
            "email": "cookie-only@example.com",
            "initial_session_token": "saved-session-token",
            "initial_session_cookies": [
                {
                    "name": "__Secure-next-auth.session-token",
                    "value": "saved-session-token",
                    "domain": ".chatgpt.com",
                    "path": "/",
                }
            ],
            "code_url": "http://127.0.0.1:8765/api/protocol-registration/code/signed",
            "sms_provider": "smsbower",
        }
    )

    assert result["ok"] is True
    assert captured["selected_sms_provider"] == "smsbower"
    assert captured["cookie_login_only"] is True
    assert captured["initial_session_token"] == ""
    assert len(captured["initial_session_cookies"]) == 1
    assert captured["initial_session_cookies"] == browser_cookies
    assert captured["password"] == ""
    assert captured["outlook_refresh_token"].endswith("/code/signed")
    assert captured["email_code_fetcher"] is plus_codex_worker._email_code_fetcher
    assert order == ["cookies_synced", "provider_created"]
    saved = load_account_record(tmp_path / "accounts.db", "cookie-only@example.com")
    assert saved["cookies"] == browser_cookies
    assert json.loads(saved["storage_state_json"])["cookies"] == browser_cookies
    assert saved["session_acquisition_method"] == "roxy_email_login"


def test_browser_oauth_stops_before_phone_purchase_and_exports_auth_cookies(
    monkeypatch,
):
    from hidemyemail_generator import plus_codex_browser

    auth_cookies = [
        {
            "name": "oai-client-auth-session",
            "value": "browser-auth-session",
            "domain": "auth.openai.com",
            "path": "/",
        }
    ]

    class Page:
        def __init__(self):
            self.url = ""

        def goto(self, url, **_kwargs):
            self.url = str(url)
            if "/oauth/authorize" in self.url:
                self.url = "https://auth.openai.com/add-phone"
            return None

        def bring_to_front(self):
            return None

        def is_closed(self):
            return False

    class Context:
        def __init__(self):
            self.page = Page()
            self.pages = []
            self.closed = False

        def new_page(self):
            return self.page

        def cookies(self):
            return auth_cookies

        def route(self, *_args):
            return None

        def close(self):
            self.closed = True

    class Browser:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class PlaywrightManager:
        def __enter__(self):
            return SimpleNamespace()

        def __exit__(self, *_args):
            return False

    context = Context()
    browser = Browser()
    playwright_sync = SimpleNamespace(sync_playwright=lambda: PlaywrightManager())
    monkeypatch.setitem(sys.modules, "playwright.sync_api", playwright_sync)
    events = []
    flow = plus_codex_browser.CodexOAuthBrowserFlow(
        email="cookie-only@example.com",
        password="",
        totp_secret="",
        code_url="http://127.0.0.1:8765/code",
        proxy_url="",
        roxy={
            "api_url": "http://127.0.0.1:50000",
            "workspace_id": "1",
            "profile_id": "roxy-profile",
        },
        emit=lambda name, payload: events.append((name, payload)),
        email_code_fetcher=lambda *_args: None,
    )
    monkeypatch.setattr(flow, "_new_context", lambda _playwright: (browser, context))

    result = flow.run()

    assert result["oauth_record"] == {}
    assert result["cookies"] == auth_cookies
    assert result["phone_challenge"] is True
    assert context.closed is False
    assert browser.closed is True
    assert [name for name, _payload in events] == [
        "roxy_login_started",
        "oauth_browser_started",
        "email_login_succeeded",
    ]


def test_roxy_totp_page_without_saved_secret_waits_for_manual_input(monkeypatch):
    from hidemyemail_generator import plus_codex_browser

    class Body:
        @staticmethod
        def inner_text(**_kwargs):
            return "Enter the code from your authenticator app"

    page = SimpleNamespace(
        url="https://auth.openai.com/mfa",
        locator=lambda selector: Body() if selector == "body" else None,
    )
    otp_input = object()
    visible_results = iter([None, None, otp_input])
    monkeypatch.setattr(
        plus_codex_browser,
        "_first_visible",
        lambda _page, _selectors: next(visible_results),
    )
    events = []
    flow = plus_codex_browser.CodexOAuthBrowserFlow(
        email="two-factor@example.com",
        password="confirmed-password",
        totp_secret="",
        code_url="http://127.0.0.1:8765/code",
        proxy_url="",
        roxy={},
        emit=lambda name, payload: events.append((name, payload)),
        email_code_fetcher=lambda *_args: pytest.fail(
            "TOTP 页面不能错误读取邮箱验证码"
        ),
    )

    assert flow._handle_identity_page(page) is False
    assert events == [
        (
            "browser_waiting",
            {
                "message": "OAuth 要求 2FA 动态码，但当前账号没有保存密钥；请在 Roxy 窗口手动完成",
                "stage": "roxy_login",
                "level": "warning",
            },
        )
    ]


def test_local_browser_oauth_url_forces_clean_email_login():
    from hidemyemail_generator.plus_codex_browser import build_codex_oauth_url

    oauth_url, state = build_codex_oauth_url("cookie-only@example.com")

    assert "auth.openai.com/oauth/authorize?" in oauth_url
    assert "login_hint=cookie-only%40example.com" in oauth_url
    assert "prompt=login" in oauth_url
    assert state


def test_protocol_runner_maps_selected_roxy_profile_into_worker_payload(tmp_path):
    class RoxyStoreStub:
        def __init__(self):
            self.calls = []

        def runtime_config(self, profile_count):
            self.calls.append(profile_count)
            return {
                "apiUrl": "http://127.0.0.1:50000",
                "workspaceId": "321",
                "profileId": "fallback-profile",
                "profileIds": ["selected-profile"],
            }

    store = RoxyStoreStub()
    runner = ProtocolPlusCodexRunner(
        db_file=tmp_path / "accounts.db",
        roxy_registration_store=store,
    )

    assert runner._roxy_payload() == {
        "api_url": "http://127.0.0.1:50000",
        "workspace_id": "321",
        "profile_id": "selected-profile",
    }
    assert store.calls == [1]


def test_local_browser_oauth_reports_rate_limit_error_without_waiting():
    import base64
    from urllib.parse import quote

    from hidemyemail_generator.plus_codex_browser import _oauth_route_error

    payload = base64.b64encode(
        json.dumps({"errorCode": "rate_limit_exceeded"}).encode("utf-8")
    ).decode("ascii")

    assert "频率受限" in _oauth_route_error(
        f"https://auth.openai.com/error?payload={quote(payload)}"
    )


def test_worker_browser_oauth_delegates_to_current_project(monkeypatch):
    from hidemyemail_generator import plus_codex_browser, plus_codex_worker

    expected = {
        "cookies": [
            {
                "name": "oai-client-auth-session",
                "value": "browser-auth-session",
                "domain": "auth.openai.com",
                "path": "/",
            }
        ],
        "oauth_record": {},
        "phone_challenge": True,
    }
    captured = {}

    def run_local(payload, *, emit, email_code_fetcher):
        captured.update(payload)
        assert emit is plus_codex_worker._emit
        assert email_code_fetcher is plus_codex_worker._email_code_fetcher
        return expected

    monkeypatch.setattr(plus_codex_browser, "run_browser_oauth_session", run_local)

    result = plus_codex_worker._browser_oauth_session(
        {
            "email": "cookie-only@example.com",
            "initial_session_token": "",
            "initial_session_cookies": [],
        }
    )

    assert result == expected
    assert captured["email"] == "cookie-only@example.com"
    assert "initial_storage_state" not in captured
    assert captured["initial_session_token"] == ""
    assert captured["initial_session_cookies"] == []
    assert "browser_project_dir" not in captured


def test_cookie_only_oauth_never_falls_back_to_email_verification(
    monkeypatch,
):
    core_root = (
        Path(__file__).parents[1]
        / "src"
        / "hidemyemail_generator"
        / "vendor"
        / "gptfree_register"
        / "core"
    )
    monkeypatch.syspath_prepend(str(core_root))
    module = importlib.import_module("gpt_trial_protocol.codex_oauth")

    class Response:
        status_code = 200
        url = "https://auth.openai.com/email-verification"
        headers = {}
        text = ""

        @staticmethod
        def json():
            return {}

    class Session:
        def __init__(self):
            self.cookies = SimpleNamespace(get=lambda *_args: "")
            self.gets = []
            self.posts = []

        async def get(self, url, **_kwargs):
            self.gets.append(url)
            return Response()

        async def post(self, *args, **kwargs):
            self.posts.append((args, kwargs))
            raise AssertionError("Cookie-only flow must not send an OTP request")

    session = Session()

    class Auth:
        device_id = "device-id"

        async def share_session_with_sentinel(self):
            return None

        async def _get_session(self):
            return session

        async def close(self):
            return None

    flow = module.CodexOAuthProtocolFlow(
        email="cookie-only@example.com",
        password="",
        outlook_refresh_token="",
        initial_session_token="saved-session-token",
        initial_session_cookies=[
            {
                "name": "__Secure-next-auth.session-token",
                "value": "saved-session-token",
            }
        ],
        cookie_login_only=True,
        auth_factory=Auth,
    )

    with pytest.raises(module.CodexOAuthProtocolError, match="Cookie"):
        asyncio.run(flow.run())

    assert session.posts == []
    assert len(session.gets) == 1
    assert "prompt=login" not in session.gets[0]


def test_model_rejects_incomplete_oauth_or_missing_phone_receipt(tmp_path):
    database = tmp_path / "accounts.db"
    email = "paid@zkgmail.com"
    job_id = "payment-job-invalid"
    save_plus_account(database, email, job_id)
    model = PlusCodexModel(database)
    model.claim(email=email, job_id=job_id, provider="smsbower")

    with pytest.raises(RuntimeError, match="完整"):
        model.complete(
            email=email,
            job_id=job_id,
            result={"access_token": "at", "phone_bound": True},
        )
    with pytest.raises(RuntimeError, match="未完成"):
        model.complete(
            email=email,
            job_id=job_id,
            result={
                "access_token": "at",
                "refresh_token": "rt",
                "id_token": "idt",
                "account_id": "acct",
                "phone_bound": False,
            },
        )


def test_model_allows_explicit_retry_after_failed_sms_claim(tmp_path):
    database = tmp_path / "accounts.db"
    email = "single-claim@icloud.com"
    first_job_id = "payment-job-first"
    save_plus_account(database, email, first_job_id)
    model = PlusCodexModel(database)

    first = model.claim(email=email, job_id=first_job_id, provider="smsbower")

    assert first["attempt"] == 1
    model.fail(
        email=email,
        job_id=first_job_id,
        error="temporary provider failure",
        provider="smsbower",
    )
    retried = model.claim(email=email, job_id=first_job_id, provider="smsbower")
    assert retried["status"] == "running"
    assert retried["attempt"] == 2

    second_job_id = "payment-job-second"
    record = load_account_record(database, email)
    second_confirmation = confirmation(email, second_job_id)
    record["payment_confirmation"] = second_confirmation
    record["payment_confirmations"][second_job_id] = second_confirmation
    connection = connect_db(str(database))
    try:
        connection.execute(
            "UPDATE settings SET value = ? WHERE key = ?",
            (json.dumps(record), f"gpt_account:{email}"),
        )
        connection.commit()
    finally:
        connection.close()

    model.fail(
        email=email,
        job_id=first_job_id,
        error="second temporary provider failure",
        provider="smsbower",
    )
    second = model.claim(email=email, job_id=second_job_id, provider="hero-sms")
    assert second["attempt"] == 3


def test_view_keeps_payment_success_separate_from_delivery(tmp_path):
    _ = tmp_path
    original = confirmation("paid@icloud.com", "payment-job-view")

    running = PlusCodexView.merge_confirmation(
        original,
        {"status": "running", "detail": "正在等待验证码"},
    )
    failed = PlusCodexView.merge_confirmation(
        original,
        {"status": "failed", "detail": "接码平台无号"},
    )
    completed = PlusCodexView.merge_confirmation(
        original,
        {"status": "completed", "export_ready": True, "sms_verified": True},
    )

    assert running["status"] == "plus_sms"
    assert running["payment_succeeded"] is True
    assert failed["status"] == "plus_sms_failed"
    assert failed["payment_succeeded"] is True
    assert completed["status"] == "plus"
    assert completed["export_ready"] is True


def test_plus_temporary_email_code_token_controls_http_access(tmp_path):
    asyncio.run(_plus_temporary_email_code_token_controls_http_access(tmp_path))


async def _plus_temporary_email_code_token_controls_http_access(tmp_path):
    email = "plus-code@zkgmail.com"

    class ZkgmailClientStub:
        def __init__(self):
            self.polled = []

        async def poll_next_code(self, polled_email, *, since=""):
            self.polled.append((polled_email, since))
            return "864209"

    app = create_app(
        base_dir=tmp_path,
        web_password="required-web-password",
        target_python=str(tmp_path / "external-project-python.exe"),
    )
    assert (
        app["plus_codex_presenter"].runner.python_executable
        == Path(sys.executable).resolve()
    )
    assert (
        app["plus_codex_presenter"].runner.roxy_registration_store
        is app["roxy_registration_store"]
    )
    zkgmail = ZkgmailClientStub()
    app["zkgmail_client"] = zkgmail
    token, _ = app["plus_codex_presenter"]._issue_code_token(email)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        valid = await client.get(f"/api/protocol-registration/code/{token}")
        invalid = await client.get(
            "/api/protocol-registration/code/invalid-plus-code-token"
        )

        assert valid.status == 200
        assert await valid.text() == "864209"
        assert len(zkgmail.polled) == 1
        assert zkgmail.polled[0][0] == email
        assert zkgmail.polled[0][1]
        assert invalid.status == 401
        assert (await invalid.json())["error"] == "请先登录"
    finally:
        await client.close()
