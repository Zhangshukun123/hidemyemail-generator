from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hidemyemail_generator import card_link_runtime


class _MarkerSession:
    def __init__(self, label):
        self.label = label
        self.closed = False

    def close(self):
        self.closed = True


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text
        self.headers = {}

    def json(self):
        return self._payload


class _ApprovalSession:
    def __init__(self, events):
        self.events = events
        self.requests = []
        self.closed = False

    def get(self, url, **kwargs):
        self.events.append("checkout_get")
        self.requests.append((url, kwargs))
        return _Response(200, text="checkout")

    def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        if url.endswith("/backend-api/sentinel/ping"):
            self.events.append("sentinel")
            return _Response(200, {})
        self.events.append("approve")
        return _Response(200, {"result": "approved"})

    def close(self):
        self.closed = True
        self.events.append("close")


class Pay153TwoProxyProtocolTests(unittest.TestCase):
    checkout_proxy = "http://pool1.example:8000"
    final_proxy = "http://pool2.example:8000"

    @staticmethod
    def _page(currency, amount=0):
        return {
            "stripe_hosted_url": "https://checkout.stripe.com/c/pay/fixture",
            "payment_method_types": ["card", "paypal"],
            "total_summary": {"due": amount},
            "currency": currency.lower(),
            "config_id": "cfg_fixture",
            "init_checksum": f"checksum_{currency}_{amount}",
        }

    @classmethod
    def _checkout(cls, country, currency, *, checkout_id="cs_pay153_fixture"):
        return {
            "cs_id": checkout_id,
            "billing_country": country,
            "currency": currency,
            "processor_entity": "openai_llc",
            "stripe_publishable_key": "pk_test_fixture",
            "oai_device_id": "device-pay153",
            "_checkout_payload": cls._page(currency),
        }

    def _run_region(
        self,
        country,
        currency,
        *,
        initial_amount=0,
        taxes_error=None,
    ):
        initial_page = self._page(currency, initial_amount)
        page = self._page(currency)
        paypal_url = (
            "https://www.paypal.com/agreements/approve?ba_token="
            f"fixture_{country.lower()}"
        )
        events = []
        built_sessions = []

        def build_chatgpt(_token, proxy_url, **kwargs):
            session = _MarkerSession(f"{proxy_url}-{len(built_sessions)}")
            built_sessions.append((proxy_url, kwargs, session))
            return session

        def stripe_init(*_args, **_kwargs):
            events.append("stripe_init")
            return dict(initial_page)

        def update(*_args, **_kwargs):
            events.append("checkout_update")
            return {
                "promotion_id": card_link_runtime.PIX_TRIAL_PROMOTION_ID,
                "promotion_applied": True,
                "payment_page": dict(page),
            }

        def taxes(*_args, **_kwargs):
            events.append("checkout_taxes")
            if taxes_error is not None:
                raise taxes_error
            return {"total_summary": {"due": 0}}

        def create_pm(*_args, **_kwargs):
            events.append("payment_method")
            return "pm_fixture"

        def confirm(*_args, **_kwargs):
            events.append("confirm")
            return {"submission_attempt": {"state": "requires_approval"}}

        def approve(*_args, **_kwargs):
            events.append("approve_poll")
            return "https://pm-redirects.stripe.com/authorize/fixture"

        with ExitStack() as stack:
            validate = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_validate_access_token",
                    return_value={},
                )
            )
            build = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_build_chatgpt_session",
                    side_effect=build_chatgpt,
                )
            )
            create = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_create_checkout",
                    return_value={
                        **self._checkout(country, currency),
                        "_checkout_payload": dict(initial_page),
                    },
                )
            )
            build_stripe = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_build_stripe_session",
                    return_value=object(),
                )
            )
            init = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_stripe_init",
                    side_effect=stripe_init,
                )
            )
            direct_update = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_chatgpt_update_checkout_promotion",
                )
            )
            post_init_update = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_apply_checkout_trial_promotion",
                    side_effect=update,
                )
            )
            wait = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_wait_for_us_tr_promoted_payment_page",
                    return_value=dict(page),
                )
            )
            billing = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_billing_for_country",
                    return_value={"country": country},
                )
            )
            checkout_taxes = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_chatgpt_checkout_taxes",
                    side_effect=taxes,
                )
            )
            payment_method = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_stripe_create_paypal_method",
                    side_effect=create_pm,
                )
            )
            stripe_confirm = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_stripe_confirm",
                    side_effect=confirm,
                )
            )
            redirect = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_redirect_url_after_confirm",
                    side_effect=approve,
                )
            )
            stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_resolve_paypal_redirect_result",
                    return_value={
                        "paypal_ba_approve_url": paypal_url,
                        "payment_link_type": "paypal_approve",
                    },
                )
            )

            generator = (
                card_link_runtime.generate_opll_paypal_gb_pay153_long_link
                if country == "GB"
                else card_link_runtime.generate_opll_paypal_pay153_long_link
            )
            if taxes_error is None:
                result = generator(
                    "token",
                    self.checkout_proxy,
                    self.final_proxy,
                    "0",
                    account_email="member@example.com",
                    session_context={"device_id": "saved-device"},
                )
                error = None
            else:
                error_prefix = (
                    "PAYPAL_GB_PROTOCOL" if country == "GB" else "PAY153_PROTOCOL"
                )
                with self.assertRaisesRegex(
                    RuntimeError,
                    f"{error_prefix}_TAXES_FAILED",
                ) as caught:
                    generator(
                        "token",
                        self.checkout_proxy,
                        self.final_proxy,
                        "0",
                        account_email="member@example.com",
                    )
                result = None
                error = caught.exception

        return SimpleNamespace(**locals())

    def test_us_and_gb_use_pool1_for_create_and_pool2_from_unique_update(self):
        for country, currency, request_locale, payment_locale, timezone in (
            ("US", "USD", "en-US", "en", "America/New_York"),
            ("GB", "GBP", "en-GB", "en", "Europe/London"),
        ):
            with self.subTest(country=country):
                case = self._run_region(country, currency)

                self.assertEqual(
                    case.events,
                    [
                        "stripe_init",
                        "checkout_update",
                        "checkout_taxes",
                        "payment_method",
                        "confirm",
                        "approve_poll",
                    ],
                )
                case.direct_update.assert_not_called()
                case.post_init_update.assert_called_once()
                self.assertEqual(
                    case.post_init_update.call_args.kwargs["chatgpt_proxy_url"],
                    self.final_proxy,
                )
                self.assertEqual(case.create.call_args.args[1:4], (
                    country,
                    currency,
                    self.checkout_proxy,
                ))
                self.assertEqual(
                    case.create.call_args.kwargs["request_locale"],
                    request_locale,
                )
                self.assertEqual(case.init.call_args.args[3], self.final_proxy)
                self.assertEqual(
                    case.init.call_args.kwargs["payment_locale"],
                    payment_locale,
                )
                self.assertEqual(
                    case.init.call_args.kwargs["browser_timezone"],
                    timezone,
                )
                self.assertEqual(
                    [item[0] for item in case.built_sessions],
                    [self.checkout_proxy, self.final_proxy, self.final_proxy],
                )
                self.assertTrue(case.built_sessions[0][2].closed)
                self.assertTrue(case.built_sessions[1][2].closed)
                self.assertTrue(case.built_sessions[2][2].closed)
                taxes_call = case.checkout_taxes.call_args
                self.assertEqual(taxes_call.args[3], self.final_proxy)
                self.assertEqual(
                    taxes_call.kwargs["request_locale"],
                    request_locale,
                )
                self.assertEqual(case.result["promotion_update_count"], 1)
                self.assertEqual(
                    case.result["promotion_update_country"],
                    country,
                )
                self.assertEqual(
                    case.result["promotion_strategy"],
                    (
                        "checkout_check_then_gb_update"
                        if country == "GB"
                        else "checkout_check_then_us_update"
                    ),
                )
                self.assertEqual(
                    case.result["paypal_flow"],
                    (
                        "gb_two_proxy_promotion"
                        if country == "GB"
                        else "pay153_protocol"
                    ),
                )
                self.assertEqual(
                    case.result["checkout_taxes_currency"],
                    currency,
                )
                self.assertEqual(
                    case.result["create_proxy_used"],
                    self.checkout_proxy,
                )
                self.assertEqual(
                    case.result["payment_proxy_used"],
                    self.final_proxy,
                )
                self.assertEqual(
                    case.result["paypal_ba_approve_url"],
                    case.paypal_url,
                )

    def test_taxes_failure_stops_before_payment_method_for_us_and_gb(self):
        for country, currency in (("US", "USD"), ("GB", "GBP")):
            with self.subTest(country=country):
                case = self._run_region(
                    country,
                    currency,
                    taxes_error=RuntimeError("taxes unavailable"),
                )

                self.assertEqual(
                    case.events,
                    ["stripe_init", "checkout_update", "checkout_taxes"],
                )
                case.payment_method.assert_not_called()
                case.stripe_confirm.assert_not_called()
                case.redirect.assert_not_called()

    def test_gb_accepts_current_pre_update_amount_snapshots(self):
        for amount in (1667, 1917, 2000):
            with self.subTest(amount=amount):
                case = self._run_region("GB", "GBP", initial_amount=amount)

                self.assertEqual(case.result["stripe_amount"], "0")
                self.assertEqual(
                    case.result["promotion_strategy"],
                    "checkout_check_then_gb_update",
                )
                self.assertEqual(
                    case.result["paypal_flow"],
                    "gb_two_proxy_promotion",
                )

    def test_public_entries_require_two_distinct_proxies_and_zero_target(self):
        for generator in (
            card_link_runtime.generate_opll_paypal_pay153_long_link,
            card_link_runtime.generate_opll_paypal_gb_pay153_long_link,
        ):
            with self.subTest(generator=generator.__name__):
                for second in ("", self.checkout_proxy):
                    with self.assertRaisesRegex(RuntimeError, "两条不同"):
                        generator(
                            "token",
                            self.checkout_proxy,
                            second,
                            "0",
                        )
                with self.assertRaisesRegex(RuntimeError, "目标金额必须为 0"):
                    generator(
                        "token",
                        self.checkout_proxy,
                        self.final_proxy,
                        "2000",
                    )

    def test_oaics_sends_only_extracted_cs_id_to_stripe(self):
        country = "US"
        currency = "USD"
        oaics_id = "oaics_pay153_fixture"
        stripe_id = "cs_pay153_materialized"
        checkout = self._checkout(
            country,
            currency,
            checkout_id=oaics_id,
        )
        checkout["_checkout_payload"]["client_secret"] = (
            f"{stripe_id}_secret_fixture"
        )
        page = self._page(currency)
        paypal_url = (
            "https://www.paypal.com/agreements/approve?ba_token=oaics_fixture"
        )

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(card_link_runtime, "opll_validate_access_token")
            )
            stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_build_chatgpt_session",
                    side_effect=lambda *_args, **_kwargs: _MarkerSession("session"),
                )
            )
            stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_create_checkout",
                    return_value=checkout,
                )
            )
            stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_build_stripe_session",
                    return_value=object(),
                )
            )
            stripe_init = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_stripe_init",
                    return_value=page,
                )
            )
            direct_update = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_chatgpt_update_checkout_promotion",
                )
            )
            post_init_update = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_apply_checkout_trial_promotion",
                    return_value={"payment_page": page},
                )
            )
            stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_wait_for_us_tr_promoted_payment_page",
                    return_value=page,
                )
            )
            stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_billing_for_country",
                    return_value={"country": country},
                )
            )
            stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_chatgpt_checkout_taxes",
                    return_value={},
                )
            )
            create_pm = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_stripe_create_paypal_method",
                    return_value="pm_fixture",
                )
            )
            stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_stripe_confirm",
                    return_value={"submission_attempt": {"state": "requires_approval"}},
                )
            )
            stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_redirect_url_after_confirm",
                    return_value="https://pm-redirects.stripe.com/authorize/fixture",
                )
            )
            stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_resolve_paypal_redirect_result",
                    return_value={
                        "paypal_ba_approve_url": paypal_url,
                        "payment_link_type": "paypal_approve",
                    },
                )
            )

            result = card_link_runtime.generate_opll_paypal_pay153_long_link(
                "token",
                self.checkout_proxy,
                self.final_proxy,
                "0",
            )

        direct_update.assert_not_called()
        post_init_update.assert_called_once()
        self.assertEqual(stripe_init.call_args.args[0], stripe_id)
        self.assertEqual(
            stripe_init.call_args.kwargs["checkout"]["cs_id"],
            stripe_id,
        )
        self.assertEqual(create_pm.call_args.args[1], stripe_id)
        self.assertEqual(result["cs_id"], oaics_id)
        self.assertEqual(result["stripe_payment_page_id"], stripe_id)
        self.assertEqual(result["promotion_update_count"], 1)

    def test_oaics_setup_intent_is_rejected_before_stripe_init(self):
        checkout = self._checkout(
            "GB",
            "GBP",
            checkout_id="oaics_pay153_setup_fixture",
        )
        checkout["_checkout_payload"] = {
            "currency": "GBP",
            "total_summary": {"due": 0},
            "payment_method_types": ["card", "paypal"],
        }

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(card_link_runtime, "opll_validate_access_token")
            )
            stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_build_chatgpt_session",
                    side_effect=lambda *_args, **_kwargs: _MarkerSession("session"),
                )
            )
            stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_create_checkout",
                    return_value=checkout,
                )
            )
            direct_update = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_chatgpt_update_checkout_promotion",
                    return_value={
                        "currency": "GBP",
                        "total_summary": {"due": 0},
                        "payment_method_types": ["card", "paypal"],
                    },
                )
            )
            stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_chatgpt_fetch_checkout",
                    return_value={},
                )
            )
            stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_chatgpt_checkout_taxes",
                    return_value={},
                )
            )
            stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_billing_for_country",
                    return_value={"country": "GB"},
                )
            )
            stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_build_stripe_session",
                    return_value=object(),
                )
            )
            stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_stripe_create_paypal_confirmation_token",
                    return_value="ctoken_fixture",
                )
            )
            materialize = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_chatgpt_confirm_custom_payment_method",
                    return_value={
                        "type": "setup_intent",
                        "client_secret": "seti_fixture_secret_value",
                    },
                )
            )
            stripe_init = stack.enter_context(
                patch.object(card_link_runtime, "opll_stripe_init")
            )
            create_pm = stack.enter_context(
                patch.object(card_link_runtime, "opll_stripe_create_paypal_method")
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "PAYPAL_GB_PROTOCOL_SETUP_INTENT_UNSUPPORTED",
            ):
                card_link_runtime.generate_opll_paypal_gb_pay153_long_link(
                    "token",
                    self.checkout_proxy,
                    self.final_proxy,
                    "0",
                )

        direct_update.assert_called_once()
        self.assertEqual(direct_update.call_args.args[2], self.final_proxy)
        self.assertFalse(materialize.call_args.kwargs["sentinel_required"])
        stripe_init.assert_not_called()
        create_pm.assert_not_called()

    def test_force_approval_ignores_early_redirect_then_warms_sentinel_and_polls(self):
        events = []
        diagnostics = []
        approval = _ApprovalSession(events)
        checkout = {
            "cs_id": "cs_pay153_fixture",
            "billing_country": "GB",
            "currency": "GBP",
            "processor_entity": "openai_llc",
            "oai_device_id": "device-pay153",
        }

        def poll(*_args, **_kwargs):
            events.append("poll")
            return "https://pm-redirects.stripe.com/authorize/fixture"

        with patch.object(
            card_link_runtime,
            "opll_stripe_payment_page_redirect_url",
            side_effect=poll,
        ), patch.object(
            card_link_runtime,
            "opll_build_chatgpt_session",
            return_value=approval,
        ) as build:
            result = card_link_runtime.opll_redirect_url_after_confirm(
                "token",
                object(),
                {
                    "next_action": {
                        "type": "redirect_to_url",
                        "redirect_to_url": {
                            "url": "https://www.paypal.com/agreements/approve?ba_token=early"
                        },
                    }
                },
                checkout["cs_id"],
                "pk_test_fixture",
                {},
                checkout,
                self.final_proxy,
                clean_session_blocked_retry=True,
                approve_sentinel_required=True,
                force_checkout_approval=True,
                diagnostic_log=diagnostics.append,
            )

        self.assertEqual(
            result,
            "https://pm-redirects.stripe.com/authorize/fixture",
        )
        self.assertEqual(events, ["checkout_get", "sentinel", "approve", "poll", "close"])
        self.assertTrue(approval.closed)
        self.assertEqual(build.call_args.args[1], self.final_proxy)
        self.assertTrue(any("忽略 Confirm 内提前返回" in item for item in diagnostics))

    def test_gb_protocol_poll_failure_requires_checkout_rebuild(self):
        events = []
        approval = _ApprovalSession(events)
        checkout = {
            "cs_id": "cs_pay153_gb_fixture",
            "billing_country": "GB",
            "currency": "GBP",
            "processor_entity": "openai_llc",
            "oai_device_id": "device-pay153-gb",
        }

        with patch.object(
            card_link_runtime,
            "opll_stripe_payment_page_redirect_url",
            side_effect=RuntimeError("poll timeout"),
        ), patch.object(
            card_link_runtime,
            "opll_build_chatgpt_session",
            return_value=approval,
        ):
            with self.assertRaisesRegex(
                card_link_runtime.OpllPay153CheckoutRebuildRequired,
                "PAYPAL_GB_PROTOCOL_APPROVAL_OR_POLL_FAILED",
            ):
                card_link_runtime.opll_redirect_url_after_confirm(
                    "token",
                    object(),
                    {"submission_attempt": {"state": "requires_approval"}},
                    checkout["cs_id"],
                    "pk_test_fixture",
                    {},
                    checkout,
                    self.final_proxy,
                    clean_session_blocked_retry=True,
                    approve_sentinel_required=True,
                    force_checkout_approval=True,
                    protocol_error_prefix="PAYPAL_GB_PROTOCOL",
                )

        self.assertEqual(events, ["checkout_get", "sentinel", "approve", "close"])
        self.assertTrue(
            card_link_runtime.opll_pay153_checkout_rebuild_required(
                RuntimeError("PAYPAL_GB_PROTOCOL_TAXES_FAILED: fixture")
            )
        )


if __name__ == "__main__":
    unittest.main()
