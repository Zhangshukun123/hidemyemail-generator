from __future__ import annotations

import unittest

from hidemyemail_generator.paypal_gb_post_approve_flow import (
    DiagnosticPayPalGbPostApproveView,
    PayPalGbPostApproveModel,
    PayPalGbPostApprovePhase,
    PayPalGbPostApprovePresenter,
)


class _Session:
    def __init__(self, device_id: str):
        self.opll_oai_device_id = device_id


class PayPalGbPostApproveFlowTests(unittest.TestCase):
    checkout_id = "cs_gb_post_approve"
    device_id = "device-gb-post-approve"
    primary_proxy = "http://gb-pool1.example:8000"
    promotion_proxy = "http://gb-pool2.example:8000"

    @staticmethod
    def _stripe_context():
        return {
            "guid": "guid-fixture",
            "muid": "muid-fixture",
            "sid": "sid-fixture",
            "stripe_js_id": "stripe-js-fixture",
            "elements_session_id": "elements-session-fixture",
        }

    def _presenter(self):
        messages: list[str] = []
        presenter = PayPalGbPostApprovePresenter(
            PayPalGbPostApproveModel(
                checkout_id=self.checkout_id,
                stripe_checkout_id=self.checkout_id,
                device_id=self.device_id,
                primary_proxy_url=self.primary_proxy,
                promotion_proxy_url=self.promotion_proxy,
            ),
            DiagnosticPayPalGbPostApproveView(messages.append),
        )
        return presenter, messages

    def test_promotion_is_gated_by_ba_approval(self):
        presenter, _messages = self._presenter()
        calls: list[str] = []

        with self.assertRaisesRegex(RuntimeError, "阶段错误"):
            presenter.apply_promotion(
                lambda: calls.append("update"),
                checkout_id=self.checkout_id,
                promotion_session=_Session(self.device_id),
                promotion_stripe_session=object(),
                promotion_proxy_url=self.promotion_proxy,
                stripe_context=self._stripe_context(),
            )

        self.assertEqual(calls, [])
        self.assertEqual(presenter.model.promotion_update_count, 0)

    def test_full_sequence_preserves_sessions_context_and_checkout(self):
        presenter, messages = self._presenter()
        primary = _Session(self.device_id)
        promotion = _Session(self.device_id)
        primary_stripe = object()
        promotion_stripe = object()
        ctx = self._stripe_context()

        presenter.bind_stripe(
            primary_chatgpt_session=primary,
            stripe_session=primary_stripe,
            stripe_context=ctx,
            amount="1917",
            currency="GBP",
            browser_http_used=True,
        )
        presenter.mark_sentinel_ready(
            chatgpt_session=primary,
            stripe_session=primary_stripe,
            stripe_context=ctx,
        )
        presenter.mark_confirmed(
            chatgpt_session=primary,
            stripe_session=primary_stripe,
            stripe_context=ctx,
        )
        presenter.mark_ba_approved(
            checkout_id=self.checkout_id,
            paypal_ba_approve_url=(
                "https://www.paypal.com/agreements/approve?ba_token=fixture"
            ),
            chatgpt_session=primary,
            stripe_session=primary_stripe,
            stripe_context=ctx,
        )
        update_calls: list[str] = []
        presenter.apply_promotion(
            lambda: update_calls.append(self.checkout_id) or {"ok": True},
            checkout_id=self.checkout_id,
            promotion_session=promotion,
            promotion_stripe_session=promotion_stripe,
            promotion_proxy_url=self.promotion_proxy,
            stripe_context=ctx,
        )
        presenter.mark_verified(
            checkout_id=self.checkout_id,
            stripe_checkout_id=self.checkout_id,
            amount="0",
            currency="GBP",
            submission_state="approved",
            payment_method_types=["card", "paypal"],
            stripe_session=promotion_stripe,
            stripe_context=ctx,
        )

        self.assertEqual(update_calls, [self.checkout_id])
        self.assertIs(presenter.model.phase, PayPalGbPostApprovePhase.VERIFIED)
        self.assertEqual(presenter.model.promotion_update_count, 1)
        self.assertTrue(
            presenter.model.result_metadata[
                "approval_completed_before_promotion"
            ]
        )
        self.assertTrue(presenter.model.result_metadata["same_checkout_promotion"])
        self.assertTrue(presenter.model.result_metadata["browser_http_used"])
        self.assertEqual(
            presenter.model.result_metadata["approval_state"],
            "approved",
        )
        self.assertTrue(
            presenter.model.result_metadata["ba_preserved_after_promotion"]
        )
        self.assertTrue(
            presenter.model.result_metadata["paypal_method_retained"]
        )
        self.assertIn("verified", messages[-1])

    def test_identity_or_context_drift_is_rejected(self):
        presenter, _messages = self._presenter()
        primary = _Session(self.device_id)
        stripe = object()
        ctx = self._stripe_context()
        presenter.bind_stripe(
            primary_chatgpt_session=primary,
            stripe_session=stripe,
            stripe_context=ctx,
            amount="1771",
            currency="GBP",
            browser_http_used=False,
        )

        with self.assertRaisesRegex(RuntimeError, "Stripe Context 发生漂移"):
            presenter.mark_sentinel_ready(
                chatgpt_session=primary,
                stripe_session=stripe,
                stripe_context={},
            )

    def test_stable_stripe_context_identity_drift_is_rejected(self):
        presenter, _messages = self._presenter()
        primary = _Session(self.device_id)
        stripe = object()
        ctx = self._stripe_context()
        presenter.bind_stripe(
            primary_chatgpt_session=primary,
            stripe_session=stripe,
            stripe_context=ctx,
            amount="1771",
            currency="GBP",
            browser_http_used=False,
        )
        ctx["muid"] = "drifted-muid"

        with self.assertRaisesRegex(RuntimeError, "稳定身份字段发生漂移"):
            presenter.mark_sentinel_ready(
                chatgpt_session=primary,
                stripe_session=stripe,
                stripe_context=ctx,
            )

    def _advance_to_promoted(self):
        presenter, _messages = self._presenter()
        primary = _Session(self.device_id)
        promotion = _Session(self.device_id)
        primary_stripe = object()
        promotion_stripe = object()
        ctx = self._stripe_context()
        presenter.bind_stripe(
            primary_chatgpt_session=primary,
            stripe_session=primary_stripe,
            stripe_context=ctx,
            amount="1917",
            currency="GBP",
            browser_http_used=True,
        )
        presenter.mark_sentinel_ready(
            chatgpt_session=primary,
            stripe_session=primary_stripe,
            stripe_context=ctx,
        )
        presenter.mark_confirmed(
            chatgpt_session=primary,
            stripe_session=primary_stripe,
            stripe_context=ctx,
        )
        presenter.mark_ba_approved(
            checkout_id=self.checkout_id,
            paypal_ba_approve_url=(
                "https://www.paypal.com/agreements/approve?ba_token=fixture"
            ),
            chatgpt_session=primary,
            stripe_session=primary_stripe,
            stripe_context=ctx,
        )
        presenter.apply_promotion(
            lambda: {"ok": True},
            checkout_id=self.checkout_id,
            promotion_session=promotion,
            promotion_stripe_session=promotion_stripe,
            promotion_proxy_url=self.promotion_proxy,
            stripe_context=ctx,
        )
        return presenter, primary_stripe, promotion_stripe, ctx

    def test_promotion_requires_an_independent_pool2_stripe_session(self):
        presenter, primary_stripe, _promotion_stripe, ctx = self._advance_to_promoted()

        with self.assertRaisesRegex(RuntimeError, "池 2 Stripe Session"):
            presenter.mark_verified(
                checkout_id=self.checkout_id,
                stripe_checkout_id=self.checkout_id,
                amount="0",
                currency="GBP",
                submission_state="approved",
                payment_method_types=["paypal"],
                stripe_session=primary_stripe,
                stripe_context=ctx,
            )

    def test_verification_requires_approved_gbp_zero_and_paypal(self):
        invalid_cases = (
            ({"submission_state": "requires_approval"}, "approved"),
            ({"amount": "1917"}, "金额必须为 0"),
            ({"currency": "USD"}, "币种必须保持 GBP"),
            ({"payment_method_types": ["card"]}, "未保留 PayPal 方法"),
        )
        for overrides, expected_error in invalid_cases:
            with self.subTest(overrides=overrides):
                presenter, _primary_stripe, promotion_stripe, ctx = (
                    self._advance_to_promoted()
                )
                arguments = {
                    "checkout_id": self.checkout_id,
                    "stripe_checkout_id": self.checkout_id,
                    "amount": "0",
                    "currency": "GBP",
                    "submission_state": "approved",
                    "payment_method_types": ["card", "paypal"],
                    "stripe_session": promotion_stripe,
                    "stripe_context": ctx,
                }
                arguments.update(overrides)

                with self.assertRaisesRegex(RuntimeError, expected_error):
                    presenter.mark_verified(**arguments)


if __name__ == "__main__":
    unittest.main()
