from __future__ import annotations

import base64
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hidemyemail_generator.inbox import connect_db
from hidemyemail_generator.plus_account_export import (
    JSON_CONTENT_TYPE,
    CpaExportStrategy,
    PlusAccountExportModel,
    PlusAccountExportPresenter,
    Sub2ApiExportStrategy,
)
from hidemyemail_generator.plus_codex import PlusCodexModel


NOW = datetime(2026, 8, 17, 4, 0, tzinfo=timezone.utc)
EXPIRES_AT = 1_787_026_800  # 2026-08-18T04:20:00Z


def jwt(payload: dict) -> str:
    encoded = (
        base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )
        .decode("ascii")
        .rstrip("=")
    )
    return f"eyJhbGciOiJub25lIn0.{encoded}.signature"


def eligible_record(email: str, *, suffix: str = "one") -> dict:
    account_id = f"acct-{suffix}"
    user_id = f"user-{suffix}"
    access_token = jwt(
        {
            "exp": EXPIRES_AT,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": account_id,
                "chatgpt_user_id": user_id,
                "chatgpt_plan_type": "plus",
            },
            "https://api.openai.com/profile": {"email": email},
        }
    )
    id_token = jwt(
        {
            "exp": EXPIRES_AT,
            "email": email,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": account_id,
                "chatgpt_user_id": user_id,
            },
        }
    )
    return {
        "email": email,
        "account_type": "plus",
        "payment_confirmation": {
            "status": "plus",
            "payment_succeeded": True,
            "plan": "plus",
        },
        "plus_codex": {
            "status": "completed",
            "export_ready": True,
            "completed_at": "2026-08-17T03:30:00Z",
        },
        "codex_oauth": {
            "status": "ready",
            "access_token": access_token,
            "refresh_token": f"refresh-{suffix}",
            "id_token": id_token,
            "account_id": account_id,
            "last_refresh": "2026-08-17T03:30:00Z",
        },
    }


class PlusAccountExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_file = Path(self.temp_dir.name) / "hidemyemail.db"
        connection = connect_db(str(self.db_file))
        connection.close()
        self.model = PlusAccountExportModel(self.db_file, clock=lambda: NOW)
        self.presenter = PlusAccountExportPresenter(self.model)

    def save(self, email: str, record: object, *, raw: bool = False) -> None:
        value = str(record) if raw else json.dumps(record, ensure_ascii=False)
        connection = connect_db(str(self.db_file))
        try:
            connection.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                (f"gpt_account:{email.casefold()}", value),
            )
            connection.commit()
        finally:
            connection.close()

    def test_cpa_single_account_has_exact_schema_and_original_tokens(self):
        email = "paid.user@icloud.com"
        record = eligible_record(email)
        self.save(email, record)

        artifact = self.presenter.export("CPA", email.upper())
        payload = json.loads(artifact.content)

        self.assertEqual(artifact.count, 1)
        self.assertEqual(artifact.content_type, JSON_CONTENT_TYPE)
        self.assertEqual(artifact.filename, "plus-cpa-paid.user-icloud.com.json")
        self.assertEqual(
            set(payload),
            {
                "type",
                "account_id",
                "chatgpt_account_id",
                "user_id",
                "chatgpt_user_id",
                "email",
                "name",
                "plan_type",
                "chatgpt_plan_type",
                "id_token",
                "access_token",
                "refresh_token",
                "last_refresh",
                "expired",
            },
        )
        self.assertEqual(payload["type"], "codex")
        self.assertEqual(payload["account_id"], "acct-one")
        self.assertEqual(payload["chatgpt_user_id"], "user-one")
        self.assertEqual(payload["email"], email)
        self.assertEqual(payload["plan_type"], "plus")
        self.assertEqual(payload["expired"], "2026-08-18T04:20:00.000Z")
        for field in ("access_token", "refresh_token", "id_token"):
            self.assertEqual(payload[field], record["codex_oauth"][field])
        self.assertNotIn("id_token_synthetic", payload)
        self.assertEqual(artifact.body, artifact.json_bytes)

    def test_cpa_batch_is_array_across_icloud_gmail_and_zkgmail(self):
        emails = (
            "paid@icloud.com",
            "paid@gmail.com",
            "paid@zkgmail.com",
        )
        for index, email in enumerate(reversed(emails)):
            self.save(email, eligible_record(email, suffix=str(index)))

        payload = json.loads(self.presenter.export_cpa().content)

        self.assertIsInstance(payload, list)
        self.assertEqual([item["email"] for item in payload], sorted(emails))

    def test_cpa_all_selection_with_one_account_remains_batch_array(self):
        email = "only@gmail.com"
        self.save(email, eligible_record(email, suffix="only"))

        payload = json.loads(self.presenter.export_cpa().content)

        self.assertIsInstance(payload, list)
        self.assertEqual([item["email"] for item in payload], [email])

    def test_sub2api_document_has_exact_current_account_schema(self):
        email = "paid@gmail.com"
        record = eligible_record(email, suffix="gmail")
        self.save(email, record)

        artifact = self.presenter.export_sub2api(email)
        payload = json.loads(artifact.content)

        self.assertEqual(set(payload), {"exported_at", "proxies", "accounts"})
        self.assertEqual(payload["exported_at"], "2026-08-17T04:00:00.000Z")
        self.assertEqual(payload["proxies"], [])
        self.assertEqual(len(payload["accounts"]), 1)
        account = payload["accounts"][0]
        self.assertEqual(
            set(account),
            {
                "name",
                "platform",
                "type",
                "expires_at",
                "auto_pause_on_expired",
                "concurrency",
                "priority",
                "credentials",
                "extra",
            },
        )
        self.assertEqual(account["platform"], "openai")
        self.assertEqual(account["type"], "oauth")
        self.assertEqual(account["expires_at"], EXPIRES_AT)
        self.assertIs(account["auto_pause_on_expired"], True)
        self.assertEqual(
            set(account["credentials"]),
            {
                "access_token",
                "refresh_token",
                "id_token",
                "account_id",
                "chatgpt_account_id",
                "user_id",
                "chatgpt_user_id",
                "email",
                "expires_at",
                "expires_in",
                "plan_type",
            },
        )
        credentials = account["credentials"]
        self.assertEqual(
            credentials["access_token"], record["codex_oauth"]["access_token"]
        )
        self.assertEqual(credentials["refresh_token"], "refresh-gmail")
        self.assertEqual(credentials["id_token"], record["codex_oauth"]["id_token"])
        self.assertEqual(credentials["chatgpt_account_id"], "acct-gmail")
        self.assertEqual(credentials["chatgpt_user_id"], "user-gmail")
        self.assertEqual(credentials["email"], email)
        self.assertEqual(credentials["plan_type"], "plus")
        self.assertEqual(credentials["expires_at"], "2026-08-18T04:20:00.000Z")
        self.assertEqual(credentials["expires_in"], 87_600)
        self.assertEqual(
            account["extra"],
            {
                "email": email,
                "email_key": "paid_gmail_com",
                "name": email,
                "auth_provider": "openai",
                "source": "codex_oauth",
                "last_refresh": "2026-08-17T03:30:00.000Z",
            },
        )

    def test_specific_email_does_not_export_other_eligible_accounts(self):
        for suffix, email in (("a", "a@icloud.com"), ("b", "b@gmail.com")):
            self.save(email, eligible_record(email, suffix=suffix))

        payload = json.loads(self.presenter.export("cpa", "B@GMAIL.COM").content)

        self.assertEqual(payload["email"], "b@gmail.com")
        self.assertEqual(payload["account_id"], "acct-b")

    def test_strictly_filters_every_required_state_and_malformed_rows(self):
        invalid: list[tuple[str, dict]] = []

        def changed(label: str, path: tuple[str, ...], value: object) -> None:
            email = f"{label}@icloud.com"
            record = eligible_record(email, suffix=label)
            target = record
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            invalid.append((email, record))

        changed("free", ("account_type",), "free")
        changed("payment-status", ("payment_confirmation", "status"), "pending")
        changed(
            "payment-false",
            ("payment_confirmation", "payment_succeeded"),
            False,
        )
        changed("codex-pending", ("plus_codex", "status"), "pending")
        changed("not-export-ready", ("plus_codex", "export_ready"), False)
        changed("oauth-failed", ("codex_oauth", "status"), "failed")
        changed("oauth-free", ("codex_oauth", "plan_type"), "free")
        changed("synthetic", ("codex_oauth", "id_token_synthetic"), True)
        changed(
            "placeholder",
            ("codex_oauth", "refresh_token"),
            "__missing_refresh_token__",
        )
        for field in ("access_token", "refresh_token", "id_token", "account_id"):
            changed(f"missing-{field.replace('_', '-')}", ("codex_oauth", field), "")

        conflict_email = "id-conflict@icloud.com"
        conflict = eligible_record(conflict_email, suffix="id-conflict")
        conflict["codex_oauth"]["id_token"] = jwt(
            {
                "exp": EXPIRES_AT,
                "email": "different@icloud.com",
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "acct-id-conflict",
                    "chatgpt_user_id": "user-id-conflict",
                },
            }
        )
        invalid.append((conflict_email, conflict))

        record_conflict_email = "record-conflict@gmail.com"
        record_conflict = eligible_record(
            record_conflict_email, suffix="record-conflict"
        )
        record_conflict["email"] = "different@gmail.com"
        invalid.append((record_conflict_email, record_conflict))

        user_conflict_email = "user-conflict@zkgmail.com"
        user_conflict = eligible_record(user_conflict_email, suffix="user-conflict")
        user_conflict["codex_oauth"]["chatgpt_user_id"] = "different-user"
        invalid.append((user_conflict_email, user_conflict))

        for email, record in invalid:
            self.save(email, record)
        self.save("malformed@gmail.com", "{broken", raw=True)
        self.save("array@zkgmail.com", [eligible_record("array@zkgmail.com")])
        self.save(
            "bad email@gmail.com",
            eligible_record("bad email@gmail.com", suffix="bad-email"),
        )

        artifact = self.presenter.export_cpa()

        self.assertEqual(artifact.count, 0)
        self.assertEqual(json.loads(artifact.content), [])

    def test_rejects_expired_token_even_when_saved_expiry_claims_future(self):
        email = "expired@zkgmail.com"
        record = eligible_record(email)
        record["codex_oauth"]["access_token"] = jwt(
            {
                "exp": int(NOW.timestamp()) - 1,
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "acct-one",
                    "chatgpt_user_id": "user-one",
                },
                "https://api.openai.com/profile": {"email": email},
            }
        )
        record["codex_oauth"]["expires_at"] = "2099-01-01T00:00:00Z"
        self.save(email, record)

        self.assertEqual(self.model.eligible_accounts(), [])

    def test_explicit_expiry_supports_opaque_real_oauth_tokens(self):
        email = "opaque@gmail.com"
        record = eligible_record(email)
        record["codex_oauth"].update(
            {
                "access_token": "opaque-access-token-from-oauth",
                "id_token": "opaque-id-token-from-oauth",
                "expires_at": "2026-08-18T04:20:00Z",
                "chatgpt_user_id": "oauth-user",
                "email": email,
            }
        )
        self.save(email, record)

        payload = json.loads(self.presenter.export_cpa(email).content)

        self.assertEqual(payload["access_token"], "opaque-access-token-from-oauth")
        self.assertEqual(payload["id_token"], "opaque-id-token-from-oauth")
        self.assertEqual(payload["chatgpt_user_id"], "oauth-user")

    def test_user_id_fields_remain_in_schema_when_oauth_does_not_expose_it(self):
        email = "no-user@gmail.com"
        record = eligible_record(email)
        record["codex_oauth"].update(
            {
                "access_token": "opaque-access-token-from-oauth",
                "id_token": "opaque-id-token-from-oauth",
                "expires_at": "2026-08-18T04:20:00Z",
                "email": email,
            }
        )
        record["codex_oauth"].pop("chatgpt_user_id", None)
        self.save(email, record)

        cpa = json.loads(self.presenter.export_cpa(email).content)
        sub2api = json.loads(self.presenter.export_sub2api(email).content)

        self.assertEqual(cpa["user_id"], "")
        self.assertEqual(cpa["chatgpt_user_id"], "")
        credentials = sub2api["accounts"][0]["credentials"]
        self.assertEqual(credentials["user_id"], "")
        self.assertEqual(credentials["chatgpt_user_id"], "")

    def test_mismatched_oauth_identity_is_not_exported(self):
        email = "owner@icloud.com"
        record = eligible_record(email)
        record["codex_oauth"]["account_id"] = "different-account"
        self.save(email, record)

        self.assertEqual(self.model.eligible_accounts(), [])

    def test_nested_oauth_tokens_are_supported_without_top_level_fallback(self):
        email = "nested@zkgmail.com"
        record = eligible_record(email)
        oauth = record.pop("codex_oauth")
        record["plus_codex"]["codex_oauth"] = {
            "status": "completed",
            "tokens": {
                key: oauth[key]
                for key in ("access_token", "refresh_token", "id_token", "account_id")
            },
            "last_refresh": oauth["last_refresh"],
        }
        self.save(email, record)

        artifact = self.presenter.export_cpa(email)

        self.assertEqual(artifact.count, 1)
        self.assertEqual(json.loads(artifact.content)["email"], email)

    def test_reads_exact_record_written_by_plus_codex_completion_model(self):
        email = "completed@zkgmail.com"
        source = eligible_record(email, suffix="persisted")
        oauth = source.pop("codex_oauth")
        source.pop("plus_codex")
        source["payment_confirmation"]["job_id"] = "payment-job"
        self.save(email, source)

        PlusCodexModel(self.db_file).complete(
            email=email,
            job_id="payment-job",
            result={
                "access_token": oauth["access_token"],
                "refresh_token": oauth["refresh_token"],
                "id_token": oauth["id_token"],
                "account_id": oauth["account_id"],
                "expires_in": 3600,
                "phone_bound": True,
                "phone": "+15551234567",
                "activation_id": "activation-secret",
                "sms_provider": "smsbower",
                "phone_attempts": 1,
            },
        )

        payload = json.loads(self.presenter.export_cpa(email).content)

        self.assertEqual(payload["access_token"], oauth["access_token"])
        self.assertEqual(payload["refresh_token"], oauth["refresh_token"])
        self.assertEqual(payload["id_token"], oauth["id_token"])
        self.assertEqual(payload["account_id"], oauth["account_id"])

    def test_presenter_rejects_unknown_or_non_string_format(self):
        with self.assertRaisesRegex(ValueError, "不支持的导出格式"):
            self.presenter.export("csv")
        with self.assertRaisesRegex(ValueError, "不支持的导出格式"):
            self.presenter.export(None)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "邮箱地址无效"):
            self.presenter.export("cpa", "bad email@gmail.com")

    def test_strategies_implement_shared_build_contract(self):
        account = PlusAccountExportModel._normalize_record(
            "contract@gmail.com", eligible_record("contract@gmail.com"), NOW
        )
        self.assertIsNotNone(account)
        accounts = [account] if account is not None else []
        self.assertIsInstance(
            CpaExportStrategy().build(accounts, exported_at=NOW), dict
        )
        sub2api = Sub2ApiExportStrategy().build(accounts, exported_at=NOW)
        self.assertEqual(sub2api["proxies"], [])  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()
