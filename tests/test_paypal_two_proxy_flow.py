import unittest

from hidemyemail_generator._card_link_payment_modes import (
    PAYMENT_MODES,
    PAYPAL_GB_TWO_PROXY_FLOW,
    PAYPAL_PAY153_PROTOCOL_FLOW,
    payment_mode_paypal_flow,
)
from hidemyemail_generator.paypal_two_proxy_flow import (
    DiagnosticPayPalCheckoutTaxesView,
    DiagnosticPayPalTwoProxyFlowView,
    PayPalCheckoutTaxesPresenter,
    PayPalTwoProxyFlowPresenter,
    country_request_locale,
)
from hidemyemail_generator.paypal_protocol_profile import (
    PAYPAL_GB_PROTOCOL_PROFILE,
    PAYPAL_US_PROTOCOL_PROFILE,
)


class PayPalTwoProxyFlowPresenterTests(unittest.TestCase):
    @staticmethod
    def _amount_reader(payload):
        amount = payload.get("amount")
        return ("" if amount is None else str(amount)), "fixture.amount"

    def _inspect(self, *, country, currency, amount):
        messages = []
        model = PayPalTwoProxyFlowPresenter(
            self._amount_reader,
            DiagnosticPayPalTwoProxyFlowView(messages.append),
        ).inspect_checkout(
            country=country,
            currency=currency,
            checkout_proxy_url=f"http://pool1-{country.lower()}.example:8000",
            final_proxy_url=f"http://pool2-{country.lower()}.example:8000",
            target_amount="0",
            payload={} if amount is None else {"amount": amount},
        )
        return model, messages

    def test_checkout_zero_still_requires_pool2_update_for_us_and_gb(self):
        for country, currency in (("US", "USD"), ("GB", "GBP")):
            with self.subTest(country=country):
                model, messages = self._inspect(
                    country=country,
                    currency=currency,
                    amount=0,
                )

                self.assertTrue(model.checkout_has_promotion)
                self.assertTrue(model.update_required)
                self.assertEqual(
                    model.update_proxy_url,
                    f"http://pool2-{country.lower()}.example:8000",
                )
                self.assertIn(
                    f"仍交给代理 2 {country} 强制执行一次 Update",
                    messages[0],
                )

    def test_us_and_gb_zero_modes_resolve_to_pay153_protocol(self):
        for mode_name, expected_flow in (
            ("PayPal支付链接 US/USD", PAYPAL_PAY153_PROTOCOL_FLOW),
            ("PayPal支付链接 GB/GBP", PAYPAL_GB_TWO_PROXY_FLOW),
        ):
            with self.subTest(mode=mode_name):
                self.assertEqual(
                    payment_mode_paypal_flow(PAYMENT_MODES[mode_name], "0"),
                    expected_flow,
                )

    def test_regional_protocol_profiles_keep_source_project_policies(self):
        self.assertEqual(
            (
                PAYPAL_US_PROTOCOL_PROFILE.country,
                PAYPAL_US_PROTOCOL_PROFILE.currency,
                PAYPAL_US_PROTOCOL_PROFILE.standard_amounts,
                PAYPAL_US_PROTOCOL_PROFILE.error_prefix,
            ),
            ("US", "USD", ("2000",), "PAY153_PROTOCOL"),
        )
        self.assertEqual(
            (
                PAYPAL_GB_PROTOCOL_PROFILE.country,
                PAYPAL_GB_PROTOCOL_PROFILE.currency,
                PAYPAL_GB_PROTOCOL_PROFILE.standard_amounts,
                PAYPAL_GB_PROTOCOL_PROFILE.error_prefix,
                PAYPAL_GB_PROTOCOL_PROFILE.promotion_timing,
                PAYPAL_GB_PROTOCOL_PROFILE.checkout_includes_trial_promo,
                PAYPAL_GB_PROTOCOL_PROFILE.primary_proxy_finishes_checkout,
                PAYPAL_GB_PROTOCOL_PROFILE.requires_same_checkout_post_approve,
                PAYPAL_GB_PROTOCOL_PROFILE.browser_http_policy,
            ),
            (
                "GB",
                "GBP",
                ("1667", "1917", "2000"),
                "PAYPAL_GB_PROTOCOL",
                "post_approve",
                False,
                True,
                True,
                "bound_when_supplied",
            ),
        )
        self.assertEqual(PAYPAL_US_PROTOCOL_PROFILE.promotion_timing, "pre_confirm")
        self.assertTrue(PAYPAL_US_PROTOCOL_PROFILE.checkout_includes_trial_promo)
        self.assertFalse(PAYPAL_US_PROTOCOL_PROFILE.primary_proxy_finishes_checkout)
        self.assertFalse(
            PAYPAL_US_PROTOCOL_PROFILE.requires_same_checkout_post_approve
        )

    def test_nonzero_or_missing_amount_still_routes_update_to_pool2(self):
        for country, currency in (("US", "USD"), ("GB", "GBP")):
            for amount in (2000, None):
                with self.subTest(country=country, amount=amount):
                    model, _messages = self._inspect(
                        country=country,
                        currency=currency,
                        amount=amount,
                    )

                    self.assertFalse(model.checkout_has_promotion)
                    self.assertTrue(model.update_required)
                    self.assertEqual(
                        model.update_proxy_url,
                        f"http://pool2-{country.lower()}.example:8000",
                    )

    def test_checkout_taxes_submit_through_region_protocol_proxy(self):
        for country, currency in (("US", "USD"), ("GB", "GBP")):
            with self.subTest(country=country):
                calls = []
                messages = []
                pool_number = 1 if country == "GB" else 2
                proxy = (
                    f"http://pool{pool_number}-{country.lower()}.example:8000"
                )
                session = object()
                checkout = {
                    "cs_id": f"cs_pay153_{country.lower()}",
                    "billing_country": country,
                    "currency": currency,
                    "oai_device_id": "device-pay153",
                }
                billing = {"country": country}

                def submitter(*args, **kwargs):
                    calls.append((args, kwargs))
                    return {"total_summary": {"due": 0}}

                model = PayPalCheckoutTaxesPresenter(
                    submitter,
                    DiagnosticPayPalCheckoutTaxesView(messages.append),
                ).submit_checkout_taxes(
                    access_token="token",
                    checkout=checkout,
                    billing=billing,
                    proxy_url=proxy,
                    country=country,
                    currency=currency,
                    diagnostic_log=messages.append,
                    session_context={"device_id": "saved-device"},
                    chatgpt_session=session,
                )

                self.assertTrue(model.applied)
                self.assertEqual(model.billing_country, country)
                self.assertEqual(model.currency, currency)
                self.assertEqual(model.response_keys, ("total_summary",))
                self.assertEqual(calls[0][0], ("token", checkout, billing, proxy))
                self.assertEqual(
                    calls[0][1]["request_locale"],
                    country_request_locale(country),
                )
                self.assertEqual(calls[0][1]["device_id"], "device-pay153")
                self.assertTrue(calls[0][1]["require_success"])
                self.assertEqual(
                    calls[0][1]["flow_label"],
                    "PayPal GB/GBP" if country == "GB" else "PayPal US/PAY153",
                )
                self.assertIs(calls[0][1]["chatgpt_session"], session)
                self.assertIn(
                    f"billing_country={country}，currency={currency}",
                    messages[0],
                )
                self.assertIn("Checkout Taxes 已应用", messages[-1])

    def test_checkout_taxes_rejects_region_mismatch_before_gateway(self):
        calls = []

        for country, currency, wrong_country in (
            ("US", "USD", "GB"),
            ("GB", "GBP", "US"),
        ):
            with self.subTest(country=country):
                with self.assertRaisesRegex(
                    RuntimeError,
                    f"地区必须统一为 {country}",
                ):
                    PayPalCheckoutTaxesPresenter(
                        lambda *args, **kwargs: calls.append((args, kwargs)),
                        DiagnosticPayPalCheckoutTaxesView(),
                    ).submit_checkout_taxes(
                        access_token="token",
                        checkout={
                            "cs_id": "cs_pay153_fixture",
                            "billing_country": country,
                            "currency": currency,
                        },
                        billing={"country": wrong_country},
                        proxy_url=f"http://pool2-{country.lower()}.example:8000",
                        country=country,
                        currency=currency,
                    )

        self.assertEqual(calls, [])

    def test_checkout_taxes_rejects_currency_mismatch_before_gateway(self):
        calls = []

        with self.assertRaisesRegex(RuntimeError, "币种必须为 GBP"):
            PayPalCheckoutTaxesPresenter(
                lambda *args, **kwargs: calls.append((args, kwargs)),
                DiagnosticPayPalCheckoutTaxesView(),
            ).submit_checkout_taxes(
                access_token="token",
                checkout={
                    "cs_id": "cs_pay153_fixture",
                    "billing_country": "GB",
                    "currency": "USD",
                },
                billing={"country": "GB"},
                proxy_url="http://pool2-gb.example:8000",
                country="GB",
                currency="GBP",
            )

        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
