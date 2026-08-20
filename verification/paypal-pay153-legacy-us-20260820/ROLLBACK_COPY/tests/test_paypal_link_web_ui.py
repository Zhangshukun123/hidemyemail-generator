import tempfile
import unittest
from pathlib import Path

from hidemyemail_generator.registration_proxy import RegistrationProxyStore
from hidemyemail_generator.web_ui import build_app_page


class PayPalLinkWebUiTests(unittest.TestCase):
    def test_page_exposes_desktop_paypal_link_modes_and_options(self):
        page = build_app_page()

        self.assertIn('value="paypal_us">PayPal / 美国 · USD', page)
        self.assertIn('value="paypal_gb">PayPal / 英国 · GBP', page)
        self.assertIn("PayPal / 美国 · USD · 双代理严格 0", page)
        self.assertIn("PayPal / 英国 · GBP · 双代理严格 0", page)
        self.assertIn("池1 Checkout/优惠检查", page)
        self.assertIn("池2 Update→Taxes→Stripe→Approve/Poll", page)
        self.assertIn('id="cardLinkTargetAmount"', page)
        self.assertIn('id="cardLinkBillingCountryState"', page)
        self.assertIn("跟随 IP 地址", page)
        self.assertIn("cardLinkPaymentPayload", page)
        self.assertIn('id="quickPaypalSentinelSo" type="checkbox" checked', page)
        self.assertIn('id="cardLinkSentinelSo" type="checkbox" checked', page)
        self.assertIn("sentinel_so_enabled", page)
        self.assertIn("Checkout 创建时携带", page)

    def test_proxy_store_accepts_new_paypal_mode_preferences(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RegistrationProxyStore(Path(temp_dir) / "hme.db")
            state = store.configure(
                enabled=False,
                proxy_line="proxy.example:3010:user:password",
                card_link_modes={
                    "paypal_us": "dynamic",
                    "paypal_gb": "dynamic",
                },
                card_link_countries={
                    "paypalUsCreate": "US",
                    "paypalUsFollowup": "US",
                    "paypalGbCreate": "GB",
                },
            )

        self.assertEqual(state["cardLinkModes"]["paypal_us"], "dynamic")
        self.assertEqual(state["cardLinkModes"]["paypal_gb"], "dynamic")
        self.assertEqual(state["cardLinkCountries"]["paypalUsCreate"], "US")
        self.assertEqual(state["cardLinkCountries"]["paypalUsFollowup"], "US")
        self.assertEqual(state["cardLinkCountries"]["paypalGbCreate"], "GB")


if __name__ == "__main__":
    unittest.main()
