from pathlib import Path
from unittest.mock import patch

import pytest

import web
from paypal.checkout_validation import (
    OaicsCheckoutValidationError,
    extract_checkout_id,
    validate_oaics_checkout,
)


VALID_BA = "BA-ABCDEFGH123456"
VALID_PROXY = ["http://127.0.0.1:9999"]
ROOT = Path(__file__).resolve().parents[1]


def test_extracts_oaics_checkout_from_chatgpt_url():
    assert extract_checkout_id(
        "https://chatgpt.com/checkout/openai_ie/oaics_test_checkout"
    ) == "oaics_test_checkout"


def test_rejects_hosted_cs_checkout_with_actionable_message():
    with pytest.raises(OaicsCheckoutValidationError) as raised:
        validate_oaics_checkout("cs_live_a1391L4WxW2P6UIt")

    assert "要求 custom Checkout 返回 oaics_" in str(raised.value)
    assert "当前为 cs_live_" in str(raised.value)


def test_create_job_requires_reference_when_oaics_validation_enabled():
    with pytest.raises(OaicsCheckoutValidationError, match="需要填写 Checkout"):
        web.create_job(
            owner_device_id="fixture-device",
            ba_token=VALID_BA,
            phone="+5511980133818",
            debug=False,
            max_card_attempts=1,
            require_oaics=True,
            proxy_pool=VALID_PROXY,
        )


def test_validated_job_records_only_oaics_verification_not_checkout_id():
    with patch("threading.Thread.start", return_value=None):
        job = web.create_job(
            owner_device_id="fixture-device",
            ba_token=VALID_BA,
            phone="+5511980133818",
            debug=False,
            max_card_attempts=1,
            require_oaics=True,
            checkout_reference=(
                "https://chatgpt.com/checkout/openai_ie/oaics_secret_checkout"
            ),
            proxy_pool=VALID_PROXY,
        )
    try:
        payload = job.to_dict(include_logs=False)
        assert payload["oaics_validation"] == {
            "required": True,
            "verified": True,
            "checkout_type": "oaics_",
        }
        assert "oaics_secret_checkout" not in str(payload)
    finally:
        with web.JOBS_LOCK:
            web.JOBS.pop(job.id, None)


def test_legacy_ba_job_remains_compatible_without_oaics_reference():
    with patch("threading.Thread.start", return_value=None):
        job = web.create_job(
            owner_device_id="legacy-device",
            ba_token=VALID_BA,
            phone="+5511980133818",
            debug=False,
            max_card_attempts=1,
            proxy_pool=VALID_PROXY,
        )
    try:
        validation = job.to_dict(include_logs=False)["oaics_validation"]
        assert validation["required"] is False
        assert validation["verified"] is False
    finally:
        with web.JOBS_LOCK:
            web.JOBS.pop(job.id, None)


def test_web_form_wires_oaics_reference_into_job_request():
    html = (ROOT / "web_static" / "index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "web_static" / "app.js").read_text(encoding="utf-8")

    assert 'id="requireOaics"' in html
    assert 'id="oaicsCheckout"' in html
    assert "require_oaics: requireOaics" in javascript
    assert "checkout_reference: requireOaics ? checkoutReference : ''" in javascript
