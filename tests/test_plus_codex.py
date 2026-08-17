import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from aiohttp.test_utils import TestClient, TestServer

from hidemyemail_generator.browser_tasks import load_account_record
from hidemyemail_generator.inbox import connect_db
from hidemyemail_generator.plus_codex import (
    PlusCodexModel,
    PlusCodexPresenter,
    PlusCodexView,
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
            "sms_country": "US-VIRTUAL",
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
    assert payload["sms_max_price"] == 0.1
    assert payload["sms_max_attempts"] == 1
    assert payload["sms_provider"] == "hero-sms"
    assert payload["proxy_url"] == "http://proxy.test:8080"
    assert "api_key" not in json.dumps(payload).lower()
    token = urlsplit(payload["code_url"]).path.rsplit("/", 1)[-1]
    assert presenter.token_record(token)["email"] == email

    runner.release.set()
    state = await wait_for_status(model, email, job_id, "completed")

    assert state["export_ready"] is True
    assert state["sms_verified"] is True
    assert state["phone_masked"] == "+***0123"
    assert synced == [email]
    assert presenter.token_record(token) is None

    record = load_account_record(database, email)
    assert record["account_type"] == "plus"
    assert record["payment_confirmation"]["payment_succeeded"] is True
    assert record["codex_oauth"]["refresh_token"] == "codex-refresh-token"
    assert record["codex_oauth"]["id_token"] == "codex-id-token"
    assert record["plus_sms"]["max_price"] == 0.1
    assert record["plus_sms"]["service_code"] == "dr"
    assert record["plus_sms"]["phone_masked"] == "+***0123"
    assert "activation-secret-id" not in json.dumps(record["plus_sms"])

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


def test_model_enforces_one_persistent_sms_claim_per_plus_account(tmp_path):
    database = tmp_path / "accounts.db"
    email = "single-claim@icloud.com"
    first_job_id = "payment-job-first"
    save_plus_account(database, email, first_job_id)
    model = PlusCodexModel(database)

    first = model.claim(email=email, job_id=first_job_id, provider="smsbower")

    assert first["attempt"] == 1
    with pytest.raises(RuntimeError, match="唯一一次"):
        model.claim(email=email, job_id=first_job_id, provider="smsbower")

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

    with pytest.raises(RuntimeError, match="唯一一次"):
        model.claim(email=email, job_id=second_job_id, provider="hero-sms")


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

        async def poll_next_code(self, polled_email):
            self.polled.append(polled_email)
            return "864209"

    app = create_app(base_dir=tmp_path, web_password="required-web-password")
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
        assert zkgmail.polled == [email]
        assert invalid.status == 401
        assert (await invalid.json())["error"] == "请先登录"
    finally:
        await client.close()
