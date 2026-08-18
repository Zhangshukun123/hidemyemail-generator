import asyncio
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import unquote, urlsplit

from aiohttp.test_utils import TestClient, TestServer
from rich.console import Console

from hidemyemail_generator.account_verifier import mark_account_session_invalid
from hidemyemail_generator.browser_tasks import (
    _save_account_record,
    load_account_record,
)
from hidemyemail_generator.card_link_proxy_resolver import (
    CardLinkProxyResolutionError,
    CardLinkProxyResolver,
)
from hidemyemail_generator.inbox import (
    InboxConfig,
    connect_db,
    insert_message,
    save_config,
)
from hidemyemail_generator.payment_proxy_pool import PaymentProxyPoolPresenter
from hidemyemail_generator.webapp import (
    CardLinkBridgeError,
    OPENAI_CODE_INBOX_SYNC_LIMIT,
    WORKBENCH_OPENAI_CODE_PATH,
    _configured_inventory_service_token,
    _configured_workbench_import_token,
    _configure_utf8_stdio,
    _generation_failure_message,
    _latest_code_for_email,
    _load_local_env_file,
    _run_card_link_bridge,
    _save_account_card_link,
    create_app,
)
from hidemyemail_generator.liandong_shop import LiandongShopError


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


class CardLinkBridgeStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_bridge_publishes_log_before_process_finishes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bridge = root / "fake_card_link_bridge.py"
            bridge.write_text(
                "import json,time\n"
                "print('HME_CARD_LINK_LOG:' + json.dumps({'message':'legacy live step'}), flush=True)\n"
                "time.sleep(0.25)\n"
                "print('HME_CARD_LINK_EVENT:' + json.dumps({"
                "'status':'success','url':'https://chatgpt.com/checkout/openai_llc/cs_stream',"
                "'country':'US','currency':'USD'}), flush=True)\n",
                encoding="utf-8",
            )
            progress: list[str] = []
            first_log = asyncio.Event()

            def publish(message: str) -> None:
                progress.append(message)
                first_log.set()

            task = asyncio.create_task(
                _run_card_link_bridge(
                    target_project_dir=root,
                    python_executable=Path(sys.executable),
                    bridge_file=bridge,
                    access_token="at-streaming-test",
                    method="standard",
                    country="US",
                    currency="USD",
                    locale="en-US",
                    progress_callback=publish,
                )
            )
            await asyncio.wait_for(first_log.wait(), timeout=2)
            self.assertFalse(task.done())
            result = await asyncio.wait_for(task, timeout=2)

        self.assertEqual(progress, ["legacy live step"])
        self.assertEqual(result["logs"], ["legacy live step"])


class CardLinkProxyResolverTests(unittest.TestCase):
    def test_discards_wrong_country_before_returning_matching_candidate(self):
        candidates = iter(["http://candidate-one", "http://candidate-two"])
        observed = iter(["NL", "GB"])
        resolver = CardLinkProxyResolver(
            health_detector=lambda *_args, **_kwargs: SimpleNamespace(
                success=True,
                country=next(observed),
            ),
            max_candidates=3,
        )

        selection = resolver.resolve(lambda: next(candidates), "GB")

        self.assertEqual(selection.proxy_url, "http://candidate-two")
        self.assertEqual(selection.actual_country, "GB")
        self.assertEqual(selection.candidates_tested, 2)
        self.assertEqual(selection.observations, ("NL", "GB"))

    def test_exhaustion_error_reports_countries_without_proxy_credentials(self):
        secret_url = "http://private-user:private-password@proxy.example:1000"
        resolver = CardLinkProxyResolver(
            health_detector=lambda *_args, **_kwargs: SimpleNamespace(
                success=True,
                country="NL",
            ),
            max_candidates=2,
        )

        with self.assertRaises(CardLinkProxyResolutionError) as context:
            resolver.resolve(lambda: secret_url, "GB")

        detail = str(context.exception)
        self.assertIn("连续 2 个 GB", detail)
        self.assertIn("NL、NL", detail)
        self.assertNotIn("private-user", detail)
        self.assertNotIn("private-password", detail)

    def test_discards_previous_real_exit_ip_before_returning_fresh_candidate(self):
        candidates = iter(["http://candidate-one", "http://candidate-two"])
        observed_ips = iter(["192.0.2.10", "192.0.2.20"])
        resolver = CardLinkProxyResolver(
            health_detector=lambda *_args, **_kwargs: SimpleNamespace(
                success=True,
                country="GB",
                ip=next(observed_ips),
            ),
            max_candidates=3,
        )

        selection = resolver.resolve(
            lambda: next(candidates),
            "GB",
            excluded_exit_ips={"192.0.2.10"},
            require_exit_ip=True,
        )

        self.assertEqual(selection.proxy_url, "http://candidate-two")
        self.assertEqual(selection.actual_ip, "192.0.2.20")
        self.assertEqual(selection.candidates_tested, 2)


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


class LiandongShopUploadEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app(base_dir=Path(self.temp_dir.name))
        self.email = "shop-upload@zkgmail.com"
        _save_account_record(
            self.app["db_file"],
            self.email,
            result={
                "access_token": "at-shop-upload",
                "session_json": '{"accessToken":"at-shop-upload"}',
            },
            password="Account-Password",
            password_confirmed=True,
            two_factor={"enabled": True, "secret": "JBSWY3DPEHPK3PXP"},
        )
        self.set_account_fields(account_type="plus")
        self.app["liandong_shop_config"].save("merchant-test-token")
        self.upload_card = mock.AsyncMock(return_value={"code": 1, "message": "ok"})
        self.app["liandong_shop_client"].upload_card = self.upload_card
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp_dir.cleanup()

    def set_account_fields(self, email: str = "", **changes):
        target = email or self.email
        record = load_account_record(self.app["db_file"], target)
        record.update(changes)
        conn = connect_db(str(self.app["db_file"]))
        try:
            conn.execute(
                """
                INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (f"gpt_account:{target}", json.dumps(record)),
            )
            conn.commit()
        finally:
            conn.close()

    async def test_uploads_unbound_account_and_marks_only_after_success(self):
        headers = {"X-Local-Token": self.app["local_token"]}
        response = await self.client.post(
            "/api/account/liandong-shop-upload",
            json={"email": self.email},
            headers=headers,
        )

        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertTrue(payload["uploaded"])
        self.assertEqual(payload["goodsId"], 698207)
        self.assertEqual(payload["goodsLabel"], "未接码商品")
        self.upload_card.assert_awaited_once()
        call = self.upload_card.await_args.kwargs
        self.assertEqual(call["token"], "merchant-test-token")
        self.assertEqual(call["goods"].goods_id, 698207)
        self.assertEqual(
            call["content"],
            f"{self.email}-----------Account-Password----------JBSWY3DPEHPK3PXP",
        )
        stored = load_account_record(self.app["db_file"], self.email)
        self.assertTrue(stored["liandong_shop"]["uploaded"])
        self.assertNotIn("content", stored["liandong_shop"])
        listed = await self.client.get("/api/gpt-emails")
        listed_account = next(
            item
            for item in (await listed.json())["items"]
            if item["email"] == self.email
        )
        self.assertTrue(listed_account["liandongShopUploaded"])
        self.assertEqual(listed_account["liandongShopGoodsLabel"], "未接码商品")

    async def test_uploads_passwordless_account_with_code_portal(self):
        self.set_account_fields(
            password="",
            password_confirmed=False,
            two_factor={},
        )
        with mock.patch.dict(os.environ, {"LIANDONG_SHOP_CODE_URL": ""}):
            response = await self.client.post(
                "/api/account/liandong-shop-upload",
                json={"email": self.email},
                headers={"X-Local-Token": self.app["local_token"]},
            )

        self.assertEqual(response.status, 200)
        self.assertEqual(
            self.upload_card.await_args.kwargs["content"],
            f"{self.email}--------https://icloud-code.8-208-13-52.sslip.io/code",
        )

    async def test_routes_phone_bound_account_to_bound_goods(self):
        self.set_account_fields(plus_sms={"status": "completed", "phone_bound": True})
        response = await self.client.post(
            "/api/account/liandong-shop-upload",
            json={"email": self.email},
            headers={"X-Local-Token": self.app["local_token"]},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["goodsId"], 685418)
        self.assertEqual(self.upload_card.await_args.kwargs["goods"].goods_id, 685418)

    async def test_existing_manual_upload_marker_is_never_uploaded_again(self):
        self.set_account_fields(
            liandong_shop={
                "uploaded": True,
                "uploaded_at": "2026-08-17T00:00:00+00:00",
            }
        )
        response = await self.client.post(
            "/api/account/liandong-shop-upload",
            json={"email": self.email},
            headers={"X-Local-Token": self.app["local_token"]},
        )

        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["alreadyUploaded"])
        self.upload_card.assert_not_awaited()

    async def test_failed_shop_response_does_not_mark_account(self):
        self.upload_card.side_effect = LiandongShopError("小铺库存添加失败")
        response = await self.client.post(
            "/api/account/liandong-shop-upload",
            json={"email": self.email},
            headers={"X-Local-Token": self.app["local_token"]},
        )

        self.assertEqual(response.status, 502)
        record = load_account_record(self.app["db_file"], self.email)
        self.assertFalse(record.get("liandong_shop", {}).get("uploaded", False))

    async def test_status_and_config_never_return_merchant_token(self):
        status = await self.client.get("/api/liandong-shop/status")
        status_payload = await status.json()
        self.assertTrue(status_payload["configured"])
        self.assertNotIn("merchant-test-token", json.dumps(status_payload))

        configured = await self.client.post(
            "/api/liandong-shop/config",
            json={"merchantToken": "replacement-secret-token"},
            headers={"X-Local-Token": self.app["local_token"]},
        )
        configured_payload = await configured.json()
        self.assertEqual(configured.status, 200)
        self.assertNotIn("replacement-secret-token", json.dumps(configured_payload))

    async def test_rejects_upload_when_account_is_not_plus(self):
        self.set_account_fields(account_type="free")
        response = await self.client.post(
            "/api/account/liandong-shop-upload",
            json={"email": self.email},
            headers={"X-Local-Token": self.app["local_token"]},
        )

        self.assertEqual(response.status, 409)
        self.assertIn("Plus", (await response.json())["error"])
        self.upload_card.assert_not_awaited()


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
        self.proxy_health_calls = []

        def proxy_health_detector(proxy_url, **_kwargs):
            self.proxy_health_calls.append(proxy_url)
            decoded = unquote(proxy_url)
            country_match = re.search(
                r"(?:region-|[-])([A-Z]{2})(?:-sid-|-)",
                decoded,
                re.IGNORECASE,
            )
            country = country_match.group(1).upper() if country_match else "US"
            return SimpleNamespace(
                success=True,
                country=country,
                ip=f"192.0.2.{len(self.proxy_health_calls) + 20}",
            )

        proxy_resolver = CardLinkProxyResolver(
            health_detector=proxy_health_detector,
            max_candidates=3,
        )
        self.app["card_link_proxy_resolver"] = proxy_resolver
        self.app["payment_proxy_pool_presenter"] = PaymentProxyPoolPresenter(
            proxy_resolver
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

    def save_setting(self, key, value):
        connection = connect_db(str(self.app["db_file"]))
        try:
            connection.execute(
                """
                INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, json.dumps(value)),
            )
            connection.commit()
        finally:
            connection.close()

    def set_account_fields(self, email, **fields):
        record = load_account_record(self.app["db_file"], email)
        record.update(fields)
        self.save_setting(f"gpt_account:{email}", record)

    async def test_plus_account_skips_card_link_and_paypal_payment_endpoints(self):
        email = "card-link@icloud.com"
        self.set_account_fields(email, account_type="plus")
        bridge = mock.AsyncMock()
        create_job = mock.AsyncMock()

        with (
            mock.patch(
                "hidemyemail_generator.webapp._run_card_link_bridge",
                new=bridge,
            ),
            mock.patch.object(self.app["paypal_service"], "create_job", create_job),
        ):
            card_link_response = await self.client.post(
                "/api/account/card-link",
                json={"email": email, "country": "US"},
                headers={"X-Local-Token": self.app["local_token"]},
            )
            payment_response = await self.client.post(
                "/api/account/paypal-payment",
                json={"email": email},
                headers={"X-Local-Token": self.app["local_token"]},
            )

        self.assertEqual(card_link_response.status, 409)
        self.assertEqual(payment_response.status, 409)
        self.assertIn("已是 Plus", (await card_link_response.json())["error"])
        self.assertIn("无需再次提链支付", (await payment_response.json())["error"])
        bridge.assert_not_awaited()
        create_job.assert_not_awaited()

    async def test_account_list_projects_legacy_phone_binding_as_bound(self):
        email = "card-link@icloud.com"
        self.set_account_fields(
            email,
            account_type="plus",
            plus_codex={"status": "failed", "sms_verified": False},
            plus_sms={"status": "completed", "phone_bound": True},
        )

        response = await self.client.get("/api/gpt-emails")
        item = next(
            entry
            for entry in (await response.json())["items"]
            if entry["email"] == email
        )

        self.assertTrue(item["plusPhoneBound"])
        self.assertEqual(item["plusPhoneBindingStatus"], "completed")

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

    async def test_card_link_progress_is_available_while_post_is_still_running(self):
        generated = {
            "status": "success",
            "url": "https://chatgpt.com/checkout/openai_llc/cs_live_progress",
            "country": "US",
            "currency": "USD",
            "logs": ["步骤 1/2：已创建 Checkout", "步骤 2/2：已生成链接"],
        }
        bridge_started = asyncio.Event()
        release_bridge = asyncio.Event()

        async def bridge(**kwargs):
            publish = kwargs["progress_callback"]
            publish("步骤 1/2：已创建 Checkout")
            bridge_started.set()
            await release_bridge.wait()
            publish("步骤 2/2：已生成链接")
            return generated

        with (
            mock.patch(
                "hidemyemail_generator.webapp.access_token_is_expired",
                return_value=False,
            ),
            mock.patch(
                "hidemyemail_generator.webapp._run_card_link_bridge",
                new=mock.AsyncMock(side_effect=bridge),
            ),
        ):
            post_task = asyncio.create_task(
                self.client.post(
                    "/api/account/card-link",
                    json={
                        "email": "card-link@icloud.com",
                        "country": "US",
                        "progress_id": "cardlink-live-test",
                    },
                    headers={"X-Local-Token": self.app["local_token"]},
                )
            )
            await asyncio.wait_for(bridge_started.wait(), timeout=2)

            progress_response = await self.client.get(
                "/api/account/card-link/progress/cardlink-live-test?log_after=0",
                headers={"X-Local-Token": self.app["local_token"]},
            )
            progress = await progress_response.json()
            self.assertFalse(post_task.done())
            self.assertEqual(progress_response.status, 200)
            self.assertTrue(progress["running"])
            self.assertEqual(
                [item["message"] for item in progress["logs"]],
                ["步骤 1/2：已创建 Checkout"],
            )

            release_bridge.set()
            response = await post_task
            self.assertEqual(response.status, 200)
            final_response = await self.client.get(
                "/api/account/card-link/progress/cardlink-live-test?log_after=1",
                headers={"X-Local-Token": self.app["local_token"]},
            )
            final_progress = await final_response.json()
            self.assertFalse(final_progress["running"])
            self.assertEqual(final_progress["status"], "completed")
            self.assertEqual(
                [item["message"] for item in final_progress["logs"]],
                ["步骤 2/2：已生成链接"],
            )

    async def test_direct_card_link_failure_returns_bridge_step_logs(self):
        bridge_error = CardLinkBridgeError(
            "chatgpt approve result: 'blocked' (request_id=test-EWR)",
            logs=[
                "[PayPal US] 步骤 2/7：第一代理创建 US/USD Checkout",
                "[PayPal US] 步骤 6/7：正在提交 Confirm 并读取 PayPal Approve 跳转",
            ],
        )
        with (
            mock.patch(
                "hidemyemail_generator.webapp.access_token_is_expired",
                return_value=False,
            ),
            mock.patch(
                "hidemyemail_generator.webapp._run_card_link_bridge",
                new=mock.AsyncMock(side_effect=bridge_error),
            ),
        ):
            response = await self.client.post(
                "/api/account/card-link",
                json={
                    "email": "card-link@icloud.com",
                    "method": "paypal_us",
                    "target_amount": "0",
                    "create_proxy": "create.example:8000:user:pass",
                    "promotion_proxy": "followup.example:9000:user:pass",
                },
                headers={"X-Local-Token": self.app["local_token"]},
            )

        payload = await response.json()
        self.assertEqual(response.status, 502)
        self.assertIn("approve result: 'blocked'", payload["error"])
        self.assertEqual(len(payload["logs"]), 2)
        self.assertIn("步骤 6/7", payload["logs"][1])

    async def test_generates_paypal_link_for_zkgmail_account(self):
        email = "paypal-link@zkgmail.com"
        _save_account_record(
            self.app["db_file"],
            email,
            result={
                "access_token": "at-zkgmail-card-link",
                "session_json": '{"accessToken":"at-zkgmail-card-link"}',
            },
        )
        generated = {
            "status": "success",
            "url": "https://www.paypal.com/agreements/approve?ba_token=zkgmail_link",
            "method": "paypal_us",
            "country": "US",
            "currency": "USD",
            "link_proxy_country": "US",
            "link_proxy_ip": "203.0.113.35",
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
                    "email": email,
                    "method": "paypal_us",
                    "create_proxy": "create.example:8000:user:pass",
                    "promotion_proxy": "followup.example:9000:user:pass",
                },
                headers={"X-Local-Token": self.app["local_token"]},
            )

        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(bridge.await_args.kwargs["account_email"], email)
        saved = load_account_record(self.app["db_file"], email)["card_link"]
        self.assertEqual(saved["method"], "paypal_us")
        self.assertEqual(saved["link_proxy_country"], "US")

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
            link_proxy_country="TH",
            link_proxy_ip="203.0.113.31",
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
                json={
                    "email": "card-link@icloud.com",
                },
                headers={"X-Local-Token": self.app["local_token"]},
            )

        payload = await response.json()
        protocol = created.await_args.args[0]
        self.assertEqual(response.status, 201)
        self.assertEqual(payload["country"], "TH")
        self.assertEqual(payload["smsProvider"], "smsbower")
        self.assertEqual(protocol["source_account_email"], "card-link@icloud.com")
        self.assertEqual(protocol["account_cookies"], cookies)
        self.assertEqual(protocol["completion_target"], "openai_plus")
        self.assertFalse(protocol["post_payment_phone_binding"])
        self.assertFalse(payload["postPaymentPhoneBinding"])
        self.assertEqual(protocol["country"], "TH")
        self.assertEqual(protocol["sms_provider"], "smsbower")
        self.assertEqual(len(protocol["proxy_pool"]), 3)
        self.assertEqual(len(set(protocol["proxy_pool"])), 3)
        self.assertEqual(payload["proxyCandidateCount"], 3)
        self.assertEqual(payload["proxyBackupCount"], 2)
        self.assertEqual(payload["proxyExitCount"], 3)
        payment_exits = self.app[
            "registration_proxy_store"
        ].last_payment_exit_ips()
        self.assertEqual(len(payment_exits), 3)
        self.assertNotIn("203.0.113.31", payment_exits)
        self.assertIn("-region-TH-sid-", unquote(urlsplit(protocol["proxy_pool"][0]).username or ""))
        set_cookies = "\n".join(response.headers.getall("Set-Cookie", []))
        self.assertIn("paypal_web_device_id=", set_cookies)
        self.assertIn("hme_paypal_auto_device_id=", set_cookies)

    async def test_missing_paypal_job_releases_account_payment_guard(self):
        email = "card-link@icloud.com"
        job_id = "payment-job-missing"
        guard = self.app["account_payment_guard"]
        self.assertTrue(await guard.reserve(email))
        await guard.started(email, job_id)
        self.assertEqual(await guard.active_emails(), {email})
        active_response = await self.client.get("/api/gpt-emails")
        self.assertEqual(active_response.status, 200)
        active_items = (await active_response.json())["items"]
        active_item = next(item for item in active_items if item["email"] == email)
        self.assertTrue(active_item["accountPaymentRunning"])
        self.client.session.cookie_jar.update_cookies(
            {"hme_paypal_auto_device_id": "a" * 32},
            response_url=self.client.make_url("/"),
        )
        missing = mock.AsyncMock(return_value=(404, {"error": "任务不存在"}))

        with mock.patch.object(self.app["paypal_service"], "get_job", missing):
            response = await self.client.get(
                f"/api/account/paypal-payment/{job_id}"
            )

        self.assertEqual(response.status, 404)
        self.assertEqual((await response.json())["error"], "任务不存在")
        self.assertEqual(await guard.active_emails(), set())
        released_response = await self.client.get("/api/gpt-emails")
        released_items = (await released_response.json())["items"]
        released_item = next(item for item in released_items if item["email"] == email)
        self.assertFalse(released_item["accountPaymentRunning"])

        self.assertTrue(await guard.reserve(email))
        await guard.started(email, job_id)
        cancel_missing = mock.AsyncMock(
            return_value=(404, {"error": "任务不存在"})
        )
        with mock.patch.object(
            self.app["paypal_service"], "cancel_job", cancel_missing
        ):
            cancel_response = await self.client.post(
                f"/api/account/paypal-payment/{job_id}/cancel",
                headers={"X-Local-Token": self.app["local_token"]},
            )

        self.assertEqual(cancel_response.status, 404)
        self.assertEqual(await guard.active_emails(), set())

    async def test_global_sms_config_endpoint_persists_routing_without_leaking_keys(self):
        response = await self.client.post(
            "/api/payment-sms/config",
            headers={"X-Local-Token": self.app["local_token"]},
            json={
                "binding": {
                    "provider": "hero-sms",
                    "maxPrice": 0.071,
                    "countries": ["US", "CL"],
                },
                "paypal": {
                    "provider": "smsbower",
                    "maxPrice": 0.123,
                    "countries": ["GB", "US"],
                },
                "apiKeys": {
                    "smsbower": "smsbower-api-secret",
                    "hero-sms": "hero-api-secret",
                },
            },
        )
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["routing"]["binding"]["provider"], "hero-sms")
        self.assertEqual(payload["routing"]["binding"]["countries"], ["US", "CL"])
        self.assertEqual(payload["routing"]["paypal"]["maxPrice"], 0.123)
        self.assertEqual(payload["routing"]["paypal"]["countries"], ["GB", "US"])
        self.assertNotIn("api-secret", json.dumps(payload))

        status = await self.client.get("/api/payment-sms/status")
        status_payload = await status.json()
        self.assertEqual(status.status, 200)
        self.assertEqual(status_payload["routing"]["binding"]["provider"], "hero-sms")
        self.assertNotIn("api-secret", json.dumps(status_payload))

    async def test_paypal_country_outside_global_sms_allowlist_is_rejected(self):
        email = "gb-paypal-country-block@zkgmail.com"
        _save_account_record(
            self.app["db_file"],
            email,
            result={"cookies_json": '[{"name":"session","value":"cookie"}]'},
        )
        _save_account_card_link(
            self.app["db_file"],
            email,
            url="https://www.paypal.com/agreements/approve?ba_token=gb_blocked",
            country="GB",
            currency="GBP",
            method="paypal_gb",
            link_proxy_country="GB",
            link_proxy_ip="203.0.113.39",
        )
        self.app["sms_routing_config_store"].configure(
            {
                "paypal": {
                    "provider": "smsbower",
                    "maxPrice": 0.123,
                    "countries": ["US"],
                },
                "apiKeys": {"smsbower": "smsbower-test-key"},
            }
        )
        created = mock.AsyncMock()

        with mock.patch.object(self.app["paypal_service"], "create_job", created):
            response = await self.client.post(
                "/api/account/paypal-payment",
                json={"email": email},
                headers={"X-Local-Token": self.app["local_token"]},
            )
        payload = await response.json()

        self.assertEqual(response.status, 409)
        self.assertIn("PayPal 国家 GB 未在全局接码配置中启用", payload["error"])
        created.assert_not_awaited()

    async def test_one_click_us_paypal_uses_global_sms_budget(self):
        email = "us-paypal-phone@zkgmail.com"
        _save_account_record(
            self.app["db_file"],
            email,
            result={"cookies_json": '[{"name":"session","value":"cookie"}]'},
        )
        _save_account_card_link(
            self.app["db_file"],
            email,
            url="https://www.paypal.com/agreements/approve?ba_token=us_phone",
            country="US",
            currency="USD",
            method="paypal_us",
            link_proxy_country="US",
            link_proxy_ip="203.0.113.36",
        )
        self.app["card_link_proxy_store"].configure(
            enabled=True,
            mode="dynamic",
            country="US",
            proxy_line="extract.example:3010:extract-user:extract-password",
            card_link_modes={"paypal_us": "dynamic"},
        )
        self.app["sms_routing_config_store"].configure(
            {
                "paypal": {
                    "provider": "smsbower",
                    "maxPrice": 0.123,
                    "countries": ["US"],
                },
                "apiKeys": {"smsbower": "smsbower-test-key"},
            }
        )
        created = mock.AsyncMock(return_value=(201, {"job": {"id": "pay-us-phone"}}))
        with mock.patch.object(self.app["paypal_service"], "create_job", created):
            response = await self.client.post(
                "/api/account/paypal-payment",
                json={"email": email},
                headers={"X-Local-Token": self.app["local_token"]},
            )

        payload = await response.json()
        protocol = created.await_args.args[0]
        self.assertEqual(response.status, 201)
        self.assertEqual(protocol["country"], "US")
        self.assertEqual(protocol["sms_provider"], "smsbower")
        self.assertEqual(protocol["sms_service"], "paypal")
        self.assertEqual(protocol["sms_country"], "US")
        self.assertEqual(protocol["sms_max_price"], 0.123)
        self.assertEqual(payload["smsCountry"], "US")
        self.assertEqual(payload["smsService"], "paypal")
        self.assertEqual(payload["smsServiceCode"], "ts")
        self.assertEqual(payload["smsMaxPrice"], 0.123)
        self.assertTrue(payload["smsVirtualAllowed"])

    async def test_one_click_paypal_uses_configured_hero_sms_provider(self):
        email = "hero-paypal-phone@zkgmail.com"
        _save_account_record(
            self.app["db_file"],
            email,
            result={"cookies_json": '[{"name":"session","value":"cookie"}]'},
        )
        _save_account_card_link(
            self.app["db_file"],
            email,
            url="https://www.paypal.com/agreements/approve?ba_token=hero_phone",
            country="US",
            currency="USD",
            method="paypal_us",
            link_proxy_country="US",
            link_proxy_ip="203.0.113.38",
        )
        self.app["card_link_proxy_store"].configure(
            enabled=True,
            mode="dynamic",
            country="US",
            proxy_line="extract.example:3010:extract-user:extract-password",
            card_link_modes={"paypal_us": "dynamic"},
        )
        self.app["sms_routing_config_store"].configure(
            {
                "paypal": {
                    "provider": "hero-sms",
                    "maxPrice": 0.456,
                    "countries": ["US"],
                },
                "apiKeys": {"hero-sms": "hero-test-key"},
            }
        )
        created = mock.AsyncMock(
            return_value=(201, {"job": {"id": "pay-hero-phone"}})
        )

        with mock.patch.object(self.app["paypal_service"], "create_job", created):
            response = await self.client.post(
                "/api/account/paypal-payment",
                json={"email": email},
                headers={"X-Local-Token": self.app["local_token"]},
            )

        payload = await response.json()
        protocol = created.await_args.args[0]
        self.assertEqual(response.status, 201)
        self.assertEqual(protocol["sms_provider"], "hero-sms")
        self.assertEqual(protocol["sms_max_price"], 0.456)
        self.assertEqual(payload["smsProvider"], "hero-sms")
        self.assertEqual(payload["smsProviderLabel"], "HeroSMS")
        self.assertFalse(payload["smsVirtualAllowed"])
        status = await self.client.get("/api/payment-sms/status")
        status_payload = await status.json()
        self.assertTrue(status_payload["configured"])
        self.assertEqual(status_payload["provider"], "hero-sms")
        self.assertEqual(status_payload["timeoutSeconds"], 60)

    async def test_one_click_gb_paypal_uses_gb_proxy_phone_and_budget(self):
        email = "gb-paypal-phone@zkgmail.com"
        _save_account_record(
            self.app["db_file"],
            email,
            result={"cookies_json": '[{"name":"session","value":"cookie"}]'},
        )
        _save_account_card_link(
            self.app["db_file"],
            email,
            url="https://www.paypal.com/agreements/approve?ba_token=gb_phone",
            country="GB",
            currency="GBP",
            method="paypal_gb",
            link_proxy_country="GB",
            link_proxy_ip="203.0.113.37",
        )
        self.app["card_link_proxy_store"].configure(
            enabled=True,
            mode="dynamic",
            country="GB",
            proxy_line="extract.example:3010:extract-user:extract-password",
            card_link_modes={"paypal_gb": "dynamic"},
        )
        self.app["smsbower_config_store"].configure(
            api_key="smsbower-test-key",
            max_price=0.05,
        )
        created = mock.AsyncMock(return_value=(201, {"job": {"id": "pay-gb-phone"}}))
        with mock.patch.object(self.app["paypal_service"], "create_job", created):
            response = await self.client.post(
                "/api/account/paypal-payment",
                json={"email": email},
                headers={"X-Local-Token": self.app["local_token"]},
            )

        payload = await response.json()
        protocol = created.await_args.args[0]
        self.assertEqual(response.status, 201)
        self.assertEqual(protocol["country"], "GB")
        self.assertEqual(protocol["sms_provider"], "smsbower")
        self.assertEqual(protocol["sms_service"], "paypal")
        self.assertEqual(protocol["sms_country"], "GB")
        self.assertEqual(protocol["sms_max_price"], 0.30)
        self.assertEqual(payload["checkoutCountry"], "GB")
        self.assertEqual(payload["linkProxyCountry"], "GB")
        self.assertEqual(payload["smsCountry"], "GB")
        self.assertEqual(payload["smsService"], "paypal")
        self.assertEqual(payload["smsServiceCode"], "ts")
        self.assertEqual(payload["smsMaxPrice"], 0.30)
        self.assertFalse(payload["smsVirtualAllowed"])

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
            link_proxy_country="TH",
            link_proxy_ip="203.0.113.32",
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

    async def test_one_click_paypal_requires_recorded_link_proxy_country(self):
        _save_account_record(
            self.app["db_file"],
            "card-link@icloud.com",
            result={"cookies_json": '[{"name":"session","value":"cookie"}]'},
        )
        _save_account_card_link(
            self.app["db_file"],
            "card-link@icloud.com",
            url="https://www.paypal.com/agreements/approve?ba_token=legacy_link",
            country="DE",
            currency="EUR",
            method="de_oaics_paypal",
        )
        created = mock.AsyncMock()
        with mock.patch.object(self.app["paypal_service"], "create_job", created):
            response = await self.client.post(
                "/api/account/paypal-payment",
                json={"email": "card-link@icloud.com"},
                headers={"X-Local-Token": self.app["local_token"]},
            )

        payload = await response.json()
        self.assertEqual(response.status, 409)
        self.assertIn("缺少真实出口国家", payload["error"])
        created.assert_not_awaited()

    async def test_one_click_paypal_prefers_card_link_proxy_for_link_country(self):
        _save_account_record(
            self.app["db_file"],
            "card-link@icloud.com",
            result={"cookies_json": '[{"name":"session","value":"cookie"}]'},
        )
        _save_account_card_link(
            self.app["db_file"],
            "card-link@icloud.com",
            url="https://www.paypal.com/agreements/approve?ba_token=de_auto_proxy",
            country="DE",
            currency="EUR",
            method="de_oaics_paypal",
            link_proxy_country="BR",
            link_proxy_ip="203.0.113.33",
        )
        self.app["card_link_proxy_store"].configure(
            enabled=True,
            mode="dynamic",
            country="BR",
            proxy_line="extract.example:3010:extract-user:extract-password",
            card_link_modes={"de_oaics_paypal": "dynamic"},
        )
        self.app["registration_proxy_store"].configure(
            enabled=True,
            mode="dynamic",
            country="BR",
            proxy_line="register.example:3010:register-user:register-password",
        )
        self.app["smsbower_config_store"].configure(api_key="smsbower-test-key")
        created = mock.AsyncMock(return_value=(201, {"job": {"id": "pay-de-auto"}}))
        with mock.patch.object(self.app["paypal_service"], "create_job", created):
            response = await self.client.post(
                "/api/account/paypal-payment",
                json={"email": "card-link@icloud.com"},
                headers={"X-Local-Token": self.app["local_token"]},
            )

        payload = await response.json()
        proxy_url = created.await_args.args[0]["proxy_pool"][0]
        self.assertEqual(response.status, 201)
        self.assertEqual(payload["proxySource"], "card_link")
        self.assertEqual(payload["country"], "BR")
        self.assertEqual(payload["checkoutCountry"], "DE")
        self.assertEqual(payload["linkProxyCountry"], "BR")
        self.assertEqual(created.await_args.args[0]["country"], "BR")
        self.assertEqual(urlsplit(proxy_url).hostname, "extract.example")
        self.assertIn("extract-user-region-BR-sid-", unquote(urlsplit(proxy_url).username or ""))

    async def test_registration_save_callbacks_only_sync_inventory(self):
        process = self.app["registration_manager"].process_factory()
        callbacks = (
            self.app["browser_manager"].on_account_saved,
            self.app["protocol_registration_manager"].on_account_saved,
            process.browser_manager.on_account_saved,
        )
        bridge = mock.AsyncMock()

        with mock.patch(
            "hidemyemail_generator.webapp._run_card_link_bridge",
            new=bridge,
        ):
            for callback in callbacks:
                self.assertIsNotNone(callback)
                self.assertEqual(callback.__name__, "sync_saved_account_to_remote")
                await callback("card-link@icloud.com")

        bridge.assert_not_awaited()
        saved = load_account_record(self.app["db_file"], "card-link@icloud.com")
        self.assertNotIn("registration_checkout_probe", saved)

    async def test_legacy_checkout_probe_is_not_exposed_or_retryable(self):
        record = load_account_record(self.app["db_file"], "card-link@icloud.com")
        record["registration_checkout_probe"] = {
            "status": "verified",
            "checkout_id_type": "cs_live",
            "is_oaics": False,
        }
        conn = connect_db(str(self.app["db_file"]))
        try:
            conn.execute(
                "UPDATE settings SET value = ? WHERE key = ?",
                (
                    json.dumps(record, ensure_ascii=False),
                    "gpt_account:card-link@icloud.com",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        listed = await self.client.get("/api/gpt-emails")
        payload = await listed.json()
        item = next(
            row for row in payload["items"]
            if row["email"] == "card-link@icloud.com"
        )
        retry = await self.client.post(
            "/api/account/checkout-probe",
            json={"email": "card-link@icloud.com"},
            headers={"X-Local-Token": self.app["local_token"]},
        )

        self.assertEqual(listed.status, 200)
        self.assertNotIn("checkoutProbeStatus", item)
        self.assertNotIn("checkoutIdType", item)
        self.assertNotIn("checkoutIsOaics", item)
        self.assertEqual(retry.status, 404)

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

    async def test_us_billing_country_failure_retries_with_fresh_first_proxy(self):
        self.app["card_link_proxy_store"].configure(
            enabled=True,
            mode="dynamic",
            country="US",
            proxy_line="extract.example:3010:extract-user:extract-password",
        )
        mismatch = CardLinkBridgeError(
            'checkout create failed: HTTP 400 {"detail":'
            '"Billing country must match request country."}',
            logs=["[PayPal US] 步骤 2/7：第一代理创建 US/USD Checkout"],
            retryable=True,
        )
        generated = {
            "status": "success",
            "url": "https://www.paypal.com/agreements/approve?ba_token=retry_country",
            "method": "paypal_us",
            "country": "US",
            "currency": "USD",
            "link_proxy_country": "US",
            "link_proxy_ip": "203.0.113.40",
            "logs": ["[PayPal US] 步骤 7/7：PayPal 跳转链接提取完成"],
        }
        bridge = mock.AsyncMock(side_effect=[mismatch, generated])
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
                    "method": "paypal_us",
                    "proxy_mode": "dynamic",
                    "create_proxy_country": "US",
                    "promotion_proxy_country": "US",
                    "attempt_limit": 3,
                },
                headers={"X-Local-Token": self.app["local_token"]},
            )

        payload = await response.json()
        calls = bridge.await_args_list
        first_proxy = calls[0].kwargs["create_proxy_url"]
        second_proxy = calls[1].kwargs["create_proxy_url"]
        self.assertEqual(response.status, 200)
        self.assertEqual(bridge.await_count, 2)
        self.assertEqual(payload["attemptCount"], 2)
        self.assertNotEqual(first_proxy, second_proxy)
        self.assertIn("-region-US-sid-", unquote(urlsplit(first_proxy).username or ""))
        self.assertIn("-region-US-sid-", unquote(urlsplit(second_proxy).username or ""))
        self.assertEqual(calls[0].kwargs["promotion_proxy_url"], first_proxy)
        self.assertEqual(calls[1].kwargs["promotion_proxy_url"], second_proxy)
        self.assertTrue(any("提链重试" in item for item in payload["logs"]))

    async def test_us_non_retryable_failure_stops_before_attempt_limit(self):
        bridge = mock.AsyncMock(
            side_effect=CardLinkBridgeError(
                "authentication token has been invalidated",
                retryable=False,
            )
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
            response = await self.client.post(
                "/api/account/card-link",
                json={
                    "email": "card-link@icloud.com",
                    "method": "paypal_us",
                    "create_proxy": "create.example:8000:user:pass",
                    "attempt_limit": 4,
                },
                headers={"X-Local-Token": self.app["local_token"]},
            )

        payload = await response.json()
        self.assertEqual(response.status, 502)
        self.assertEqual(bridge.await_count, 1)
        self.assertEqual(payload["attemptCount"], 1)
        self.assertEqual(payload["attemptLimit"], 4)
        self.assertFalse(payload["attemptsExhausted"])

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
            "link_proxy_country": "BR",
            "link_proxy_ip": "203.0.113.34",
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
        self.assertEqual(saved["link_proxy_country"], "BR")
        self.assertEqual(saved["link_proxy_ip"], "203.0.113.34")
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

    async def test_generates_us_paypal_link_with_desktop_flow_options(self):
        browser_manager = self.app["browser_manager"]
        browser_manager.target_project_dir = Path(self.temp_dir.name) / "missing-runtime"
        browser_manager.python_executable = Path(self.temp_dir.name) / "missing-python.exe"
        generated = {
            "status": "success",
            "url": "https://www.paypal.com/agreements/approve?ba_token=us_web",
            "method": "paypal_us",
            "country": "US",
            "currency": "USD",
            "payment_link_type": "paypal_approve",
            "amount": "1933",
            "amount_currency": "USD",
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
                    "method": "paypal_us",
                    "target_amount": "1933",
                    "create_proxy": "create.example:8000:user:pass",
                    "promotion_proxy": "followup.example:9000:user:pass",
                    "secondary_proxy": "secondary.example:9000:user:pass",
                    "independent_proxy_pair": True,
                    "use_secondary_proxy": True,
                    "promotion_proxy_choice": "second",
                },
                headers={"X-Local-Token": self.app["local_token"]},
            )

        call = bridge.await_args.kwargs
        self.assertEqual(response.status, 200)
        self.assertEqual(call["country"], "US")
        self.assertEqual(call["currency"], "USD")
        self.assertEqual(call["target_amount"], "1933")
        self.assertIn("create.example:8000", call["create_proxy_url"])
        self.assertEqual(call["promotion_proxy_url"], call["create_proxy_url"])
        self.assertEqual(call["python_executable"], Path(sys.executable))
        self.assertEqual(
            call["target_project_dir"], self.app["card_link_bridge_file"].parents[2]
        )
        self.assertIs(call["shared_presenter"], self.app["card_link_bridge_service"])

    async def test_generates_gb_paypal_link_with_first_proxy_only(self):
        browser_manager = self.app["browser_manager"]
        browser_manager.target_project_dir = Path(self.temp_dir.name) / "missing-runtime"
        browser_manager.python_executable = Path(self.temp_dir.name) / "missing-python.exe"
        generated = {
            "status": "success",
            "url": "https://www.paypal.com/agreements/approve?ba_token=gb_web",
            "method": "paypal_gb",
            "country": "GB",
            "currency": "GBP",
            "payment_link_type": "paypal_approve",
            "amount": "0",
            "amount_currency": "GBP",
            "promotion_applied": True,
            "link_proxy_country": "GB",
            "link_proxy_ip": "203.0.113.38",
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
                    "method": "paypal_gb",
                    "target_amount": "1933",
                    "create_proxy": "proxy-GB-link.example:8000:user:pass",
                    "promotion_proxy": "ignored.example:9000:user:pass",
                    "secondary_proxy": "ignored-second.example:9000:user:pass",
                    "independent_proxy_pair": True,
                    "use_secondary_proxy": True,
                    "promotion_proxy_choice": "second",
                },
                headers={"X-Local-Token": self.app["local_token"]},
            )

        call = bridge.await_args.kwargs
        self.assertEqual(response.status, 200)
        self.assertEqual(call["country"], "GB")
        self.assertEqual(call["currency"], "GBP")
        self.assertEqual(call["locale"], "en-GB")
        self.assertEqual(call["target_amount"], "0")
        self.assertIn("proxy-GB-link.example:8000", call["create_proxy_url"])
        self.assertEqual(call["promotion_proxy_url"], call["create_proxy_url"])
        self.assertEqual(call["python_executable"], Path(sys.executable))
        self.assertEqual(
            call["target_project_dir"], self.app["card_link_bridge_file"].parents[2]
        )
        self.assertIs(call["shared_presenter"], self.app["card_link_bridge_service"])

    async def test_gb_kookeey_preflight_discards_nl_without_using_link_attempt(self):
        self.app["card_link_proxy_store"].configure(
            enabled=True,
            mode="kookeey",
            country="TH",
            proxy_endpoint="gate.kookeey.info:1000",
            proxy_username="1234567-AbCdEf1234",
            proxy_password="private-secret",
        )
        candidates = []
        exits = iter([("NL", "192.0.2.10"), ("GB", "192.0.2.20")])

        def detector(proxy_url, **_kwargs):
            candidates.append(proxy_url)
            country, exit_ip = next(exits)
            return SimpleNamespace(success=True, country=country, ip=exit_ip)

        self.app["card_link_proxy_resolver"] = CardLinkProxyResolver(
            health_detector=detector,
            max_candidates=3,
        )
        generated = {
            "status": "success",
            "url": "https://www.paypal.com/agreements/approve?ba_token=gb_preflight",
            "method": "paypal_gb",
            "country": "GB",
            "currency": "GBP",
            "link_proxy_country": "GB",
            "link_proxy_ip": "203.0.113.38",
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
                    "method": "paypal_gb",
                    "proxy_mode": "kookeey",
                    # Simulate a stale browser snapshot: the method is
                    # authoritative and must bind every proxy role to GB.
                    "create_proxy_country": "NL",
                    "promotion_proxy_country": "NL",
                    "secondary_proxy_country": "NL",
                    "independent_proxy_pair": True,
                    "attempt_limit": 3,
                },
                headers={"X-Local-Token": self.app["local_token"]},
            )

        payload = await response.json()
        passwords = [unquote(urlsplit(item).password or "") for item in candidates]
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["attemptCount"], 1)
        self.assertEqual(payload["attemptLimit"], 3)
        self.assertEqual(len(candidates), 2)
        self.assertNotEqual(candidates[0], candidates[1])
        self.assertTrue(
            all(
                re.fullmatch(r"private-secret-GB-[A-Za-z0-9]{8}-5m", password)
                for password in passwords
            )
        )
        self.assertEqual(bridge.await_count, 1)
        self.assertEqual(bridge.await_args.kwargs["create_proxy_url"], candidates[1])
        self.assertEqual(bridge.await_args.kwargs["promotion_proxy_url"], candidates[1])
        self.assertTrue(any("预检丢弃了 1 个" in item for item in payload["logs"]))

    async def test_reextract_skips_previous_link_exit_ip(self):
        _save_account_card_link(
            self.app["db_file"],
            "card-link@icloud.com",
            url="https://www.paypal.com/agreements/approve?ba_token=old_link",
            country="GB",
            currency="GBP",
            method="paypal_gb",
            link_proxy_country="GB",
            link_proxy_ip="192.0.2.10",
        )
        self.app["card_link_proxy_store"].configure(
            enabled=True,
            mode="kookeey",
            country="GB",
            proxy_endpoint="gate.kookeey.info:1000",
            proxy_username="1234567-AbCdEf1234",
            proxy_password="private-secret",
        )
        candidates = []
        exits = iter(["192.0.2.10", "192.0.2.20"])

        def detector(proxy_url, **_kwargs):
            candidates.append(proxy_url)
            return SimpleNamespace(success=True, country="GB", ip=next(exits))

        self.app["card_link_proxy_resolver"] = CardLinkProxyResolver(
            health_detector=detector,
            max_candidates=3,
        )
        generated = {
            "status": "success",
            "url": "https://www.paypal.com/agreements/approve?ba_token=fresh_link",
            "method": "paypal_gb",
            "country": "GB",
            "currency": "GBP",
            "link_proxy_country": "GB",
            "link_proxy_ip": "192.0.2.20",
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
                    "method": "paypal_gb",
                    "proxy_mode": "kookeey",
                    "create_proxy_country": "GB",
                    "force_retry": True,
                },
                headers={"X-Local-Token": self.app["local_token"]},
            )

        payload = await response.json()
        self.assertEqual(response.status, 200, payload)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(bridge.await_args.kwargs["create_proxy_url"], candidates[1])
        self.assertTrue(any("预检丢弃了 1 个" in item for item in payload["logs"]))
        self.assertEqual(
            self.app["card_link_proxy_store"].last_card_link_exit_ip(),
            "192.0.2.20",
        )

    async def test_paypal_link_reuse_path_rotates_away_from_registration_exit(self):
        email = "reuse-paypal-proxy@icloud.com"
        registration_proxy = "http://old-user:old-pass@register.example:8000"
        _save_account_record(
            self.app["db_file"],
            email,
            result={
                "access_token": "at-reuse-paypal",
                "session_json": '{"accessToken":"at-reuse-paypal"}',
                "registration_proxy_url": registration_proxy,
                "registration_environment": {
                    "proxy_mode": "dynamic",
                    "proxy_country": "GB",
                    "exit_ip": "192.0.2.10",
                    "exit_country": "GB",
                },
            },
        )
        self.app["registration_proxy_store"].configure(
            enabled=True,
            mode="dynamic",
            country="GB",
            proxy_line="fresh.example:3010:fresh-user:fresh-password",
        )
        exits = iter(["192.0.2.10", "192.0.2.20"])
        candidates = []

        def detector(proxy_url, **_kwargs):
            candidates.append(proxy_url)
            return SimpleNamespace(success=True, country="GB", ip=next(exits))

        self.app["card_link_proxy_resolver"] = CardLinkProxyResolver(
            health_detector=detector,
            max_candidates=3,
        )
        generated = {
            "status": "success",
            "url": "https://www.paypal.com/agreements/approve?ba_token=reused_fresh",
            "method": "paypal_gb",
            "country": "GB",
            "currency": "GBP",
            "link_proxy_country": "GB",
            "link_proxy_ip": "192.0.2.20",
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
                    "email": email,
                    "method": "paypal_gb",
                    "proxy_mode": "dynamic",
                    "reuse_registration_proxy": True,
                    "create_proxy_country": "GB",
                    "force_retry": True,
                },
                headers={"X-Local-Token": self.app["local_token"]},
            )

        payload = await response.json()
        self.assertEqual(response.status, 200, payload)
        self.assertEqual(len(candidates), 2)
        selected_proxy = bridge.await_args.kwargs["create_proxy_url"]
        self.assertEqual(selected_proxy, candidates[1])
        self.assertNotEqual(selected_proxy, registration_proxy)
        self.assertEqual(
            self.app["registration_proxy_store"].last_card_link_exit_ip(),
            "192.0.2.20",
        )

    async def test_reextract_rejects_fixed_proxy_when_exit_cannot_change(self):
        _save_account_card_link(
            self.app["db_file"],
            "card-link@icloud.com",
            url="https://www.paypal.com/agreements/approve?ba_token=old_fixed",
            country="GB",
            currency="GBP",
            method="paypal_gb",
            link_proxy_country="GB",
            link_proxy_ip="192.0.2.10",
        )
        self.app["card_link_proxy_resolver"] = CardLinkProxyResolver(
            health_detector=lambda *_args, **_kwargs: SimpleNamespace(
                success=True,
                country="GB",
                ip="192.0.2.10",
            ),
            max_candidates=2,
        )
        bridge = mock.AsyncMock()
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
                    "method": "paypal_gb",
                    "create_proxy": "gb-fixed.example:8000:user:pass",
                    "force_retry": True,
                },
                headers={"X-Local-Token": self.app["local_token"]},
            )

        payload = await response.json()
        self.assertEqual(response.status, 502, payload)
        self.assertEqual(bridge.await_count, 0)
        self.assertIn("重复IP", payload["error"])

    async def test_gb_kookeey_preflight_exhaustion_never_starts_bridge(self):
        self.app["card_link_proxy_store"].configure(
            enabled=True,
            mode="kookeey",
            country="TH",
            proxy_endpoint="gate.kookeey.info:1000",
            proxy_username="1234567-AbCdEf1234",
            proxy_password="private-secret",
        )
        self.app["card_link_proxy_resolver"] = CardLinkProxyResolver(
            health_detector=lambda *_args, **_kwargs: SimpleNamespace(
                success=True,
                country="NL",
            ),
            max_candidates=2,
        )
        bridge = mock.AsyncMock()
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
                    "method": "paypal_gb",
                    "proxy_mode": "kookeey",
                    "create_proxy_country": "GB",
                    "independent_proxy_pair": True,
                    "attempt_limit": 3,
                },
                headers={"X-Local-Token": self.app["local_token"]},
            )

        payload = await response.json()
        self.assertEqual(response.status, 502)
        self.assertEqual(bridge.await_count, 0)
        self.assertIn("连续 2 个 GB", payload["error"])
        self.assertIn("NL、NL", payload["error"])
        self.assertNotIn("private-secret", payload["error"])


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
                        "emails": [
                            "ONE@icloud.com",
                            "two@icloud.com",
                            "three@zkgmail.com",
                        ],
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
                    "emails": [
                        "one@icloud.com",
                        "two@icloud.com",
                        "three@zkgmail.com",
                    ],
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
                self.cookie_refresh_starts = []
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

            def start_with_saved_cookies(self, *, emails, concurrency):
                if not self.allow_protocol:
                    raise AssertionError("cookie refresh called on wrong manager")
                self.cookie_refresh_starts.append(
                    {"emails": emails, "concurrency": concurrency}
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
            verification_manager.cookie_refresh_starts,
            [
                {
                    "emails": ["protocol@gmail.com"],
                    "concurrency": 1,
                }
            ],
        )
        self.assertEqual(verification_manager.browser_refresh_starts, [])

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
            verification_manager.cookie_refresh_starts,
            [
                {
                    "emails": ["protocol@icloud.com"],
                    "concurrency": 1,
                }
            ],
        )
        self.assertEqual(verification_manager.browser_refresh_starts, [])
        self.assertEqual(verification_manager.verify_starts, [])
        self.assertEqual(browser_manager.browser_starts, 0)


if __name__ == "__main__":
    unittest.main()
