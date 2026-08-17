from unittest.mock import patch

from paypal import proxy_health


class FakeResponse:
    status_code = 204


class FakeCurlSession:
    instances = []

    def __init__(self, *, impersonate):
        self.impersonate = impersonate
        self.calls = []
        self.closed = False
        self.trust_env = True
        self.instances.append(self)

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse()

    def close(self):
        self.closed = True


def test_proxy_probe_uses_same_curl_transport_as_paypal_session(monkeypatch):
    FakeCurlSession.instances.clear()
    monkeypatch.delenv("PAYPAL_HTTP_ENGINE", raising=False)
    with patch.object(proxy_health, "CurlSession", FakeCurlSession):
        result = proxy_health.ProxyHealthChecker().check(
            "http://user:password@proxy.test:8080", 4.0
        )

    session = FakeCurlSession.instances[-1]
    assert result.ok is True
    assert result.engine == "curl_cffi"
    assert session.trust_env is False
    assert session.closed is True
    assert session.calls[0][1]["proxies"]["https"].endswith("proxy.test:8080")
    assert session.calls[0][1]["timeout"] == 4.0


def test_proxy_probe_honors_explicit_httpx_engine(monkeypatch):
    monkeypatch.setenv("PAYPAL_HTTP_ENGINE", "httpx")
    with patch.object(proxy_health, "CurlSession", FakeCurlSession):
        checker = proxy_health.ProxyHealthChecker()

    assert checker.adapter.engine == "httpx"
