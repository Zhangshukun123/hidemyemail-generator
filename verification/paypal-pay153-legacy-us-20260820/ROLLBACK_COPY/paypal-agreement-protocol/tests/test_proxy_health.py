from unittest.mock import patch

import pytest

from paypal import proxy_health


class FakeResponse:
    def __init__(self, status_code=204):
        self.status_code = status_code


class FakeCurlSession:
    instances = []
    response_status_code = 204

    def __init__(self, *, impersonate):
        self.impersonate = impersonate
        self.calls = []
        self.closed = False
        self.trust_env = True
        self.instances.append(self)

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.response_status_code)

    def close(self):
        self.closed = True


class FlakySequenceAdapter:
    engine = "curl_cffi"

    def __init__(self):
        self.calls = []

    def probe(self, proxy_url, timeout_seconds, target_url=None):
        self.calls.append((proxy_url, timeout_seconds, target_url))
        if len(self.calls) == 1:
            return proxy_health.ProxyProbeResult(True, "HTTP 302", self.engine)
        raise OSError("proxy tunnel closed after the first request")


class LegacyTwoArgumentAdapter:
    engine = "legacy"

    def __init__(self):
        self.calls = []

    def probe(self, proxy_url, timeout_seconds):
        self.calls.append((proxy_url, timeout_seconds))
        return proxy_health.ProxyProbeResult(True, "HTTP 204", self.engine)


def test_proxy_probe_uses_same_curl_transport_as_paypal_session(monkeypatch):
    FakeCurlSession.instances.clear()
    monkeypatch.delenv("PAYPAL_HTTP_ENGINE", raising=False)
    with patch.object(proxy_health, "CurlSession", FakeCurlSession):
        result = proxy_health.ProxyHealthChecker(
            policy=proxy_health.ProxyStabilityPolicy(
                rounds=3,
                interval_seconds=0,
            )
        ).check("http://user:password@proxy.test:8080", 4.0)

    assert result.ok is True
    assert result.engine == "curl_cffi"
    assert "stable 3/3" in result.detail
    assert len(FakeCurlSession.instances) == 3
    assert [session.calls[0][0] for session in FakeCurlSession.instances] == [
        "https://www.paypal.com/",
        "https://www.paypal.com/signin",
        "https://www.paypal.com/",
    ]
    for session in FakeCurlSession.instances:
        assert session.trust_env is False
        assert session.closed is True
        assert session.calls[0][1]["proxies"]["https"].endswith(
            "proxy.test:8080"
        )
        assert session.calls[0][1]["timeout"] == 4.0


def test_proxy_probe_honors_explicit_httpx_engine(monkeypatch):
    monkeypatch.setenv("PAYPAL_HTTP_ENGINE", "httpx")
    with patch.object(proxy_health, "CurlSession", FakeCurlSession):
        checker = proxy_health.ProxyHealthChecker()

    assert checker.adapter.engine == "httpx"


def test_one_success_then_disconnect_fails_stability_preflight(monkeypatch):
    monkeypatch.delenv("PAYPAL_PROXY_PROBE_ROUNDS", raising=False)
    monkeypatch.setenv("PAYPAL_PROXY_PROBE_INTERVAL_MS", "0")
    adapter = FlakySequenceAdapter()

    result = proxy_health.ProxyHealthChecker(adapter=adapter).check(
        "http://user:password@proxy.test:8080", 4.0
    )

    assert result.ok is False
    assert len(adapter.calls) == 2
    assert "2/3" in result.detail
    assert "after HTTP 302" in result.detail
    assert "tunnel closed" in result.detail


def test_checker_keeps_legacy_two_argument_adapter_compatible():
    adapter = LegacyTwoArgumentAdapter()

    result = proxy_health.ProxyHealthChecker(
        adapter=adapter,
        policy=proxy_health.ProxyStabilityPolicy(
            rounds=3,
            interval_seconds=0,
        ),
    ).check("http://user:password@proxy.test:8080", 4.0)

    assert result.ok is True
    assert len(adapter.calls) == 3


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"rounds": 0}, "rounds"),
        ({"interval_seconds": -0.1}, "interval"),
        ({"max_latency_ms": 0}, "latency"),
    ],
)
def test_stability_policy_rejects_invalid_direct_configuration(kwargs, message):
    with pytest.raises(ValueError, match=message):
        proxy_health.ProxyStabilityPolicy(**kwargs)


def test_stability_preflight_rejects_latency_above_policy_limit():
    adapter = FlakySequenceAdapter()
    adapter.probe = lambda *_args: proxy_health.ProxyProbeResult(
        True, "HTTP 302", adapter.engine
    )
    clock_values = iter((10.0, 16.001))
    policy = proxy_health.ProxyStabilityPolicy(
        rounds=3,
        interval_seconds=0,
        max_latency_ms=6000,
    )

    result = proxy_health.ProxyHealthChecker(
        adapter=adapter,
        policy=policy,
        clock=lambda: next(clock_values),
    ).check("http://user:password@proxy.test:8080", 8.0)

    assert result.ok is False
    assert "round 1/3" in result.detail
    assert "6001ms > 6000ms" in result.detail


@pytest.mark.parametrize("status_code", [407, 429, 500, 503])
def test_curl_probe_rejects_unusable_http_status(monkeypatch, status_code):
    FakeCurlSession.instances.clear()
    FakeCurlSession.response_status_code = status_code
    monkeypatch.delenv("PAYPAL_HTTP_ENGINE", raising=False)
    try:
        with patch.object(proxy_health, "CurlSession", FakeCurlSession):
            result = proxy_health.ProxyHealthChecker(
                policy=proxy_health.ProxyStabilityPolicy(
                    rounds=3,
                    interval_seconds=0,
                )
            ).check("http://user:password@proxy.test:8080", 4.0)
    finally:
        FakeCurlSession.response_status_code = 204

    assert result.ok is False
    assert "round 1/3" in result.detail
    assert f"HTTP {status_code}" in result.detail
    assert len(FakeCurlSession.instances) == 1
    assert FakeCurlSession.instances[0].closed is True


def test_select_working_proxy_moves_to_next_candidate_after_stability_failure():
    import web

    pool = [
        "proxy-one.test:8001:user:password",
        "proxy-two.test:8002:user:password",
        "proxy-three.test:8003:user:password",
    ]
    probe_results = iter(
        (
            (False, "curl_cffi: stability round 2/3 failed"),
            (True, "curl_cffi: stable 3/3"),
        )
    )

    with (
        patch.object(web.random, "shuffle", side_effect=lambda _items: None),
        patch.object(
            web,
            "proxy_probe",
            side_effect=lambda *_args: next(probe_results),
        ) as probe,
    ):
        selected = web.select_working_proxy(pool, country="")

    assert probe.call_count == 2
    assert selected.entry is not None
    assert selected.entry.host == "proxy-two.test"
    assert selected.entry.port == 8002
