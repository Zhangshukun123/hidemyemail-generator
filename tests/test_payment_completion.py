import asyncio
import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from hidemyemail_generator.account_plan import AccountPlanResult
from hidemyemail_generator.browser_tasks import (
    _save_account_record,
    jwt_account_type,
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


class ClaimPlanPresenter:
    """Deterministic accounts/check stand-in for payment orchestration tests."""

    def check(self, token: str, **_kwargs) -> AccountPlanResult:
        account_type, raw_plan = jwt_account_type(token)
        return AccountPlanResult(
            status=account_type or "unknown",
            plan_type=raw_plan,
            detail=f"test accounts/check plan={raw_plan or 'unknown'}",
        )


class PaymentCompletionPresenterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_file = Path(self.temp_dir.name) / "accounts.db"
        self.email = "payment-at@icloud.com"
        self.old_token = token_for(self.email, "free", "old")
        self.plan_presenter = ClaimPlanPresenter()
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
            PaymentCompletionModel(self.db_file),
            session_refresher=refresher,
            plan_presenter=self.plan_presenter,
            initial_delay_seconds=0,
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
            PaymentCompletionModel(self.db_file),
            session_refresher=replay_refresher,
            plan_presenter=self.plan_presenter,
            initial_delay_seconds=0,
        ).confirm(self.job())
        self.assertEqual(replay, first)
        replay_refresher.assert_not_awaited()

    async def test_live_accounts_check_plus_wins_over_stale_free_session_and_jwt(self):
        calls = []

        class LivePlusPresenter:
            def check(_self, token, **kwargs):
                calls.append((token, kwargs))
                return AccountPlanResult(
                    status="plus",
                    plan_type="plus",
                    detail="accounts/check selected acct-payment-test; plan=plus",
                    has_active_subscription=True,
                )

        async def refresher(**_kwargs):
            return {
                "access_token": self.old_token,
                "session_json": json.dumps(
                    session_for(self.email, self.old_token, "free")
                ),
                "cookies_json": json.dumps(self.cookies),
                "storage_state_json": json.dumps(
                    {"cookies": self.cookies, "origins": []}
                ),
            }

        outcome = await PaymentCompletionPresenter(
            PaymentCompletionModel(self.db_file),
            session_refresher=refresher,
            plan_presenter=LivePlusPresenter(),
            initial_delay_seconds=0,
        ).confirm(self.job("payment-job-live-plus"))

        self.assertEqual(outcome["status"], "plus")
        self.assertTrue(outcome["at_refreshed"])
        self.assertFalse(outcome["at_changed"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], self.old_token)
        record = load_account_record(self.db_file, self.email)
        self.assertEqual(record["account_type"], "plus")
        self.assertIn("accounts/check 实时套餐=plus", record["verification_detail"])

    async def test_non_plus_new_at_keeps_payment_success_without_overwriting_old_label(self):
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
            plan_presenter=self.plan_presenter,
            max_attempts=1,
            initial_delay_seconds=0,
        )
        outcome = await presenter.confirm(self.job("payment-job-free"))

        self.assertEqual(outcome["status"], "not_plus")
        self.assertTrue(outcome["protocol_succeeded"])
        self.assertTrue(outcome["payment_succeeded"])
        self.assertFalse(outcome["plus_confirmed"])
        self.assertEqual(outcome["account_type"], "free")
        record = load_account_record(self.db_file, self.email)
        self.assertEqual(record["account_type"], "plus")
        self.assertEqual(record["account_type_source"], "manual")
        self.assertEqual(record["access_token"], new_token)

    async def test_refresh_failure_keeps_payment_success_and_does_not_leak_secrets(self):
        async def refresher(**_kwargs):
            raise RuntimeError(
                "http://proxy-user:proxy-pass@proxy.test:8080 rejected "
                + self.old_token
            )

        presenter = PaymentCompletionPresenter(
            PaymentCompletionModel(self.db_file),
            session_refresher=refresher,
            plan_presenter=self.plan_presenter,
            max_attempts=1,
            initial_delay_seconds=0,
        )
        outcome = await presenter.confirm(self.job("payment-job-error"))

        self.assertEqual(outcome["status"], "refresh_failed")
        self.assertTrue(outcome["protocol_succeeded"])
        self.assertTrue(outcome["payment_succeeded"])
        self.assertFalse(outcome["plus_confirmed"])
        self.assertFalse(outcome["at_refreshed"])
        serialized = json.dumps(outcome)
        self.assertNotIn("proxy-user", serialized)
        self.assertNotIn("proxy-pass", serialized)
        self.assertNotIn(self.old_token, serialized)
        record = load_account_record(self.db_file, self.email)
        self.assertNotEqual(record.get("account_type_source"), "payment_at_refresh")

    async def test_refresh_waits_ten_seconds_then_retries_every_ten_seconds(self):
        now = [1000.0]
        refresh_times = []

        async def refresher(**_kwargs):
            refresh_times.append(now[0])
            raise RuntimeError(
                "Cookie 刷新未返回有效 Session：session 返回 HTTP 403；"
                "session?refresh=true 返回 HTTP 403"
            )

        presenter = PaymentCompletionPresenter(
            PaymentCompletionModel(self.db_file),
            session_refresher=refresher,
            plan_presenter=self.plan_presenter,
            clock=lambda: now[0],
        )
        job = self.job("payment-job-delayed-refresh")

        scheduled = await presenter.confirm(job)
        self.assertEqual(scheduled["status"], "retrying")
        self.assertEqual(scheduled["attempt"], 0)
        self.assertEqual(scheduled["max_attempts"], 3)
        self.assertEqual(scheduled["retry_after"], 1010.0)
        self.assertIn("等待 10 秒后进行第 1/3 次", scheduled["detail"])
        self.assertEqual(refresh_times, [])

        now[0] = 1009.9
        waiting = await presenter.confirm(job)
        self.assertEqual(waiting, scheduled)
        self.assertEqual(refresh_times, [])

        now[0] = 1010.0
        first = await presenter.confirm(job)
        self.assertEqual(first["status"], "retrying")
        self.assertEqual(first["attempt"], 1)
        self.assertEqual(first["retry_after"], 1020.0)
        self.assertIn("10 秒后自动进行第 2/3 次", first["detail"])

        now[0] = 1019.9
        self.assertEqual(await presenter.confirm(job), first)
        self.assertEqual(refresh_times, [1010.0])

        now[0] = 1020.0
        second = await presenter.confirm(job)
        self.assertEqual(second["status"], "retrying")
        self.assertEqual(second["attempt"], 2)
        self.assertEqual(second["retry_after"], 1030.0)
        self.assertIn("10 秒后自动进行第 3/3 次", second["detail"])

        now[0] = 1030.0
        exhausted = await presenter.confirm(job)
        self.assertEqual(exhausted["status"], "refresh_failed")
        self.assertEqual(exhausted["attempt"], 3)
        self.assertEqual(exhausted["retry_after"], 0)
        self.assertIn("第 3/3 次 Cookie 登录获取新 AT 失败", exhausted["detail"])
        self.assertIn("已停止重试", exhausted["detail"])
        self.assertEqual(refresh_times, [1010.0, 1020.0, 1030.0])

        now[0] = 1040.0
        self.assertEqual(await presenter.confirm(job), exhausted)
        self.assertEqual(refresh_times, [1010.0, 1020.0, 1030.0])

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
            plan_presenter=self.plan_presenter,
            initial_delay_seconds=0,
            retry_delay_seconds=0,
        )
        first = await presenter.confirm(self.job("payment-job-propagation"))
        second = await presenter.confirm(self.job("payment-job-propagation"))

        self.assertEqual(first["status"], "retrying")
        self.assertTrue(first["payment_succeeded"])
        self.assertFalse(first["plus_confirmed"])
        self.assertEqual(second["status"], "plus")
        self.assertTrue(second["payment_succeeded"])
        self.assertEqual(second["attempt"], 2)
        record = load_account_record(self.db_file, self.email)
        self.assertEqual(record["account_type"], "plus")
        self.assertEqual(record["account_type_source"], "payment_at_refresh")

    async def test_missing_registration_proxy_uses_cookie_login_directly(self):
        new_token = token_for(self.email, "plus", "direct-cookie-login")
        refresh_calls = []

        async def refresher(**kwargs):
            refresh_calls.append(kwargs)
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
            plan_presenter=self.plan_presenter,
            initial_delay_seconds=0,
        )
        with mock.patch(
            "hidemyemail_generator.payment_completion.account_registration_proxy_url",
            return_value="",
        ):
            outcome = await presenter.confirm(self.job("payment-job-direct-refresh"))

        self.assertEqual(outcome["status"], "plus")
        self.assertTrue(outcome["payment_succeeded"])
        self.assertTrue(outcome["plus_confirmed"])
        self.assertIn("accounts/check 实时套餐=plus", outcome["detail"])
        self.assertEqual(len(refresh_calls), 1)
        self.assertEqual(refresh_calls[0]["proxy_url"], "")

    async def test_non_success_protocol_job_never_refreshes_or_changes_account(self):
        refresher = mock.AsyncMock()
        presenter = PaymentCompletionPresenter(
            PaymentCompletionModel(self.db_file),
            session_refresher=refresher,
            plan_presenter=self.plan_presenter,
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
            PaymentCompletionModel(self.db_file),
            session_refresher=refresher,
            plan_presenter=self.plan_presenter,
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
        presenter = PaymentCompletionPresenter(
            PaymentCompletionModel(self.db_file),
            plan_presenter=self.plan_presenter,
        )

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
            PaymentCompletionModel(self.db_file),
            session_refresher=refresher,
            plan_presenter=self.plan_presenter,
            initial_delay_seconds=0,
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
