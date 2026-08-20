from __future__ import annotations

import subprocess
import sys
import unittest

from hidemyemail_generator import card_link_runtime


class _CheckoutResponse:
    status_code = 200
    text = ""

    @staticmethod
    def json() -> dict:
        return {
            "checkout_session_id": "cs_test_sentinel",
            "processor_entity": "openai_llc",
        }


class _CheckoutSession:
    opll_oai_device_id = "device-checkout-test"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def post(self, url, **options):
        self.calls.append({"url": url, **options})
        return _CheckoutResponse()


class PayPalCheckoutSentinelTests(unittest.TestCase):
    def test_vendored_sentinel_package_imports_without_core_path_injection(self):
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                (
                    "from hidemyemail_generator.vendor.gptfree_register.core."
                    "gpt_trial_protocol.models import BrowserProfile; "
                    "print(BrowserProfile.__name__)"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "BrowserProfile")

    def test_checkout_create_promotion_at_target_skips_update(self):
        self.assertFalse(
            card_link_runtime.opll_should_update_checkout_promotion(
                apply_trial_promotion=True,
                checkout_includes_trial_promo=True,
                target_amount="0",
                actual_amount="0",
            )
        )

    def test_checkout_create_promotion_amount_mismatch_requests_single_fallback_update(self):
        # The caller consumes this decision once for the existing Checkout; it
        # must not acquire a second proxy or rebuild the Checkout first.
        self.assertTrue(
            card_link_runtime.opll_should_update_checkout_promotion(
                apply_trial_promotion=True,
                checkout_includes_trial_promo=True,
                target_amount="0",
                actual_amount="2000",
            )
        )

    def test_checkout_create_submits_promotion_and_sen_so_headers_together(self):
        session = _CheckoutSession()
        observed_provider_args: list[tuple[str, str, str]] = []

        def provider(proxy_url: str, device_id: str, locale: str) -> dict[str, str]:
            observed_provider_args.append((proxy_url, device_id, locale))
            return {
                "openai-sentinel-token": '{"p":"main"}',
                "openai-sentinel-so-token": '{"so":"observer"}',
            }

        checkout = card_link_runtime.opll_create_checkout(
            "at-checkout-test",
            "US",
            "USD",
            "http://proxy.example:8000",
            request_locale="en-US",
            include_trial_promo=True,
            chatgpt_session=session,
            sentinel_so_enabled=True,
            sentinel_header_provider=provider,
        )

        self.assertEqual(checkout["cs_id"], "cs_test_sentinel")
        self.assertEqual(
            observed_provider_args,
            [("http://proxy.example:8000", "device-checkout-test", "en-US")],
        )
        request = session.calls[0]
        self.assertEqual(
            request["json"]["promo_campaign"]["promo_campaign_id"],
            "plus-1-month-free",
        )
        self.assertEqual(
            request["headers"]["openai-sentinel-token"],
            '{"p":"main"}',
        )
        self.assertEqual(
            request["headers"]["openai-sentinel-so-token"],
            '{"so":"observer"}',
        )


if __name__ == "__main__":
    unittest.main()
