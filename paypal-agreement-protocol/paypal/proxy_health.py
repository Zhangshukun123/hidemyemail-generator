"""Transport-aligned proxy health checks for PayPal protocol jobs."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

import httpx

try:
    from curl_cffi.requests import Session as CurlSession
except Exception:  # pragma: no cover - optional runtime dependency
    CurlSession = None


PAYPAL_PROBE_URL = "https://www.paypal.com/"


@dataclass(frozen=True)
class ProxyProbeResult:
    ok: bool
    detail: str
    engine: str


class ProxyProbeAdapter(Protocol):
    """Adapter contract shared by the production PayPal transports."""

    engine: str

    def probe(self, proxy_url: str, timeout_seconds: float) -> ProxyProbeResult: ...


class CurlCffiProbeAdapter:
    engine = "curl_cffi"

    def probe(self, proxy_url: str, timeout_seconds: float) -> ProxyProbeResult:
        session = CurlSession(
            impersonate=(os.getenv("PAYPAL_CURL_IMPERSONATE") or "chrome").strip()
        )
        try:
            session.trust_env = False
            response = session.get(
                PAYPAL_PROBE_URL,
                proxies={"http": proxy_url, "https": proxy_url},
                timeout=timeout_seconds,
                allow_redirects=False,
            )
            if response.status_code == 407:
                return ProxyProbeResult(False, "proxy authentication required (HTTP 407)", self.engine)
            return ProxyProbeResult(True, f"HTTP {response.status_code}", self.engine)
        finally:
            session.close()


class HttpxProbeAdapter:
    engine = "httpx"

    def probe(self, proxy_url: str, timeout_seconds: float) -> ProxyProbeResult:
        with httpx.Client(
            proxy=proxy_url,
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = client.get(PAYPAL_PROBE_URL)
        if response.status_code == 407:
            return ProxyProbeResult(False, "proxy authentication required (HTTP 407)", self.engine)
        return ProxyProbeResult(True, f"HTTP {response.status_code}", self.engine)


class ProxyHealthChecker:
    """Strategy selector that mirrors the transport used by ``PayPalSession``."""

    def __init__(self, adapter: ProxyProbeAdapter | None = None) -> None:
        requested = (os.getenv("PAYPAL_HTTP_ENGINE") or "curl_cffi").strip().lower()
        selected = (
            CurlCffiProbeAdapter()
            if requested != "httpx" and CurlSession is not None
            else HttpxProbeAdapter()
        )
        self.adapter = adapter or selected

    def check(self, proxy_url: str, timeout_seconds: float = 8.0) -> ProxyProbeResult:
        try:
            return self.adapter.probe(proxy_url, timeout_seconds)
        except Exception as error:
            return ProxyProbeResult(False, str(error), self.adapter.engine)
