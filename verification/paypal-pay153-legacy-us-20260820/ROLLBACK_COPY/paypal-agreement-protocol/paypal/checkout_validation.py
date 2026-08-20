"""Validation helpers for OpenAI custom Checkout references.

The PayPal agreement flow normally receives only a ``BA-`` token.  Callers
that originate from the DE/EUR OAICS link flow can additionally provide the
source Checkout reference so the agreement job refuses a hosted ``cs_``
Checkout before consuming a proxy or starting the PayPal protocol.
"""

from __future__ import annotations

import re


CHECKOUT_ID_RE = re.compile(
    r"(?<![A-Za-z0-9_-])((?:oaics|cs)_[A-Za-z0-9_-]{4,})(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)


class OaicsCheckoutValidationError(ValueError):
    """The supplied source Checkout is missing or is not an OAICS Checkout."""


def extract_checkout_id(value: str) -> str:
    """Return an ``oaics_``/``cs_`` identifier from an ID, URL, or log line."""

    match = CHECKOUT_ID_RE.search(str(value or "").strip())
    return match.group(1) if match else ""


def validate_oaics_checkout(value: str) -> str:
    """Require the source custom Checkout to use the ``oaics_`` prefix."""

    checkout_id = extract_checkout_id(value)
    if not checkout_id:
        raise OaicsCheckoutValidationError(
            "DE/EUR OAICS 验证需要填写 Checkout ID 或 ChatGPT Checkout URL"
        )
    if not checkout_id.casefold().startswith("oaics_"):
        raise OaicsCheckoutValidationError(
            "PayPal DE/EUR OAICS 模式要求 custom Checkout 返回 oaics_；"
            f"当前为 {checkout_id[:24]}"
        )
    return checkout_id


__all__ = [
    "OaicsCheckoutValidationError",
    "extract_checkout_id",
    "validate_oaics_checkout",
]
