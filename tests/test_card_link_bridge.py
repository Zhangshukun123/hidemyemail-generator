import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from hidemyemail_generator.openai_card_link_bridge import EVENT_PREFIX


class CardLinkBridgeTests(unittest.TestCase):
    def test_classifies_de_kookeey_checkout_probe_without_exposing_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir)
            (target / "app_backend.py").write_text(
                "def opll_create_checkout(token, country, currency, proxy, **options):\n"
                "    assert token == 'at-test'\n"
                "    assert country == 'DE'\n"
                "    assert currency == 'EUR'\n"
                "    assert proxy == 'http://kookeey.example:1000'\n"
                "    assert options['request_locale'] == 'de-DE'\n"
                "    assert options['include_trial_promo'] is True\n"
                "    assert options['checkout_ui_mode'] == 'custom'\n"
                "    assert options['return_raw_payload'] is True\n"
                "    return {'cs_id':'oaics_private_probe_id'}\n",
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
                    "HME_CARD_LINK_CREATE_PROXY_URL": (
                        "http://kookeey.example:1000"
                    ),
                }
            )
            process = subprocess.run(
                [
                    sys.executable,
                    str(bridge),
                    "--source-dir",
                    str(target),
                    "--method",
                    "oaics_probe",
                    "--country",
                    "DE",
                    "--currency",
                    "EUR",
                    "--locale",
                    "de-DE",
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
        self.assertEqual(event["classification"], "oaics")
        self.assertEqual(event["checkout_id_type"], "oaics")
        self.assertEqual(event["method"], "oaics_probe")
        self.assertNotIn("oaics_private_probe_id", process.stdout)

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
                "def generate_opll_philippines_short_link(token, create_proxy, promo_proxy, amount):\n"
                "    assert token == 'at-test'\n"
                "    assert create_proxy == 'http://create.example:8000'\n"
                "    assert promo_proxy == 'socks5://promo.example:9000'\n"
                "    assert amount == '0'\n"
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


if __name__ == "__main__":
    unittest.main()
