import asyncio
import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from hidemyemail_generator.browser_tasks import (
    _save_account_record,
    load_account_record,
    set_manual_account_type,
)
from hidemyemail_generator.payment_completion import (
    PaymentCompletionModel,
    PaymentCompletionPresenter,
    reconcile_openai_protocol_job,
)


def token_for(email: str, plan: str, marker: str) -> str:
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    payload = {
        "exp": time.time() + 3600,
        "email": email,
        "marker": marker,
        "https://api.openai.com/auth": {
            "email": email,
            "chatgpt_account_id": "acct-payment-test",
            "chatgpt_plan_type": plan,
        },
    }
    return f"{encode({'alg': 'none'})}.{encode(payload)}.signature"


def session_for(email: str, token: str, plan: str) -> dict:
    return {
        "accessToken": token,
        "user": {"email": email},
        "account": {"id": "acct-payment-test", "planType": plan},
    }


class PaymentCompletionPresenterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_file = Path(self.temp_dir.name) / "accounts.db"
        self.email = "payment-at@icloud.com"
        self.old_token = token_for(self.email, "free", "old")
        self.cookies = [
            {
                "name": "__Secure-next-auth.session-token",
                "value": "saved-cookie",
                "domain": "chatgpt.com",
                "path": "/",
            }
        ]
        _save_account_record(
            self.db_file,
            self.email,
            result={
                "access_token": self.old_token,
                "session_json": json.dumps(
                    session_for(self.email, self.old_token, "free")
                ),
                "cookies_json": json.dumps(self.cookies),
                "storage_state_json": json.dumps(
                    {"cookies": self.cookies, "origins": []}
                ),
                "registration_proxy_url": "http://proxy-user:proxy-pass@proxy.test:8080",
            },
        )

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    def job(self, job_id="payment-job-plus"):
        return {
            "id": job_id,
            "status": "completed",
            "source_account_email": self.email,
            "result": {"status": "success", "settlement_status": "confirmed"},
        }

    async def test_new_plus_at_is_persisted_and_marks_account_plus_once(self):
        new_token = token_for(self.email, "plus", "new")
        calls = []

        async def refresher(**kwargs):
            calls.append(kwargs)
            return {
                "access_token": new_token,
                "session_json": json.dumps(session_for(self.email, new_token, "plus")),
                "cookies_json": json.dumps(self.cookies),
                "storage_state_json": json.dumps(
                    {"cookies": self.cookies, "origins": []}
                ),
            }

        presenter = PaymentCompletionPresenter(
            PaymentCompletionModel(self.db_file), session_refresher=refresher
        )
        first, second = await asyncio.gather(
            presenter.confirm(self.job()), presenter.confirm(self.job())
        )

        self.assertEqual(first, second)
        self.assertEqual(first["status"], "plus")
        self.assertTrue(first["payment_succeeded"])
        self.assertTrue(first["at_refreshed"])
        self.assertEqual(first["account_type"], "plus")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["previous_token"], self.old_token)
        self.assertEqual(calls[0]["cookies"], self.cookies)
        record = load_account_record(self.db_file, self.email)
        self.assertEqual(record["access_token"], new_token)
        self.assertEqual(record["account_type"], "plus")
        self.assertEqual(record["account_type_source"], "payment_at_refresh")
        self.assertEqual(record["session_acquisition_method"], "payment_cookie_refresh")
        self.assertNotIn(new_token, json.dumps(first))
        replay_refresher = mock.AsyncMock()
        replay = await PaymentCompletionPresenter(
            PaymentCompletionModel(self.db_file), session_refresher=replay_refresher
        ).confirm(self.job())
        self.assertEqual(replay, first)
        replay_refresher.assert_not_awaited()

    async def test_non_plus_new_at_fails_payment_without_overwriting_old_label(self):
        set_manual_account_type(self.db_file, self.email, "plus")
        new_token = token_for(self.email, "free", "new-free")

        async def refresher(**_kwargs):
            return {
                "access_token": new_token,
                "session_json": json.dumps(session_for(self.email, new_token, "plus")),
                "cookies_json": json.dumps(self.cookies),
                "storage_state_json": json.dumps(
                    {"cookies": self.cookies, "origins": []}
                ),
            }

        presenter = PaymentCompletionPresenter(
            PaymentCompletionModel(self.db_file),
            session_refresher=refresher,
            max_attempts=1,
        )
        outcome = await presenter.confirm(self.job("payment-job-free"))

        self.assertEqual(outcome["status"], "not_plus")
        self.assertFalse(outcome["payment_succeeded"])
        self.assertEqual(outcome["account_type"], "free")
        record = load_account_record(self.db_file, self.email)
        self.assertEqual(record["account_type"], "plus")
        self.assertEqual(record["account_type_source"], "manual")
        self.assertEqual(record["access_token"], new_token)

    async def test_refresh_failure_does_not_mark_payment_success_or_leak_secrets(self):
        async def refresher(**_kwargs):
            raise RuntimeError(
                "http://proxy-user:proxy-pass@proxy.test:8080 rejected "
                + self.old_token
            )

        presenter = PaymentCompletionPresenter(
            PaymentCompletionModel(self.db_file),
            session_refresher=refresher,
            max_attempts=1,
        )
        outcome = await presenter.confirm(self.job("payment-job-error"))

        self.assertEqual(outcome["status"], "refresh_failed")
        self.assertFalse(outcome["payment_succeeded"])
        self.assertFalse(outcome["at_refreshed"])
        serialized = json.dumps(outcome)
        self.assertNotIn("proxy-user", serialized)
        self.assertNotIn("proxy-pass", serialized)
        self.assertNotIn(self.old_token, serialized)
        record = load_account_record(self.db_file, self.email)
        self.assertNotEqual(record.get("account_type_source"), "payment_at_refresh")

    async def test_free_plan_is_retried_until_a_newer_at_reports_plus(self):
        tokens = [
            token_for(self.email, "free", "propagating"),
            token_for(self.email, "plus", "propagated"),
        ]

        async def refresher(**_kwargs):
            token = tokens.pop(0)
            plan = "free" if tokens else "plus"
            return {
                "access_token": token,
                "session_json": json.dumps(session_for(self.email, token, plan)),
                "cookies_json": json.dumps(self.cookies),
                "storage_state_json": json.dumps(
                    {"cookies": self.cookies, "origins": []}
                ),
            }

        presenter = PaymentCompletionPresenter(
            PaymentCompletionModel(self.db_file),
            session_refresher=refresher,
            retry_delay_seconds=0,
        )
        first = await presenter.confirm(self.job("payment-job-propagation"))
        second = await presenter.confirm(self.job("payment-job-propagation"))

        self.assertEqual(first["status"], "retrying")
        self.assertFalse(first["payment_succeeded"])
        self.assertEqual(second["status"], "plus")
        self.assertTrue(second["payment_succeeded"])
        self.assertEqual(second["attempt"], 2)
        record = load_account_record(self.db_file, self.email)
        self.assertEqual(record["account_type"], "plus")
        self.assertEqual(record["account_type_source"], "payment_at_refresh")

    async def test_non_success_protocol_job_never_refreshes_or_changes_account(self):
        refresher = mock.AsyncMock()
        presenter = PaymentCompletionPresenter(
            PaymentCompletionModel(self.db_file), session_refresher=refresher
        )
        before = load_account_record(self.db_file, self.email)

        outcome = await presenter.confirm(
            {
                **self.job("payment-job-running"),
                "status": "running",
                "result": {},
            }
        )

        self.assertEqual(outcome["status"], "refresh_failed")
        self.assertFalse(outcome["payment_succeeded"])
        refresher.assert_not_awaited()
        self.assertEqual(load_account_record(self.db_file, self.email), before)

    async def test_pending_settlement_never_refreshes_or_marks_plus(self):
        refresher = mock.AsyncMock()
        presenter = PaymentCompletionPresenter(
            PaymentCompletionModel(self.db_file), session_refresher=refresher
        )
        before = load_account_record(self.db_file, self.email)

        outcome = await presenter.confirm(
            {
                **self.job("payment-job-pending-settlement"),
                "result": {
                    "status": "success",
                    "settlement_status": "pending_verification",
                },
            }
        )

        self.assertEqual(outcome["status"], "refresh_failed")
        self.assertFalse(outcome["payment_succeeded"])
        self.assertIn("尚未确认到账", outcome["detail"])
        refresher.assert_not_awaited()
        self.assertEqual(load_account_record(self.db_file, self.email), before)

    async def test_missing_account_returns_failure_without_creating_phantom_record(
        self,
    ):
        missing = "missing-payment@icloud.com"
        presenter = PaymentCompletionPresenter(PaymentCompletionModel(self.db_file))

        outcome = await presenter.confirm(
            {
                "id": "payment-job-missing",
                "status": "completed",
                "source_account_email": missing,
                "result": {"status": "success"},
            }
        )

        self.assertEqual(outcome["status"], "refresh_failed")
        self.assertFalse(outcome["payment_succeeded"])
        self.assertEqual(load_account_record(self.db_file, missing), {})

    async def test_legacy_openai_bridge_false_negative_refreshes_and_confirms_plus(
        self,
    ):
        new_token = token_for(self.email, "plus", "legacy-reconciled")

        async def refresher(**_kwargs):
            return {
                "access_token": new_token,
                "session_json": json.dumps(session_for(self.email, new_token, "plus")),
                "cookies_json": json.dumps(self.cookies),
                "storage_state_json": json.dumps(
                    {"cookies": self.cookies, "origins": []}
                ),
            }

        legacy_job = {
            "id": "legacy-openai-false-negative",
            "status": "failed",
            "stage": "最终授权失败",
            "error": "BRAINTREE_VAULT_FAILED",
            "source_account_email": self.email,
            "result": {
                "status": "error",
                "error_code": "BRAINTREE_VAULT_FAILED",
                "error": "bridge refused",
                "paypal_authorized": True,
                "redirect_status": "succeeded",
                "settlement_status": "vault_failed",
                "final_redirect_url": (
                    "https://pay.openai.com/complete?redirect_status=succeeded"
                ),
                "verification_url": (
                    "https://chatgpt.com/checkout/verify?plan_type=plus"
                ),
            },
        }

        reconciled = reconcile_openai_protocol_job(legacy_job)
        outcome = await PaymentCompletionPresenter(
            PaymentCompletionModel(self.db_file), session_refresher=refresher
        ).confirm(legacy_job)

        self.assertEqual(reconciled["status"], "completed")
        self.assertEqual(reconciled["result"]["status"], "success")
        self.assertEqual(reconciled["result"]["settlement_status"], "confirmed")
        self.assertEqual(
            reconciled["result"]["braintree_bridge_status"], "not_applicable"
        )
        self.assertEqual(outcome["status"], "plus")
        self.assertTrue(outcome["payment_succeeded"])

    def test_non_openai_bridge_failure_is_not_reconciled(self):
        legacy_job = {
            "id": "legacy-grok-failure",
            "status": "failed",
            "result": {
                "status": "error",
                "error_code": "BRAINTREE_VAULT_FAILED",
                "paypal_authorized": True,
                "redirect_status": "succeeded",
                "final_redirect_url": "https://grok.com/payments/complete",
            },
        }

        self.assertIs(reconcile_openai_protocol_job(legacy_job), legacy_job)

    def test_browser_parser_differential_url_is_not_reconciled(self):
        legacy_job = {
            "id": "legacy-host-spoof",
            "status": "failed",
            "result": {
                "status": "error",
                "error_code": "BRAINTREE_VAULT_FAILED",
                "paypal_authorized": True,
                "redirect_status": "succeeded",
                "final_redirect_url": (
                    "https://evil.example\\@chatgpt.com/checkout/verify"
                ),
            },
        }

        self.assertIs(reconcile_openai_protocol_job(legacy_job), legacy_job)

    def test_derived_openai_urls_do_not_launder_an_untrusted_final_host(self):
        legacy_job = {
            "id": "legacy-derived-host-spoof",
            "status": "failed",
            "result": {
                "status": "error",
                "error_code": "BRAINTREE_VAULT_FAILED",
                "paypal_authorized": True,
                "redirect_status": "succeeded",
                "final_redirect_url": (
                    "https://merchant.example/finish?redirect_status=succeeded&"
                    "success_return_url=https%3A%2F%2Fchatgpt.com%2Fcheckout%2Fverify"
                ),
                "verification_url": "https://chatgpt.com/checkout/verify",
                "pending_url": "https://chatgpt.com/checkout/verify",
            },
        }

        self.assertIs(reconcile_openai_protocol_job(legacy_job), legacy_job)

    def test_nested_redirect_status_does_not_confirm_a_legacy_job(self):
        legacy_job = {
            "id": "legacy-nested-status",
            "status": "failed",
            "result": {
                "status": "error",
                "error_code": "BRAINTREE_VAULT_FAILED",
                "paypal_authorized": True,
                "redirect_status": "succeeded",
                "final_redirect_url": (
                    "https://pay.openai.com/complete?success_return_url="
                    "https%3A%2F%2Fchatgpt.com%2Fcheckout%2Fverify%3F"
                    "redirect_status%3Dsucceeded"
                ),
                "verification_url": (
                    "https://chatgpt.com/checkout/verify?redirect_status=succeeded"
                ),
            },
        }

        self.assertIs(reconcile_openai_protocol_job(legacy_job), legacy_job)


if __name__ == "__main__":
    unittest.main()
