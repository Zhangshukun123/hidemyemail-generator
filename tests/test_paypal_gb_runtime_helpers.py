import json
from urllib.parse import parse_qs, urlsplit

from hidemyemail_generator import card_link_runtime


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text
        self.headers = {}

    def json(self):
        return self._payload


class _RecordingSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.opll_oai_device_id = "device-gb-fixture"

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def _stripe_context():
    return {
        "guid": "guid-fixture",
        "muid": "muid-fixture",
        "sid": "sid-fixture",
        "stripe_js_id": "stripe-js-fixture",
        "elements_session_id": "elements-session-fixture",
        "elements_session_config_id": "elements-config-fixture",
        "config_id": "checkout-config-fixture",
        "stripe_version": card_link_runtime.STRIPE_VERSION_FULL,
    }


def test_gb_confirm_return_url_uses_exact_nested_checkout_contract():
    checkout_id = "cs_gb_return_fixture"
    hosted_url = (
        f"https://checkout.stripe.com/c/pay/{checkout_id}"
        "?ignored=old#fidkdWxOYHwnPyd1blpxYHZxWjA0"
    )
    checkout = {
        "billing_country": "GB",
        "processor_entity": "openai_llc",
    }

    result = card_link_runtime.opll_paypal_gb_confirm_return_url(
        checkout_id,
        checkout,
        hosted_url,
    )
    verified = card_link_runtime.opll_require_paypal_gb_confirm_return_url(
        result,
        checkout_id,
        hosted_url,
    )

    assert verified == result
    parsed = urlsplit(result)
    assert parsed.netloc == "checkout.stripe.com"
    assert parsed.path == f"/c/pay/{checkout_id}"
    assert parsed.fragment == "fidkdWxOYHwnPyd1blpxYHZxWjA0"
    query = parse_qs(parsed.query, keep_blank_values=True)
    assert set(query) == {"returned_from_redirect", "ui_mode", "return_url"}
    assert query["returned_from_redirect"] == ["true"]
    assert query["ui_mode"] == ["custom"]
    inner = urlsplit(query["return_url"][0])
    assert inner.netloc == "chatgpt.com"
    assert inner.path == "/checkout/verify"
    assert parse_qs(inner.query)["stripe_session_id"] == [checkout_id]


def test_gb_checkout_create_omits_ui_mode_and_uses_compact_json():
    checkout_id = "cs_gb_create_fixture"
    session = _RecordingSession(
        [
            _Response(
                payload={
                    "id": checkout_id,
                    "processor_entity": "openai_llc",
                    "stripe_publishable_key": "pk_test_fixture",
                }
            )
        ]
    )

    checkout = card_link_runtime.opll_create_checkout(
        "token",
        "GB",
        "GBP",
        "",
        request_locale="en-GB",
        include_trial_promo=False,
        checkout_ui_mode=None,
        referer_url="https://chatgpt.com/",
        chatgpt_session=session,
        compact_json=True,
    )

    assert checkout["cs_id"] == checkout_id
    _, kwargs = session.calls[0]
    assert "json" not in kwargs
    payload = json.loads(kwargs["data"])
    assert "checkout_ui_mode" not in payload
    assert "promo_campaign" not in payload
    assert payload["billing_details"] == {"country": "GB", "currency": "GBP"}
    assert kwargs["headers"]["Referer"] == "https://chatgpt.com/"


def test_gb_tax_region_posts_full_address_without_empty_state():
    page = {
        "currency": "gbp",
        "total_summary": {"due": 1917},
        "payment_method_types": ["card", "paypal"],
    }
    session = _RecordingSession(
        [_Response(payload={"payment_page": page})]
    )
    billing = {
        "country": "GB",
        "line1": "10 Downing Street",
        "city": "London",
        "state": "",
        "postal_code": "SW1A 2AA",
    }

    result = card_link_runtime.opll_stripe_update_tax_region(
        session,
        "cs_gb_tax_fixture",
        "pk_test_fixture",
        _stripe_context(),
        billing,
        omit_empty_address_fields=True,
    )

    assert result == page
    url, kwargs = session.calls[0]
    assert url.endswith("/v1/payment_pages/cs_gb_tax_fixture")
    body = kwargs["data"]
    assert body["tax_region[country]"] == "GB"
    assert body["tax_region[line1]"] == "10 Downing Street"
    assert body["tax_region[city]"] == "London"
    assert body["tax_region[postal_code]"] == "SW1A 2AA"
    assert "tax_region[state]" not in body


def test_gb_snapshot_uses_nested_billing_address_and_checkout_referer():
    session = _RecordingSession([_Response(status_code=204)])
    checkout = {
        "cs_id": "cs_gb_snapshot_fixture",
        "processor_entity": "openai_llc",
    }
    billing = {
        "name": "Ada Lovelace",
        "country": "GB",
        "line1": "12 St James's Square",
        "line2": "",
        "city": "London",
        "state": "",
        "postal_code": "SW1Y 4LB",
    }

    result = card_link_runtime.opll_paypal_gb_chatgpt_checkout_snapshot(
        session,
        checkout,
        billing,
    )

    assert result == {"status_code": 204, "applied": True}
    url, kwargs = session.calls[0]
    assert url.endswith("/backend-api/payments/checkout/snapshot")
    assert kwargs["headers"]["Referer"] == (
        "https://chatgpt.com/checkout/openai_llc/cs_gb_snapshot_fixture"
    )
    payload = json.loads(kwargs["data"])
    address = payload["snapshot"]["billing_address"]
    assert address["name"] == "Ada Lovelace"
    assert address["address"] == {
        "line1": "12 St James's Square",
        "city": "London",
        "line2": "",
        "postal_code": "SW1Y 4LB",
        "country": "GB",
    }


def test_paypal_payment_method_omits_empty_gb_state_but_keeps_us_fallback():
    for country, expected_state in (("GB", None), ("US", "CA")):
        session = _RecordingSession(
            [_Response(payload={"id": f"pm_{country.lower()}_fixture"})]
        )
        billing = {
            "name": "Test Member",
            "email": "member@example.com",
            "phone": "+442079460000",
            "country": country,
            "line1": "10 Test Street",
            "city": "London" if country == "GB" else "Los Angeles",
            "state": "",
            "postal_code": "SW1A 2AA" if country == "GB" else "90026",
        }

        card_link_runtime.opll_stripe_create_paypal_method(
            session,
            f"cs_{country.lower()}_fixture",
            _stripe_context(),
            billing,
            "pk_test_fixture",
        )

        body = session.calls[0][1]["data"]
        state_key = "billing_details[address][state]"
        if expected_state is None:
            assert state_key not in body
        else:
            assert body[state_key] == expected_state
