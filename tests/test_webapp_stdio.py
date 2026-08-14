import asyncio
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import unquote, urlsplit

from aiohttp.test_utils import TestClient, TestServer
from rich.console import Console

from hidemyemail_generator.account_verifier import mark_account_session_invalid
from hidemyemail_generator.browser_tasks import (
    _save_account_record,
    load_account_record,
)
from hidemyemail_generator.inbox import (
    InboxConfig,
    connect_db,
    insert_message,
    save_config,
)
from hidemyemail_generator.webapp import (
    OPENAI_CODE_INBOX_SYNC_LIMIT,
    WORKBENCH_OPENAI_CODE_PATH,
    _configured_inventory_service_token,
    _configured_workbench_import_token,
    _configure_utf8_stdio,
    _generation_failure_message,
    _latest_code_for_email,
    _load_local_env_file,
    _save_account_card_link,
    create_app,
)


class WebAppStdioTests(unittest.TestCase):
    def test_workbench_import_uses_dedicated_local_token(self):
        with mock.patch.dict(
            os.environ,
            {
                "HME_IMPORT_TOKEN": "unrelated-remote-token",
                "ACCOUNT_WORKBENCH_IMPORT_TOKEN": "local-workbench-token",
            },
            clear=False,
        ):
            self.assertEqual(
                _configured_workbench_import_token(), "local-workbench-token"
            )

    def test_workbench_import_does_not_fall_back_to_remote_token(self):
        with mock.patch.dict(
            os.environ,
            {"HME_IMPORT_TOKEN": "unrelated-remote-token"},
            clear=False,
        ):
            os.environ.pop("ACCOUNT_WORKBENCH_IMPORT_TOKEN", None)
            self.assertEqual(_configured_workbench_import_token(), "")

    def test_inventory_does_not_fall_back_to_local_workbench_token(self):
        with mock.patch.dict(
            os.environ,
            {"ACCOUNT_WORKBENCH_IMPORT_TOKEN": "unrelated-local-token"},
            clear=False,
        ):
            os.environ.pop("HIDEMYEMAIL_INVENTORY_TOKEN", None)
            self.assertEqual(_configured_inventory_service_token(), "")

    def test_inventory_uses_its_dedicated_remote_token(self):
        with mock.patch.dict(
            os.environ,
            {"HIDEMYEMAIL_INVENTORY_TOKEN": "inventory-token"},
            clear=False,
        ):
            self.assertEqual(
                _configured_inventory_service_token(), "inventory-token"
            )

    def test_loads_local_workbench_settings_without_overriding_environment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            env_file.write_text(
                "ACCOUNT_WORKBENCH_URL=http://127.0.0.1:3000\n"
                "ACCOUNT_WORKBENCH_IMPORT_TOKEN=local-token\n"
                "UNRELATED=value\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {"ACCOUNT_WORKBENCH_URL": "http://existing:3000"},
                clear=False,
            ):
                os.environ.pop("ACCOUNT_WORKBENCH_IMPORT_TOKEN", None)
                os.environ.pop("UNRELATED", None)
                _load_local_env_file(env_file)

                self.assertEqual(
                    os.environ["ACCOUNT_WORKBENCH_URL"], "http://existing:3000"
                )
                self.assertEqual(
                    os.environ["ACCOUNT_WORKBENCH_IMPORT_TOKEN"], "local-token"
                )
                self.assertNotIn("UNRELATED", os.environ)

    def test_generation_error_preserves_icloud_detail(self):
        message = _generation_failure_message(
            {
                "error": {
                    "code": "HME_RESERVE_FAILED",
                    "message": "Unable to reserve generated address",
                    "retry_after": 12,
                }
            }
        )

        self.assertIn("Unable to reserve generated address", message)
        self.assertIn("HME_RESERVE_FAILED", message)
        self.assertIn("12 秒后重试", message)

    def test_reconfigures_gbk_streams_before_rich_writes_unicode(self):
        stdout = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
        stderr = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
        original_stdout = sys.stdout
        original_stderr = sys.stderr
        try:
            sys.stdout = stdout
            sys.stderr = stderr
            _configure_utf8_stdio()

            self.assertEqual(stdout.encoding.lower(), "utf-8")
            self.assertEqual(stderr.encoding.lower(), "utf-8")
            Console(file=stdout, force_terminal=False).print(":star:")
            stdout.flush()
        finally:
            sys.stdout = original_stdout
            sys.stderr = original_stderr

    def test_default_openai_runtime_uses_current_sibling_checkout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir) / "hidemyemail-generator"
            app = create_app(base_dir=base_dir)

            self.assertNotIn("inbox_sync_interval", app)
            self.assertEqual(
                app["browser_manager"].target_project_dir,
                (base_dir.parent / "openai-register-paylink").resolve(),
            )

    def test_default_openai_runtime_falls_back_to_packaged_sibling(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir) / "hidemyemail-generator"
            packaged = (
                base_dir.parent
                / "openai-register-paylink-ui-dist-20260706-README-deploy"
            )
            packaged.mkdir()
            (packaged / "app_backend.py").write_text(
                "# packaged runtime\n", encoding="utf-8"
            )

            app = create_app(base_dir=base_dir)

            self.assertEqual(
                app["browser_manager"].target_project_dir,
                packaged.resolve(),
            )


class WorkbenchOpenAICodeEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        app = create_app(
            base_dir=Path(self.temp_dir.name),
            web_password="web-password",
            workbench_import_token="shared-workbench-token",
        )
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp_dir.cleanup()

    async def test_shared_token_bypasses_web_login_but_still_validates_email(self):
        response = await self.client.post(
            WORKBENCH_OPENAI_CODE_PATH,
            json={"email": "not-an-icloud-address"},
            headers={"X-HME-Import-Token": "shared-workbench-token"},
        )

        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"], "邮箱地址无效")

    async def test_missing_shared_token_cannot_use_workbench_endpoint(self):
        response = await self.client.post(
            WORKBENCH_OPENAI_CODE_PATH,
            json={"email": "one@icloud.com"},
        )

        self.assertEqual(response.status, 401)
        self.assertEqual((await response.json())["error"], "请先登录")


class LocalWorkerTokenAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app(
            base_dir=Path(self.temp_dir.name),
            web_password="web-password",
        )
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp_dir.cleanup()

    async def test_local_worker_token_bypasses_web_login_for_code_routes(self):
        headers = {"X-Local-Token": self.app["local_token"]}

        gpt_code = await self.client.post(
            "/api/gpt-code",
            json={"email": "not-an-email"},
            headers=headers,
        )
        registration_code = await self.client.post(
            "/api/registration/code/poll",
            json={"email": "worker@icloud.com", "requestId": "worker-test"},
            headers=headers,
        )

        self.assertEqual(gpt_code.status, 400)
        self.assertEqual((await gpt_code.json())["error"], "邮箱地址无效")
        self.assertEqual(registration_code.status, 409)
        self.assertNotEqual(
            (await registration_code.json())["error"],
            "请先登录",
        )

    async def test_missing_or_invalid_local_worker_token_still_requires_login(self):
        missing = await self.client.post(
            "/api/gpt-code",
            json={"email": "not-an-email"},
        )
        invalid = await self.client.post(
            "/api/gpt-code",
            json={"email": "not-an-email"},
            headers={"X-Local-Token": "invalid-worker-token"},
        )

        self.assertEqual(missing.status, 401)
        self.assertEqual((await missing.json())["error"], "请先登录")
        self.assertEqual(invalid.status, 401)
        self.assertEqual((await invalid.json())["error"], "请先登录")


class GptCredentialEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app(base_dir=Path(self.temp_dir.name))
        _save_account_record(
            self.app["db_file"],
            "saved@gmail.com",
            result={
                "access_token": "at-gmail-test",
                "session_json": '{"accessToken":"at-gmail-test"}',
            },
        )
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp_dir.cleanup()

    async def test_gmail_access_token_can_be_copied(self):
        response = await self.client.post(
            "/api/gpt-credential",
            json={"email": "saved@gmail.com", "kind": "access_token"},
            headers={"X-Local-Token": self.app["local_token"]},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(
            await response.json(),
            {"ok": True, "value": "at-gmail-test"},
        )

    async def test_malformed_email_is_still_rejected(self):
        response = await self.client.post(
            "/api/gpt-credential",
            json={"email": "not-an-email", "kind": "access_token"},
            headers={"X-Local-Token": self.app["local_token"]},
        )

        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"], "邮箱地址无效")


class CardLinkEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        target = root / "openai-runtime"
        target.mkdir()
        (target / "app_backend.py").write_text("# fake runtime\n", encoding="utf-8")
        self.app = create_app(
            base_dir=root,
            target_project_dir=str(target),
            target_python=sys.executable,
        )
        _save_account_record(
            self.app["db_file"],
            "card-link@icloud.com",
            result={
                "access_token": "at-card-link",
                "session_json": '{"accessToken":"at-card-link"}',
            },
        )
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp_dir.cleanup()

    async def test_generates_and_persists_card_link(self):
        generated = {
            "status": "success",
            "url": "https://chatgpt.com/checkout/openai_llc/cs_test_endpoint",
            "country": "JP",
            "currency": "JPY",
        }
        with (
            mock.patch(
                "hidemyemail_generator.webapp.access_token_is_expired",
                return_value=False,
            ),
            mock.patch(
                "hidemyemail_generator.webapp._run_card_link_bridge",
                new=mock.AsyncMock(return_value=generated),
            ) as bridge,
        ):
            response = await self.client.post(
                "/api/account/card-link",
                json={"email": "card-link@icloud.com", "country": "JP"},
                headers={"X-Local-Token": self.app["local_token"]},
            )

        payload = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["currency"], "JPY")
        self.assertEqual(
            load_account_record(
                self.app["db_file"], "card-link@icloud.com"
            )["card_link"]["url"],
            generated["url"],
        )
        self.assertEqual(bridge.await_args.kwargs["locale"], "ja-JP")

    async def test_one_click_paypal_uses_matching_country_cookie_proxy_and_sms(self):
        cookies = [{"name": "session", "value": "current-account-cookie", "domain": ".openai.com"}]
        _save_account_record(
            self.app["db_file"],
            "card-link@icloud.com",
            result={"cookies_json": json.dumps(cookies)},
        )
        _save_account_card_link(
            self.app["db_file"],
            "card-link@icloud.com",
            url="https://www.paypal.com/agreements/approve?ba_token=th_one_click",
            country="TH",
            currency="THB",
            method="de_oaics_paypal",
        )
        self.app["registration_proxy_store"].configure(
            enabled=True,
            mode="dynamic",
            country="TH",
            proxy_line="proxy.example:8000:shared-user:shared-pass",
        )
        self.app["smsbower_config_store"].configure(api_key="smsbower-test-key")
        created = mock.AsyncMock(return_value=(201, {"job": {"id": "pay-th-1"}}))
        with mock.patch.object(self.app["paypal_service"], "create_job", created):
            response = await self.client.post(
                "/api/account/paypal-payment",
                json={"email": "card-link@icloud.com"},
                headers={"X-Local-Token": self.app["local_token"]},
            )

        payload = await response.json()
        protocol = created.await_args.args[0]
        self.assertEqual(response.status, 201)
        self.assertEqual(payload["country"], "TH")
        self.assertEqual(payload["smsProvider"], "smsbower")
        self.assertEqual(protocol["source_account_email"], "card-link@icloud.com")
        self.assertEqual(protocol["account_cookies"], cookies)
        self.assertEqual(protocol["country"], "TH")
        self.assertEqual(protocol["sms_provider"], "smsbower")
        self.assertIn("-region-TH-sid-", unquote(urlsplit(protocol["proxy_pool"][0]).username or ""))
        self.assertIn("paypal_web_device_id=", response.headers.get("Set-Cookie", ""))

    async def test_one_click_paypal_ignores_manual_country_and_uses_link_country(self):
        _save_account_record(
            self.app["db_file"],
            "card-link@icloud.com",
            result={"cookies_json": '[{"name":"session","value":"cookie"}]'},
        )
        _save_account_card_link(
            self.app["db_file"],
            "card-link@icloud.com",
            url="https://www.paypal.com/agreements/approve?ba_token=th_country_lock",
            country="TH",
            currency="THB",
            method="de_oaics_paypal",
        )
        self.app["registration_proxy_store"].configure(
            enabled=True,
            mode="dynamic",
            country="TH",
            proxy_line="proxy.example:8000:shared-user:shared-pass",
        )
        self.app["smsbower_config_store"].configure(api_key="smsbower-test-key")
        created = mock.AsyncMock(return_value=(201, {"job": {"id": "pay-th-auto"}}))
        with mock.patch.object(self.app["paypal_service"], "create_job", created):
            response = await self.client.post(
                "/api/account/paypal-payment",
                json={"email": "card-link@icloud.com", "country": "US"},
                headers={"X-Local-Token": self.app["local_token"]},
            )

        payload = await response.json()
        self.assertEqual(response.status, 201)
        self.assertEqual(payload["country"], "TH")
        self.assertEqual(created.await_args.args[0]["country"], "TH")

    async def test_registration_callback_uses_configured_first_card_link_proxy(self):
        email = "probe@icloud.com"
        _save_account_record(
            self.app["db_file"],
            email,
            result={
                "access_token": "at-probe",
                "session_json": '{"accessToken":"at-probe"}',
                "registration_proxy_url": "http://register.example:8000",
                "registration_environment": {
                    "registration_mode": "protocol",
                    "email_type": "icloud_hide_my_email",
                    "proxy_enabled": True,
                    "proxy_mode": "dynamic",
                    "proxy_country": "JP",
                    "proxy_endpoint": "register.example:8000",
                    "captured_at": "2026-08-12T00:00:00+00:00",
                },
            },
        )
        self.app["card_link_proxy_store"].configure(
            enabled=True,
            mode="kookeey",
            country="JP",
            card_link_countries={"de": "TH"},
            card_link_modes={"de_oaics_paypal": "kookeey"},
            proxy_line=(
                "gate.kookeey.info:1000:1234567-AbCdEf1234:private-secret"
            ),
        )

        async def fake_proxy_test(_proxy_url, expected_country):
            return {
                "exitIp": "203.0.113.10" if expected_country == "TH" else "192.0.2.10",
                "country": expected_country,
                "latencyMs": 125,
            }

        self.app["registration_proxy_tester"] = fake_proxy_test
        with (
            mock.patch(
                "hidemyemail_generator.webapp.access_token_is_expired",
                return_value=False,
            ),
            mock.patch(
                "hidemyemail_generator.webapp._run_card_link_bridge",
                new=mock.AsyncMock(
                    return_value={
                        "status": "classified",
                        "classification": "oaics",
                        "checkout_id_type": "oaics",
                    }
                ),
            ) as bridge,
        ):
            await self.app["validate_saved_registration_checkout"](email)

        saved = load_account_record(self.app["db_file"], email)
        probe = saved["registration_checkout_probe"]
        self.assertEqual(probe["status"], "verified")
        self.assertTrue(probe["is_oaics"])
        self.assertEqual(probe["checkout_id_type"], "oaics")
        self.assertEqual(probe["registration_environment"]["exit_ip"], "192.0.2.10")
        self.assertEqual(probe["checkout_proxy"]["proxy_mode"], "kookeey")
        self.assertEqual(probe["checkout_proxy"]["proxy_country"], "TH")
        self.assertEqual(probe["checkout_proxy"]["proxy_role"], "first_card_link_proxy")
        self.assertEqual(probe["checkout_proxy"]["exit_ip"], "203.0.113.10")
        self.assertTrue(probe["differences"]["proxy_mode_changed"])
        self.assertTrue(probe["differences"]["proxy_country_changed"])
        self.assertEqual(bridge.await_args.kwargs["method"], "oaics_probe")
        self.assertEqual(bridge.await_args.kwargs["country"], "DE")
        self.assertEqual(bridge.await_args.kwargs["currency"], "EUR")
        self.assertIn("-TH-", bridge.await_args.kwargs["create_proxy_url"])
        self.assertIn("gate.kookeey.info:1000", bridge.await_args.kwargs["create_proxy_url"])
        self.assertNotIn("private-secret", json.dumps(probe))

    async def test_registration_callback_retries_probe_with_fresh_first_proxy(self):
        email = "probe-retry@icloud.com"
        _save_account_record(
            self.app["db_file"],
            email,
            result={
                "access_token": "at-probe-retry",
                "session_json": '{"accessToken":"at-probe-retry"}',
                "registration_environment": {
                    "registration_mode": "browser_headed",
                    "proxy_country": "JP",
                    "captured_at": "2026-08-12T00:00:01+00:00",
                },
            },
        )
        self.app["card_link_proxy_store"].configure(
            enabled=True,
            mode="kookeey",
            country="JP",
            card_link_countries={"de": "GB"},
            card_link_modes={"de_oaics_paypal": "kookeey"},
            proxy_line=(
                "gate.kookeey.info:1000:1234567-AbCdEf1234:private-secret"
            ),
        )

        async def fake_proxy_test(_proxy_url, expected_country):
            return {
                "exitIp": "203.0.113.20",
                "country": expected_country or "JP",
                "latencyMs": 80,
            }

        self.app["registration_proxy_tester"] = fake_proxy_test
        retry_sleep = mock.AsyncMock()
        self.app["auto_oaics_probe_sleep"] = retry_sleep
        bridge_result = {
            "status": "classified",
            "classification": "oaics",
            "checkout_id_type": "oaics",
        }
        bridge = mock.AsyncMock(
            side_effect=[
                RuntimeError("temporary checkout timeout"),
                RuntimeError("temporary upstream reset"),
                bridge_result,
            ]
        )
        with (
            mock.patch(
                "hidemyemail_generator.webapp.access_token_is_expired",
                return_value=False,
            ),
            mock.patch(
                "hidemyemail_generator.webapp._run_card_link_bridge",
                new=bridge,
            ),
        ):
            await self.app["validate_saved_registration_checkout"](email)

        probe = load_account_record(
            self.app["db_file"], email
        )["registration_checkout_probe"]
        proxies = [
            call.kwargs["create_proxy_url"]
            for call in bridge.await_args_list
        ]
        self.assertEqual(bridge.await_count, 3)
        self.assertEqual(retry_sleep.await_count, 2)
        self.assertEqual(probe["status"], "verified")
        self.assertEqual(probe["attempt_count"], 3)
        self.assertEqual(probe["max_attempts"], 3)
        self.assertEqual(len(probe["attempt_errors"]), 2)
        self.assertTrue(
            all(call.kwargs["country"] == "DE" for call in bridge.await_args_list)
        )
        self.assertTrue(
            all(call.kwargs["currency"] == "EUR" for call in bridge.await_args_list)
        )
        self.assertTrue(
            all(call.kwargs["locale"] == "de-DE" for call in bridge.await_args_list)
        )
        self.assertTrue(all("-GB-" in proxy for proxy in proxies))
        self.assertEqual(len(set(proxies)), 3)
        self.assertNotIn("private-secret", json.dumps(probe))

    async def test_cs_live_retries_until_attempt_limit_then_returns_link(self):
        classified = {
            "status": "classified",
            "classification": "cs_live",
            "checkout_id_type": "cs_live",
            "method": "de_oaics_paypal",
        }
        generated = {
            "status": "success",
            "url": "https://www.paypal.com/agreements/approve?ba_token=retry_limit",
            "method": "de_oaics_paypal",
            "country": "DE",
            "currency": "EUR",
        }
        bridge = mock.AsyncMock(side_effect=[classified, classified, generated])
        with (
            mock.patch(
                "hidemyemail_generator.webapp.access_token_is_expired",
                return_value=False,
            ),
            mock.patch(
                "hidemyemail_generator.webapp._run_card_link_bridge",
                new=bridge,
            ),
        ):
            response = await self.client.post(
                "/api/account/card-link",
                json={
                    "email": "card-link@icloud.com",
                    "method": "de_oaics_paypal",
                    "create_proxy": "create.example:8000:user:pass",
                    "attempt_limit": 5,
                },
                headers={"X-Local-Token": self.app["local_token"]},
            )

        payload = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(bridge.await_count, 3)
        self.assertEqual(payload["cardLinkStatus"], "generated")
        self.assertEqual(payload["attemptCount"], 3)
        self.assertEqual(payload["attemptLimit"], 5)
        self.assertFalse(payload["attemptsExhausted"])

    async def test_cs_live_stops_only_after_attempt_limit_is_exhausted(self):
        classified = {
            "status": "classified",
            "classification": "cs_live",
            "checkout_id_type": "cs_live",
            "method": "de_oaics_paypal",
        }
        bridge = mock.AsyncMock(return_value=classified)
        with (
            mock.patch(
                "hidemyemail_generator.webapp.access_token_is_expired",
                return_value=False,
            ),
            mock.patch(
                "hidemyemail_generator.webapp._run_card_link_bridge",
                new=bridge,
            ),
        ):
            response = await self.client.post(
                "/api/account/card-link",
                json={
                    "email": "card-link@icloud.com",
                    "method": "de_oaics_paypal",
                    "create_proxy": "create.example:8000:user:pass",
                    "attempt_limit": 3,
                    "force_retry": True,
                },
                headers={"X-Local-Token": self.app["local_token"]},
            )

        payload = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(bridge.await_count, 3)
        self.assertEqual(payload["cardLinkStatus"], "cs_live")
        self.assertEqual(payload["attemptCount"], 3)
        self.assertEqual(payload["attemptLimit"], 3)
        self.assertTrue(payload["attemptsExhausted"])

    async def test_failed_checkout_probe_can_be_retried_from_account_action(self):
        callback = mock.AsyncMock(
            return_value={
                "status": "verified",
                "checkout_id_type": "oaics",
                "is_oaics": True,
                "attempt_count": 2,
                "max_attempts": 3,
            }
        )
        self.app["validate_saved_registration_checkout"] = callback

        response = await self.client.post(
            "/api/account/checkout-probe",
            json={"email": "probe-retry@icloud.com"},
            headers={"X-Local-Token": self.app["local_token"]},
        )

        payload = await response.json()
        self.assertEqual(response.status, 200)
        self.assertTrue(payload["is_oaics"])
        self.assertEqual(payload["attempt_count"], 2)
        callback.assert_awaited_once_with("probe-retry@icloud.com", force=True)

    async def test_registration_stop_targets_only_requested_process(self):
        manager = mock.Mock()
        manager.stop = mock.AsyncMock(
            return_value={"runningCount": 1, "tasks": [{"processId": "process-b"}]}
        )
        manager.close = mock.AsyncMock()
        self.app["registration_manager"] = manager

        response = await self.client.post(
            "/api/registration/stop",
            json={"process_id": "process-a"},
            headers={"X-Local-Token": self.app["local_token"]},
        )

        payload = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["task"]["runningCount"], 1)
        manager.stop.assert_awaited_once_with(process_id="process-a")

    async def test_protocol_stop_targets_only_requested_process(self):
        manager = mock.Mock()
        manager.stop = mock.AsyncMock(
            return_value={"runningCount": 1, "tasks": [{"processId": "protocol-b"}]}
        )
        manager.close = mock.AsyncMock()
        self.app["protocol_registration_manager"] = manager

        response = await self.client.post(
            "/api/protocol-registration/stop",
            json={"processId": "protocol-a"},
            headers={"X-Local-Token": self.app["local_token"]},
        )

        payload = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["task"]["runningCount"], 1)
        manager.stop.assert_awaited_once_with(process_id="protocol-a")

    async def test_rejects_unsupported_card_region(self):
        response = await self.client.post(
            "/api/account/card-link",
            json={"email": "card-link@icloud.com", "country": "ZZ"},
            headers={"X-Local-Token": self.app["local_token"]},
        )

        self.assertEqual(response.status, 400)
        self.assertIn("不支持", (await response.json())["error"])

    async def test_generates_ph_hosted_strict_zero_link_with_two_proxies(self):
        generated = {
            "status": "success",
            "url": "https://chatgpt.com/checkout/openai_ie/oaics_test_ph_hosted",
            "method": "ph_hosted",
            "country": "PH",
            "currency": "PHP",
            "payment_link_type": "chatgpt_checkout_short",
            "checkout_ui_mode": "hosted",
            "amount": "0",
            "amount_currency": "PHP",
            "amount_verification": "checkout_update",
            "promotion_applied": True,
            "promotion_strategy": "gpt_link_hosted_create_and_update",
        }
        with (
            mock.patch(
                "hidemyemail_generator.webapp.access_token_is_expired",
                return_value=False,
            ),
            mock.patch(
                "hidemyemail_generator.webapp._run_card_link_bridge",
                new=mock.AsyncMock(return_value=generated),
            ) as bridge,
        ):
            response = await self.client.post(
                "/api/account/card-link",
                json={
                    "email": "card-link@icloud.com",
                    "method": "ph_hosted",
                    "create_proxy": "create.example:8000:user:pass",
                    "promotion_proxy": "socks5://promo.example:9000",
                },
                headers={"X-Local-Token": self.app["local_token"]},
            )

        payload = await response.json()
        saved = load_account_record(
            self.app["db_file"], "card-link@icloud.com"
        )["card_link"]
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["method"], "ph_hosted")
        self.assertEqual(saved["amount"], "0")
        self.assertEqual(saved["checkout_ui_mode"], "hosted")
        self.assertNotIn("proxy", saved)
        self.assertEqual(bridge.await_args.kwargs["country"], "PH")
        self.assertEqual(bridge.await_args.kwargs["currency"], "PHP")
        self.assertEqual(
            bridge.await_args.kwargs["create_proxy_url"],
            "http://user:pass@create.example:8000",
        )
        self.assertEqual(
            bridge.await_args.kwargs["promotion_proxy_url"],
            "socks5://promo.example:9000",
        )

    async def test_rejects_invalid_card_link_proxy(self):
        response = await self.client.post(
            "/api/account/card-link",
            json={
                "email": "card-link@icloud.com",
                "method": "ph_hosted",
                "create_proxy": "file:///tmp/not-a-proxy",
            },
            headers={"X-Local-Token": self.app["local_token"]},
        )

        self.assertEqual(response.status, 400)
        self.assertIn("代理", (await response.json())["error"])

    async def test_resolves_selected_card_link_countries_from_saved_proxy(self):
        self.app["card_link_proxy_store"].configure(
            enabled=False,
            country="NL",
            proxy_line="proxy.example:3010:private-user:private-password",
        )
        generated = {
            "status": "success",
            "url": "https://chatgpt.com/checkout/openai_ie/oaics_country_proxy",
            "method": "ph_hosted",
            "country": "PH",
            "currency": "PHP",
            "amount": "0",
            "promotion_applied": True,
        }
        with (
            mock.patch(
                "hidemyemail_generator.webapp.access_token_is_expired",
                return_value=False,
            ),
            mock.patch(
                "hidemyemail_generator.webapp._run_card_link_bridge",
                new=mock.AsyncMock(return_value=generated),
            ) as bridge,
        ):
            response = await self.client.post(
                "/api/account/card-link",
                json={
                    "email": "card-link@icloud.com",
                    "method": "ph_hosted",
                    "create_proxy_country": "US",
                    "promotion_proxy_country": "TR",
                },
                headers={"X-Local-Token": self.app["local_token"]},
            )

        create_proxy = urlsplit(bridge.await_args.kwargs["create_proxy_url"])
        promotion_proxy = urlsplit(
            bridge.await_args.kwargs["promotion_proxy_url"]
        )
        self.assertEqual(response.status, 200)
        self.assertIn("-region-US-sid-", unquote(create_proxy.username or ""))
        self.assertIn("-region-TR-sid-", unquote(promotion_proxy.username or ""))
        self.assertEqual(
            self.app["registration_proxy_store"].public_state()["country"],
            "NL",
        )

    async def test_generates_de_oaics_paypal_link_with_first_proxy_only(self):
        generated = {
            "status": "success",
            "url": "https://www.paypal.com/agreements/approve?ba_token=de_test",
            "method": "de_oaics_paypal",
            "country": "DE",
            "currency": "EUR",
            "payment_link_type": "paypal_approve",
            "checkout_ui_mode": "custom",
            "amount": "0",
            "amount_currency": "EUR",
            "amount_verification": "checkout_create",
            "promotion_applied": True,
            "promotion_strategy": "de_oaics_checkout_create_native",
        }
        with (
            mock.patch(
                "hidemyemail_generator.webapp.access_token_is_expired",
                return_value=False,
            ),
            mock.patch(
                "hidemyemail_generator.webapp._run_card_link_bridge",
                new=mock.AsyncMock(return_value=generated),
            ) as bridge,
        ):
            response = await self.client.post(
                "/api/account/card-link",
                json={
                    "email": "card-link@icloud.com",
                    "method": "de_oaics_paypal",
                    "country": "US",
                    "create_proxy": "create.example:8000:user:pass",
                    "promotion_proxy": "socks5://ignored.example:9000",
                },
                headers={"X-Local-Token": self.app["local_token"]},
            )

        payload = await response.json()
        saved = load_account_record(
            self.app["db_file"], "card-link@icloud.com"
        )["card_link"]
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["method"], "de_oaics_paypal")
        self.assertEqual(saved["country"], "DE")
        self.assertEqual(saved["currency"], "EUR")
        self.assertEqual(saved["amount"], "0")
        self.assertEqual(saved["payment_link_type"], "paypal_approve")
        self.assertEqual(bridge.await_args.kwargs["country"], "DE")
        self.assertEqual(bridge.await_args.kwargs["currency"], "EUR")
        self.assertEqual(bridge.await_args.kwargs["locale"], "de-DE")
        self.assertEqual(
            bridge.await_args.kwargs["create_proxy_url"],
            "http://user:pass@create.example:8000",
        )
        self.assertEqual(bridge.await_args.kwargs["promotion_proxy_url"], "")
        self.assertEqual(
            bridge.await_args.kwargs["account_email"], "card-link@icloud.com"
        )

    async def test_independent_extraction_proxy_selects_first_and_second_exits(self):
        self.app["card_link_proxy_store"].configure(
            enabled=True,
            mode="dynamic",
            country="DE",
            proxy_line="extract.example:3010:extract-user:extract-password",
        )
        generated = {
            "status": "success",
            "url": "https://www.paypal.com/agreements/approve?ba_token=independent_pair",
            "method": "de_oaics_paypal",
            "country": "DE",
            "currency": "EUR",
        }
        with (
            mock.patch(
                "hidemyemail_generator.webapp.access_token_is_expired",
                return_value=False,
            ),
            mock.patch(
                "hidemyemail_generator.webapp._run_card_link_bridge",
                new=mock.AsyncMock(return_value=generated),
            ) as bridge,
        ):
            first = await self.client.post(
                "/api/account/card-link",
                json={
                    "email": "card-link@icloud.com",
                    "method": "de_oaics_paypal",
                    "proxy_mode": "dynamic",
                    "create_proxy_country": "US",
                    "secondary_proxy_country": "TH",
                    "reuse_registration_proxy": False,
                    "independent_proxy_pair": True,
                    "promotion_proxy_choice": "second",
                },
                headers={"X-Local-Token": self.app["local_token"]},
            )
            first_call = bridge.await_args.kwargs
            retry = await self.client.post(
                "/api/account/card-link",
                json={
                    "email": "card-link@icloud.com",
                    "method": "de_oaics_paypal",
                    "proxy_mode": "dynamic",
                    "create_proxy_country": "US",
                    "secondary_proxy_country": "TH",
                    "reuse_registration_proxy": False,
                    "independent_proxy_pair": True,
                    "use_secondary_proxy": True,
                    "promotion_proxy_choice": "first",
                    "force_retry": True,
                },
                headers={"X-Local-Token": self.app["local_token"]},
            )
            retry_call = bridge.await_args.kwargs

        self.assertEqual(first.status, 200)
        self.assertEqual(retry.status, 200)
        self.assertIn("-region-US-sid-", unquote(urlsplit(first_call["create_proxy_url"]).username or ""))
        self.assertIn("-region-TH-sid-", unquote(urlsplit(first_call["promotion_proxy_url"]).username or ""))
        self.assertIn("-region-TH-sid-", unquote(urlsplit(retry_call["create_proxy_url"]).username or ""))
        self.assertIn("-region-US-sid-", unquote(urlsplit(retry_call["promotion_proxy_url"]).username or ""))

    async def test_quick_flow_reuses_registration_ip_and_only_retry_uses_second_ip(self):
        email = "shared-proxy@icloud.com"
        first_proxy = "http://same-user:same-pass@register.example:8000"
        _save_account_record(
            self.app["db_file"],
            email,
            result={
                "access_token": "at-shared-proxy",
                "session_json": '{"accessToken":"at-shared-proxy"}',
                "registration_proxy_url": first_proxy,
                "registration_environment": {
                    "proxy_mode": "dynamic",
                    "proxy_country": "DE",
                },
            },
        )
        self.app["registration_proxy_store"].configure(
            enabled=True,
            mode="dynamic",
            country="DE",
            proxy_line="fresh.example:3010:shared-user:shared-password",
        )
        generated = {
            "status": "success",
            "url": "https://www.paypal.com/agreements/approve?ba_token=shared_proxy",
            "method": "de_oaics_paypal",
            "country": "DE",
            "currency": "EUR",
        }
        with (
            mock.patch(
                "hidemyemail_generator.webapp.access_token_is_expired",
                return_value=False,
            ),
            mock.patch(
                "hidemyemail_generator.webapp._run_card_link_bridge",
                new=mock.AsyncMock(return_value=generated),
            ) as bridge,
        ):
            first = await self.client.post(
                "/api/account/card-link",
                json={
                    "email": email,
                    "method": "de_oaics_paypal",
                    "proxy_mode": "dynamic",
                    "create_proxy_country": "DE",
                    "secondary_proxy_country": "DE",
                    "reuse_registration_proxy": True,
                    "promotion_proxy_choice": "first",
                },
                headers={"X-Local-Token": self.app["local_token"]},
            )
            first_call = bridge.await_args.kwargs
            retry = await self.client.post(
                "/api/account/card-link",
                json={
                    "email": email,
                    "method": "de_oaics_paypal",
                    "proxy_mode": "dynamic",
                    "create_proxy_country": "DE",
                    "secondary_proxy_country": "DE",
                    "reuse_registration_proxy": True,
                    "use_secondary_proxy": True,
                    "promotion_proxy_choice": "first",
                    "force_retry": True,
                },
                headers={"X-Local-Token": self.app["local_token"]},
            )
            retry_call = bridge.await_args.kwargs

        self.assertEqual(first.status, 200)
        self.assertEqual(retry.status, 200)
        self.assertEqual(first_call["create_proxy_url"], first_proxy)
        self.assertEqual(first_call["promotion_proxy_url"], first_proxy)
        self.assertNotEqual(retry_call["create_proxy_url"], first_proxy)
        self.assertIn("-region-DE-sid-", unquote(urlsplit(retry_call["create_proxy_url"]).username or ""))
        self.assertEqual(retry_call["promotion_proxy_url"], first_proxy)

    async def test_quick_flow_can_update_promotion_with_second_ip(self):
        email = "promotion-second@icloud.com"
        first_proxy = "http://same-user:same-pass@register.example:8000"
        _save_account_record(
            self.app["db_file"],
            email,
            result={
                "access_token": "at-promotion-second",
                "session_json": '{"accessToken":"at-promotion-second"}',
                "registration_proxy_url": first_proxy,
                "registration_environment": {
                    "proxy_mode": "dynamic",
                    "proxy_country": "DE",
                },
            },
        )
        self.app["registration_proxy_store"].configure(
            enabled=True,
            mode="dynamic",
            country="DE",
            proxy_line="fresh.example:3010:shared-user:shared-password",
        )
        with (
            mock.patch(
                "hidemyemail_generator.webapp.access_token_is_expired",
                return_value=False,
            ),
            mock.patch(
                "hidemyemail_generator.webapp._run_card_link_bridge",
                new=mock.AsyncMock(return_value={
                    "status": "success",
                    "url": "https://www.paypal.com/agreements/approve?ba_token=promotion_second",
                    "method": "de_oaics_paypal",
                    "country": "DE",
                    "currency": "EUR",
                }),
            ) as bridge,
        ):
            response = await self.client.post(
                "/api/account/card-link",
                json={
                    "email": email,
                    "method": "de_oaics_paypal",
                    "proxy_mode": "dynamic",
                    "create_proxy_country": "DE",
                    "secondary_proxy_country": "DE",
                    "reuse_registration_proxy": True,
                    "promotion_proxy_choice": "second",
                },
                headers={"X-Local-Token": self.app["local_token"]},
            )

        call = bridge.await_args.kwargs
        self.assertEqual(response.status, 200)
        self.assertEqual(call["create_proxy_url"], first_proxy)
        self.assertNotEqual(call["promotion_proxy_url"], first_proxy)
        self.assertIn("-region-DE-sid-", unquote(urlsplit(call["promotion_proxy_url"]).username or ""))

    async def test_marks_de_cs_live_but_force_retry_reprocesses_same_account(self):
        classified = {
            "status": "classified",
            "classification": "cs_live",
            "checkout_id_type": "cs_live",
            "method": "de_oaics_paypal",
        }
        with (
            mock.patch(
                "hidemyemail_generator.webapp.access_token_is_expired",
                return_value=False,
            ),
            mock.patch(
                "hidemyemail_generator.webapp._run_card_link_bridge",
                new=mock.AsyncMock(return_value=classified),
            ) as bridge,
        ):
            first = await self.client.post(
                "/api/account/card-link",
                json={
                    "email": "card-link@icloud.com",
                    "method": "de_oaics_paypal",
                    "create_proxy": "create.example:8000:user:pass",
                },
                headers={"X-Local-Token": self.app["local_token"]},
            )
            second = await self.client.post(
                "/api/account/card-link",
                json={
                    "email": "card-link@icloud.com",
                    "method": "de_oaics_paypal",
                },
                headers={"X-Local-Token": self.app["local_token"]},
            )
            accounts = await self.client.get("/api/gpt-emails")

        first_payload = await first.json()
        second_payload = await second.json()
        account = next(
            item
            for item in (await accounts.json())["items"]
            if item["email"] == "card-link@icloud.com"
        )
        self.assertEqual(first.status, 200)
        self.assertEqual(first_payload["cardLinkStatus"], "cs_live")
        self.assertEqual(first_payload["method"], "de_oaics_paypal")
        self.assertEqual(first_payload["url"], "")
        self.assertEqual(second.status, 200)
        self.assertTrue(second_payload["skipped"])
        self.assertEqual(bridge.await_count, 1)
        self.assertEqual(account["cardLinkStatus"], "cs_live")
        self.assertEqual(account["cardLinkMethod"], "de_oaics_paypal")
        self.assertFalse(account["cardLink"])

        retried_link = "https://www.paypal.com/agreements/approve?ba_token=retry_same_account"
        with (
            mock.patch(
                "hidemyemail_generator.webapp.access_token_is_expired",
                return_value=False,
            ),
            mock.patch(
                "hidemyemail_generator.webapp._run_card_link_bridge",
                new=mock.AsyncMock(
                    return_value={
                        "status": "success",
                        "url": retried_link,
                        "method": "de_oaics_paypal",
                        "country": "DE",
                        "currency": "EUR",
                    }
                ),
            ) as retry_bridge,
        ):
            retried = await self.client.post(
                "/api/account/card-link",
                json={
                    "email": "card-link@icloud.com",
                    "method": "de_oaics_paypal",
                    "create_proxy": "create.example:8000:user:pass",
                    "force_retry": True,
                },
                headers={"X-Local-Token": self.app["local_token"]},
            )

        retried_payload = await retried.json()
        self.assertEqual(retried.status, 200)
        self.assertEqual(retried_payload["cardLinkStatus"], "generated")
        self.assertEqual(retried_payload["url"], retried_link)
        retry_bridge.assert_awaited_once()
        self.assertEqual(
            load_account_record(
                self.app["db_file"], "card-link@icloud.com"
            )["card_link"]["url"],
            retried_link,
        )

        ph_generated = {
            "status": "success",
            "url": "https://chatgpt.com/checkout/openai_ie/oaics_other_mode",
            "method": "ph_hosted",
            "country": "PH",
            "currency": "PHP",
            "amount": "0",
            "promotion_applied": True,
        }
        with (
            mock.patch(
                "hidemyemail_generator.webapp.access_token_is_expired",
                return_value=False,
            ),
            mock.patch(
                "hidemyemail_generator.webapp._run_card_link_bridge",
                new=mock.AsyncMock(return_value=ph_generated),
            ) as ph_bridge,
        ):
            ph_response = await self.client.post(
                "/api/account/card-link",
                json={
                    "email": "card-link@icloud.com",
                    "method": "ph_hosted",
                    "create_proxy": "create.example:8000:user:pass",
                    "promotion_proxy": "promo.example:9000:user:pass",
                },
                headers={"X-Local-Token": self.app["local_token"]},
            )

        self.assertEqual(ph_response.status, 200)
        ph_bridge.assert_awaited_once()


class CodePortalTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def fake_hide_my_email():
        class FakeHideMyEmail:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def list_email(self):
                return {
                    "success": True,
                    "result": {
                        "hmeEmails": [
                            {
                                "hme": "one@icloud.com",
                                "anonymousId": "one",
                                "isActive": True,
                            }
                        ]
                    },
                }

        return FakeHideMyEmail

    async def test_account_list_uses_local_records_when_icloud_session_is_invalid(self):
        class InvalidSessionHideMyEmail:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def list_email(self):
                return {"success": False, "error": "Invalid global session"}

        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app(base_dir=Path(temp_dir))
            app["cookie_file"].write_text("fake-cookie", encoding="utf-8")
            conn = connect_db(str(app["db_file"]))
            try:
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?)",
                    (
                        "gpt_account:352121354@qq.com",
                        json.dumps(
                            {
                                "password": "Manual!Password123",
                                "password_confirmed": True,
                            }
                        ),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                with mock.patch(
                    "hidemyemail_generator.webapp.RichHideMyEmail",
                    InvalidSessionHideMyEmail,
                ):
                    response = await client.get("/api/gpt-emails")
                    payload = await response.json()
            finally:
                await client.close()

        self.assertEqual(response.status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["items"][0]["email"], "352121354@qq.com")
        self.assertEqual(payload["identityWarning"], "Invalid global session")

    async def test_public_alias_only_portal_keeps_admin_private(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app(
                base_dir=Path(temp_dir),
                web_password="private-token",
                workbench_import_token="private-token",
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                page = await client.get("/code", allow_redirects=False)
                html = await page.text()
                invalid_lookup = await client.post(
                    "/api/code/latest", json={"email": "invalid"}
                )
                private_admin = await client.get(
                    "/api/gpt-emails", allow_redirects=False
                )
                invalid = await client.get(
                    "/access?token=wrong", allow_redirects=False
                )
                granted = await client.get(
                    "/access?token=private-token", allow_redirects=False
                )
            finally:
                await client.close()

        self.assertEqual(page.status, 200)
        self.assertEqual(invalid_lookup.status, 400)
        self.assertEqual(private_admin.status, 401)
        self.assertEqual(invalid.status, 404)
        self.assertEqual(granted.status, 302)
        self.assertEqual(granted.headers["Location"], "/code")
        self.assertIn("输入“隐藏我的邮箱”子邮箱", html)
        self.assertIn("/api/code/latest", html)
        self.assertNotIn("password", html.lower())

    async def test_concurrent_alias_lookups_remain_isolated(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "hidemyemail.db"
            conn = connect_db(str(db_file))
            try:
                messages = [
                    ("one@icloud.com", "111111", "2026-08-04T01:00:00+00:00", "u1"),
                    ("two@icloud.com", "222222", "2026-08-04T02:00:00+00:00", "u2"),
                    ("one@icloud.com", "333333", "2026-08-04T03:00:00+00:00", "u3"),
                ]
                for address, code, received_at, uid in messages:
                    conn.execute(
                        """
                        INSERT INTO messages(
                            account_key, folder, uid, sender, hme_address,
                            subject, code, body_preview, received_at, created_at
                        ) VALUES (?, 'INBOX', ?, 'sender@example.com', ?,
                                  'Verification code', ?, '', ?, ?)
                        """,
                        ("icloud", uid, address, code, received_at, received_at),
                    )
                conn.commit()
            finally:
                conn.close()
            identities = [
                {"hme": "one@icloud.com", "anonymousId": "one"},
                {"hme": "two@icloud.com", "anonymousId": "two"},
            ]
            first, second = await asyncio.gather(
                asyncio.to_thread(
                    _latest_code_for_email,
                    db_file,
                    "one@icloud.com",
                    identities,
                ),
                asyncio.to_thread(
                    _latest_code_for_email,
                    db_file,
                    "two@icloud.com",
                    identities,
                ),
            )

        self.assertEqual(first["code"], "333333")
        self.assertEqual(second["code"], "222222")

    async def test_code_lookup_syncs_only_on_demand_and_shares_cooldown(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "cookies.txt").write_text("cookie", encoding="utf-8")
            save_config(
                InboxConfig(
                    host="imap.example.com",
                    port=993,
                    username="inbox@example.com",
                    password="app-password",
                ),
                str(root / "inbox_config.json"),
            )
            app = create_app(base_dir=root)
            client = TestClient(TestServer(app))
            with (
                mock.patch(
                    "hidemyemail_generator.webapp.RichHideMyEmail",
                    self.fake_hide_my_email(),
                ),
                mock.patch(
                    "hidemyemail_generator.webapp.sync_inbox",
                    return_value=[],
                ) as sync,
            ):
                await client.start_server()
                try:
                    # The deactivation scanner performs one startup sync.  Let
                    # it finish, then isolate the public code-lookup requests.
                    for _ in range(100):
                        if (
                            sync.call_count
                            and app["deactivation_scan_state"].get("status")
                            != "running"
                        ):
                            break
                        await asyncio.sleep(0.01)
                    sync.reset_mock()
                    app["inbox_on_demand_next_attempt"] = 0.0
                    first = await client.post(
                        "/api/code/latest", json={"email": "one@icloud.com"}
                    )
                    second = await client.post(
                        "/api/code/latest", json={"email": "one@icloud.com"}
                    )
                finally:
                    await client.close()

        self.assertEqual(first.status, 404)
        self.assertEqual(second.status, 404)
        self.assertEqual(sync.call_count, 1)
        self.assertEqual(sync.call_args.args[2], OPENAI_CODE_INBOX_SYNC_LIMIT)

    async def test_gpt_code_returns_direct_junk_match_with_expired_icloud_cookie(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "cookies.txt").write_text("expired-cookie", encoding="utf-8")
            save_config(
                InboxConfig(
                    host="imap.example.com",
                    port=993,
                    username="inbox@example.com",
                    password="app-password",
                ),
                str(root / "inbox_config.json"),
            )
            app = create_app(base_dir=root)

            def sync_junk(_config, db_file, _limit):
                conn = connect_db(str(db_file))
                try:
                    record = {
                        "account_key": "inbox@example.com@imap.example.com/Junk",
                        "folder": "Junk",
                        "uid": "junk-1",
                        "sender": "noreply@openai.com",
                        "recipients": "relay@icloud.com",
                        "hme_address": "relay@icloud.com",
                        "subject": "Your ChatGPT verification code",
                        "code": "938388",
                        "body_preview": "Your verification code is 938388",
                        "received_at": "2026-08-11T02:53:50+00:00",
                        "created_at": "2026-08-11T02:53:51+00:00",
                    }
                    insert_message(conn, record)
                    return [record]
                finally:
                    conn.close()

            client = TestClient(TestServer(app))
            with (
                mock.patch(
                    "hidemyemail_generator.webapp.sync_inbox",
                    side_effect=sync_junk,
                ),
                mock.patch(
                    "hidemyemail_generator.webapp.RichHideMyEmail"
                ) as icloud_client,
            ):
                await client.start_server()
                try:
                    response = await client.post(
                        "/api/gpt-code",
                        json={"email": "relay@icloud.com"},
                        headers={"X-Local-Token": app["local_token"]},
                    )
                    payload = await response.json()
                    public_response = await client.post(
                        "/api/code/latest",
                        json={"email": "relay@icloud.com"},
                    )
                    public_payload = await public_response.json()
                finally:
                    await client.close()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["code"], "938388")
        self.assertEqual(public_response.status, 200)
        self.assertEqual(public_payload["code"], "938388")
        icloud_client.assert_not_called()

    async def test_gpt_code_treats_expired_icloud_cookie_as_no_code_yet(self):
        class ExpiredSessionHideMyEmail:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def list_email(self):
                return {"success": False, "error": "Invalid global session"}

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "cookies.txt").write_text("expired-cookie", encoding="utf-8")
            save_config(
                InboxConfig(
                    host="imap.example.com",
                    port=993,
                    username="inbox@example.com",
                    password="app-password",
                ),
                str(root / "inbox_config.json"),
            )
            app = create_app(base_dir=root)
            client = TestClient(TestServer(app))
            with (
                mock.patch(
                    "hidemyemail_generator.webapp.sync_inbox",
                    return_value=[],
                ),
                mock.patch(
                    "hidemyemail_generator.webapp.RichHideMyEmail",
                    ExpiredSessionHideMyEmail,
                ),
            ):
                await client.start_server()
                try:
                    response = await client.post(
                        "/api/gpt-code",
                        json={
                            "email": "relay@icloud.com",
                            "since": "2026-08-12T10:00:00+00:00",
                        },
                        headers={"X-Local-Token": app["local_token"]},
                    )
                    payload = await response.json()
                finally:
                    await client.close()

        self.assertEqual(response.status, 404)
        self.assertEqual(payload["error"], "暂未获取到该邮箱的 OpenAI 验证码")
        self.assertNotIn("Invalid global session", payload["error"])

    async def test_authentication_failure_enters_backoff(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "cookies.txt").write_text("cookie", encoding="utf-8")
            save_config(
                InboxConfig(
                    host="imap.example.com",
                    port=993,
                    username="inbox@example.com",
                    password="app-password",
                ),
                str(root / "inbox_config.json"),
            )
            app = create_app(base_dir=root)
            client = TestClient(TestServer(app))
            with (
                mock.patch(
                    "hidemyemail_generator.webapp.RichHideMyEmail",
                    self.fake_hide_my_email(),
                ),
                mock.patch(
                    "hidemyemail_generator.webapp.sync_inbox",
                    side_effect=RuntimeError("AUTHENTICATIONFAILED"),
                ) as sync,
            ):
                await client.start_server()
                try:
                    first = await client.post(
                        "/api/code/latest", json={"email": "one@icloud.com"}
                    )
                    second = await client.post(
                        "/api/code/latest", json={"email": "one@icloud.com"}
                    )
                    first_payload = await first.json()
                    second_payload = await second.json()
                finally:
                    await client.close()

        self.assertEqual(first.status, 502)
        self.assertEqual(second.status, 502)
        self.assertIn("IMAP 登录失败", first_payload["error"])
        self.assertEqual(second_payload["error"], first_payload["error"])
        self.assertEqual(sync.call_count, 1)


class VerifyAccountEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_account_can_start_dedicated_two_factor_task(self):
        class BrowserManagerStub:
            def __init__(self):
                self.starts = []

            def snapshot(self):
                return {"running": False}

            def start(self, accounts, **options):
                self.starts.append({"accounts": accounts, **options})
                return {"running": True, "accounts": [{"email": accounts[0]["email"]}]}

            async def close(self):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app(base_dir=Path(temp_dir))
            _save_account_record(
                app["db_file"],
                "secure@icloud.com",
                password="Existing!Password123",
                password_confirmed=True,
                two_factor={
                    "enabled": False,
                    "status": "enrolled",
                    "secret": "JBSWY3DPEHPK3PXP",
                    "factor_id": "factor-1",
                    "session_id": "session-1",
                },
            )
            browser_manager = BrowserManagerStub()
            app["browser_manager"] = browser_manager
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post(
                    "/api/account/enable-2fa",
                    json={"email": "secure@icloud.com", "headless": False},
                    headers={"X-Local-Token": app["local_token"]},
                )
                payload = await response.json()
            finally:
                await client.close()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["mode"], "enable_2fa")
        started = browser_manager.starts[0]
        self.assertTrue(started["headless"])
        self.assertEqual(started["concurrency"], 1)
        account = started["accounts"][0]
        self.assertTrue(account["password_confirmed"])
        self.assertTrue(account["enable_2fa"])
        self.assertEqual(account["two_factor"]["status"], "enrolled")

    async def test_unconfirmed_account_cannot_start_two_factor_task(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app(base_dir=Path(temp_dir))
            _save_account_record(
                app["db_file"],
                "pending@icloud.com",
                password="LocalOnly!Password123",
                password_confirmed=False,
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post(
                    "/api/account/enable-2fa",
                    json={"email": "pending@icloud.com"},
                    headers={"X-Local-Token": app["local_token"]},
                )
                payload = await response.json()
            finally:
                await client.close()

        self.assertEqual(response.status, 409)
        self.assertEqual(payload["error"], "请先设置并确认账号密码，再添加 2FA")

    async def test_password_reset_never_enables_two_factor(self):
        class BrowserManagerStub:
            def __init__(self):
                self.starts = []

            def snapshot(self):
                return {"running": False}

            def start(self, accounts, **options):
                self.starts.append({"accounts": accounts, **options})
                return {"running": True, "accounts": accounts}

            async def close(self):
                return None

        class FakeHideMyEmail:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def list_email(self):
                return {
                    "success": True,
                    "result": {
                        "hmeEmails": [
                            {
                                "hme": "reset@icloud.com",
                                "anonymousId": "reset-id",
                                "isActive": True,
                            }
                        ]
                    },
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            (base_dir / "cookies.txt").write_text("cookie", encoding="utf-8")
            (base_dir / "inbox_config.json").write_text("{}\n", encoding="utf-8")
            app = create_app(base_dir=base_dir)
            _save_account_record(
                app["db_file"],
                "reset@icloud.com",
                password="Existing!Password123",
                password_confirmed=True,
                result={"access_token": "existing-token"},
            )
            browser_manager = BrowserManagerStub()
            app["browser_manager"] = browser_manager
            client = TestClient(TestServer(app))
            with mock.patch(
                "hidemyemail_generator.webapp.RichHideMyEmail", FakeHideMyEmail
            ):
                await client.start_server()
                try:
                    response = await client.post(
                        "/api/account/verify-or-register",
                        json={
                            "email": "reset@icloud.com",
                            "headless": True,
                            "reset_password": True,
                        },
                        headers={"X-Local-Token": app["local_token"]},
                    )
                    payload = await response.json()
                finally:
                    await client.close()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["mode"], "set_password")
        account = browser_manager.starts[0]["accounts"][0]
        self.assertTrue(account["ensure_password"])
        self.assertFalse(account["enable_2fa"])

    async def test_browser_endpoint_rejects_two_factor_activation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app(base_dir=Path(temp_dir))
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post(
                    "/api/browser/fetch-selected",
                    json={
                        "emails": ["one@icloud.com"],
                        "concurrency": 1,
                        "enable_2fa": True,
                    },
                    headers={"X-Local-Token": app["local_token"]},
                )
                payload = await response.json()
            finally:
                await client.close()

        self.assertEqual(response.status, 400)
        self.assertEqual(payload["error"], "已停用新账号的 2FA 设置")

    async def test_bulk_verification_uses_headless_browser_batch(self):
        class VerificationManagerStub:
            def __init__(self):
                self.starts = []

            def snapshot(self):
                return {"running": False}

            def start_with_browser(
                self, *, emails, concurrency, force_refresh=False
            ):
                self.starts.append({"emails": emails, "concurrency": concurrency})
                return {
                    "running": True,
                    "headless": True,
                    "concurrency": concurrency,
                    "accounts": [{"email": email} for email in emails],
                }

            async def close(self):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app(base_dir=Path(temp_dir))
            manager = VerificationManagerStub()
            app["verification_manager"] = manager
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post(
                    "/api/account-verification/start",
                    json={
                        "concurrency": 4,
                        "emails": ["ONE@icloud.com", "two@icloud.com"],
                    },
                    headers={"X-Local-Token": app["local_token"]},
                )
                payload = await response.json()
            finally:
                await client.close()

        self.assertEqual(response.status, 200)
        self.assertTrue(payload["task"]["headless"])
        self.assertEqual(
            manager.starts,
            [
                {
                    "emails": ["one@icloud.com", "two@icloud.com"],
                    "concurrency": 4,
                }
            ],
        )

    async def test_verify_reuses_valid_session_or_relogs_when_missing(self):
        class ManagerStub:
            def __init__(self, *, allow_protocol=False, allow_verify=False):
                self.allow_protocol = allow_protocol
                self.allow_verify = allow_verify
                self.protocol_emails = []
                self.browser_refresh_starts = []
                self.verify_starts = []
                self.browser_starts = 0

            def snapshot(self):
                return {"running": False}

            def start_protocol_relogin(self, *, email, headless=False):
                if not self.allow_protocol:
                    raise AssertionError("protocol relogin called on wrong manager")
                self.protocol_emails.append(email)
                return {"running": True, "accounts": [{"email": email}]}

            def start_with_browser(
                self, *, emails, concurrency, force_refresh=False
            ):
                if not self.allow_protocol:
                    raise AssertionError("browser refresh called on wrong manager")
                self.browser_refresh_starts.append(
                    {
                        "emails": emails,
                        "concurrency": concurrency,
                        "force_refresh": force_refresh,
                    }
                )
                return {"running": True, "accounts": [{"email": emails[0]}]}

            def start(self, *_args, **kwargs):
                if self.allow_verify:
                    self.verify_starts.append(kwargs)
                    return {"running": True, "accounts": []}
                self.browser_starts += 1
                raise AssertionError("browser must not start during account verification")

            async def close(self):
                return None

        class FakeHideMyEmail:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def list_email(self):
                return {
                    "success": True,
                    "result": {
                        "hmeEmails": [
                            {
                                "hme": "protocol@icloud.com",
                                "anonymousId": "protocol-id",
                                "isActive": True,
                            }
                        ]
                    },
                }

        async def run_case(
            *,
            has_valid_session,
            marked_invalid=False,
            refresh_with_cookie=False,
            email="protocol@icloud.com",
        ):
            with tempfile.TemporaryDirectory() as temp_dir:
                base_dir = Path(temp_dir)
                (base_dir / "cookies.txt").write_text("cookie", encoding="utf-8")
                (base_dir / "inbox_config.json").write_text("{}\n", encoding="utf-8")
                app = create_app(base_dir=base_dir)
                if has_valid_session:
                    _save_account_record(
                        app["db_file"],
                        email,
                        result={
                            "access_token": "valid-session-token",
                            "session_json": '{"accessToken":"valid-session-token"}',
                            "cookies_json": json.dumps(
                                [
                                    {
                                        "name": "session",
                                        "value": "saved-cookie",
                                        "domain": "chatgpt.com",
                                        "path": "/",
                                    }
                                ]
                            ),
                        },
                    )
                    if marked_invalid:
                        mark_account_session_invalid(
                            app["db_file"],
                            email,
                            "online endpoint returned 401",
                        )
                verification_manager = ManagerStub(
                    allow_protocol=True,
                    allow_verify=True,
                )
                browser_manager = ManagerStub()
                app["verification_manager"] = verification_manager
                app["browser_manager"] = browser_manager
                client = TestClient(TestServer(app))
                with (
                    mock.patch(
                        "hidemyemail_generator.webapp.RichHideMyEmail",
                        FakeHideMyEmail,
                    ),
                    mock.patch(
                        "hidemyemail_generator.webapp.access_token_is_expired",
                        return_value=False,
                    ),
                ):
                    await client.start_server()
                    try:
                        response = await client.post(
                            "/api/account/verify-or-register",
                            json={
                                "email": email,
                                "headless": False,
                                "reset_password": False,
                                "refresh_with_cookie": refresh_with_cookie,
                            },
                            headers={"X-Local-Token": app["local_token"]},
                        )
                        payload = await response.json()
                    finally:
                        await client.close()
                return response, payload, verification_manager, browser_manager

        response, payload, verification_manager, browser_manager = await run_case(
            has_valid_session=True
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["mode"], "verify")
        self.assertEqual(
            verification_manager.verify_starts,
            [{"concurrency": 1, "emails": ["protocol@icloud.com"]}],
        )
        self.assertEqual(verification_manager.protocol_emails, [])
        self.assertEqual(verification_manager.browser_refresh_starts, [])
        self.assertEqual(browser_manager.browser_starts, 0)

        response, payload, verification_manager, browser_manager = await run_case(
            has_valid_session=False
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["mode"], "refresh_session")
        self.assertEqual(verification_manager.protocol_emails, [])
        self.assertEqual(
            verification_manager.browser_refresh_starts,
            [
                {
                    "emails": ["protocol@icloud.com"],
                    "concurrency": 1,
                    "force_refresh": False,
                }
            ],
        )
        self.assertEqual(verification_manager.verify_starts, [])
        self.assertEqual(browser_manager.browser_starts, 0)

        response, payload, verification_manager, browser_manager = await run_case(
            has_valid_session=True,
            refresh_with_cookie=True,
            email="protocol@gmail.com",
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["mode"], "refresh_cookie")
        self.assertEqual(
            verification_manager.browser_refresh_starts,
            [
                {
                    "emails": ["protocol@gmail.com"],
                    "concurrency": 1,
                    "force_refresh": True,
                }
            ],
        )

        response, payload, verification_manager, browser_manager = await run_case(
            has_valid_session=True,
            marked_invalid=True,
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["mode"], "refresh_session")
        self.assertEqual(
            verification_manager.browser_refresh_starts,
            [
                {
                    "emails": ["protocol@icloud.com"],
                    "concurrency": 1,
                    "force_refresh": False,
                }
            ],
        )
        self.assertEqual(verification_manager.verify_starts, [])
        self.assertEqual(browser_manager.browser_starts, 0)

        response, payload, verification_manager, browser_manager = await run_case(
            has_valid_session=True,
            refresh_with_cookie=True,
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["mode"], "refresh_cookie")
        self.assertEqual(
            verification_manager.browser_refresh_starts,
            [
                {
                    "emails": ["protocol@icloud.com"],
                    "concurrency": 1,
                    "force_refresh": True,
                }
            ],
        )
        self.assertEqual(verification_manager.verify_starts, [])
        self.assertEqual(browser_manager.browser_starts, 0)


if __name__ == "__main__":
    unittest.main()
