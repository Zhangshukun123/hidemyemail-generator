"""MVP state machine for the GB PayPal post-approval promotion protocol."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Protocol, TypeVar


PromotionResult = TypeVar("PromotionResult")
MessageSink = Callable[[str], None]
STRIPE_CONTEXT_IDENTITY_FIELDS = (
    "guid",
    "muid",
    "sid",
    "stripe_js_id",
    "elements_session_id",
)


class PayPalGbPostApprovePhase(str, Enum):
    """Allowed phases for one immutable Checkout identity."""

    STANDARD_CHECKOUT = "standard_checkout"
    STRIPE_READY = "stripe_ready"
    SENTINEL_READY = "sentinel_ready"
    CONFIRMED = "confirmed"
    BA_APPROVED = "ba_approved"
    PROMOTED = "promoted"
    VERIFIED = "verified"


@dataclass(slots=True)
class PayPalGbPostApproveModel:
    """Bound identities and observable state for one GB extraction."""

    checkout_id: str
    stripe_checkout_id: str
    device_id: str
    primary_proxy_url: str
    promotion_proxy_url: str
    phase: PayPalGbPostApprovePhase = PayPalGbPostApprovePhase.STANDARD_CHECKOUT
    pre_promotion_amount: str = ""
    post_approval_amount: str = ""
    paypal_ba_approve_url: str = ""
    post_promotion_submission_state: str = ""
    paypal_method_retained: bool = False
    promotion_update_count: int = 0
    browser_http_used: bool = False
    _primary_chatgpt_session: object | None = field(default=None, repr=False)
    _primary_stripe_session: object | None = field(default=None, repr=False)
    _promotion_chatgpt_session: object | None = field(default=None, repr=False)
    _promotion_stripe_session: object | None = field(default=None, repr=False)
    _stripe_context: dict | None = field(default=None, repr=False)
    _stripe_context_identity: tuple[tuple[str, str], ...] = field(
        default_factory=tuple,
        repr=False,
    )

    @property
    def result_metadata(self) -> dict[str, object]:
        return {
            "promotion_timing": "post_approve",
            "promotion_checkout_id": self.checkout_id,
            "approval_completed_before_promotion": (
                self.phase
                in {
                    PayPalGbPostApprovePhase.PROMOTED,
                    PayPalGbPostApprovePhase.VERIFIED,
                }
            ),
            "same_checkout_promotion": self.promotion_update_count == 1,
            "checkout_identity_preserved": self.promotion_update_count == 1,
            "session_proxy_consistent": self.phase is PayPalGbPostApprovePhase.VERIFIED,
            "stripe_context_consistent": self.phase is PayPalGbPostApprovePhase.VERIFIED,
            "browser_http_used": self.browser_http_used,
            "pre_promotion_amount": self.pre_promotion_amount,
            "post_approval_amount": self.post_approval_amount,
            "paypal_ba_status": (
                "approved" if self.paypal_ba_approve_url else ""
            ),
            "approval_state": self.post_promotion_submission_state,
            "paypal_ba_state": (
                "approved" if self.paypal_ba_approve_url else ""
            ),
            "ba_preserved_after_promotion": (
                self.phase is PayPalGbPostApprovePhase.VERIFIED
                and self.post_promotion_submission_state == "approved"
            ),
            "paypal_method_retained": self.paypal_method_retained,
        }


class PayPalGbPostApproveView(Protocol):
    """View contract kept independent of the protocol gateways."""

    def show_phase(self, model: PayPalGbPostApproveModel) -> None:
        ...


class DiagnosticPayPalGbPostApproveView:
    """Diagnostic View used by the headless runtime."""

    def __init__(self, sink: MessageSink | None = None):
        self._sink = sink

    def show_phase(self, model: PayPalGbPostApproveModel) -> None:
        if callable(self._sink):
            self._sink(
                "[PayPal GB/GBP] 后置优惠阶段："
                f"{model.phase.value}，checkout={model.checkout_id[:20]}..."
            )


class PayPalGbPostApprovePresenter:
    """Presenter enforcing order and identity/session/proxy continuity."""

    def __init__(
        self,
        model: PayPalGbPostApproveModel,
        view: PayPalGbPostApproveView,
    ):
        if not model.checkout_id or not model.stripe_checkout_id:
            raise RuntimeError("PayPal GB 后置优惠缺少 Checkout ID")
        if model.checkout_id != model.stripe_checkout_id:
            raise RuntimeError("PayPal GB 标准 Checkout 必须直接使用同一个 Stripe cs_ ID")
        if not model.device_id:
            raise RuntimeError("PayPal GB 后置优惠缺少固定设备 ID")
        if (
            not model.primary_proxy_url
            or not model.promotion_proxy_url
            or model.primary_proxy_url == model.promotion_proxy_url
        ):
            raise RuntimeError("PayPal GB 后置优惠需要两条不同的固定代理")
        self.model = model
        self._view = view
        self._view.show_phase(self.model)

    def _require(self, expected: PayPalGbPostApprovePhase) -> None:
        if self.model.phase is not expected:
            raise RuntimeError(
                "PayPal GB 后置优惠阶段错误："
                f"expected={expected.value}, actual={self.model.phase.value}"
            )

    def _transition(self, phase: PayPalGbPostApprovePhase) -> None:
        self.model.phase = phase
        self._view.show_phase(self.model)

    @staticmethod
    def _context_identity(stripe_context: dict) -> tuple[tuple[str, str], ...]:
        if not isinstance(stripe_context, dict):
            raise RuntimeError("PayPal GB Stripe Context 无效")
        identity: list[tuple[str, str]] = []
        for field_name in STRIPE_CONTEXT_IDENTITY_FIELDS:
            value = str(stripe_context.get(field_name) or "").strip()
            if not value:
                raise RuntimeError(
                    "PayPal GB Stripe Context 缺少稳定身份字段："
                    f"{field_name}"
                )
            identity.append((field_name, value))
        return tuple(identity)

    def _assert_stripe_context(self, stripe_context: dict) -> None:
        if stripe_context is not self.model._stripe_context:
            raise RuntimeError("PayPal GB Stripe Context 发生漂移")
        if self._context_identity(stripe_context) != (
            self.model._stripe_context_identity
        ):
            raise RuntimeError("PayPal GB Stripe Context 稳定身份字段发生漂移")

    def bind_stripe(
        self,
        *,
        primary_chatgpt_session: object,
        stripe_session: object,
        stripe_context: dict,
        amount: str,
        currency: str,
        browser_http_used: bool,
    ) -> None:
        self._require(PayPalGbPostApprovePhase.STANDARD_CHECKOUT)
        normalized_amount = str(amount or "").strip()
        if not normalized_amount.isdigit() or int(normalized_amount) <= 0:
            raise RuntimeError("PayPal GB 标准 Checkout 必须保留非零 GBP 金额")
        if str(currency or "").strip().upper() != "GBP":
            raise RuntimeError("PayPal GB 标准 Checkout 的 Stripe 币种必须为 GBP")
        stripe_context_identity = self._context_identity(stripe_context)
        self.model.pre_promotion_amount = normalized_amount
        self.model.browser_http_used = bool(browser_http_used)
        self.model._primary_chatgpt_session = primary_chatgpt_session
        self.model._primary_stripe_session = stripe_session
        self.model._stripe_context = stripe_context
        self.model._stripe_context_identity = stripe_context_identity
        self._transition(PayPalGbPostApprovePhase.STRIPE_READY)

    def _assert_primary_bindings(
        self,
        *,
        chatgpt_session: object,
        stripe_session: object,
        stripe_context: dict,
    ) -> None:
        if chatgpt_session is not self.model._primary_chatgpt_session:
            raise RuntimeError("PayPal GB 主链 ChatGPT Session 发生漂移")
        if stripe_session is not self.model._primary_stripe_session:
            raise RuntimeError("PayPal GB 池 1 Stripe Session 发生漂移")
        self._assert_stripe_context(stripe_context)
        session_device_id = str(
            getattr(chatgpt_session, "opll_oai_device_id", "") or ""
        ).strip()
        if session_device_id and session_device_id != self.model.device_id:
            raise RuntimeError("PayPal GB 主链设备 ID 发生漂移")

    def mark_sentinel_ready(
        self,
        *,
        chatgpt_session: object,
        stripe_session: object,
        stripe_context: dict,
    ) -> None:
        self._require(PayPalGbPostApprovePhase.STRIPE_READY)
        self._assert_primary_bindings(
            chatgpt_session=chatgpt_session,
            stripe_session=stripe_session,
            stripe_context=stripe_context,
        )
        self._transition(PayPalGbPostApprovePhase.SENTINEL_READY)

    def mark_confirmed(
        self,
        *,
        chatgpt_session: object,
        stripe_session: object,
        stripe_context: dict,
    ) -> None:
        self._require(PayPalGbPostApprovePhase.SENTINEL_READY)
        self._assert_primary_bindings(
            chatgpt_session=chatgpt_session,
            stripe_session=stripe_session,
            stripe_context=stripe_context,
        )
        self._transition(PayPalGbPostApprovePhase.CONFIRMED)

    def mark_ba_approved(
        self,
        *,
        checkout_id: str,
        paypal_ba_approve_url: str,
        chatgpt_session: object,
        stripe_session: object,
        stripe_context: dict,
    ) -> None:
        self._require(PayPalGbPostApprovePhase.CONFIRMED)
        self._assert_primary_bindings(
            chatgpt_session=chatgpt_session,
            stripe_session=stripe_session,
            stripe_context=stripe_context,
        )
        if str(checkout_id or "").strip() != self.model.checkout_id:
            raise RuntimeError("PayPal GB Approve 返回了不同 Checkout")
        url = str(paypal_ba_approve_url or "").strip()
        if not url:
            raise RuntimeError("PayPal GB 未确认 BA 前禁止应用优惠")
        self.model.paypal_ba_approve_url = url
        self._transition(PayPalGbPostApprovePhase.BA_APPROVED)

    def apply_promotion(
        self,
        gateway: Callable[[], PromotionResult],
        *,
        checkout_id: str,
        promotion_session: object,
        promotion_stripe_session: object,
        promotion_proxy_url: str,
        stripe_context: dict,
    ) -> PromotionResult:
        self._require(PayPalGbPostApprovePhase.BA_APPROVED)
        if str(checkout_id or "").strip() != self.model.checkout_id:
            raise RuntimeError("PayPal GB 优惠 Update 必须复用原 Checkout")
        if str(promotion_proxy_url or "").strip() != self.model.promotion_proxy_url:
            raise RuntimeError("PayPal GB 优惠 Update 必须使用固定池 2 代理")
        session_device_id = str(
            getattr(promotion_session, "opll_oai_device_id", "") or ""
        ).strip()
        if session_device_id and session_device_id != self.model.device_id:
            raise RuntimeError("PayPal GB 优惠 Session 设备 ID 发生漂移")
        if promotion_stripe_session is None:
            raise RuntimeError("PayPal GB 优惠复查缺少池 2 Stripe Session")
        if promotion_stripe_session is self.model._primary_stripe_session:
            raise RuntimeError("PayPal GB 优惠复查必须使用独立池 2 Stripe Session")
        self._assert_stripe_context(stripe_context)
        if (
            self.model._promotion_chatgpt_session is not None
            and promotion_session is not self.model._promotion_chatgpt_session
        ):
            raise RuntimeError("PayPal GB 池 2 ChatGPT Session 发生漂移")
        if (
            self.model._promotion_stripe_session is not None
            and promotion_stripe_session
            is not self.model._promotion_stripe_session
        ):
            raise RuntimeError("PayPal GB 池 2 Stripe Session 发生漂移")
        if self.model.promotion_update_count:
            raise RuntimeError("PayPal GB 同一 Checkout 只允许一次优惠 Update")
        self.model._promotion_chatgpt_session = promotion_session
        self.model._promotion_stripe_session = promotion_stripe_session
        result = gateway()
        self.model.promotion_update_count = 1
        self._transition(PayPalGbPostApprovePhase.PROMOTED)
        return result

    def mark_verified(
        self,
        *,
        checkout_id: str,
        stripe_checkout_id: str,
        amount: str,
        currency: str,
        submission_state: str,
        payment_method_types: Iterable[str],
        stripe_session: object,
        stripe_context: dict,
    ) -> None:
        self._require(PayPalGbPostApprovePhase.PROMOTED)
        if str(checkout_id or "").strip() != self.model.checkout_id:
            raise RuntimeError("PayPal GB 优惠后 ChatGPT Checkout ID 发生变化")
        if str(stripe_checkout_id or "").strip() != self.model.stripe_checkout_id:
            raise RuntimeError("PayPal GB 优惠后 Stripe Checkout ID 发生变化")
        if stripe_session is not self.model._promotion_stripe_session:
            raise RuntimeError("PayPal GB 优惠复查未复用池 2 Stripe Session")
        self._assert_stripe_context(stripe_context)
        normalized_submission_state = str(submission_state or "").strip().lower()
        if normalized_submission_state != "approved":
            raise RuntimeError("PayPal GB 优惠后的 Stripe 状态必须为 approved")
        if str(amount or "").strip() != "0":
            raise RuntimeError("PayPal GB 优惠后的应付金额必须为 0")
        if str(currency or "").strip().upper() != "GBP":
            raise RuntimeError("PayPal GB 优惠后的 Stripe 币种必须保持 GBP")
        normalized_methods = {
            str(method or "").strip().lower()
            for method in payment_method_types
        }
        if "paypal" not in normalized_methods:
            raise RuntimeError("PayPal GB 优惠后的 Stripe 页面未保留 PayPal 方法")
        self.model.post_approval_amount = "0"
        self.model.post_promotion_submission_state = "approved"
        self.model.paypal_method_retained = True
        self._transition(PayPalGbPostApprovePhase.VERIFIED)
