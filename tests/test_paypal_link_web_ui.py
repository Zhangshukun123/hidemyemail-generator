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
        self.assertIn("PayPal / 英国 · GBP · 全程第一代理", page)
        self.assertIn('id="cardLinkTargetAmount"', page)
        self.assertIn('id="cardLinkBillingCountryState"', page)
        self.assertIn("跟随 IP 地址", page)
        self.assertIn("cardLinkPaymentPayload", page)

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
                    "paypalGbCreate": "GB",
                },
            )

        self.assertEqual(state["cardLinkModes"]["paypal_us"], "dynamic")
        self.assertEqual(state["cardLinkModes"]["paypal_gb"], "dynamic")
        self.assertEqual(state["cardLinkCountries"]["paypalUsCreate"], "US")
        self.assertEqual(state["cardLinkCountries"]["paypalGbCreate"], "GB")


if __name__ == "__main__":
    unittest.main()
