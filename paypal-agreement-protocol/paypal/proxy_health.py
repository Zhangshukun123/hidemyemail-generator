"""Transport-aligned proxy health checks for PayPal protocol jobs."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from inspect import signature
from typing import Callable, Protocol
from urllib.parse import urlsplit

import httpx

try:
    from curl_cffi.requests import Session as CurlSession
except Exception:  # pragma: no cover - optional runtime dependency
    CurlSession = None


PAYPAL_PROBE_URL = "https://www.paypal.com/"
PAYPAL_PROBE_URLS = (
    PAYPAL_PROBE_URL,
    "https://www.paypal.com/signin",
)
Clock = Callable[[], float]
Sleeper = Callable[[float], None]


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.getenv(name) or default).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


@dataclass(frozen=True)
class ProxyStabilityPolicy:
    """Model for independent CONNECT/TLS probes before a payment starts."""

    rounds: int = 3
    interval_seconds: float = 0.25
    max_latency_ms: int = 6000
    targets: tuple[str, ...] = PAYPAL_PROBE_URLS

    def __post_init__(self) -> None:
        if self.rounds < 1:
            raise ValueError("proxy stability rounds must be at least 1")
        if self.interval_seconds < 0:
            raise ValueError("proxy stability interval cannot be negative")
        if self.max_latency_ms <= 0:
            raise ValueError("proxy stability latency limit must be positive")

    @classmethod
    def from_environment(cls) -> "ProxyStabilityPolicy":
        return cls(
            rounds=_env_int("PAYPAL_PROXY_PROBE_ROUNDS", 3, 2, 5),
            interval_seconds=(
                _env_int("PAYPAL_PROXY_PROBE_INTERVAL_MS", 250, 0, 2000) / 1000
            ),
            max_latency_ms=_env_int(
                "PAYPAL_PROXY_PROBE_MAX_LATENCY_MS", 6000, 500, 30000
            ),
        )

    def target_for_round(self, round_index: int) -> str:
        if not self.targets:
            return PAYPAL_PROBE_URL
        return self.targets[round_index % len(self.targets)]


@dataclass(frozen=True)
class ProxyProbeResult:
    ok: bool
    detail: str
    engine: str


class ProxyProbeAdapter(Protocol):
    """Adapter contract shared by the production PayPal transports."""

    engine: str

    def probe(
        self,
        proxy_url: str,
        timeout_seconds: float,
        target_url: str = PAYPAL_PROBE_URL,
    ) -> ProxyProbeResult: ...


class CurlCffiProbeAdapter:
    engine = "curl_cffi"

    def probe(
        self,
        proxy_url: str,
        timeout_seconds: float,
        target_url: str = PAYPAL_PROBE_URL,
    ) -> ProxyProbeResult:
        session = CurlSession(
            impersonate=(os.getenv("PAYPAL_CURL_IMPERSONATE") or "chrome").strip()
        )
        try:
            session.trust_env = False
            response = session.get(
                target_url,
                proxies={"http": proxy_url, "https": proxy_url},
                timeout=timeout_seconds,
                allow_redirects=False,
            )
            if response.status_code == 407:
                return ProxyProbeResult(False, "proxy authentication required (HTTP 407)", self.engine)
            if response.status_code == 429 or response.status_code >= 500:
                return ProxyProbeResult(
                    False, f"PayPal rejected probe (HTTP {response.status_code})", self.engine
                )
            return ProxyProbeResult(True, f"HTTP {response.status_code}", self.engine)
        finally:
            session.close()


class HttpxProbeAdapter:
    engine = "httpx"

    def probe(
        self,
        proxy_url: str,
        timeout_seconds: float,
        target_url: str = PAYPAL_PROBE_URL,
    ) -> ProxyProbeResult:
        with httpx.Client(
            proxy=proxy_url,
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = client.get(target_url)
        if response.status_code == 407:
            return ProxyProbeResult(False, "proxy authentication required (HTTP 407)", self.engine)
        if response.status_code == 429 or response.status_code >= 500:
            return ProxyProbeResult(
                False, f"PayPal rejected probe (HTTP {response.status_code})", self.engine
            )
        return ProxyProbeResult(True, f"HTTP {response.status_code}", self.engine)


class ProxyHealthChecker:
    """Presenter that applies a stability model through one transport strategy."""

    def __init__(
        self,
        adapter: ProxyProbeAdapter | None = None,
        *,
        policy: ProxyStabilityPolicy | None = None,
        clock: Clock = time.monotonic,
        sleeper: Sleeper = time.sleep,
    ) -> None:
        requested = (os.getenv("PAYPAL_HTTP_ENGINE") or "curl_cffi").strip().lower()
        selected = (
            CurlCffiProbeAdapter()
            if requested != "httpx" and CurlSession is not None
            else HttpxProbeAdapter()
        )
        self.adapter = adapter or selected
        self.policy = policy or ProxyStabilityPolicy.from_environment()
        self.clock = clock
        self.sleeper = sleeper
        try:
            signature(self.adapter.probe).bind(
                "http://proxy.invalid", 1.0, PAYPAL_PROBE_URL
            )
            self._adapter_accepts_target = True
        except TypeError:
            self._adapter_accepts_target = False
        except (ValueError, AttributeError):
            self._adapter_accepts_target = True

    def _probe(
        self,
        proxy_url: str,
        timeout_seconds: float,
        target_url: str,
    ) -> ProxyProbeResult:
        if self._adapter_accepts_target:
            return self.adapter.probe(proxy_url, timeout_seconds, target_url)
        return self.adapter.probe(proxy_url, timeout_seconds)

    def check(self, proxy_url: str, timeout_seconds: float = 8.0) -> ProxyProbeResult:
        observations: list[str] = []
        peak_latency_ms = 0
        for round_index in range(self.policy.rounds):
            target_url = self.policy.target_for_round(round_index)
            started = self.clock()
            try:
                result = self._probe(proxy_url, timeout_seconds, target_url)
            except Exception as error:
                result = ProxyProbeResult(False, str(error), self.adapter.engine)
            latency_ms = max(0, round((self.clock() - started) * 1000))
            peak_latency_ms = max(peak_latency_ms, latency_ms)
            round_number = round_index + 1
            target = urlsplit(target_url)
            target_label = f"{target.hostname or 'paypal'}{target.path or '/'}"
            if not result.ok:
                previous = (
                    f" after {', '.join(observations)}" if observations else ""
                )
                return ProxyProbeResult(
                    False,
                    f"stability round {round_number}/{self.policy.rounds} "
                    f"failed at {target_label}{previous}: {result.detail}",
                    self.adapter.engine,
                )
            if latency_ms > self.policy.max_latency_ms:
                return ProxyProbeResult(
                    False,
                    f"stability round {round_number}/{self.policy.rounds} "
                    f"too slow at {target_label}: {latency_ms}ms > "
                    f"{self.policy.max_latency_ms}ms",
                    self.adapter.engine,
                )
            observations.append(result.detail)
            if (
                round_number < self.policy.rounds
                and self.policy.interval_seconds > 0
            ):
                self.sleeper(self.policy.interval_seconds)
        return ProxyProbeResult(
            True,
            f"stable {self.policy.rounds}/{self.policy.rounds}; "
            f"peak {peak_latency_ms}ms; {', '.join(observations)}",
            self.adapter.engine,
        )
