"""支付模式目录与旧配置迁移规则。

该模块只描述“有哪些模式”及其静态元数据，不包含网络请求或界面状态。
支付流程和 UI 共同依赖这里，避免两边各自维护一份模式判断。
"""

from __future__ import annotations


PAYPAL_STRICT_ZERO_MODE = "PayPal严格0链接 DE/EUR"
PAYPAL_US_TR_MODE = "PayPal推断模式 US/TR"
PAYPAL_US_TR_FLOW = "us_tr_promotion"
PAYPAL_PAY153_PROTOCOL_FLOW = "pay153_protocol"
PAYPAL_GB_TWO_PROXY_FLOW = "gb_two_proxy_promotion"
PHILIPPINES_SHORT_LINK_MODE = "菲律宾短链"
PHILIPPINES_PAYPAL_CHECK_MODE = "菲律宾 PayPal 支付链接"
PHILIPPINES_CUSTOM_PROMO_FLOW = "ph_custom_promotion"  # 旧结果兼容
PHILIPPINES_GPT_LINK_FLOW = "pay153_ph_short"
PHILIPPINES_PAYPAL_CHECK_FLOW = "ph_paypal_availability_check"
PAYPAL_JP_STRICT_ZERO_MODE = "PayPal支付链接 JP/JPY"
PAYPAL_EUR_JP_STYLE_ZERO_MODE = "PayPal EUR提取 DE/EUR（JP逻辑）"
PAYPAL_NATIVE_PROMO_MODE = "PayPal原生优惠 DE/EUR"

PAYPAL_BILLING_COUNTRY_IP = "跟随 IP 地址"
PAYPAL_BILLING_COUNTRY_CURRENCY = "跟随货币国家"
PAYPAL_BILLING_COUNTRY_CHOICES = (
    PAYPAL_BILLING_COUNTRY_IP,
    PAYPAL_BILLING_COUNTRY_CURRENCY,
)

PAYPAL_BR_DE_STRICT_ZERO_FLOW = "br_de_strict_zero"
PAYPAL_JP_STRICT_ZERO_FLOW = "jp_strict_zero"
PAYPAL_EUR_JP_STYLE_ZERO_FLOW = "de_eur_jp_style_zero"
# 内部值保留旧名称以兼容历史结果；当前实现已改为 PIX 风格流水线。
PAYPAL_FR_IDEAL_STYLE_ZERO_FLOW = "fr_ideal_style_zero"
PAYPAL_DE_NATIVE_PROMO_FLOW = "de_native_promotion"
PAYPAL_DE_OAICS_FLOW = "de_oaics_promotion"

UPI_STRICT_ZERO_MODE = "UPI严格0链接 IN/INR"
IDEAL_TEMPORARY_MODE = "iDEAL 临时授权链接 NL/EUR"
KAKAO_STRICT_ZERO_MODE = "Kakao Pay 严格0链接 KR/KRW"
MOMO_VN_MODE = "MoMo 支付页 VN/VND"
GCASH_PH_MODE = "GCash 支付链接 PH/PHP"

# 仅用于迁移旧设置；不再作为可见支付模式。
UPI_ACTIVITY_STRICT_ZERO_MODE = "UPI活动严格0链接 IN/INR"


PAYMENT_MODES = {
    "PayPal支付链接 US/USD": {
        "country": "US",
        "currency": "USD",
        # 目标为 0 时使用两条 US 粘性代理的 PAY153 流程。
        "zero_target_paypal_flow": PAYPAL_PAY153_PROTOCOL_FLOW,
    },
    PAYPAL_US_TR_MODE: {
        "country": "US",
        "currency": "USD",
        "paypal_flow": PAYPAL_US_TR_FLOW,
    },
    PHILIPPINES_SHORT_LINK_MODE: {
        "country": "PH",
        "currency": "PHP",
        "payment_provider": "chatgpt_checkout",
        "target_amount": "0",
        "paypal_flow": PHILIPPINES_GPT_LINK_FLOW,
    },
    PHILIPPINES_PAYPAL_CHECK_MODE: {
        "country": "PH",
        "currency": "PHP",
        "payment_provider": "chatgpt_checkout",
        "target_amount": "0",
        "paypal_flow": PHILIPPINES_PAYPAL_CHECK_FLOW,
        "paypal_availability_check": True,
    },
    PAYPAL_STRICT_ZERO_MODE: {
        "country": "DE",
        "currency": "EUR",
        "target_amount": "0",
        "paypal_flow": PAYPAL_BR_DE_STRICT_ZERO_FLOW,
    },
    PAYPAL_EUR_JP_STYLE_ZERO_MODE: {
        "country": "DE",
        "currency": "EUR",
        "target_amount": "0",
        "paypal_flow": PAYPAL_EUR_JP_STYLE_ZERO_FLOW,
    },
    PAYPAL_NATIVE_PROMO_MODE: {
        "country": "DE",
        "currency": "EUR",
        "paypal_flow": PAYPAL_DE_NATIVE_PROMO_FLOW,
    },
    "PayPal支付链接 DE/EUR": {
        "country": "DE",
        "currency": "EUR",
        "target_amount": "0",
        "paypal_flow": PAYPAL_DE_OAICS_FLOW,
    },
    "PayPal支付链接 FR/EUR": {
        "country": "FR",
        "currency": "EUR",
        "target_amount": "0",
        "paypal_flow": PAYPAL_FR_IDEAL_STYLE_ZERO_FLOW,
    },
    "PayPal支付链接 GB/GBP": {
        "country": "GB",
        "currency": "GBP",
        # 英区零元流程与美区同拓扑，但保留独立协议标识。
        "zero_target_paypal_flow": PAYPAL_GB_TWO_PROXY_FLOW,
    },
    "PayPal支付链接 CA/CAD": {"country": "CA", "currency": "CAD"},
    "PayPal支付链接 AU/AUD": {"country": "AU", "currency": "AUD"},
    PAYPAL_JP_STRICT_ZERO_MODE: {
        "country": "JP",
        "currency": "JPY",
        "target_amount": "0",
        "paypal_flow": PAYPAL_JP_STRICT_ZERO_FLOW,
    },
    "GoPay 长链接 ID/IDR": {"country": "ID", "currency": "IDR", "payment_provider": "gopay"},
    IDEAL_TEMPORARY_MODE: {
        "country": "NL",
        "currency": "EUR",
        "payment_provider": "ideal",
        "target_amount": "0",
    },
    KAKAO_STRICT_ZERO_MODE: {
        "country": "KR",
        "currency": "KRW",
        "payment_provider": "kakao",
        "target_amount": "0",
    },
    MOMO_VN_MODE: {
        "country": "VN",
        "currency": "VND",
        "payment_provider": "momo",
        "momo_direct": True,
        "target_amount": "0",
    },
    GCASH_PH_MODE: {
        "country": "PH",
        "currency": "PHP",
        "payment_provider": "gcash",
        "target_amount": "0",
    },
    "PIX 长链接 BR/BRL": {"country": "BR", "currency": "BRL", "payment_provider": "pix"},
    UPI_STRICT_ZERO_MODE: {
        "country": "IN",
        "currency": "INR",
        "payment_provider": "upi",
        "target_amount": "0",
    },
    "试用短链 PayPal US/USD": {"country": "US", "currency": "USD", "trial_short_link": True},
    "Apple Pay 支付页 US/USD": {"country": "US", "currency": "USD", "apple_pay_hosted": True},
    "Apple Pay 支付页 JP/JPY": {"country": "JP", "currency": "JPY", "apple_pay_hosted": True},
}


PAYMENT_MODE_MENU_GROUPS = (
    (
        "PayPal · 常规支付",
        (
            ("PayPal支付链接 US/USD", "美国  ·  USD"),
            ("PayPal支付链接 DE/EUR", "德国  ·  EUR"),
            ("PayPal支付链接 GB/GBP", "英国  ·  GBP"),
            ("PayPal支付链接 CA/CAD", "加拿大  ·  CAD"),
            ("PayPal支付链接 AU/AUD", "澳大利亚  ·  AUD"),
            (PAYPAL_JP_STRICT_ZERO_MODE, "日本  ·  JPY  ·  严格 0"),
        ),
    ),
    (
        "PayPal · 专用流程",
        (
            (PAYPAL_US_TR_MODE, "推断模式  ·  US / TR"),
            (
                PHILIPPINES_SHORT_LINK_MODE,
                "PAY153 直卡  ·  PH / PHP custom  ·  US 创建 / TR 优惠",
            ),
            (
                PHILIPPINES_PAYPAL_CHECK_MODE,
                "菲律宾 PH  ·  PHP oaics_ 严格零元 / 第一代理提链 · 第二代理优惠",
            ),
            (PAYPAL_STRICT_ZERO_MODE, "严格 0  ·  DE / EUR"),
            (PAYPAL_EUR_JP_STYLE_ZERO_MODE, "EUR 提取  ·  JP 逻辑"),
            (PAYPAL_NATIVE_PROMO_MODE, "原生优惠  ·  DE / EUR"),
            ("PayPal支付链接 FR/EUR", "PIX 风格  ·  FR / EUR  ·  严格 0"),
        ),
    ),
    (
        "本地支付",
        (
            ("GoPay 长链接 ID/IDR", "GoPay  ·  印尼 ID  ·  IDR"),
            (IDEAL_TEMPORARY_MODE, "iDEAL  ·  荷兰 NL  ·  EUR  ·  临时授权"),
            (KAKAO_STRICT_ZERO_MODE, "Kakao Pay  ·  韩国 KR  ·  KRW  ·  严格 0"),
            (MOMO_VN_MODE, "MoMo  ·  越南 VN  ·  VND  ·  最终支付链接"),
            (GCASH_PH_MODE, "GCash  ·  菲律宾 PH  ·  PHP  ·  第一代理提链 / 第二代理优惠"),
            ("PIX 长链接 BR/BRL", "PIX  ·  巴西 BR  ·  BRL"),
            (UPI_STRICT_ZERO_MODE, "UPI  ·  印度 IN  ·  INR  ·  严格 0"),
        ),
    ),
    (
        "快捷与钱包",
        (
            ("试用短链 PayPal US/USD", "PayPal 试用短链  ·  US / USD"),
            ("Apple Pay 支付页 US/USD", "Apple Pay  ·  美国 US  ·  USD"),
            ("Apple Pay 支付页 JP/JPY", "Apple Pay  ·  日本 JP  ·  JPY"),
        ),
    ),
)


PAYMENT_MODE_DISPLAY_LABELS = {
    mode: f"{group.split('·', 1)[0].strip()}  /  {label}"
    for group, items in PAYMENT_MODE_MENU_GROUPS
    for mode, label in items
}


PAYMENT_MODE_ALIASES = {
    "无卡长链接 US/USD": "PayPal支付链接 US/USD",
    "PayPal严格0链接 FR/EUR": PAYPAL_STRICT_ZERO_MODE,
    "PayPal支付链接 BR/BRL": PAYPAL_STRICT_ZERO_MODE,
    "无卡长链接 BR/BRL": PAYPAL_STRICT_ZERO_MODE,
    "无卡长链接 DE/EUR": "PayPal支付链接 DE/EUR",
    "无卡长链接 FR/EUR": "PayPal支付链接 FR/EUR",
    "无卡长链接 GB/GBP": "PayPal支付链接 GB/GBP",
    "无卡长链接 CA/CAD": "PayPal支付链接 CA/CAD",
    "无卡长链接 AU/AUD": "PayPal支付链接 AU/AUD",
    "无卡长链接 JP/JPY": PAYPAL_JP_STRICT_ZERO_MODE,
    "PayPal 长链接 US/USD": "PayPal支付链接 US/USD",
    "PayPal 长链接 FR/EUR": "PayPal支付链接 FR/EUR",
    "PayPal 短链 US/USD": "PayPal支付链接 US/USD",
    "PayPal 短链 FR/EUR": "PayPal支付链接 FR/EUR",
    "UPI 长链接 IN/INR": UPI_STRICT_ZERO_MODE,
    UPI_ACTIVITY_STRICT_ZERO_MODE: UPI_STRICT_ZERO_MODE,
    "iDEAL 长链接 NL/EUR": IDEAL_TEMPORARY_MODE,
    "Kakao Pay 长链接 KR/KRW": KAKAO_STRICT_ZERO_MODE,
    "MoMo 长链接 VN/VND": MOMO_VN_MODE,
    "GCash 长链接 PH/PHP": GCASH_PH_MODE,
}


def normalize_payment_mode_name(payment_mode: str) -> str:
    """把历史保存值转换成当前支付模式名称。"""
    value = str(payment_mode or "").strip()
    return PAYMENT_MODE_ALIASES.get(value, value)


def normalize_paypal_billing_country_mode(value: str) -> str:
    """Normalize the persisted PayPal billing-country selector value."""
    normalized = str(value or "").strip()
    aliases = {
        "ip": PAYPAL_BILLING_COUNTRY_IP,
        "proxy": PAYPAL_BILLING_COUNTRY_IP,
        "跟随IP": PAYPAL_BILLING_COUNTRY_IP,
        "跟随 IP": PAYPAL_BILLING_COUNTRY_IP,
        "currency": PAYPAL_BILLING_COUNTRY_CURRENCY,
        "货币国家": PAYPAL_BILLING_COUNTRY_CURRENCY,
        "跟随货币国家": PAYPAL_BILLING_COUNTRY_CURRENCY,
        # 旧版本保存值迁移：原“固定 DE”实际表示采用支付模式国家。
        "de": PAYPAL_BILLING_COUNTRY_CURRENCY,
        "固定DE": PAYPAL_BILLING_COUNTRY_CURRENCY,
        "固定 DE": PAYPAL_BILLING_COUNTRY_CURRENCY,
        "固定 DE 地址": PAYPAL_BILLING_COUNTRY_CURRENCY,
    }
    normalized = aliases.get(normalized.lower(), aliases.get(normalized, normalized))
    return (
        normalized
        if normalized in PAYPAL_BILLING_COUNTRY_CHOICES
        else PAYPAL_BILLING_COUNTRY_CURRENCY
    )


def payment_mode_target_amount(mode: dict, configured_target: str) -> str:
    """模式固定金额优先，否则采用界面配置金额。"""
    if "target_amount" in mode:
        return str(mode.get("target_amount") or "")
    return str(configured_target or "").strip()


def payment_mode_paypal_flow(mode: dict, target_amount: str) -> str:
    """Resolve the PayPal pipeline after the effective target is known."""
    flow = str(mode.get("paypal_flow") or "").strip().lower()
    if not flow and str(target_amount or "").strip() == "0":
        flow = str(mode.get("zero_target_paypal_flow") or "").strip().lower()
    return flow
