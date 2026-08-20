"""浏览器指纹及国家语言环境构造。

函数保持无状态，代理检测结果只需提供 success/country/timezone 属性。
"""

from __future__ import annotations

import dataclasses
import random
import re
from typing import Protocol

from ._card_link_country_profiles import (
    COUNTRY_BROWSER_LOCALE,
    COUNTRY_TIMEZONE,
    DEVICE_PROFILES,
    LOCALE_MAP,
    PAYMENT_DEVICE_PROFILES,
    REGISTER_DEVICE_PROFILES,
    TEAM_DEVICE_PROFILES,
)


class ProxyExitInfo(Protocol):
    success: bool
    country: str
    timezone: str


@dataclasses.dataclass
class DeviceFingerprint:
    user_agent: str
    locale: str
    languages: list[str]
    timezone: str
    viewport_width: int
    viewport_height: int
    screen_width: int
    screen_height: int
    outer_width: int
    outer_height: int
    device_scale_factor: float
    hardware_concurrency: int
    device_memory: int
    platform: str
    vendor: str = "Google Inc."
    max_touch_points: int = 0

    @property
    def accept_language(self) -> str:
        if not self.languages:
            return self.locale
        return ",".join([self.languages[0], *[f"{lang};q={max(0.5, 0.9 - i * 0.1):.1f}" for i, lang in enumerate(self.languages[1:], start=0)]])

    @property
    def chrome_major(self) -> str:
        match = re.search(r"Chrome/(\d+)", self.user_agent)
        return match.group(1) if match else "146"

    @property
    def chrome_full(self) -> str:
        match = re.search(r"Chrome/([\d.]+)", self.user_agent)
        return match.group(1) if match else "146.0.0.0"


def generate_fingerprint(profiles: list[dict] | None = None) -> DeviceFingerprint:
    profile = random.choice(profiles or DEVICE_PROFILES)
    viewport = random.choice([
        (1280, 720, 1280, 720, 1),
        (1365, 768, 1366, 768, 1),
        (1440, 900, 1440, 900, 1),
        (1536, 864, 1536, 864, 1.25),
        (1600, 900, 1600, 900, 1),
        (1920, 1080, 1920, 1080, 1),
    ])
    major = random.randint(134, 146)
    build = random.randint(6000, 9999)
    patch = random.randint(50, 220)
    user_agent = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.{build}.{patch} Safari/537.36"
    return DeviceFingerprint(
        user_agent=user_agent,
        locale=profile["locale"],
        languages=list(profile["languages"]),
        timezone=profile["timezone"],
        viewport_width=viewport[0],
        viewport_height=viewport[1],
        screen_width=viewport[2],
        screen_height=viewport[3],
        outer_width=viewport[0] + random.randint(8, 16),
        outer_height=viewport[1] + random.randint(72, 96),
        device_scale_factor=viewport[4],
        hardware_concurrency=random.choice([4, 6, 8, 8, 12, 16]),
        device_memory=random.choice([4, 8, 8, 16]),
        platform="Win32",
    )


def generate_register_fingerprint() -> DeviceFingerprint:
    return generate_fingerprint(REGISTER_DEVICE_PROFILES)


def generate_team_fingerprint() -> DeviceFingerprint:
    return generate_fingerprint(TEAM_DEVICE_PROFILES)


def generate_payment_fingerprint() -> DeviceFingerprint:
    return generate_fingerprint(PAYMENT_DEVICE_PROFILES)


def country_code_upper(country: str) -> str:
    return str(country or "").strip().upper()


def country_browser_locale(country: str) -> str:
    """出口/账单国家 → 浏览器 locale（BCP47）。"""
    code = country_code_upper(country)
    return COUNTRY_BROWSER_LOCALE.get(code, "en-US")


def country_timezone(country: str, detected: str = "") -> str:
    """优先用检测源时区；缺失时按国家主时区回填，避免 UTC 与 IP 错位。"""
    detected_text = str(detected or "").strip()
    if detected_text:
        return detected_text
    code = country_code_upper(country)
    return COUNTRY_TIMEZONE.get(code, "UTC")


def country_payment_locale(country: str) -> str:
    """Stripe elements/payment_locale：短码或 pt-BR 等特殊值。"""
    browser_locale, elements_locale = locale_parts(country_browser_locale(country))
    return elements_locale or browser_locale or "en"


def country_request_locale(country: str) -> str:
    """ChatGPT oai-language / Accept-Language 主 locale。"""
    return country_browser_locale(country)


def locale_language_list(locale: str) -> list[str]:
    text = str(locale or "en-US").strip() or "en-US"
    if "-" not in text:
        return [text]
    primary = text.split("-", 1)[0]
    if primary.lower() == text.lower():
        return [text]
    return [text, primary]


def opll_accept_language_for_locale(request_locale: str) -> tuple[str, str]:
    """返回 (Accept-Language, oai-language)，按请求 locale 与 IP/国家对齐。"""
    browser_locale, _elements = locale_parts(request_locale)
    normalized = str(browser_locale or request_locale or "en-US").strip()
    lower = normalized.lower()
    if lower in {"pt-br", "pt_br"}:
        return "pt-BR,pt;q=0.9,en;q=0.8", "pt-BR"
    if lower.startswith("de"):
        return "de-DE,de;q=0.9,en;q=0.8", "de-DE"
    if lower in {"en-in", "en_in"}:
        return "en-IN,en;q=0.9", "en-IN"
    if lower.startswith("ja"):
        return "ja-JP,ja;q=0.9,en;q=0.8", "ja-JP"
    if lower.startswith("fr"):
        return "fr-FR,fr;q=0.9,en;q=0.8", "fr-FR"
    if lower.startswith("es"):
        tag = "es-MX" if "mx" in lower else "es-ES"
        return f"{tag},es;q=0.9,en;q=0.8", tag
    if lower.startswith("id"):
        return "id-ID,id;q=0.9,en;q=0.8", "id-ID"
    if lower.startswith("it"):
        return "it-IT,it;q=0.9,en;q=0.8", "it-IT"
    if lower.startswith("nl"):
        return "nl-NL,nl;q=0.9,en;q=0.8", "nl-NL"
    if lower.startswith("ko"):
        return "ko-KR,ko;q=0.9,en;q=0.8", "ko-KR"
    if lower in {"zh-tw", "zh_tw"}:
        return "zh-TW,zh;q=0.9,en;q=0.8", "zh-TW"
    if lower.startswith("zh"):
        return "zh-CN,zh;q=0.9,en;q=0.8", "zh-CN"
    if lower.startswith("th"):
        return "th-TH,th;q=0.9,en;q=0.8", "th-TH"
    if lower.startswith("vi"):
        return "vi-VN,vi;q=0.9,en;q=0.8", "vi-VN"
    if lower in {"en-gb", "en_gb"}:
        return "en-GB,en;q=0.9", "en-GB"
    if lower in {"en-au", "en_au"}:
        return "en-AU,en;q=0.9", "en-AU"
    if lower in {"en-ca", "en_ca"}:
        return "en-CA,en;q=0.9", "en-CA"
    if lower.startswith("en-"):
        return f"{normalized},en;q=0.9", normalized
    return "en-US,en;q=0.9", "en-US"


def opll_locale_context_for_country(country: str, detected_timezone: str = "") -> dict[str, str]:
    """账单/出口国家 → ChatGPT locale、Stripe locale、浏览器时区。"""
    code = country_code_upper(country) or "US"
    request_locale = country_request_locale(code)
    return {
        "country": code,
        "request_locale": request_locale,
        "payment_locale": country_payment_locale(code),
        "browser_timezone": country_timezone(code, detected_timezone),
        "browser_locale": country_browser_locale(code),
    }


def generate_fingerprint_for_exit(exit_info: ProxyExitInfo) -> DeviceFingerprint:
    if not exit_info.success or not exit_info.country:
        raise ValueError("代理出口信息不可用，无法生成匹配指纹")
    locale = country_browser_locale(exit_info.country)
    profile = {
        "locale": locale,
        "languages": locale_language_list(locale),
        "timezone": country_timezone(exit_info.country, exit_info.timezone),
    }
    return generate_fingerprint([profile])

def locale_parts(locale: str = "en") -> tuple[str, str]:
    key = str(locale or "").strip()
    if key in LOCALE_MAP:
        return LOCALE_MAP[key]
    lower = key.lower()
    for map_key, value in LOCALE_MAP.items():
        if map_key.lower() == lower:
            return value
    # 未知 BCP47：尽量保留主标签，避免全部掉回 en-US
    if "-" in key:
        primary = key.split("-", 1)[0].lower()
        if primary in LOCALE_MAP:
            return LOCALE_MAP[primary]
        return (key, primary)
    if lower in LOCALE_MAP:
        return LOCALE_MAP[lower]
    return LOCALE_MAP["en"]
