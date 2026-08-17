from __future__ import annotations

import ipaddress
import re
import threading
from collections.abc import Collection
from dataclasses import dataclass
from typing import Callable

from .card_link_runtime import detect_proxy_health


DEFAULT_PROXY_CANDIDATE_LIMIT = 6


class CardLinkProxyResolutionError(RuntimeError):
    """Report country-selection exhaustion without exposing proxy credentials."""


@dataclass(frozen=True)
class CardLinkProxySelection:
    proxy_url: str
    expected_country: str
    actual_country: str
    actual_ip: str
    candidates_tested: int
    observations: tuple[str, ...]


class CardLinkProxyResolver:
    """Select a fresh proxy candidate whose measured exit matches the request."""

    def __init__(
        self,
        *,
        health_detector: Callable[..., object] = detect_proxy_health,
        max_candidates: int = DEFAULT_PROXY_CANDIDATE_LIMIT,
        timeout_seconds: int = 15,
    ) -> None:
        candidate_limit = int(max_candidates)
        if not 1 <= candidate_limit <= 20:
            raise ValueError("提链代理候选次数必须在 1–20 之间")
        self.health_detector = health_detector
        self.max_candidates = candidate_limit
        self.timeout_seconds = max(1, int(timeout_seconds))
        self._selection_lock = threading.RLock()
        self._recent_exit_ip = ""

    def resolve(
        self,
        proxy_factory: Callable[[], str],
        expected_country: str,
        *,
        excluded_exit_ips: Collection[str] = (),
        require_exit_ip: bool = False,
    ) -> CardLinkProxySelection:
        with self._selection_lock:
            excluded = set(excluded_exit_ips or ())
            if self._recent_exit_ip:
                excluded.add(self._recent_exit_ip)
            selection = self._resolve(
                proxy_factory,
                expected_country,
                excluded_exit_ips=excluded,
                require_exit_ip=require_exit_ip,
            )
            if selection.actual_ip:
                self._recent_exit_ip = selection.actual_ip
            return selection

    def _resolve(
        self,
        proxy_factory: Callable[[], str],
        expected_country: str,
        *,
        excluded_exit_ips: Collection[str],
        require_exit_ip: bool,
    ) -> CardLinkProxySelection:
        expected = str(expected_country or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", expected):
            raise ValueError("提链代理目标国家无效")
        excluded = {
            normalized
            for value in excluded_exit_ips
            if (normalized := self.normalize_exit_ip(value))
        }

        observations: list[str] = []
        for candidate_number in range(1, self.max_candidates + 1):
            proxy_url = str(proxy_factory() or "").strip()
            if not proxy_url:
                raise CardLinkProxyResolutionError("请先在“代理与线路”中保存代理配置")
            try:
                health = self.health_detector(
                    proxy_url,
                    timeout=self.timeout_seconds,
                    check_stripe=False,
                    check_chatgpt=False,
                )
            except Exception:
                observations.append("检测失败")
                continue

            actual = str(getattr(health, "country", "") or "").strip().upper()
            actual_ip = self.normalize_exit_ip(getattr(health, "ip", ""))
            success = bool(getattr(health, "success", False))
            observation = actual if success and actual else f"{actual or '未知'}(不可用)"
            if success and actual == expected and require_exit_ip and not actual_ip:
                observation = f"{actual}(未返回IP)"
            elif success and actual == expected and actual_ip in excluded:
                observation = f"{actual}(重复IP)"
            observations.append(observation)
            if success and actual == expected:
                if require_exit_ip and not actual_ip:
                    continue
                if actual_ip in excluded:
                    continue
                return CardLinkProxySelection(
                    proxy_url=proxy_url,
                    expected_country=expected,
                    actual_country=actual,
                    actual_ip=actual_ip,
                    candidates_tested=candidate_number,
                    observations=tuple(observations),
                )

        observed = "、".join(observations) or "未返回国家"
        raise CardLinkProxyResolutionError(
            f"连续 {self.max_candidates} 个 {expected} 代理候选未通过真实出口校验"
            f"（实测：{observed}）；请重试或检查 Kookeey 线路"
        )

    @staticmethod
    def normalize_exit_ip(value: object) -> str:
        try:
            return ipaddress.ip_address(str(value or "").strip()).compressed
        except ValueError:
            return ""


__all__ = [
    "CardLinkProxyResolutionError",
    "CardLinkProxyResolver",
    "CardLinkProxySelection",
    "DEFAULT_PROXY_CANDIDATE_LIMIT",
]
