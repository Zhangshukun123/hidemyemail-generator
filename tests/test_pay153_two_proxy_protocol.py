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


class _RetryableProtocolError(RuntimeError):
    retryable = True


class _PermanentProtocolError(RuntimeError):
    retryable = False


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
    def _page(
        currency,
        amount=0,
        *,
        state="",
        checkout_id="cs_pay153_fixture",
    ):
        page = {
            "stripe_hosted_url": (
                f"https://checkout.stripe.com/c/pay/{checkout_id}"
                "#stripe-fragment-fixture"
            ),
            "payment_method_types": ["card", "paypal"],
            "total_summary": {"due": amount},
            "currency": currency.lower(),
            "config_id": "cfg_fixture",
            "init_checksum": f"checksum_{currency}_{amount}",
        }
        if state:
            page["submission_attempt"] = {"state": state}
        return page

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
        initial_amount=None,
        tax_amount=None,
        taxes_error=None,
        tax_region_error=None,
        snapshot_error=None,
        approval_error=None,
        ba_available=True,
        update_effects=None,
        post_init_effects=None,
        post_init_states=None,
        confirm_state="requires_approval",
        expected_error_pattern="",
    ):
        if initial_amount is None:
            initial_amount = 1917 if country == "GB" else 0
        if tax_amount is None:
            tax_amount = 2000 if country == "GB" else initial_amount
        initial_page = self._page(currency, initial_amount)
        paypal_url = (
            "https://www.paypal.com/agreements/approve?ba_token="
            f"fixture_{country.lower()}"
        )
        events = []
        built_sessions = []
        pool1_stripe = _MarkerSession("pool1-stripe")
        pool2_stripe = _MarkerSession("pool2-stripe")
        update_values = list(update_effects or ({"ok": True},))
        post_init_values = list(post_init_effects or (0,))
        post_state_values = list(post_init_states or ("approved",))
        post_init_count = 0
        stripe_init_contexts = []
        stripe_init_context_snapshots = []
        confirm_context_snapshot = {}
        stable_context_fields = (
            "guid",
            "muid",
            "sid",
            "stripe_js_id",
            "elements_session_id",
        )

        def build_chatgpt(_token, proxy_url, **kwargs):
            session = _MarkerSession(f"{proxy_url}-{len(built_sessions)}")
            built_sessions.append((proxy_url, kwargs, session))
            return session

        def build_stripe(proxy_url, **_kwargs):
            return (
                pool1_stripe
                if proxy_url == self.checkout_proxy
                else pool2_stripe
            )

        def stripe_init(*_args, **_kwargs):
            nonlocal post_init_count
            stripe_context = _kwargs.get("ctx")
            stripe_init_contexts.append(stripe_context)
            stripe_init_context_snapshots.append(
                {
                    field: str((stripe_context or {}).get(field) or "")
                    for field in stable_context_fields
                }
            )
            proxy_url = _args[3]
            if country == "GB" and proxy_url == self.final_proxy:
                index = post_init_count
                post_init_count += 1
                effect = post_init_values[
                    min(index, len(post_init_values) - 1)
                ]
                if isinstance(effect, Exception):
                    events.append("post_promo_init_error")
                    raise effect
                state = post_state_values[
                    min(index, len(post_state_values) - 1)
                ]
                events.append(f"post_promo_init_{effect}_{state}")
                if isinstance(effect, dict):
                    return dict(effect)
                return self._page(currency, effect, state=state)
            events.append("stripe_init")
            return dict(initial_page)

        def direct_update(*_args, **_kwargs):
            events.append("checkout_update")
            effect = update_values.pop(0) if update_values else {"ok": True}
            if isinstance(effect, Exception):
                raise effect
            if isinstance(effect, dict) and effect != {"ok": True}:
                return dict(effect)
            return {
                "promotion_id": card_link_runtime.PIX_TRIAL_PROMOTION_ID,
                "currency": currency,
                "total_summary": {"due": 0},
                "payment_method_types": ["card", "paypal"],
            }

        def apply_update(*_args, **_kwargs):
            events.append("checkout_update")
            return {
                "promotion_id": card_link_runtime.PIX_TRIAL_PROMOTION_ID,
                "promotion_applied": True,
                "payment_page": self._page(currency),
            }

        def verify_promotion(*_args, **_kwargs):
            return self._page(currency)

        def warmup(*_args, **_kwargs):
            events.append("checkout_warmup")

        def taxes(*_args, **_kwargs):
            events.append("checkout_taxes")
            if taxes_error is not None:
                raise taxes_error
            return {"total_summary": {"due": 0}}

        def tax_region(*_args, **_kwargs):
            events.append("stripe_tax_region")
            if tax_region_error is not None:
                raise tax_region_error
            return self._page(currency, tax_amount)

        def checkout_snapshot(*_args, **_kwargs):
            events.append("checkout_snapshot")
            if snapshot_error is not None:
                raise snapshot_error
            return {"status_code": 204, "applied": True}

        def create_pm(*_args, **_kwargs):
            events.append("payment_method")
            return "pm_fixture"

        def prefetch_sentinel(*_args, **_kwargs):
            events.append("sentinel_prefetch")
            return {
                "openai-sentinel-token": "sentinel-token-fixture",
                "openai-sentinel-so-token": "sentinel-so-fixture",
            }

        def confirm(*_args, **_kwargs):
            events.append("confirm")
            confirm_context_snapshot.update(
                {
                    field: str((_args[5] or {}).get(field) or "")
                    for field in stable_context_fields
                }
            )
            confirm_context_snapshot["checkout_amount"] = str(
                (_args[5] or {}).get("checkout_amount") or ""
            )
            return {"submission_attempt": {"state": confirm_state}}

        def approve(*_args, **_kwargs):
            events.append("approve_poll")
            if approval_error is not None:
                raise approval_error
            return "https://pm-redirects.stripe.com/authorize/fixture"

        def resolve_ba(*_args, **_kwargs):
            if country == "GB":
                events.append("resolve_ba")
            return {
                "paypal_ba_approve_url": paypal_url if ba_available else "",
                "payment_link_type": (
                    "paypal_approve" if ba_available else ""
                ),
            }

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
            checkout_warmup = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_chatgpt_checkout_warmup",
                    side_effect=warmup,
                )
            )
            build_stripe = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_build_stripe_session",
                    side_effect=build_stripe,
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
                    side_effect=direct_update,
                )
            )
            post_init_update = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_apply_checkout_trial_promotion",
                    side_effect=apply_update,
                )
            )
            wait = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_wait_for_us_tr_promoted_payment_page",
                    side_effect=verify_promotion,
                )
            )
            billing = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_billing_for_country",
                    return_value={
                        "name": "Alex Fixture",
                        "email": "member@example.com",
                        "phone": "+442079460000",
                        "country": country,
                        "line1": "221B Baker Street",
                        "city": "London",
                        "state": "" if country == "GB" else "CA",
                        "postal_code": "NW1 6XE",
                        "line2": "",
                    },
                )
            )
            stripe_tax_region = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_stripe_update_tax_region",
                    side_effect=tax_region,
                )
            )
            snapshot = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_paypal_gb_chatgpt_checkout_snapshot",
                    side_effect=checkout_snapshot,
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
            sentinel_prefetch = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_prefetch_paypal_approval_sentinel",
                    side_effect=prefetch_sentinel,
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
            resolve_ba = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_resolve_paypal_redirect_result",
                    side_effect=resolve_ba,
                )
            )
            retrieve = stack.enter_context(
                patch.object(
                    card_link_runtime,
                    "opll_stripe_retrieve_payment_page",
                )
            )
            sleep = stack.enter_context(
                patch.object(card_link_runtime.time, "sleep")
            )

            generator = (
                card_link_runtime.generate_opll_paypal_gb_pay153_long_link
                if country == "GB"
                else card_link_runtime.generate_opll_paypal_pay153_long_link
            )
            def call():
                return generator(
                    "token",
                    self.checkout_proxy,
                    self.final_proxy,
                    "0",
                    account_email="member@example.com",
                    session_context={"device_id": "saved-device"},
                )
            if expected_error_pattern:
                with self.assertRaisesRegex(
                    RuntimeError,
                    expected_error_pattern,
                ) as caught:
                    call()
                result = None
                error = caught.exception
            else:
                result = call()
                error = None

        return SimpleNamespace(**locals())

    def test_us_keeps_legacy_pool2_pre_confirm_update_order(self):
        case = self._run_region("US", "USD")

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
        self.assertTrue(case.create.call_args.kwargs["include_trial_promo"])
        case.direct_update.assert_not_called()
        case.post_init_update.assert_called_once()
        self.assertEqual(
            case.post_init_update.call_args.kwargs["chatgpt_proxy_url"],
            self.final_proxy,
        )
        self.assertEqual(
            case.create.call_args.args[1:4],
            ("US", "USD", self.checkout_proxy),
        )
        self.assertEqual(case.init.call_args.args[3], self.final_proxy)
        self.assertIs(case.init.call_args.kwargs["stripe"], case.pool2_stripe)
        self.assertEqual(case.checkout_taxes.call_args.args[3], self.final_proxy)
        case.checkout_warmup.assert_not_called()
        case.stripe_tax_region.assert_not_called()
        case.snapshot.assert_not_called()
        self.assertEqual(case.result["promotion_update_count"], 1)
        self.assertEqual(case.result["promotion_update_country"], "US")
        self.assertEqual(
            case.result["promotion_strategy"],
            "checkout_check_then_us_update",
        )
        self.assertEqual(case.result["paypal_flow"], "pay153_protocol")
        self.assertEqual(case.result["payment_proxy_used"], self.final_proxy)

    def test_gb_uses_pool1_ba_then_pool2_same_checkout_update(self):
        case = self._run_region("GB", "GBP")

        self.assertEqual(
            case.events,
            [
                "checkout_warmup",
                "stripe_init",
                "stripe_tax_region",
                "checkout_snapshot",
                "payment_method",
                "sentinel_prefetch",
                "confirm",
                "approve_poll",
                "resolve_ba",
                "checkout_update",
                "post_promo_init_0_approved",
            ],
        )
        self.assertFalse(case.create.call_args.kwargs["include_trial_promo"])
        self.assertIsNone(case.create.call_args.kwargs["checkout_ui_mode"])
        self.assertTrue(case.create.call_args.kwargs["compact_json"])
        self.assertEqual(
            case.create.call_args.kwargs["referer_url"],
            "https://chatgpt.com/",
        )
        self.assertEqual(
            case.create.call_args.args[1:4],
            ("GB", "GBP", self.checkout_proxy),
        )
        case.post_init_update.assert_not_called()
        case.direct_update.assert_called_once()
        case.checkout_taxes.assert_not_called()
        case.wait.assert_not_called()
        case.retrieve.assert_not_called()
        case.resolve_ba.assert_called_once()

        update_call = case.direct_update.call_args
        self.assertEqual(update_call.args[1]["cs_id"], "cs_pay153_fixture")
        self.assertEqual(update_call.args[2], self.final_proxy)
        self.assertEqual(
            update_call.kwargs["device_id"],
            "device-pay153",
        )
        self.assertIs(
            update_call.kwargs["session"],
            case.built_sessions[-1][2],
        )
        self.assertEqual(case.built_sessions[-1][0], self.final_proxy)

        self.assertEqual(case.build_stripe.call_count, 2)
        first_stripe_build, second_stripe_build = (
            case.build_stripe.call_args_list
        )
        self.assertEqual(first_stripe_build.args[0], self.checkout_proxy)
        self.assertEqual(second_stripe_build.args[0], self.final_proxy)
        self.assertIsNot(case.pool1_stripe, case.pool2_stripe)
        self.assertEqual(case.init.call_count, 2)
        first_init, post_promo_init = case.init.call_args_list
        self.assertEqual(first_init.args[0], "cs_pay153_fixture")
        self.assertEqual(post_promo_init.args[0], "cs_pay153_fixture")
        self.assertEqual(first_init.args[3], self.checkout_proxy)
        self.assertEqual(post_promo_init.args[3], self.final_proxy)
        self.assertIs(first_init.kwargs["stripe"], case.pool1_stripe)
        self.assertIs(post_promo_init.kwargs["stripe"], case.pool2_stripe)
        self.assertIs(case.payment_method.call_args.args[0], case.pool1_stripe)
        self.assertEqual(
            case.stripe_confirm.call_args.args[4]["total_summary"]["due"],
            2000,
        )
        checkout_session = case.built_sessions[0][2]
        case.checkout_warmup.assert_called_once()
        self.assertIs(
            case.checkout_warmup.call_args.args[0],
            checkout_session,
        )
        self.assertEqual(
            case.checkout_warmup.call_args.args[1],
            "cs_pay153_fixture",
        )
        self.assertTrue(
            case.checkout_warmup.call_args.kwargs[
                "allow_retryable_status_failure"
            ]
        )
        case.stripe_tax_region.assert_called_once()
        self.assertIs(
            case.stripe_tax_region.call_args.args[0],
            case.pool1_stripe,
        )
        self.assertFalse(
            case.stripe_tax_region.call_args.kwargs["country_only"]
        )
        self.assertTrue(
            case.stripe_tax_region.call_args.kwargs[
                "omit_empty_address_fields"
            ]
        )
        case.snapshot.assert_called_once()
        self.assertIs(case.snapshot.call_args.args[0], checkout_session)
        case.sentinel_prefetch.assert_called_once()
        self.assertIs(
            case.sentinel_prefetch.call_args.args[0],
            checkout_session,
        )
        self.assertEqual(
            case.sentinel_prefetch.call_args.args[3],
            self.checkout_proxy,
        )
        self.assertEqual(
            case.sentinel_prefetch.call_args.kwargs["device_id"],
            "device-pay153",
        )
        self.assertFalse(
            case.sentinel_prefetch.call_args.kwargs["warmup_required"]
        )
        self.assertIs(case.stripe_confirm.call_args.args[0], case.pool1_stripe)
        self.assertIs(case.redirect.call_args.args[1], case.pool1_stripe)
        self.assertFalse(
            case.redirect.call_args.kwargs["approve_sentinel_required"]
        )
        self.assertTrue(case.redirect.call_args.kwargs["force_checkout_approval"])

        self.assertEqual(case.built_sessions[0][0], self.checkout_proxy)
        self.assertIs(
            case.redirect.call_args.kwargs["chatgpt_session"],
            checkout_session,
        )
        self.assertEqual(case.redirect.call_args.args[7], self.checkout_proxy)

        stable_context_fields = (
            "guid",
            "muid",
            "sid",
            "stripe_js_id",
            "elements_session_id",
        )
        confirm_ctx = case.stripe_confirm.call_args.args[5]
        post_promo_ctx = post_promo_init.kwargs["ctx"]
        self.assertIs(confirm_ctx, post_promo_ctx)
        for field in stable_context_fields:
            self.assertEqual(
                case.confirm_context_snapshot[field],
                case.stripe_init_context_snapshots[-1][field],
            )
            self.assertEqual(confirm_ctx[field], post_promo_ctx[field])
        self.assertEqual(case.confirm_context_snapshot["checkout_amount"], "2000")

        expected_return_url = (
            card_link_runtime.opll_paypal_gb_confirm_return_url(
                "cs_pay153_fixture",
                case.create.return_value,
                case.initial_page["stripe_hosted_url"],
            )
        )
        self.assertEqual(
            case.stripe_confirm.call_args.kwargs["return_url"],
            expected_return_url,
        )
        self.assertIn("returned_from_redirect=true", expected_return_url)
        self.assertIn("ui_mode=custom", expected_return_url)
        self.assertTrue(expected_return_url.endswith("#stripe-fragment-fixture"))

        self.assertEqual(
            case.result["promotion_strategy"],
            "standard_checkout_confirm_approve_then_same_checkout_update",
        )
        self.assertEqual(case.result["promotion_timing"], "post_approve")
        self.assertTrue(case.result["promotion_required"])
        self.assertEqual(case.result["promotion_update_count"], 1)
        self.assertEqual(case.result["promotion_update_attempts"], 1)
        self.assertEqual(case.result["post_approval_init_count"], 1)
        self.assertEqual(
            case.result["post_approval_init_proxy_used"],
            self.final_proxy,
        )
        self.assertEqual(
            case.result["promotion_checkout_id"],
            "cs_pay153_fixture",
        )
        self.assertTrue(case.result["approval_completed_before_promotion"])
        self.assertTrue(case.result["same_checkout_promotion"])
        self.assertTrue(case.result["session_proxy_consistent"])
        self.assertTrue(case.result["stripe_context_consistent"])
        self.assertEqual(case.result["stripe_amount"], "0")
        self.assertEqual(case.result["pre_promotion_amount"], "1917")
        self.assertEqual(case.result["pre_confirm_amount"], "2000")
        self.assertEqual(case.result["approval_state"], "approved")
        self.assertEqual(case.result["paypal_ba_state"], "approved")
        self.assertTrue(case.result["ba_preserved_after_promotion"])
        self.assertTrue(case.result["paypal_method_retained"])
        self.assertFalse(case.result["checkout_taxes_performed"])
        self.assertEqual(case.result["checkout_taxes_count"], 0)
        self.assertTrue(case.result["checkout_snapshot_performed"])
        self.assertEqual(case.result["checkout_snapshot_count"], 1)
        self.assertEqual(case.result["stripe_tax_region_count"], 1)
        self.assertEqual(case.result["stripe_tax_region_country"], "GB")
        self.assertEqual(case.result["paypal_ba_approve_url"], case.paypal_url)
        self.assertEqual(case.result["create_proxy_used"], self.checkout_proxy)
        self.assertEqual(case.result["payment_proxy_used"], self.checkout_proxy)
        self.assertEqual(case.result["approve_proxy_used"], self.checkout_proxy)
        self.assertEqual(case.result["promotion_proxy"], self.final_proxy)
        self.assertTrue(case.pool1_stripe.closed)
        self.assertTrue(case.pool2_stripe.closed)

    def test_us_taxes_failure_stops_before_payment_method(self):
        case = self._run_region(
            "US",
            "USD",
            taxes_error=RuntimeError("taxes unavailable"),
            expected_error_pattern="PAY153_PROTOCOL_TAXES_FAILED",
        )

        self.assertEqual(
            case.events,
            ["stripe_init", "checkout_update", "checkout_taxes"],
        )
        case.payment_method.assert_not_called()
        case.stripe_confirm.assert_not_called()
        case.redirect.assert_not_called()

    def test_gb_tax_region_failure_blocks_snapshot_and_payment_method(self):
        case = self._run_region(
            "GB",
            "GBP",
            tax_region_error=RuntimeError("tax region unavailable"),
            expected_error_pattern="PAYPAL_GB_PROTOCOL_TAX_REGION_FAILED",
        )

        self.assertEqual(
            case.events,
            ["checkout_warmup", "stripe_init", "stripe_tax_region"],
        )
        case.snapshot.assert_not_called()
        case.payment_method.assert_not_called()
        case.stripe_confirm.assert_not_called()
        case.direct_update.assert_not_called()

    def test_gb_approve_failure_never_updates_checkout(self):
        case = self._run_region(
            "GB",
            "GBP",
            approval_error=RuntimeError(
                "PAYPAL_GB_PROTOCOL_APPROVAL_OR_POLL_FAILED: fixture"
            ),
            expected_error_pattern="APPROVAL_OR_POLL_FAILED",
        )

        self.assertEqual(
            case.events,
            [
                "checkout_warmup",
                "stripe_init",
                "stripe_tax_region",
                "checkout_snapshot",
                "payment_method",
                "sentinel_prefetch",
                "confirm",
                "approve_poll",
            ],
        )
        case.direct_update.assert_not_called()
        case.post_init_update.assert_not_called()
        case.wait.assert_not_called()

    def test_gb_invalid_confirm_state_never_approves_or_updates(self):
        case = self._run_region(
            "GB",
            "GBP",
            confirm_state="succeeded",
            expected_error_pattern="CONFIRM_STATE_INVALID",
        )

        self.assertEqual(case.events[-1], "confirm")
        case.redirect.assert_not_called()
        case.direct_update.assert_not_called()
        case.wait.assert_not_called()

    def test_gb_missing_ba_never_updates_checkout(self):
        case = self._run_region(
            "GB",
            "GBP",
            ba_available=False,
            expected_error_pattern="BA_MISSING",
        )

        self.assertEqual(
            case.events,
            [
                "checkout_warmup",
                "stripe_init",
                "stripe_tax_region",
                "checkout_snapshot",
                "payment_method",
                "sentinel_prefetch",
                "confirm",
                "approve_poll",
                "resolve_ba",
            ],
        )
        case.direct_update.assert_not_called()
        case.post_init_update.assert_not_called()
        case.wait.assert_not_called()

    def test_gb_post_approve_update_failure_never_returns_unverified_ba(self):
        case = self._run_region(
            "GB",
            "GBP",
            update_effects=(
                _PermanentProtocolError("promotion backend unavailable"),
            ),
            expected_error_pattern="POST_APPROVE_PROMOTION_FAILED",
        )

        self.assertIn("resolve_ba", case.events)
        self.assertEqual(case.events[-1], "checkout_update")
        case.direct_update.assert_called_once()
        self.assertIsNone(case.result)
        case.init.assert_called_once()
        case.wait.assert_not_called()

    def test_gb_snapshot_failure_is_best_effort(self):
        case = self._run_region(
            "GB",
            "GBP",
            snapshot_error=RuntimeError("snapshot unavailable"),
        )

        self.assertEqual(case.result["stripe_amount"], "0")
        self.assertFalse(case.result["checkout_snapshot_performed"])
        self.assertEqual(case.result["checkout_snapshot_count"], 1)
        case.payment_method.assert_called_once()
        case.direct_update.assert_called_once()

    def test_gb_update_retry_budget_is_independent_from_post_init(self):
        case = self._run_region(
            "GB",
            "GBP",
            update_effects=(
                _RetryableProtocolError("temporary update 1"),
                _RetryableProtocolError("temporary update 2"),
                {"ok": True},
            ),
        )

        self.assertEqual(case.direct_update.call_count, 3)
        self.assertEqual(case.result["promotion_update_attempts"], 3)
        self.assertEqual(case.result["promotion_update_count"], 1)
        self.assertEqual(case.result["post_approval_init_count"], 1)
        self.assertEqual(case.sleep.call_count, 2)

    def test_gb_post_init_retries_without_repeating_update(self):
        case = self._run_region(
            "GB",
            "GBP",
            post_init_effects=(
                _RetryableProtocolError("temporary init"),
                1917,
                0,
            ),
            post_init_states=("approved", "approved", "approved"),
        )

        case.direct_update.assert_called_once()
        self.assertEqual(case.result["promotion_update_attempts"], 1)
        self.assertEqual(case.result["post_approval_init_count"], 3)
        self.assertEqual(case.init.call_count, 4)
        self.assertEqual(case.sleep.call_count, 2)
        case.wait.assert_not_called()
        case.retrieve.assert_not_called()
        case.resolve_ba.assert_called_once()

    def test_gb_accepts_current_pre_update_amount_snapshots(self):
        for amount in (1667, 1917, 2000):
            with self.subTest(amount=amount):
                case = self._run_region("GB", "GBP", initial_amount=amount)

                self.assertEqual(case.result["stripe_amount"], "0")
                self.assertEqual(
                    case.result["promotion_strategy"],
                    "standard_checkout_confirm_approve_then_same_checkout_update",
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
            "total_summary": {"due": 1917},
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
                "PAYPAL_GB_PROTOCOL_STANDARD_CHECKOUT_REQUIRED",
            ):
                card_link_runtime.generate_opll_paypal_gb_pay153_long_link(
                    "token",
                    self.checkout_proxy,
                    self.final_proxy,
                    "0",
                )

        direct_update.assert_not_called()
        materialize.assert_not_called()
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
