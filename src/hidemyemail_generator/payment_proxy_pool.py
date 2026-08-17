from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from typing import Callable, Sequence

from .card_link_proxy_resolver import (
    CardLinkProxyResolutionError,
    CardLinkProxyResolver,
)


DEFAULT_PAYMENT_PROXY_COUNT = 3
MINIMUM_PAYMENT_PROXY_COUNT = 2
RECENT_PAYMENT_EXIT_LIMIT = 30


class PaymentProxyPoolError(RuntimeError):
    """Report that a primary and backup exit could not be prepared."""


@dataclass(frozen=True)
class PaymentProxySource:
    """Model one configured source capable of producing fresh proxy sessions."""

    name: str
    mode: str
    proxy_factory: Callable[[], str]
    stable_endpoints: bool = True


@dataclass(frozen=True)
class PaymentProxyCandidate:
    proxy_url: str
    exit_ip: str
    country: str
    source: str
    mode: str

    @property
    def exit_fingerprint(self) -> str:
        return hashlib.sha256(self.exit_ip.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class PaymentProxyPoolSelection:
    candidates: tuple[PaymentProxyCandidate, ...]
    target_count: int
    minimum_count: int
    exhausted_sources: tuple[str, ...] = ()

    @property
    def proxy_urls(self) -> list[str]:
        return [candidate.proxy_url for candidate in self.candidates]

    @property
    def backup_count(self) -> int:
        return max(0, len(self.candidates) - 1)

    @property
    def exit_fingerprints(self) -> tuple[str, ...]:
        return tuple(candidate.exit_fingerprint for candidate in self.candidates)


class PaymentProxyPoolPresenter:
    """Build a payment ViewModel with distinct, measured primary/backup exits."""

    def __init__(
        self,
        resolver: CardLinkProxyResolver,
        *,
        recent_exit_limit: int = RECENT_PAYMENT_EXIT_LIMIT,
    ) -> None:
        self.resolver = resolver
        self._build_lock = threading.RLock()
        self._recent_exit_limit = max(3, min(100, int(recent_exit_limit)))
        self._recent_exit_ips: tuple[str, ...] = ()

    def build(
        self,
        sources: Sequence[PaymentProxySource],
        expected_country: str,
        *,
        excluded_exit_ips: Sequence[str] = (),
        target_count: int = DEFAULT_PAYMENT_PROXY_COUNT,
        minimum_count: int = MINIMUM_PAYMENT_PROXY_COUNT,
    ) -> PaymentProxyPoolSelection:
        with self._build_lock:
            selection = self._build(
                sources,
                expected_country,
                excluded_exit_ips=(
                    *tuple(excluded_exit_ips or ()),
                    *self._recent_exit_ips,
                ),
                target_count=target_count,
                minimum_count=minimum_count,
            )
            recent = list(self._recent_exit_ips)
            recent.extend(candidate.exit_ip for candidate in selection.candidates)
            self._recent_exit_ips = tuple(
                dict.fromkeys(reversed(recent))
            )[: self._recent_exit_limit][::-1]
            return selection

    def _build(
        self,
        sources: Sequence[PaymentProxySource],
        expected_country: str,
        *,
        excluded_exit_ips: Sequence[str] = (),
        target_count: int = DEFAULT_PAYMENT_PROXY_COUNT,
        minimum_count: int = MINIMUM_PAYMENT_PROXY_COUNT,
    ) -> PaymentProxyPoolSelection:
        target = int(target_count)
        minimum = int(minimum_count)
        if not 1 <= minimum <= target <= 10:
            raise ValueError("支付代理数量必须满足 1 <= 最少数量 <= 目标数量 <= 10")
        if not sources:
            raise PaymentProxyPoolError("没有可用于支付的代理来源")

        excluded = {
            normalized
            for value in excluded_exit_ips
            if (normalized := self.resolver.normalize_exit_ip(value))
        }
        selected: list[PaymentProxyCandidate] = []
        selected_urls: set[str] = set()
        exhausted: list[str] = []

        for source in sources:
            source_name = str(source.name or "proxy").strip() or "proxy"
            source_mode = str(source.mode or "dynamic").strip() or "dynamic"
            if not source.stable_endpoints:
                exhausted.append(f"{source_name}(端点不固定)")
                continue
            while len(selected) < target:
                try:
                    selection = self.resolver.resolve(
                        source.proxy_factory,
                        expected_country,
                        excluded_exit_ips=excluded,
                        require_exit_ip=True,
                    )
                except (CardLinkProxyResolutionError, RuntimeError, ValueError):
                    exhausted.append(source_name)
                    break

                if selection.proxy_url in selected_urls:
                    selected = [
                        candidate
                        for candidate in selected
                        if candidate.proxy_url != selection.proxy_url
                    ]
                    selected_urls.discard(selection.proxy_url)
                    excluded.add(selection.actual_ip)
                    exhausted.append(f"{source_name}(端点重复)")
                    break
                selected.append(
                    PaymentProxyCandidate(
                        proxy_url=selection.proxy_url,
                        exit_ip=selection.actual_ip,
                        country=selection.actual_country,
                        source=source_name,
                        mode=source_mode,
                    )
                )
                selected_urls.add(selection.proxy_url)
                excluded.add(selection.actual_ip)
            if len(selected) >= target:
                break

        if len(selected) < minimum:
            raise PaymentProxyPoolError(
                f"支付代理池至少需要 {minimum} 个不同真实出口 IP，"
                f"当前仅生成 {len(selected)} 个；请检查代理线路后重试"
            )
        return PaymentProxyPoolSelection(
            candidates=tuple(selected),
            target_count=target,
            minimum_count=minimum,
            exhausted_sources=tuple(exhausted),
        )


__all__ = [
    "DEFAULT_PAYMENT_PROXY_COUNT",
    "MINIMUM_PAYMENT_PROXY_COUNT",
    "RECENT_PAYMENT_EXIT_LIMIT",
    "PaymentProxyCandidate",
    "PaymentProxyPoolError",
    "PaymentProxyPoolPresenter",
    "PaymentProxyPoolSelection",
    "PaymentProxySource",
]
