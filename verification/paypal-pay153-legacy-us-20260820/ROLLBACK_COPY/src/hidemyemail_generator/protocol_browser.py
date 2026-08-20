"""Shared browser persona for protocol registration HTTP sessions."""

from __future__ import annotations

from dataclasses import dataclass
import re


CHROME_IMPERSONATE_PROFILES = (
    "chrome136",
    "chrome142",
    "chrome145",
    "chrome146",
)
FIREFOX_IMPERSONATE_PROFILES = (
    "firefox133",
    "firefox135",
    "firefox144",
    "firefox147",
)
PROTOCOL_IMPERSONATE_PROFILES = (
    *CHROME_IMPERSONATE_PROFILES,
    *FIREFOX_IMPERSONATE_PROFILES,
)
DEFAULT_PROTOCOL_IMPERSONATE = "chrome136"


def normalize_protocol_impersonate(value: object, *, default: str = "") -> str:
    """Return a curl_cffi profile from the supported protocol pool."""

    normalized = str(value or "").strip().casefold()
    if normalized in PROTOCOL_IMPERSONATE_PROFILES:
        return normalized
    return default


def language_header(language: str) -> str:
    primary = str(language or "en-US").strip() or "en-US"
    root = primary.split("-", 1)[0]
    if primary.casefold() == "en-us":
        return "en-US,en;q=0.9"
    return f"{primary},{root};q=0.9,en-US;q=0.8,en;q=0.7"


@dataclass(frozen=True)
class ProtocolBrowserPersona:
    """One internally consistent TLS/header browser identity."""

    impersonate: str
    family: str
    major_version: str

    @classmethod
    def from_impersonate(cls, value: object) -> "ProtocolBrowserPersona":
        impersonate = normalize_protocol_impersonate(
            value,
            default=DEFAULT_PROTOCOL_IMPERSONATE,
        )
        match = re.fullmatch(r"(chrome|firefox)(\d+)", impersonate)
        if match is None:  # pragma: no cover - constants are validated above
            raise ValueError(f"不支持的协议浏览器指纹：{impersonate}")
        return cls(
            impersonate=impersonate,
            family=match.group(1),
            major_version=match.group(2),
        )

    @property
    def firefox(self) -> bool:
        return self.family == "firefox"

    @property
    def user_agent(self) -> str:
        version = self.major_version
        if self.firefox:
            return (
                f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{version}.0) "
                f"Gecko/20100101 Firefox/{version}.0"
            )
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{version}.0.0.0 Safari/537.36"
        )

    @property
    def sec_ch_ua(self) -> str:
        if self.firefox:
            return ""
        version = self.major_version
        return (
            f'"Not:A-Brand";v="99", "Google Chrome";v="{version}", '
            f'"Chromium";v="{version}"'
        )

    @property
    def sec_ch_ua_platform(self) -> str:
        return "" if self.firefox else '"Windows"'

    @property
    def sec_ch_ua_mobile(self) -> str:
        return "" if self.firefox else "?0"

    def session_headers(self, language: str) -> dict[str, str]:
        headers = {
            "accept-language": language_header(language),
            "user-agent": self.user_agent,
        }
        if self.sec_ch_ua:
            headers["sec-ch-ua"] = self.sec_ch_ua
        if self.sec_ch_ua_mobile:
            headers["sec-ch-ua-mobile"] = self.sec_ch_ua_mobile
        if self.sec_ch_ua_platform:
            headers["sec-ch-ua-platform"] = self.sec_ch_ua_platform
        return headers


__all__ = [
    "CHROME_IMPERSONATE_PROFILES",
    "DEFAULT_PROTOCOL_IMPERSONATE",
    "FIREFOX_IMPERSONATE_PROFILES",
    "PROTOCOL_IMPERSONATE_PROFILES",
    "ProtocolBrowserPersona",
    "language_header",
    "normalize_protocol_impersonate",
]
