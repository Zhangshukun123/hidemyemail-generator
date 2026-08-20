from zkgmail_code_server.rate_limit import SlidingWindowRateLimiter


def test_rate_limiter_enforces_window_and_retry_after():
    now = [10.0]
    limiter = SlidingWindowRateLimiter(
        request_limit=2,
        window_seconds=10,
        monotonic=lambda: now[0],
    )

    assert limiter.allow("client") == (True, 0)
    now[0] = 11.0
    assert limiter.allow("client") == (True, 0)
    now[0] = 12.0
    assert limiter.allow("client") == (False, 8)
    now[0] = 20.0
    assert limiter.allow("client") == (True, 0)


def test_rate_limiter_keeps_a_hard_key_capacity():
    now = [100.0]
    limiter = SlidingWindowRateLimiter(
        request_limit=2,
        window_seconds=60,
        max_keys=32,
        monotonic=lambda: now[0],
    )

    for index in range(64):
        assert limiter.allow(f"client-{index}") == (True, 0)
        now[0] += 0.1

    assert len(limiter._events) == 32
