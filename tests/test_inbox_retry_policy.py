from hidemyemail_generator.inbox_retry_policy import InboxRetryPolicy


def test_transient_network_backoff_recovers_within_thirty_seconds() -> None:
    policy = InboxRetryPolicy()

    assert [
        policy.decide("无法连接 IMAP 服务器", failures).delay_seconds
        for failures in range(1, 8)
    ] == [5, 10, 20, 30, 30, 30, 30]
    assert policy.decide("无法连接 IMAP 服务器", 6).kind == "transient"


def test_authentication_backoff_remains_bounded() -> None:
    policy = InboxRetryPolicy()

    decision = policy.decide("IMAP 登录失败，请检查应用专用密码", 8)

    assert decision.kind == "authentication"
    assert decision.delay_seconds == 15 * 60
