"""MVP decisions for the two-proxy PayPal extraction flow.

This is the package-local port of the PAY153 presenter used by the current
desktop payment-link project.  The presenter is region-aware so the same
checkout ownership contract can be used by both US/USD and GB/GBP.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol


AmountReader = Callable[[dict], tuple[str, str]]
MessageSink = Callable[[str], None]


class CheckoutTaxesSubmitter(Protocol):
    """Gateway used by the Presenter to submit one Checkout Taxes request."""

    def __call__(
        self,
        access_token: str,
        checkout: dict,
        billing: dict,
        proxy_url: str,
        **kwargs,
    ) -> dict:
        ...


@dataclass(frozen=True, slots=True)
class PayPalTwoProxyFlowModel:
    """Stable proxy ownership and promotion state for one checkout."""

    country: str
    currency: str
    checkout_proxy_url: str
    final_proxy_url: str
    target_amount: str
    checkout_amount: str = ""
    checkout_amount_source: str = "missing_payload"
    checkout_has_promotion: bool = False
    force_update: bool = True

    @property
    def update_required(self) -> bool:
        return self.force_update

    @property
    def update_proxy_url(self) -> str:
        return self.final_proxy_url

    @property
    def flow_label(self) -> str:
        return regional_flow_label(self.country)


class PayPalTwoProxyFlowView(Protocol):
    """View contract used by the presenter without depending on a UI toolkit."""

    def show_checkout_promotion(self, model: PayPalTwoProxyFlowModel) -> None:
        ...


class DiagnosticPayPalTwoProxyFlowView:
    """Diagnostic-log View for the checkout decision Presenter."""

    def __init__(self, sink: MessageSink | None = None):
        self._sink = sink

    def show_checkout_promotion(self, model: PayPalTwoProxyFlowModel) -> None:
        if not callable(self._sink):
            return
        if model.checkout_has_promotion:
            action = (
                f"已存在目标优惠，仍交给代理 2 {model.country} "
                "强制执行一次 Update 并继续后续流程"
            )
        else:
            action = (
                f"未确认目标优惠，交给代理 2 {model.country} "
                "执行 Update 并继续后续流程"
            )
        self._sink(
            f"[{model.flow_label}] 代理 1 Checkout 优惠检查："
            f"amount={model.checkout_amount or 'unknown'}，"
            f"source={model.checkout_amount_source}；{action}"
        )


class PayPalTwoProxyFlowPresenter:
    """Presenter that converts a Checkout payload into an immutable decision."""

    def __init__(self, amount_reader: AmountReader, view: PayPalTwoProxyFlowView):
        self._amount_reader = amount_reader
        self._view = view

    def inspect_checkout(
        self,
        *,
        country: str,
        currency: str,
        checkout_proxy_url: str,
        final_proxy_url: str,
        target_amount: str,
        payload: dict,
    ) -> PayPalTwoProxyFlowModel:
        normalized_country = str(country or "").strip().upper()
        normalized_currency = str(currency or "").strip().upper()
        if not normalized_country or not normalized_currency:
            raise RuntimeError("PayPal 双代理流程缺少国家或币种")
        amount, source = self._amount_reader(
            payload if isinstance(payload, dict) else {}
        )
        normalized_target = str(target_amount or "").strip()
        normalized_amount = str(amount or "").strip()
        model = PayPalTwoProxyFlowModel(
            country=normalized_country,
            currency=normalized_currency,
            checkout_proxy_url=str(checkout_proxy_url or "").strip(),
            final_proxy_url=str(final_proxy_url or "").strip(),
            target_amount=normalized_target,
            checkout_amount=normalized_amount,
            checkout_amount_source=str(source or "missing_payload").strip(),
            checkout_has_promotion=bool(
                normalized_target
                and normalized_amount
                and normalized_amount == normalized_target
            ),
        )
        self._view.show_checkout_promotion(model)
        return model


@dataclass(frozen=True, slots=True)
class PayPalCheckoutTaxesModel:
    """Non-sensitive state recorded for the mandatory Checkout Taxes step."""

    checkout_id: str
    billing_country: str
    currency: str
    proxy_url: str
    applied: bool = False
    response_keys: tuple[str, ...] = ()

    @property
    def flow_label(self) -> str:
        return regional_flow_label(self.billing_country)


class PayPalCheckoutTaxesView(Protocol):
    """View contract for Checkout Taxes progress without backend coupling."""

    def show_checkout_taxes_started(self, model: PayPalCheckoutTaxesModel) -> None:
        ...

    def show_checkout_taxes_applied(self, model: PayPalCheckoutTaxesModel) -> None:
        ...


class DiagnosticPayPalCheckoutTaxesView:
    """Diagnostic-log View for the Checkout Taxes Presenter."""

    def __init__(self, sink: MessageSink | None = None):
        self._sink = sink

    def show_checkout_taxes_started(self, model: PayPalCheckoutTaxesModel) -> None:
        if callable(self._sink):
            self._sink(
                f"[{model.flow_label}] 正在提交 Checkout Taxes："
                f"billing_country={model.billing_country}，"
                f"currency={model.currency}"
            )

    def show_checkout_taxes_applied(self, model: PayPalCheckoutTaxesModel) -> None:
        if callable(self._sink):
            self._sink(
                f"[{model.flow_label}] Checkout Taxes 已应用："
                f"billing_country={model.billing_country}，"
                f"currency={model.currency}"
            )


class PayPalCheckoutTaxesPresenter:
    """Validate and submit the mandatory pool-2 Checkout Taxes step."""

    def __init__(
        self,
        submitter: CheckoutTaxesSubmitter,
        view: PayPalCheckoutTaxesView,
    ):
        self._submitter = submitter
        self._view = view

    def submit_checkout_taxes(
        self,
        *,
        access_token: str,
        checkout: dict,
        billing: dict,
        proxy_url: str,
        country: str,
        currency: str,
        diagnostic_log=None,
        session_context: dict | None = None,
        chatgpt_session=None,
    ) -> PayPalCheckoutTaxesModel:
        expected_country = str(country or "").strip().upper()
        expected_currency = str(currency or "").strip().upper()
        checkout_id = str(checkout.get("cs_id") or "").strip()
        checkout_country = str(
            checkout.get("billing_country") or ""
        ).strip().upper()
        billing_country = str(billing.get("country") or "").strip().upper()
        checkout_currency = str(checkout.get("currency") or "").strip().upper()
        normalized_proxy = str(proxy_url or "").strip()
        flow_label = regional_flow_label(expected_country)
        if not checkout_id:
            raise RuntimeError(f"{flow_label} Checkout Taxes 缺少 Checkout ID")
        if (
            checkout_country != expected_country
            or billing_country != expected_country
        ):
            raise RuntimeError(
                f"{flow_label} Checkout Taxes 地区必须统一为 {expected_country}: "
                f"checkout={checkout_country or 'missing'}, "
                f"billing={billing_country or 'missing'}"
            )
        if checkout_currency != expected_currency:
            raise RuntimeError(
                f"{flow_label} Checkout Taxes 币种必须为 {expected_currency}: "
                f"currency={checkout_currency or 'missing'}"
            )
        if not normalized_proxy:
            raise RuntimeError(f"{flow_label} Checkout Taxes 缺少代理池 2")

        pending = PayPalCheckoutTaxesModel(
            checkout_id=checkout_id,
            billing_country=billing_country,
            currency=checkout_currency,
            proxy_url=normalized_proxy,
        )
        self._view.show_checkout_taxes_started(pending)
        submit_kwargs = {
            "request_locale": country_request_locale(expected_country),
            "device_id": str(checkout.get("oai_device_id") or "").strip(),
            "diagnostic_log": diagnostic_log,
            "require_success": True,
            "flow_label": flow_label,
        }
        if session_context:
            submit_kwargs["session_context"] = session_context
        if chatgpt_session is not None:
            submit_kwargs["chatgpt_session"] = chatgpt_session
        payload = self._submitter(
            access_token,
            checkout,
            billing,
            normalized_proxy,
            **submit_kwargs,
        )
        if not isinstance(payload, dict):
            raise RuntimeError(f"{flow_label} Checkout Taxes 响应格式无效")
        applied = PayPalCheckoutTaxesModel(
            checkout_id=checkout_id,
            billing_country=billing_country,
            currency=checkout_currency,
            proxy_url=normalized_proxy,
            applied=True,
            response_keys=tuple(sorted(str(key) for key in payload)),
        )
        self._view.show_checkout_taxes_applied(applied)
        return applied


def country_request_locale(country: str) -> str:
    """Return the request locale used by the two supported PAY153 regions."""

    return "en-GB" if str(country or "").strip().upper() == "GB" else "en-US"


def regional_flow_label(country: str) -> str:
    """Return the source-project label for one regional protocol profile."""

    return (
        "PayPal GB/GBP"
        if str(country or "").strip().upper() == "GB"
        else "PayPal US/PAY153"
    )
