import ast
import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from hidemyemail_generator import card_link_runtime
from hidemyemail_generator.openai_card_link_bridge import (
    EVENT_PREFIX,
    card_link_error_is_retryable,
    generate_paypal_gb_event,
    generate_paypal_de_event,
    generate_paypal_us_event,
)
from scripts.vendor_card_link_runtime import source_segment_with_decorators


class CardLinkBridgeTests(unittest.TestCase):
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

    def test_runs_us_paypal_authorization_flow_with_embedded_runtime(self):
        def generate(token, country, currency, create, followup, approve, target, **options):
            self.assertEqual(token, "at-test")
            self.assertEqual((country, currency), ("US", "USD"))
            self.assertEqual(create, "http://create.example:8000")
            self.assertEqual(followup, create)
            self.assertEqual(approve, create)
            self.assertEqual(target, "1933")
            self.assertEqual(options["account_email"], "member@icloud.com")
            self.assertTrue(options["checkout_create_promotion_only"])
            self.assertTrue(options["sentinel_so_enabled"])
            return {
                "cs_id": "cs_test_us",
                "billing_country": "US",
                "currency": "USD",
                "paypal_ba_approve_url": "https://www.paypal.com/agreements/approve?ba_token=us_test",
                "payment_link_type": "paypal_approve",
                "stripe_amount": "1933",
                "amount_currency": "USD",
            }

        runtime = SimpleNamespace(
            generate_opll_paypal_long_link=generate,
            opll_is_paypal_success_url=lambda value: value.endswith("us_test"),
        )
        event = generate_paypal_us_event(
            "at-test",
            "http://create.example:8000",
            "socks5://ignored-followup.example:9000",
            "1933",
            account_email="member@icloud.com",
            sentinel_so_enabled=True,
            runtime=runtime,
        )

        self.assertEqual(event["method"], "paypal_us")
        self.assertEqual(event["country"], "US")
        self.assertEqual(event["currency"], "USD")
        self.assertEqual(event["amount"], "1933")

    def test_runs_gb_paypal_zero_flow_entirely_on_first_proxy(self):
        first_proxy = "http://gb-first.example:8000"

        def detect(proxy, **options):
            self.assertEqual(proxy, first_proxy)
            self.assertEqual(
                options,
                {"check_stripe": False, "check_chatgpt": False},
            )
            return SimpleNamespace(
                success=True,
                country="GB",
                ip="203.0.113.44",
                error="",
                failed_stage="",
            )

        def generate(token, country, currency, create, followup, approve, target, **options):
            self.assertEqual(token, "at-test")
            self.assertEqual((country, currency), ("GB", "GBP"))
            self.assertEqual((create, followup, approve), (first_proxy,) * 3)
            self.assertEqual(target, "0")
            self.assertEqual(options["account_email"], "member@icloud.com")
            self.assertTrue(options["checkout_create_promotion_only"])
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
                "promotion_strategy": "post_init_update",
            }

        runtime = SimpleNamespace(
            detect_proxy_health=detect,
            generate_opll_paypal_long_link=generate,
            opll_is_paypal_success_url=lambda value: value.endswith("gb_test"),
        )
        event = generate_paypal_gb_event(
            "at-test",
            first_proxy,
            "socks5://ignored-second.example:9000",
            "999",
            account_email="member@icloud.com",
            sentinel_so_enabled=True,
            runtime=runtime,
        )

        self.assertEqual(event["method"], "paypal_gb")
        self.assertEqual(event["country"], "GB")
        self.assertEqual(event["currency"], "GBP")
        self.assertEqual(event["amount"], "0")
        self.assertEqual(event["link_proxy_country"], "GB")
        self.assertEqual(event["link_proxy_ip"], "203.0.113.44")

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

    def test_us_zero_flow_source_has_no_checkout_promotion_update_call(self):
        source = inspect.getsource(card_link_runtime.generate_opll_paypal_us_tr_long_link)

        self.assertNotIn("opll_chatgpt_update_checkout_promotion(", source)
        self.assertIn("include_trial_promo=True", source)

    def test_us_paypal_zero_target_uses_embedded_zero_amount_flow(self):
        logs = []

        def generate(token, create, followup, target, **options):
            self.assertEqual(token, "at-test")
            self.assertEqual(create, "http://create.example:8000")
            self.assertEqual(followup, create)
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
            }

        runtime = SimpleNamespace(
            generate_opll_paypal_us_tr_long_link=generate,
            opll_is_paypal_success_url=lambda value: value.endswith("us_zero"),
        )
        event = generate_paypal_us_event(
            "at-test",
            "http://create.example:8000",
            "socks5://ignored-followup.example:9000",
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
