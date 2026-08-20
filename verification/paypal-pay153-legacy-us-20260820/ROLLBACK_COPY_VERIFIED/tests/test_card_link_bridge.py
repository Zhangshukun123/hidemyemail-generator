import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hidemyemail_generator import card_link_runtime
from hidemyemail_generator.openai_card_link_bridge import (
    EVENT_PREFIX,
    card_link_error_is_retryable,
    detect_paypal_proxy_pair,
    generate_paypal_gb_event,
    generate_paypal_de_event,
    generate_paypal_us_event,
    paypal_ba_approve_url,
)
from scripts.vendor_card_link_runtime import source_segment_with_decorators


class _JsonResponse:
    def __init__(self, payload=None, *, status_code=200, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class _ApprovalSession:
    def __init__(self, label, result, events):
        self.label = label
        self.result = result
        self.events = events
        self.requests = []
        self.closed = False

    def get(self, url, **kwargs):
        self.requests.append(
            {"operation": "checkout", "method": "GET", "url": url, **kwargs}
        )
        self.events.append((self.label, "checkout", url))
        return _JsonResponse(headers={"x-request-id": f"req-{self.label}-checkout"})

    def post(self, url, **kwargs):
        if url.endswith("/backend-api/sentinel/ping"):
            operation = "sentinel"
            payload = {"result": "ok"}
        elif url.endswith("/backend-api/payments/checkout/approve"):
            operation = "approve"
            payload = {"result": self.result}
        else:  # pragma: no cover - makes unexpected protocol calls explicit
            raise AssertionError(f"unexpected approval request: {url}")
        self.requests.append(
            {"operation": operation, "method": "POST", "url": url, **kwargs}
        )
        self.events.append((self.label, operation, url))
        return _JsonResponse(
            payload,
            headers={"x-request-id": f"req-{self.label}-{operation}"},
        )

    def close(self):
        self.closed = True
        self.events.append((self.label, "close", ""))


class CardLinkBridgeTests(unittest.TestCase):
    def _run_us_oaics_promotion_case(self, create_amount):
        first_proxy = "http://us-sticky.example:8000"
        second_proxy = "socks5://us-final.example:9000"
        oaics_id = "oaics_us_promotion_behavior"
        stripe_id = "cs_live_uspromotionbehavior"
        paypal_url = (
            "https://www.paypal.com/agreements/approve?"
            "ba_token=us_promotion_behavior"
        )
        raw_create_payload = {
            "checkout_session_id": oaics_id,
            "currency": "USD",
            "total_summary": {"due": create_amount},
            "payment_method_types": ["card", "paypal"],
        }
        materialized_payload = {
            "checkout_session_id": oaics_id,
            "client_secret": f"{stripe_id}_secret_fixturetoken",
            "currency": "USD",
            "total_summary": {"due": 0},
            "payment_method_types": ["card", "paypal"],
        }
        checkout = {
            "cs_id": oaics_id,
            "billing_country": "US",
            "currency": "USD",
            "processor_entity": "openai_llc",
            "stripe_publishable_key": "pk_test_us",
            "oai_device_id": "did-us-promotion",
            "_checkout_payload": raw_create_payload,
        }
        payment_page = {
            "stripe_hosted_url": f"https://checkout.stripe.com/c/pay/{stripe_id}",
            "payment_method_types": ["card", "paypal"],
            "currency": "usd",
            "total_summary": {"due": 0},
            "config_id": "cfg-us-promotion",
            "init_checksum": "checksum-us-promotion",
        }
        chatgpt_session = object()
        stripe_session = object()
        diagnostics = []

        with ExitStack() as stack:
            validate = stack.enter_context(
                patch.object(card_link_runtime, "opll_validate_access_token", return_value={})
            )
            create_checkout = stack.enter_context(
                patch.object(card_link_runtime, "opll_create_checkout", return_value=checkout)
            )
            build_chatgpt = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_build_chatgpt_session",
                    return_value=chatgpt_session,
                )
            )
            update_checkout = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_chatgpt_update_checkout_promotion",
                    return_value=materialized_payload,
                )
            )
            fetch_checkout = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_chatgpt_fetch_checkout",
                    return_value=materialized_payload,
                )
            )
            build_stripe = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_build_stripe_session",
                    return_value=stripe_session,
                )
            )
            stripe_init = stack.enter_context(
                patch.object(card_link_runtime, "opll_stripe_init", return_value=payment_page)
            )
            apply_promotion = stack.enter_context(
                patch.object(card_link_runtime, "opll_apply_checkout_trial_promotion")
            )
            wait_for_page = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_wait_for_us_tr_promoted_payment_page",
                    return_value=payment_page,
                )
            )
            billing = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_billing_for_country",
                    return_value={
                        "name": "Taylor Morgan",
                        "email": "member@icloud.com",
                        "phone": "+12125550123",
                        "country": "US",
                        "line1": "88 Broadway",
                        "city": "New York",
                        "state": "NY",
                        "postal_code": "10007",
                    },
                )
            )
            checkout_taxes = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_chatgpt_checkout_taxes",
                    return_value={"status": "success"},
                )
            )
            create_method = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_stripe_create_paypal_method",
                    return_value="pm_us_promotion",
                )
            )
            confirm = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_stripe_confirm",
                    return_value={"submission_attempt": {"state": "requires_approval"}},
                )
            )
            redirect = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_redirect_url_after_confirm",
                    return_value="https://pm-redirects.stripe.com/authorize/us-behavior",
                )
            )
            resolve = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_resolve_paypal_redirect_result",
                    return_value={
                        "selected_url": paypal_url,
                        "paypal_ba_approve_url": paypal_url,
                        "payment_link_type": "paypal_approve",
                    },
                )
            )

            result = card_link_runtime.generate_opll_paypal_us_tr_long_link(
                "at-test",
                first_proxy,
                second_proxy,
                "0",
                account_email="member@icloud.com",
                diagnostic_log=diagnostics.append,
                session_context={"session_json": '{"accessToken":"at-test"}'},
            )

        return SimpleNamespace(
            result=result,
            first_proxy=first_proxy,
            second_proxy=second_proxy,
            oaics_id=oaics_id,
            stripe_id=stripe_id,
            chatgpt_session=chatgpt_session,
            stripe_session=stripe_session,
            diagnostics=diagnostics,
            validate=validate,
            create_checkout=create_checkout,
            build_chatgpt=build_chatgpt,
            update_checkout=update_checkout,
            fetch_checkout=fetch_checkout,
            build_stripe=build_stripe,
            stripe_init=stripe_init,
            apply_promotion=apply_promotion,
            wait_for_page=wait_for_page,
            billing=billing,
            checkout_taxes=checkout_taxes,
            create_method=create_method,
            confirm=confirm,
            redirect=redirect,
            resolve=resolve,
        )

    def test_us_billing_country_mismatch_is_retryable_with_fresh_proxy(self):
        error = RuntimeError(
            'checkout create failed: HTTP 400 {"detail":'
            '"Billing country must match request country."}'
        )
        self.assertTrue(card_link_error_is_retryable("paypal_us", error))
        self.assertFalse(
            card_link_error_is_retryable(
                "paypal_us",
                RuntimeError("authentication token has been invalidated"),
            )
        )

    def test_gb_proxy_country_mismatch_is_retryable_with_fresh_proxy(self):
        self.assertTrue(
            card_link_error_is_retryable(
                "paypal_gb",
                RuntimeError("提链代理真实出口国家与 GB 不一致：当前=US"),
            )
        )

    def test_embedded_proxy_health_result_is_constructible(self):
        result = card_link_runtime.ProxyHealthResult(
            True,
            country="US",
            ip="203.0.113.1",
        )

        self.assertTrue(result.success)
        self.assertEqual(result.country, "US")
        self.assertEqual(result.ip, "203.0.113.1")

    def test_pay153_proxy_pair_rejects_wrong_country_and_same_exit(self):
        first_proxy = "http://first.example:8000"
        second_proxy = "http://second.example:9000"
        cases = (
            ("GB", "203.0.113.20", "代理池 2.*国家与 US 不一致"),
            ("US", "203.0.113.10", "同一出口 IP"),
        )
        for final_country, final_ip, message in cases:
            with self.subTest(final_country=final_country, final_ip=final_ip):
                def detect(proxy, **_options):
                    if proxy == first_proxy:
                        country, ip = "US", "203.0.113.10"
                    else:
                        country, ip = final_country, final_ip
                    return SimpleNamespace(
                        success=True,
                        country=country,
                        ip=ip,
                        error="",
                        failed_stage="",
                    )

                with self.assertRaisesRegex(RuntimeError, message):
                    detect_paypal_proxy_pair(
                        SimpleNamespace(detect_proxy_health=detect),
                        first_proxy,
                        second_proxy,
                        "US",
                    )

    def test_paypal_event_requires_explicit_ba_approve_field(self):
        runtime = SimpleNamespace(
            opll_is_paypal_ba_approve_url=lambda value: "ba_token=" in value,
        )
        checkout = {
            "provider_redirect_url": "https://paypal.com/checkout?token=not-ba",
            "long_url": "https://paypal.com/checkout?token=not-ba",
        }

        with self.assertRaisesRegex(RuntimeError, "PayPal BA 授权地址"):
            paypal_ba_approve_url(runtime, checkout)

    def test_runtime_vendor_preserves_class_decorators(self):
        source = (
            "import dataclasses\n\n"
            "@dataclasses.dataclass(frozen=True)\n"
            "class Example:\n"
            "    value: str\n"
        )
        class_node = next(
            node for node in ast.parse(source).body if isinstance(node, ast.ClassDef)
        )

        rendered = source_segment_with_decorators(source, class_node)

        self.assertTrue(rendered.startswith("@dataclasses.dataclass(frozen=True)"))
        self.assertIn("class Example:", rendered)

    def test_rejects_removed_registration_checkout_probe_method(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge = (
                Path(__file__).resolve().parents[1]
                / "src"
                / "hidemyemail_generator"
                / "openai_card_link_bridge.py"
            )
            env = os.environ.copy()
            env["HME_OPENAI_ACCESS_TOKEN"] = "at-test"
            process = subprocess.run(
                [
                    sys.executable,
                    str(bridge),
                    "--source-dir",
                    temp_dir,
                    "--method",
                    "oaics_probe",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

        self.assertEqual(process.returncode, 2)
        self.assertIn("invalid choice", process.stderr)
        self.assertNotIn(EVENT_PREFIX, process.stdout)

    def test_builds_standard_chatgpt_checkout_link(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir)
            (target / "app_backend.py").write_text(
                "def opll_create_checkout(token, country, currency, **options):\n"
                "    assert token == 'at-test'\n"
                "    assert country == 'JP'\n"
                "    assert options['include_trial_promo'] is False\n"
                "    return {'cs_id':'cs_test_bridge','billing_country':'JP','currency':'JPY','processor_entity':'openai_llc'}\n"
                "def opll_chatgpt_checkout_page_url(cs_id, country, entity):\n"
                "    return f'https://chatgpt.com/checkout/{entity}/{cs_id}'\n"
                "def opll_is_chatgpt_checkout_page_url(value):\n"
                "    return value == 'https://chatgpt.com/checkout/openai_llc/cs_test_bridge'\n",
                encoding="utf-8",
            )
            bridge = (
                Path(__file__).resolve().parents[1]
                / "src"
                / "hidemyemail_generator"
                / "openai_card_link_bridge.py"
            )
            env = os.environ.copy()
            env["HME_OPENAI_ACCESS_TOKEN"] = "at-test"
            process = subprocess.run(
                [
                    sys.executable,
                    str(bridge),
                    "--source-dir",
                    str(target),
                    "--country",
                    "JP",
                    "--currency",
                    "JPY",
                    "--locale",
                    "ja-JP",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

        event_line = next(
            line for line in process.stdout.splitlines() if line.startswith(EVENT_PREFIX)
        )
        event = json.loads(event_line[len(EVENT_PREFIX) :])
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(event["status"], "success")
        self.assertEqual(event["currency"], "JPY")

    def test_builds_ph_hosted_strict_zero_link_with_two_proxies(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir)
            (target / "app_backend.py").write_text(
                "import os\n"
                "def generate_opll_philippines_short_link(token, create_proxy, promo_proxy, amount, **options):\n"
                "    assert token == 'at-test'\n"
                "    assert create_proxy == 'http://create.example:8000'\n"
                "    assert promo_proxy == 'socks5://promo.example:9000'\n"
                "    assert amount == '0'\n"
                "    assert callable(options['diagnostic_log'])\n"
                "    return {'cs_id':'oaics_test_hosted','billing_country':'PH','currency':'PHP','processor_entity':'openai_ie','chatgpt_checkout_url':'https://chatgpt.com/checkout/openai_ie/oaics_test_hosted','payment_link_type':'chatgpt_checkout_short','checkout_ui_mode':'hosted','stripe_amount':'0','amount_currency':'PHP','amount_verification':'checkout_update','promotion_applied':True,'promotion_strategy':'gpt_link_hosted_create_and_update'}\n"
                "def opll_is_chatgpt_checkout_page_url(value):\n"
                "    return value == 'https://chatgpt.com/checkout/openai_ie/oaics_test_hosted'\n",
                encoding="utf-8",
            )
            bridge = (
                Path(__file__).resolve().parents[1]
                / "src"
                / "hidemyemail_generator"
                / "openai_card_link_bridge.py"
            )
            env = os.environ.copy()
            env.update(
                {
                    "HME_OPENAI_ACCESS_TOKEN": "at-test",
                    "HME_CARD_LINK_CREATE_PROXY_URL": "http://create.example:8000",
                    "HME_CARD_LINK_PROMO_PROXY_URL": "socks5://promo.example:9000",
                }
            )
            process = subprocess.run(
                [
                    sys.executable,
                    str(bridge),
                    "--source-dir",
                    str(target),
                    "--method",
                    "ph_hosted",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

        event_line = next(
            line for line in process.stdout.splitlines() if line.startswith(EVENT_PREFIX)
        )
        event = json.loads(event_line[len(EVENT_PREFIX) :])
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(event["method"], "ph_hosted")
        self.assertEqual(event["country"], "PH")
        self.assertEqual(event["currency"], "PHP")
        self.assertEqual(event["amount"], "0")
        self.assertTrue(event["promotion_applied"])

    def test_builds_de_oaics_paypal_zero_link_with_selected_promotion_proxy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir)
            (target / "app_backend.py").write_text(
                "class ProxyHealth:\n"
                "    success = True\n"
                "    country = 'BR'\n"
                "    ip = '203.0.113.45'\n"
                "    error = ''\n"
                "    failed_stage = ''\n"
                "def detect_proxy_health(proxy, **options):\n"
                "    assert proxy == 'http://create.example:8000'\n"
                "    assert options == {'check_stripe': False, 'check_chatgpt': False}\n"
                "    return ProxyHealth()\n"
                "def generate_opll_de_oaics_paypal_link(token, create_proxy, promo_proxy, amount, **options):\n"
                "    assert token == 'at-test'\n"
                "    assert create_proxy == 'http://create.example:8000'\n"
                "    assert promo_proxy == 'socks5://promo.example:9000'\n"
                "    assert amount == '0'\n"
                "    assert options['account_email'] == 'member@icloud.com'\n"
                "    return {'cs_id':'oaics_test_de','billing_country':'DE','currency':'EUR','paypal_ba_approve_url':'https://www.paypal.com/agreements/approve?ba_token=de_test','payment_link_type':'paypal_approve','checkout_ui_mode':'custom','stripe_amount':'0','amount_currency':'EUR','amount_verification':'checkout_create','promotion_applied':True,'promotion_strategy':'de_oaics_checkout_create_native'}\n"
                "def opll_is_paypal_success_url(value):\n"
                "    return value == 'https://www.paypal.com/agreements/approve?ba_token=de_test'\n",
                encoding="utf-8",
            )
            bridge = (
                Path(__file__).resolve().parents[1]
                / "src"
                / "hidemyemail_generator"
                / "openai_card_link_bridge.py"
            )
            env = os.environ.copy()
            env.update(
                {
                    "HME_OPENAI_ACCESS_TOKEN": "at-test",
                    "HME_CARD_LINK_CREATE_PROXY_URL": "http://create.example:8000",
                    "HME_CARD_LINK_PROMO_PROXY_URL": "socks5://promo.example:9000",
                }
            )
            process = subprocess.run(
                [
                    sys.executable,
                    str(bridge),
                    "--source-dir",
                    str(target),
                    "--method",
                    "de_oaics_paypal",
                    "--account-email",
                    "member@icloud.com",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

        event_line = next(
            line for line in process.stdout.splitlines() if line.startswith(EVENT_PREFIX)
        )
        event = json.loads(event_line[len(EVENT_PREFIX) :])
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(event["method"], "de_oaics_paypal")
        self.assertEqual(event["country"], "DE")
        self.assertEqual(event["currency"], "EUR")
        self.assertEqual(event["amount"], "0")
        self.assertEqual(event["payment_link_type"], "paypal_approve")
        self.assertTrue(event["promotion_applied"])
        self.assertEqual(event["link_proxy_country"], "BR")
        self.assertEqual(event["link_proxy_ip"], "203.0.113.45")

    def test_classifies_de_cs_live_without_exposing_checkout_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir)
            (target / "app_backend.py").write_text(
                "def generate_opll_de_oaics_paypal_link(*args, **kwargs):\n"
                "    raise RuntimeError('OAICS required; current cs_live_private_checkout')\n",
                encoding="utf-8",
            )
            bridge = (
                Path(__file__).resolve().parents[1]
                / "src"
                / "hidemyemail_generator"
                / "openai_card_link_bridge.py"
            )
            env = os.environ.copy()
            env["HME_OPENAI_ACCESS_TOKEN"] = "at-test"
            process = subprocess.run(
                [
                    sys.executable,
                    str(bridge),
                    "--source-dir",
                    str(target),
                    "--method",
                    "de_oaics_paypal",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

        event_line = next(
            line for line in process.stdout.splitlines() if line.startswith(EVENT_PREFIX)
        )
        event = json.loads(event_line[len(EVENT_PREFIX) :])
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(event["status"], "classified")
        self.assertEqual(event["classification"], "cs_live")
        self.assertEqual(event["method"], "de_oaics_paypal")
        self.assertNotIn("cs_live_private_checkout", process.stdout)

    def test_runs_us_pay153_with_two_us_proxy_identities(self):
        first_proxy = "http://us-first.example:8000"
        second_proxy = "socks5://us-second.example:9000"

        def detect(proxy, **options):
            self.assertEqual(
                options,
                {"check_stripe": False, "check_chatgpt": False},
            )
            ip = {
                first_proxy: "203.0.113.40",
                second_proxy: "203.0.113.41",
            }[proxy]
            return SimpleNamespace(
                success=True,
                country="US",
                ip=ip,
                error="",
                failed_stage="",
            )

        def generate(token, create, final, target, **options):
            self.assertEqual(token, "at-test")
            self.assertEqual((create, final), (first_proxy, second_proxy))
            self.assertEqual(target, "0")
            self.assertEqual(options["account_email"], "member@icloud.com")
            self.assertTrue(options["sentinel_so_enabled"])
            return {
                "cs_id": "oaics_test_us",
                "billing_country": "US",
                "currency": "USD",
                "paypal_ba_approve_url": "https://www.paypal.com/agreements/approve?ba_token=us_test",
                "payment_link_type": "paypal_approve",
                "stripe_amount": "0",
                "amount_currency": "USD",
                "amount_verification": "checkout_update",
                "promotion_applied": True,
                "promotion_strategy": "checkout_check_then_region_update",
                "paypal_flow": "pay153_protocol",
                "variant": "pay153_protocol",
                "promotion_update_count": 1,
                "promotion_update_country": "US",
                "checkout_taxes_performed": True,
                "checkout_taxes_count": 1,
                "checkout_taxes_country": "US",
                "checkout_taxes_currency": "USD",
                "approval_strategy": "pool2_clean_session_sentinel_then_approve",
                "promotion_proxy": "credential-bearing-value-must-not-leak",
            }

        runtime = SimpleNamespace(
            detect_proxy_health=detect,
            generate_opll_paypal_pay153_long_link=generate,
            opll_is_paypal_ba_approve_url=lambda value: value.endswith("us_test"),
            opll_is_paypal_success_url=lambda value: value.endswith("us_test"),
        )

        event = generate_paypal_us_event(
            "at-test",
            first_proxy,
            second_proxy,
            "0",
            account_email="member@icloud.com",
            sentinel_so_enabled=True,
            runtime=runtime,
        )

        self.assertEqual(event["method"], "paypal_us")
        self.assertEqual(event["country"], "US")
        self.assertEqual(event["currency"], "USD")
        self.assertEqual(event["amount"], "0")
        self.assertEqual(event["checkout_proxy_ip"], "203.0.113.40")
        self.assertEqual(event["link_proxy_ip"], "203.0.113.41")
        self.assertTrue(event["independent_proxy_pair"])
        self.assertEqual(event["promotion_update_count"], 1)
        self.assertTrue(event["checkout_taxes_performed"])
        self.assertNotIn("promotion_proxy", event)

    def test_cli_preserves_pay153_second_proxy_for_us_and_gb(self):
        from hidemyemail_generator import openai_card_link_bridge

        create_proxy = "http://pool1:user-secret@create.example:8000"
        final_proxy = "socks5://pool2:user-secret@final.example:9000"
        for method, generator_name in (
            ("paypal_us", "generate_paypal_us_event"),
            ("paypal_gb", "generate_paypal_gb_event"),
        ):
            with self.subTest(method=method):
                with (
                    patch.dict(
                        os.environ,
                        {
                            "HME_OPENAI_ACCESS_TOKEN": "at-cli-test",
                            "HME_CARD_LINK_CREATE_PROXY_URL": create_proxy,
                            "HME_CARD_LINK_PROMO_PROXY_URL": final_proxy,
                        },
                        clear=True,
                    ),
                    patch.object(
                        sys,
                        "argv",
                        ["openai_card_link_bridge.py", "--method", method],
                    ),
                    patch.object(
                        openai_card_link_bridge,
                        generator_name,
                        return_value={
                            "status": "success",
                            "url": "https://www.paypal.com/agreements/approve?ba_token=test",
                            "method": method,
                        },
                    ) as generator,
                    patch.object(openai_card_link_bridge, "emit"),
                ):
                    exit_status = openai_card_link_bridge.main()

                self.assertEqual(exit_status, 0)
                self.assertEqual(generator.call_args.args[1], create_proxy)
                self.assertEqual(generator.call_args.args[2], final_proxy)

    def test_runs_gb_pay153_with_pool2_as_link_identity(self):
        first_proxy = "http://gb-first.example:8000"
        second_proxy = "socks5://gb-second.example:9000"

        def detect(proxy, **options):
            self.assertEqual(
                options,
                {"check_stripe": False, "check_chatgpt": False},
            )
            return SimpleNamespace(
                success=True,
                country="GB",
                ip={
                    first_proxy: "203.0.113.44",
                    second_proxy: "203.0.113.45",
                }[proxy],
                error="",
                failed_stage="",
            )

        def generate(token, create, final, target, **options):
            self.assertEqual(token, "at-test")
            self.assertEqual((create, final), (first_proxy, second_proxy))
            self.assertEqual(target, "0")
            self.assertEqual(options["account_email"], "member@icloud.com")
            self.assertTrue(options["sentinel_so_enabled"])
            return {
                "cs_id": "cs_test_gb",
                "billing_country": "GB",
                "currency": "GBP",
                "paypal_ba_approve_url": "https://www.paypal.com/agreements/approve?ba_token=gb_test",
                "payment_link_type": "paypal_approve",
                "stripe_amount": "0",
                "amount_currency": "GBP",
                "amount_verification": "checkout_update",
                "promotion_applied": True,
                "promotion_strategy": "checkout_check_then_region_update",
                "paypal_flow": "gb_two_proxy_promotion",
                "variant": "gb_two_proxy_promotion",
                "promotion_update_count": 1,
                "promotion_update_country": "GB",
                "checkout_taxes_performed": True,
                "checkout_taxes_count": 1,
                "checkout_taxes_country": "GB",
                "checkout_taxes_currency": "GBP",
                "approval_strategy": "pool2_clean_session_sentinel_then_approve",
            }

        runtime = SimpleNamespace(
            detect_proxy_health=detect,
            generate_opll_paypal_gb_two_proxy_long_link=generate,
            opll_is_paypal_ba_approve_url=lambda value: value.endswith("gb_test"),
            opll_is_paypal_success_url=lambda value: value.endswith("gb_test"),
        )
        event = generate_paypal_gb_event(
            "at-test",
            first_proxy,
            second_proxy,
            "0",
            account_email="member@icloud.com",
            sentinel_so_enabled=True,
            runtime=runtime,
        )

        self.assertEqual(event["method"], "paypal_gb")
        self.assertEqual(event["country"], "GB")
        self.assertEqual(event["currency"], "GBP")
        self.assertEqual(event["amount"], "0")
        self.assertEqual(event["link_proxy_country"], "GB")
        self.assertEqual(event["checkout_proxy_ip"], "203.0.113.44")
        self.assertEqual(event["link_proxy_ip"], "203.0.113.45")
        self.assertEqual(event["promotion_update_country"], "GB")
        self.assertEqual(event["checkout_taxes_currency"], "GBP")
        self.assertEqual(event["paypal_flow"], "gb_two_proxy_promotion")
        self.assertEqual(event["variant"], "gb_two_proxy_promotion")

    def test_de_paypal_uses_checkout_create_promotion_without_update_proxy(self):
        first_proxy = "http://de-first.example:8000"

        def generate(token, country, currency, create, followup, approve, target, **options):
            self.assertEqual((country, currency), ("DE", "EUR"))
            self.assertEqual((create, followup, approve), (first_proxy,) * 3)
            self.assertEqual(target, "0")
            self.assertTrue(options["checkout_includes_trial_promo"])
            self.assertTrue(options["checkout_create_promotion_only"])
            self.assertTrue(options["sentinel_so_enabled"])
            return {
                "cs_id": "oaics_test_de",
                "billing_country": "DE",
                "currency": "EUR",
                "paypal_ba_approve_url": "https://www.paypal.com/agreements/approve?ba_token=de_create",
                "stripe_amount": "0",
                "promotion_applied": True,
                "promotion_strategy": "checkout_create",
            }

        runtime = SimpleNamespace(
            PAYPAL_BR_DE_STRICT_ZERO_FLOW="br_de_strict_zero",
            detect_proxy_health=lambda *_args, **_kwargs: SimpleNamespace(
                success=True, country="DE", ip="203.0.113.49", error="", failed_stage="",
            ),
            generate_opll_paypal_long_link=generate,
            opll_is_paypal_success_url=lambda value: value.endswith("de_create"),
        )
        event = generate_paypal_de_event(
            "at-test",
            first_proxy,
            "http://ignored-promo.example:9000",
            "0",
            account_email="member@icloud.com",
            sentinel_so_enabled=True,
            runtime=runtime,
        )

        self.assertEqual(event["method"], "de_oaics_paypal")
        self.assertEqual(event["promotion_strategy"], "checkout_create")

    def test_us_create_zero_amount_still_updates_once_on_second_proxy(self):
        case = self._run_us_oaics_promotion_case(create_amount=0)

        case.update_checkout.assert_called_once()
        update_args = case.update_checkout.call_args.args
        update_kwargs = case.update_checkout.call_args.kwargs
        self.assertEqual(update_args[0], "at-test")
        self.assertEqual(update_args[1]["cs_id"], case.oaics_id)
        self.assertEqual(update_args[2], case.second_proxy)
        self.assertTrue(update_kwargs["include_checkout_context"])
        case.fetch_checkout.assert_not_called()
        self.assertEqual(
            [call.args[1] for call in case.build_chatgpt.call_args_list],
            [case.first_proxy, case.second_proxy, case.second_proxy],
        )
        self.assertEqual(case.create_checkout.call_args.args[3], case.first_proxy)
        self.assertTrue(case.create_checkout.call_args.kwargs["include_trial_promo"])
        self.assertEqual(case.stripe_init.call_args.args[0], case.stripe_id)
        self.assertEqual(case.stripe_init.call_args.args[3], case.second_proxy)
        self.assertEqual(
            case.stripe_init.call_args.kwargs["browser_timezone"],
            "America/New_York",
        )
        case.apply_promotion.assert_not_called()
        self.assertEqual(case.checkout_taxes.call_args.args[3], case.second_proxy)
        self.assertEqual(case.result["cs_id"], case.oaics_id)
        self.assertEqual(
            case.result["promotion_strategy"],
            "checkout_check_then_us_update",
        )
        self.assertEqual(case.result["promotion_proxy"], case.second_proxy)
        self.assertEqual(case.result["promotion_update_count"], 1)
        self.assertTrue(any("唯一一次 Update" in item for item in case.diagnostics))

    def test_us_create_nonzero_amount_updates_original_oaics_once_on_second_proxy(self):
        case = self._run_us_oaics_promotion_case(create_amount=1933)

        case.update_checkout.assert_called_once()
        update_args = case.update_checkout.call_args.args
        update_kwargs = case.update_checkout.call_args.kwargs
        self.assertEqual(update_args[0], "at-test")
        self.assertEqual(update_args[1]["cs_id"], case.oaics_id)
        self.assertEqual(update_args[2], case.second_proxy)
        self.assertIs(update_kwargs["session"], case.chatgpt_session)
        self.assertTrue(update_kwargs["include_checkout_context"])
        self.assertEqual(case.build_chatgpt.call_args.args[1], case.second_proxy)
        case.fetch_checkout.assert_not_called()
        case.apply_promotion.assert_not_called()
        self.assertEqual(case.stripe_init.call_args.args[0], case.stripe_id)
        self.assertEqual(case.stripe_init.call_args.args[3], case.second_proxy)
        self.assertEqual(case.checkout_taxes.call_args.args[3], case.second_proxy)
        self.assertEqual(case.result["cs_id"], case.oaics_id)
        self.assertEqual(
            case.result["promotion_strategy"],
            "checkout_check_then_us_update",
        )
        self.assertEqual(case.result["promotion_proxy"], case.second_proxy)
        self.assertTrue(any("唯一一次 Update" in item for item in case.diagnostics))

    def test_approve_blocked_retries_with_fresh_session_on_same_proxy_in_order(self):
        events = []
        first = _ApprovalSession("first", "blocked", events)
        second = _ApprovalSession("second", "approved", events)
        proxy = "http://us-sticky.example:8000"
        fallback_proxy = "http://us-fallback.example:8000"
        checkout_id = "oaics_us_approve_behavior"
        checkout = {
            "billing_country": "US",
            "processor_entity": "openai_llc",
            "oai_device_id": "did-us-approve",
        }
        diagnostics = []

        with patch.object(
            card_link_runtime,
            "opll_build_chatgpt_session",
            side_effect=[first, second],
        ) as build_session:
            result = card_link_runtime.opll_chatgpt_approve_with_retry(
                "at-test",
                checkout_id,
                checkout,
                [proxy, fallback_proxy],
                request_locale="en-US",
                attempts=3,
                interval_seconds=0,
                rotate_ip_each_attempt=False,
                diagnostic_log=diagnostics.append,
            )

        self.assertIs(result, second)
        self.assertEqual(build_session.call_count, 2)
        self.assertEqual(
            [call.args[1] for call in build_session.call_args_list],
            [proxy, proxy],
        )
        self.assertEqual(
            [call.kwargs["device_id"] for call in build_session.call_args_list],
            ["did-us-approve", "did-us-approve"],
        )
        self.assertEqual(
            [(label, operation) for label, operation, _url in events if operation != "close"],
            [
                ("first", "checkout"),
                ("first", "sentinel"),
                ("first", "approve"),
                ("second", "checkout"),
                ("second", "sentinel"),
                ("second", "approve"),
            ],
        )
        self.assertTrue(first.closed)
        self.assertFalse(second.closed)
        expected_checkout_url = (
            "https://chatgpt.com/checkout/openai_llc/"
            f"{checkout_id}"
        )
        for session in (first, second):
            self.assertEqual(
                [request["operation"] for request in session.requests],
                ["checkout", "sentinel", "approve"],
            )
            self.assertEqual(session.requests[0]["url"], expected_checkout_url)
            self.assertEqual(session.requests[1]["json"], {})
            self.assertEqual(
                session.requests[2]["json"],
                {
                    "checkout_session_id": checkout_id,
                    "processor_entity": "openai_llc",
                },
            )
        self.assertTrue(any("同一粘性代理" in item for item in diagnostics))

    def test_approve_second_blocked_stops_without_third_session(self):
        events = []
        first = _ApprovalSession("first", "blocked", events)
        second = _ApprovalSession("second", "blocked", events)
        proxy = "http://us-sticky.example:8000"
        fallback_proxy = "http://us-fallback.example:8000"
        checkout_id = "oaics_us_approve_behavior"
        checkout = {
            "billing_country": "US",
            "processor_entity": "openai_llc",
            "oai_device_id": "did-us-approve",
        }

        with patch.object(
            card_link_runtime,
            "opll_build_chatgpt_session",
            side_effect=[first, second],
        ) as build_session:
            with self.assertRaises(card_link_runtime.OpllChatgptApproveBlocked):
                card_link_runtime.opll_chatgpt_approve_with_retry(
                    "at-test",
                    checkout_id,
                    checkout,
                    [proxy, fallback_proxy],
                    request_locale="en-US",
                    attempts=3,
                    interval_seconds=0,
                    rotate_ip_each_attempt=False,
                )

        self.assertEqual(build_session.call_count, 2)
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)
        self.assertEqual(
            [call.args[1] for call in build_session.call_args_list],
            [proxy, proxy],
        )
        for session in (first, second):
            self.assertEqual(
                session.requests[0]["url"],
                "https://chatgpt.com/checkout/openai_llc/"
                f"{checkout_id}",
            )
            self.assertEqual(
                session.requests[2]["json"]["checkout_session_id"],
                checkout_id,
            )
        self.assertEqual(
            [(label, operation) for label, operation, _url in events if operation != "close"],
            [
                ("first", "checkout"),
                ("first", "sentinel"),
                ("first", "approve"),
                ("second", "checkout"),
                ("second", "sentinel"),
                ("second", "approve"),
            ],
        )

    def test_us_paypal_zero_target_uses_embedded_zero_amount_flow(self):
        logs = []
        first_proxy = "http://us-first.example:8000"
        second_proxy = "socks5://us-second.example:9000"

        def detect(proxy, **_options):
            return SimpleNamespace(
                success=True,
                country="US",
                ip={
                    first_proxy: "192.0.2.10",
                    second_proxy: "192.0.2.20",
                }[proxy],
                error="",
                failed_stage="",
            )

        def generate(token, create, final, target, **options):
            self.assertEqual(token, "at-test")
            self.assertEqual(create, first_proxy)
            self.assertEqual(final, second_proxy)
            self.assertEqual(target, "0")
            self.assertEqual(options["account_email"], "member@icloud.com")
            options["diagnostic_log"](
                "[PayPal US] 步骤 6/7：正在提交 Confirm 并读取 PayPal Approve 跳转"
            )
            return {
                "cs_id": "oaics_test_us",
                "billing_country": "US",
                "currency": "USD",
                "paypal_ba_approve_url": "https://www.paypal.com/agreements/approve?ba_token=us_zero",
                "payment_link_type": "paypal_approve",
                "stripe_amount": "0",
                "amount_currency": "USD",
                "promotion_update_count": 1,
                "checkout_taxes_performed": True,
                "checkout_taxes_count": 1,
            }

        runtime = SimpleNamespace(
            detect_proxy_health=detect,
            generate_opll_paypal_pay153_long_link=generate,
            opll_is_paypal_ba_approve_url=lambda value: value.endswith("us_zero"),
            opll_is_paypal_success_url=lambda value: value.endswith("us_zero"),
        )
        event = generate_paypal_us_event(
            "at-test",
            first_proxy,
            second_proxy,
            "0",
            account_email="member@icloud.com",
            diagnostic_log=logs.append,
            runtime=runtime,
        )

        self.assertEqual(event["method"], "paypal_us")
        self.assertEqual(event["checkout_id_type"], "oaics")
        self.assertEqual(event["amount"], "0")
        self.assertIn("步骤 6/7", logs[0])



if __name__ == "__main__":
    unittest.main()
