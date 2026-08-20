from hidemyemail_generator.paypal_payment_protocol import (
    PAYPAL_PAYMENT_PROTOCOL_CURRENT,
    PAYPAL_PAYMENT_PROTOCOL_PAY153_LEGACY_US,
    PayPalPaymentProtocolPresenter,
)


def test_presenter_selects_legacy_pay153_only_for_us_link_method():
    presenter = PayPalPaymentProtocolPresenter()

    us = presenter.present("paypal_us")
    gb = presenter.present("paypal_gb")

    assert us.payment_protocol == PAYPAL_PAYMENT_PROTOCOL_PAY153_LEGACY_US
    assert us.buyer_mode == "identity_elevation"
    assert gb.payment_protocol == PAYPAL_PAYMENT_PROTOCOL_CURRENT
    assert gb.buyer_mode == "identity_elevation"
