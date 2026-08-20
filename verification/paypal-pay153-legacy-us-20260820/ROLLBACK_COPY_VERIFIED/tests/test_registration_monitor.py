from __future__ import annotations

import json
from pathlib import Path

import pytest

from hidemyemail_generator.inbox import connect_db
from hidemyemail_generator.registration_monitor import (
    REDACTED,
    REGISTRATION_PROCESS_FAILURE_PREFIX,
    RegistrationFailureClassifier,
    RegistrationFailureRepository,
    RegistrationMonitorPresenter,
    redact_text,
    sanitize_value,
)


def make_presenter(
    tmp_path: Path, *, jsonl_max_bytes: int = 5 * 1024 * 1024
) -> RegistrationMonitorPresenter:
    repository = RegistrationFailureRepository(
        tmp_path / "monitor.db",
        tmp_path / "registration-failures.jsonl",
        jsonl_max_bytes=jsonl_max_bytes,
        jsonl_backups=2,
    )
    return RegistrationMonitorPresenter(repository)


@pytest.mark.parametrize(
    ("task", "reason_code"),
    [
        ({"message": "库存服务认证失败：HTTP 401"}, "inventory_auth"),
        ({"message": "远端邮箱库存令牌已失效"}, "inventory_auth"),
        ({"message": "邮箱库存为空，没有可用邮箱"}, "inventory_empty"),
        ({"message": "库存服务网络连接超时"}, "inventory_network"),
        ({"message": "proxy tunnel connection failed"}, "proxy_unavailable"),
        ({"message": "Page.goto: net::ERR_CONNECTION_CLOSED"}, "network_error"),
        ({"message": "HTTP 429 resource exhausted"}, "resource_exhausted"),
        ({"message": "启动失败，内存使用率超出 95%"}, "resource_exhausted"),
        (
            {"currentStage": "email_verification", "message": "验证码校验失败"},
            "email_verification",
        ),
        ({"currentStage": "password", "message": "password rejected"}, "password"),
        ({"currentStage": "two_factor", "message": "TOTP setup failed"}, "two_factor"),
        (
            {"currentStage": "session", "message": "Session acquisition failed"},
            "session",
        ),
        ({"message": "browser target has been closed"}, "browser_closed"),
        ({"message": "page navigation timeout while redirecting"}, "page_navigation"),
        ({"message": "ChatGPT 首页免费注册按钮点击失败"}, "page_navigation"),
        ({"message": "registration worker exited with code 1"}, "worker_exit"),
        ({"message": "Mail Auth 协议注册失败：invalid state"}, "protocol_auth_failed"),
        ({"message": "unexpected registration problem"}, "unknown"),
    ],
)
def test_classifier_covers_stable_failure_codes(task: dict, reason_code: str) -> None:
    decision = RegistrationFailureClassifier().classify(task)

    assert decision.reason_code == reason_code
    assert decision.category
    assert decision.failure_reason
    assert decision.suggested_action
    assert decision.failed_stage


@pytest.mark.parametrize(
    ("stage", "message", "reason_code"),
    [
        ("two_factor", "TOTP setup failed", "two_factor"),
        ("password", "password rejected", "password"),
        ("network", "connection reset", "network_error"),
    ],
)
def test_classifier_prefers_current_failure_over_successful_history(
    stage: str, message: str, reason_code: str
) -> None:
    decision = RegistrationFailureClassifier().classify(
        {
            "currentStage": stage,
            "message": message,
            "logs": [
                {
                    "stage": "email_verification",
                    "status": "success",
                    "message": "邮箱验证码已通过",
                }
            ],
        }
    )

    assert decision.reason_code == reason_code


def test_classifier_preserves_upstream_terminal_reason_code() -> None:
    decision = RegistrationFailureClassifier().classify(
        {
            "currentStage": "failed",
            "message": "浏览器流程终止",
            "failedAccounts": [
                {
                    "terminalReasonCode": "google_login_required",
                    "terminalRetryDecision": "独立指纹仍要求 Google 登录",
                }
            ],
        }
    )

    assert decision.reason_code == "google_login_required"
    assert decision.failure_reason == "独立指纹仍要求 Google 登录"


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        ("Bearer supersecret", "supersecret"),
        ("api_key=supersecret", "supersecret"),
        ("apiKey=supersecret", "supersecret"),
        ("Cookie: sid=secret; refresh=other", "secret"),
        ("Cookie: sid=secret; refresh=other", "other"),
        ("password: hunter 2", "hunter 2"),
    ],
)
def test_text_redactor_covers_headers_and_common_credential_forms(
    text: str, secret: str
) -> None:
    assert secret not in redact_text(text)


def test_recursive_sanitizer_removes_api_key_variants() -> None:
    sanitized = sanitize_value(
        {
            "apiKey": "one-secret",
            "api_key": "two-secret",
            "x-api-key": "three-secret",
            "status": "failed",
        }
    )

    assert sanitized == {"status": "failed"}


def test_presenter_filters_non_failures_and_prefers_frozen_failure_context(
    tmp_path: Path,
) -> None:
    presenter = make_presenter(tmp_path)

    assert (
        presenter.record_failure(
            {"processId": "completed", "status": "completed", "message": "done"}
        )
        is None
    )
    assert (
        presenter.record_failure(
            {
                "processId": "completed-with-old-context",
                "status": "completed",
                "failureContext": {"status": "failed", "message": "stale failure"},
            }
        )
        is None
    )
    saved = presenter.record_failure(
        {
            "processId": "process-1",
            "status": "failed",
            "message": "later mutable message",
            "currentStage": "running",
            "failureContext": {
                "message": "验证码在提交后被拒绝",
                "currentStage": "email_verification",
                "currentLocation": "OpenAI 验证码页",
                "currentAction": "提交本轮验证码",
                "failedStage": "email_verification_submit",
                "logs": [{"message": "frozen final log"}],
            },
        }
    )

    assert saved is not None
    assert saved["message"] == "验证码在提交后被拒绝"
    assert saved["currentStage"] == "email_verification"
    assert saved["failedStage"] == "email_verification_submit"
    assert saved["reasonCode"] == "email_verification"
    assert saved["logs"] == [{"message": "frozen final log"}]
    assert presenter.snapshot()["total"] == 1


def test_presenter_cuts_logs_at_frozen_failure_index(tmp_path: Path) -> None:
    presenter = make_presenter(tmp_path)

    saved = presenter.record_failure(
        {
            "processId": "frozen-log-index",
            "status": "failed",
            "message": "later inventory follow-up",
            "logs": [
                {"message": "验证码提交失败"},
                {"message": "库存已释放"},
            ],
            "failureContext": {
                "message": "验证码提交失败",
                "currentStage": "email_verification",
                "failureLogIndex": 0,
            },
        }
    )

    assert saved is not None
    assert saved["failedStage"] == "email_verification"
    assert saved["logs"] == [{"message": "验证码提交失败"}]


def test_presenter_uses_failed_accounts_and_keeps_structured_diagnostics(
    tmp_path: Path,
) -> None:
    presenter = make_presenter(tmp_path)

    saved = presenter.record_failure(
        {
            "processId": "mixed-batch",
            "status": "failed",
            "email": "success@example.com",
            "emails": ["success@example.com", "failed@example.com"],
            "message": "批量注册未全部成功",
            "failedAccounts": [
                {
                    "email": "failed@example.com",
                    "status": "failed",
                    "terminalReasonCode": "google_login_required",
                    "terminalRetryDecision": "独立指纹仍要求 Google 登录",
                    "registrationChain": {
                        "currentCode": "registration_clicked",
                        "apiKey": "chain-secret",
                    },
                    "pageState": {
                        "stage": "google_oauth",
                        "authorization": "Bearer page-secret",
                    },
                }
            ],
        }
    )

    assert saved is not None
    assert saved["email"] == "failed@example.com"
    assert saved["emails"] == ["failed@example.com"]
    assert saved["attemptedEmails"] == [
        "success@example.com",
        "failed@example.com",
    ]
    assert saved["reasonCode"] == "google_login_required"
    assert saved["failureReason"] == "独立指纹仍要求 Google 登录"
    assert saved["registrationChain"]["currentCode"] == "registration_clicked"
    assert saved["pageState"]["stage"] == "google_oauth"
    serialized = json.dumps(saved, ensure_ascii=False)
    assert "chain-secret" not in serialized
    assert "page-secret" not in serialized


def test_presenter_redacts_secrets_recursively_and_keeps_only_last_80_logs(
    tmp_path: Path,
) -> None:
    presenter = make_presenter(tmp_path)
    jwt = "abcdefghijk.abcdefghijkl.mnopqrstuvwxyz"
    logs = [{"message": f"ordinary-{index}"} for index in range(82)]
    logs.extend(
        [
            {
                "message": (
                    "password=hunter2 token=token-value OTP: 123456 "
                    "proxy=http://alice:proxy-pass@proxy.example:8080 "
                    f"jwt={jwt}"
                ),
                "password": "nested-password",
                "authorization": "Bearer access-secret",
                "cookie": "session=private-cookie",
                "otpStatus": "failed",
            }
        ]
    )
    saved = presenter.record_failure(
        {
            "processId": "secret-process",
            "status": "failed",
            "message": (
                "验证码失败 password=top-secret token=top-token OTP=654321 "
                "via socks5://proxy-user:proxy-secret@host.example:1080"
            ),
            "logs": logs,
            "failedAccounts": [
                {
                    "email": "failed@example.com",
                    "password": "account-password",
                    "accessToken": "account-token",
                    "details": {"totp": "JBSWY3DPEHPK3PXP"},
                }
            ],
        }
    )

    assert saved is not None
    serialized = json.dumps(saved, ensure_ascii=False)
    for secret in (
        "hunter2",
        "token-value",
        "123456",
        "proxy-pass",
        jwt,
        "top-secret",
        "top-token",
        "654321",
        "proxy-secret",
        "account-password",
        "account-token",
        "JBSWY3DPEHPK3PXP",
        "access-secret",
        "private-cookie",
    ):
        assert secret not in serialized
    assert REDACTED in serialized
    assert len(saved["logs"]) == 80
    assert "password" not in saved["logs"][-1]
    assert "authorization" not in saved["logs"][-1]
    assert "cookie" not in saved["logs"][-1]
    assert saved["logs"][-1]["otpStatus"] == "failed"


def test_repository_upsert_is_idempotent_while_jsonl_audits_each_write(
    tmp_path: Path,
) -> None:
    presenter = make_presenter(tmp_path)
    base = {
        "processId": "same-process",
        "status": "failed",
        "currentStage": "password",
    }

    presenter.record_failure({**base, "message": "first password failure"})
    presenter.record_failure({**base, "message": "second password failure"})

    snapshot = presenter.snapshot()
    assert snapshot["total"] == 1
    assert snapshot["records"][0]["message"] == "second password failure"
    lines = presenter.repository.log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["processId"] for line in lines] == [
        "same-process",
        "same-process",
    ]


def test_snapshot_skips_bad_json_classifies_legacy_rows_and_paginates(
    tmp_path: Path,
) -> None:
    presenter = make_presenter(tmp_path)
    connection = connect_db(str(tmp_path / "monitor.db"))
    try:
        rows = [
            (
                "oldest",
                {
                    "processId": "oldest",
                    "status": "failed",
                    "email": "other@example.com",
                    "message": "Session expired",
                    "recordedAt": "2026-08-15T01:00:00+00:00",
                },
            ),
            (
                "middle",
                {
                    "processId": "middle",
                    "status": "failed",
                    "email": "target@example.com",
                    "message": "验证码错误",
                    "recordedAt": "2026-08-15T02:00:00+00:00",
                },
            ),
            (
                "newest",
                {
                    "processId": "newest",
                    "status": "failed",
                    "email": "target@example.com",
                    "message": "邮箱验证码超时",
                    "recordedAt": "2026-08-15T03:00:00+00:00",
                },
            ),
        ]
        for process_id, value in rows:
            connection.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?)",
                (
                    f"{REGISTRATION_PROCESS_FAILURE_PREFIX}{process_id}",
                    json.dumps(value, ensure_ascii=False),
                ),
            )
        connection.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?)",
            (f"{REGISTRATION_PROCESS_FAILURE_PREFIX}broken", "{bad-json"),
        )
        connection.commit()
    finally:
        connection.close()

    page = presenter.snapshot(limit=1, offset=1)
    assert page["total"] == 3
    assert [item["processId"] for item in page["records"]] == ["middle"]
    assert page["records"][0]["reasonCode"] == "email_verification"
    assert page["summary"]["byReason"] == {
        "email_verification": 2,
        "session": 1,
    }

    filtered = presenter.snapshot(
        email="TARGET@EXAMPLE.COM", reason_code="email_verification"
    )
    assert filtered["total"] == 2
    assert [item["processId"] for item in filtered["records"]] == [
        "newest",
        "middle",
    ]
    assert filtered["logFile"] == str(presenter.repository.log_file.resolve())


def test_snapshot_can_return_lightweight_records_for_status_polling(
    tmp_path: Path,
) -> None:
    presenter = make_presenter(tmp_path)
    presenter.record_failure(
        {
            "processId": "lightweight",
            "status": "failed",
            "message": "验证码错误",
            "logs": [{"message": "验证码错误", "stage": "email_verification"}],
            "failedAccounts": [{"email": "failed@example.com", "status": "failed"}],
        }
    )

    snapshot = presenter.snapshot(include_details=False)

    assert snapshot["records"][0]["reasonCode"] == "email_verification"
    assert snapshot["records"][0]["logCount"] == 1
    assert snapshot["records"][0]["failedAccountCount"] == 1
    assert "logs" not in snapshot["records"][0]
    assert "failedAccounts" not in snapshot["records"][0]


def test_snapshot_sorts_mixed_timezone_offsets_by_instant(tmp_path: Path) -> None:
    presenter = make_presenter(tmp_path)
    presenter.record_failure(
        {
            "processId": "older-local-offset",
            "status": "failed",
            "message": "older",
            "recordedAt": "2026-08-15T10:00:00+08:00",
        }
    )
    presenter.record_failure(
        {
            "processId": "newer-utc",
            "status": "failed",
            "message": "newer",
            "recordedAt": "2026-08-15T03:00:00+00:00",
        }
    )

    assert [item["processId"] for item in presenter.snapshot()["records"]] == [
        "newer-utc",
        "older-local-offset",
    ]


def test_jsonl_is_utf8_valid_and_rotates(tmp_path: Path) -> None:
    presenter = make_presenter(tmp_path, jsonl_max_bytes=450)

    for index in range(5):
        presenter.record_failure(
            {
                "processId": f"rotate-{index}",
                "status": "failed",
                "message": "验证码失败；中文日志用于验证 UTF-8 " + ("详情" * 30),
                "recordedAt": f"2026-08-15T0{index}:00:00+00:00",
            }
        )

    log_file = presenter.repository.log_file
    rotated = Path(f"{log_file}.1")
    assert log_file.exists()
    assert rotated.exists()
    decoded: list[dict] = []
    for candidate in (log_file, rotated, Path(f"{log_file}.2")):
        if not candidate.exists():
            continue
        text = candidate.read_text(encoding="utf-8")
        assert "中文日志" in text
        decoded.extend(json.loads(line) for line in text.splitlines())
    assert decoded
    assert all(item["status"] == "failed" for item in decoded)
