"""MVP components for reusing one active HeroSMS PayPal number.

The model owns the thread-safe lease state.  The presenter coordinates a
provider strategy with a small job-facing view.  Keeping the two concerns
separate is important because the web runner starts several account payments
concurrently, while one activation must never be polled by two jobs at once.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Callable, Protocol

from paypal.smsbower import (
    SMSBowerPhoneActivation,
    SMSBowerPhoneCancelled,
)


REUSABLE_SMS_PROVIDER = "hero-sms"
HERO_SMS_ACTIVATION_TTL_SECONDS = 20 * 60
MAX_REUSABLE_PAYMENT_FAILURES = 2

Clock = Callable[[], float]


@dataclass(frozen=True)
class SmsActivationKey:
    """Isolation boundary for a reusable activation."""

    owner_id: str
    provider: str
    service: str
    country: str


@dataclass(frozen=True)
class SmsActivationLease:
    """One provider activation plus its original, non-renewing deadline."""

    activation: SMSBowerPhoneActivation
    key: SmsActivationKey | None
    reservation_token: str
    acquired_at: float
    expires_at: float
    successful_uses: int = 0
    payment_failures: int = 0
    reused: bool = False
    consumed_codes: frozenset[str] = frozenset()

    @property
    def reusable(self) -> bool:
        return self.key is not None and self.key.provider == REUSABLE_SMS_PROVIDER

    def with_consumed_code(self, code: str) -> "SmsActivationLease":
        """Return a lease that remembers one code already handed to a job."""

        normalized = str(code or "").strip()
        if not normalized or normalized in self.consumed_codes:
            return self
        return replace(self, consumed_codes=self.consumed_codes | {normalized})

    @classmethod
    def unmanaged(
        cls,
        activation: SMSBowerPhoneActivation,
        *,
        clock: Clock = time.monotonic,
    ) -> "SmsActivationLease":
        now = float(clock())
        return cls(
            activation=activation,
            key=None,
            reservation_token="",
            acquired_at=now,
            expires_at=now,
        )


@dataclass(frozen=True)
class SmsActivationReservation:
    """Exclusive right to use or replace the activation for one key."""

    key: SmsActivationKey
    token: str
    lease: SmsActivationLease | None
    expired: tuple[SmsActivationLease, ...] = ()


@dataclass(frozen=True)
class PendingSmsActivationCleanup:
    """Provider transition retained after a transient cleanup failure."""

    lease: SmsActivationLease
    force_complete: bool


@dataclass
class _LeaseState:
    available: SmsActivationLease | None = None
    busy_token: str = ""
    busy_expires_at: float = 0.0
    waiters: int = 0


class SmsActivationView(Protocol):
    """View contract consumed by :class:`SmsActivationReusePresenter`."""

    def attach_sms_activation_lease(self, lease: SmsActivationLease) -> None: ...

    def take_sms_activation_lease(self) -> SmsActivationLease | None: ...

    def set_status(self, status: str, stage: str | None = None) -> None: ...

    def add_log(self, level: str, message: str, *, ts: float | None = None) -> None: ...


class ReusableSmsActivationModel:
    """Thread-safe lease repository with one active user per isolation key."""

    def __init__(
        self,
        *,
        ttl_seconds: float = HERO_SMS_ACTIVATION_TTL_SECONDS,
        max_payment_failures: int = MAX_REUSABLE_PAYMENT_FAILURES,
        clock: Clock = time.monotonic,
        wait_interval_seconds: float = 0.1,
    ) -> None:
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.max_payment_failures = max(0, int(max_payment_failures))
        self._clock = clock
        self._wait_interval_seconds = max(0.01, float(wait_interval_seconds))
        self._condition = threading.Condition()
        self._states: dict[SmsActivationKey, _LeaseState] = {}
        self._pending_cleanup: dict[str, PendingSmsActivationCleanup] = {}

    @staticmethod
    def key(
        *, owner_id: str, provider: str, service: str, country: str
    ) -> SmsActivationKey:
        return SmsActivationKey(
            owner_id=str(owner_id or "").strip().lower(),
            provider=str(provider or "").strip().lower(),
            service=str(service or "paypal").strip().lower(),
            country=str(country or "").strip().upper(),
        )

    def reserve(
        self,
        *,
        owner_id: str,
        provider: str,
        service: str,
        country: str,
        cancel_event: threading.Event | None = None,
    ) -> SmsActivationReservation:
        """Wait for and atomically reserve one key.

        A reservation is taken even when no cached lease exists.  This closes
        the race where two concurrently-started account jobs would both buy a
        number before either one could publish it for reuse.
        """

        key = self.key(
            owner_id=owner_id,
            provider=provider,
            service=service,
            country=country,
        )
        if key.provider != REUSABLE_SMS_PROVIDER:
            raise ValueError("当前接码平台不使用共享号码租约")
        with self._condition:
            now = float(self._clock())
            expired = self._prune_expired_locked(now)
            state = self._states.setdefault(key, _LeaseState())
            while state.busy_token:
                now = float(self._clock())
                if state.busy_expires_at and now >= state.busy_expires_at:
                    # The provider activation behind the old generation has
                    # expired.  A late finalize carries the old token and
                    # therefore cannot release this replacement generation.
                    state.busy_token = ""
                    state.busy_expires_at = 0.0
                    break
                if cancel_event is not None and cancel_event.is_set():
                    raise SMSBowerPhoneCancelled("短信号码复用等待已随任务停止")
                state.waiters += 1
                try:
                    self._condition.wait(self._wait_interval_seconds)
                finally:
                    state.waiters -= 1
            if cancel_event is not None and cancel_event.is_set():
                raise SMSBowerPhoneCancelled("短信号码复用等待已随任务停止")

            now = float(self._clock())
            available = state.available
            if available is not None and now >= available.expires_at:
                expired = (*expired, available)
                available = None
            token = uuid.uuid4().hex
            state.available = None
            state.busy_token = token
            state.busy_expires_at = (
                available.expires_at
                if available is not None
                else now + self.ttl_seconds
            )
            active = (
                replace(available, reservation_token=token, reused=True)
                if available is not None
                else None
            )
            return SmsActivationReservation(
                key=key,
                token=token,
                lease=active,
                expired=tuple(expired),
            )

    def bind_new(
        self,
        reservation: SmsActivationReservation,
        activation: SMSBowerPhoneActivation,
    ) -> SmsActivationLease:
        """Bind a freshly purchased activation to an existing reservation."""

        with self._condition:
            self._require_active(reservation.key, reservation.token)
            now = float(self._clock())
            lease = SmsActivationLease(
                activation=activation,
                key=reservation.key,
                reservation_token=reservation.token,
                acquired_at=now,
                expires_at=now + self.ttl_seconds,
            )
            self._states[reservation.key].busy_expires_at = lease.expires_at
            return lease

    def lease_for_cleanup(
        self,
        reservation: SmsActivationReservation,
        activation: SMSBowerPhoneActivation,
    ) -> SmsActivationLease:
        """Describe a purchased number whose generation changed before bind."""

        now = float(self._clock())
        return SmsActivationLease(
            activation=activation,
            key=reservation.key,
            reservation_token=reservation.token,
            acquired_at=now,
            expires_at=now + self.ttl_seconds,
        )

    def recycle(
        self, lease: SmsActivationLease, *, payment_failed: bool = False
    ) -> bool:
        """Return a settled lease to the pool without extending its TTL."""

        if lease.key is None:
            return False
        with self._condition:
            state = self._require_active(lease.key, lease.reservation_token)
            if float(self._clock()) >= lease.expires_at:
                return False
            payment_failures = lease.payment_failures + int(payment_failed)
            if payment_failures > self.max_payment_failures:
                return False
            state.available = replace(
                lease,
                reservation_token="",
                successful_uses=lease.successful_uses + int(not payment_failed),
                payment_failures=payment_failures,
                reused=False,
            )
            state.busy_token = ""
            state.busy_expires_at = 0.0
            self._condition.notify_all()
            return True

    def discard(self, lease: SmsActivationLease) -> bool:
        """Release a reservation while permanently removing its activation."""

        if lease.key is None:
            return False
        return self._release_token(lease.key, lease.reservation_token)

    def abandon(self, reservation: SmsActivationReservation) -> bool:
        """Release a reservation when fresh acquisition never produced a lease."""

        return self._release_token(reservation.key, reservation.token)

    def remaining_seconds(self, lease: SmsActivationLease | None) -> int:
        if lease is None or not lease.reusable:
            return 0
        return max(0, int(lease.expires_at - float(self._clock())))

    def reservation_is_current(
        self,
        reservation: SmsActivationReservation,
        lease: SmsActivationLease,
    ) -> bool:
        """Atomically validate the generation after a provider round trip."""

        with self._condition:
            state = self._states.get(reservation.key)
            return bool(
                state is not None
                and state.busy_token == reservation.token
                and lease.reservation_token == reservation.token
                and float(self._clock()) < lease.expires_at
            )

    def clear(self) -> tuple[SmsActivationLease, ...]:
        """Reset idle state; intended for deterministic service/test cleanup."""

        with self._condition:
            available = [
                state.available
                for state in self._states.values()
                if state.available is not None
            ]
            available.extend(
                pending.lease for pending in self._pending_cleanup.values()
            )
            self._states.clear()
            self._pending_cleanup.clear()
            self._condition.notify_all()
            return tuple(available)

    def queue_cleanup(self, lease: SmsActivationLease, *, force_complete: bool) -> None:
        with self._condition:
            self._pending_cleanup[lease.activation.activation_id] = (
                PendingSmsActivationCleanup(lease, force_complete)
            )

    def take_pending_cleanup(
        self, *, limit: int = 10
    ) -> tuple[PendingSmsActivationCleanup, ...]:
        with self._condition:
            selected_ids = tuple(self._pending_cleanup)[: max(1, int(limit))]
            return tuple(
                self._pending_cleanup.pop(activation_id)
                for activation_id in selected_ids
            )

    def pending_cleanup_count(self) -> int:
        with self._condition:
            return len(self._pending_cleanup)

    def _require_active(self, key: SmsActivationKey, token: str) -> _LeaseState:
        state = self._states.get(key)
        if state is None or not token or state.busy_token != token:
            raise RuntimeError("短信号码租约已失效")
        return state

    def _release_token(self, key: SmsActivationKey, token: str) -> bool:
        with self._condition:
            state = self._states.get(key)
            if state is None or not token or state.busy_token != token:
                return False
            state.available = None
            state.busy_token = ""
            state.busy_expires_at = 0.0
            if state.waiters == 0:
                self._states.pop(key, None)
            self._condition.notify_all()
            return True

    def _prune_expired_locked(self, now: float) -> tuple[SmsActivationLease, ...]:
        expired: list[SmsActivationLease] = []
        for key, state in tuple(self._states.items()):
            lease = state.available
            if state.busy_token or lease is None or now < lease.expires_at:
                continue
            expired.append(lease)
            state.available = None
            if state.waiters == 0:
                self._states.pop(key, None)
        return tuple(expired)


class SmsActivationReusePresenter:
    """Coordinate provider actions and the reusable-activation model."""

    def __init__(self, model: ReusableSmsActivationModel) -> None:
        self.model = model

    def acquire(
        self,
        view: SmsActivationView,
        client: Any,
        *,
        owner_id: str,
        provider: str,
        service: str,
        country: str,
        max_price: float,
        cancel_event: threading.Event | None = None,
    ) -> SMSBowerPhoneActivation:
        normalized_provider = str(provider or "").strip().lower()
        if normalized_provider != REUSABLE_SMS_PROVIDER:
            activation = client.acquire_phone(country, max_price=max_price)
            self._attach(view, SmsActivationLease.unmanaged(activation))
            return activation

        self._retry_pending_cleanup(view, client)

        reservation = self.model.reserve(
            owner_id=owner_id,
            provider=normalized_provider,
            service=service,
            country=country,
            cancel_event=cancel_event,
        )
        for expired in reservation.expired:
            self._log(view, "INFO", "HeroSMS 复用号码已到期，正在获取新手机号")
            self._finish_provider_lease(view, client, expired, force_complete=True)

        cached = reservation.lease
        if cached is not None:
            try:
                client.request_another(cached.activation)
            except Exception as error:
                self._log(
                    view,
                    "WARNING",
                    f"HeroSMS 旧号码无法继续接收验证码，正在换号：{error}",
                )
                self._finish_provider_lease(view, client, cached, force_complete=True)
                self.model.abandon(reservation)
                return self.acquire(
                    view,
                    client,
                    owner_id=owner_id,
                    provider=normalized_provider,
                    service=service,
                    country=country,
                    max_price=max_price,
                    cancel_event=cancel_event,
                )
            else:
                remaining = self.model.remaining_seconds(cached)
                if remaining <= 0 or not self.model.reservation_is_current(
                    reservation, cached
                ):
                    self._log(
                        view,
                        "INFO",
                        "HeroSMS 号码在续收验证码时到期，正在获取新手机号",
                    )
                    self._finish_provider_lease(
                        view, client, cached, force_complete=True
                    )
                    self.model.abandon(reservation)
                    return self.acquire(
                        view,
                        client,
                        owner_id=owner_id,
                        provider=normalized_provider,
                        service=service,
                        country=country,
                        max_price=max_price,
                        cancel_event=cancel_event,
                    )
                else:
                    return self._attach_cached(
                        view, client, reservation, cached, remaining
                    )

        activation: SMSBowerPhoneActivation | None = None
        lease: SmsActivationLease | None = None
        try:
            activation = client.acquire_phone(country, max_price=max_price)
            lease = self.model.bind_new(reservation, activation)
            self._attach(view, lease)
            return activation
        except BaseException:
            cleanup_lease = lease or (
                self.model.lease_for_cleanup(reservation, activation)
                if activation is not None
                else None
            )
            if cleanup_lease is not None:
                self._finish_provider_lease(
                    view,
                    client,
                    cleanup_lease,
                    force_complete=False,
                )
            self.model.abandon(reservation)
            raise

    def _attach_cached(
        self,
        view: SmsActivationView,
        client: Any,
        reservation: SmsActivationReservation,
        cached: SmsActivationLease,
        remaining: int,
    ) -> SMSBowerPhoneActivation:
        try:
            self._attach(view, cached)
        except BaseException:
            self._finish_provider_lease(view, client, cached, force_complete=True)
            self.model.abandon(reservation)
            raise
        self._status(
            view,
            "running",
            f"HeroSMS 正在复用未到期手机号（剩余约 {remaining} 秒）",
        )
        self._log(
            view,
            "INFO",
            f"HeroSMS 已复用当前手机号并请求下一条短信（剩余约 {remaining} 秒）",
        )
        return cached.activation

    def finalize(
        self,
        view: SmsActivationView,
        client: Any,
        *,
        success: bool,
        payment_failed: bool = False,
        terminal_error_code: str = "",
    ) -> bool:
        if success and payment_failed:
            raise ValueError("短信号码结果不能同时为支付成功和支付失败")
        error_code = str(terminal_error_code or "").strip().upper()
        abandon_immediately = error_code == "OAS_ERROR"
        take = getattr(view, "take_sms_activation_lease", None)
        lease = take() if callable(take) else None
        if lease is None:
            return False

        should_recycle = (
            lease.reusable
            and (success or payment_failed)
            and not abandon_immediately
        )
        failure_count = lease.payment_failures + int(payment_failed)
        if should_recycle:
            try:
                recycled = self.model.recycle(
                    lease, payment_failed=payment_failed
                )
            except RuntimeError:
                recycled = False
                self._log(
                    view,
                    "INFO",
                    "HeroSMS 号码租约已换代，旧任务结果不会覆盖新号码",
                )
            if recycled:
                remaining = self.model.remaining_seconds(lease)
                if payment_failed:
                    self._log(
                        view,
                        "WARNING",
                        "HeroSMS 手机号已记录支付失败 "
                        f"{failure_count}/{self.model.max_payment_failures}，"
                        f"仍保留供下一账号复用（剩余约 {remaining} 秒）",
                    )
                else:
                    self._log(
                        view,
                        "INFO",
                        f"HeroSMS 手机号保留供下一账号复用（剩余约 {remaining} 秒）",
                    )
                return True

        if abandon_immediately and lease.reusable:
            self._log(
                view,
                "WARNING",
                "支付返回 OAS_ERROR，HeroSMS 手机号已立即弃用，下一账号将换新号",
            )
        elif (
            payment_failed
            and lease.reusable
            and failure_count > self.model.max_payment_failures
        ):
            self._log(
                view,
                "WARNING",
                "HeroSMS 手机号累计支付失败 "
                f"{failure_count} 次，已超过 {self.model.max_payment_failures} 次，"
                "直接弃用并为下一账号换号",
            )

        try:
            self._finish_provider_lease(
                view,
                client,
                lease,
                force_complete=bool(
                    success or lease.successful_uses or lease.consumed_codes
                ),
            )
        finally:
            if lease.reusable:
                self.model.discard(lease)
        return True

    @staticmethod
    def _attach(view: SmsActivationView, lease: SmsActivationLease) -> None:
        attach_lease = getattr(view, "attach_sms_activation_lease", None)
        if callable(attach_lease):
            attach_lease(lease)
            return
        view.attach_sms_activation(lease.activation)  # type: ignore[attr-defined]

    @staticmethod
    def _status(view: SmsActivationView, status: str, stage: str) -> None:
        update = getattr(view, "set_status", None)
        if callable(update):
            update(status, stage)

    @staticmethod
    def _log(view: SmsActivationView, level: str, message: str) -> None:
        add_log = getattr(view, "add_log", None)
        if callable(add_log):
            add_log(level, message)

    def _finish_provider_lease(
        self,
        view: SmsActivationView,
        client: Any,
        lease: SmsActivationLease,
        *,
        force_complete: bool,
    ) -> bool:
        try:
            if force_complete:
                client.complete(lease.activation)
            else:
                client.cancel(lease.activation)
        except Exception as error:
            self._log(view, "WARNING", str(error))
            message = str(error)
            terminal = any(
                marker in message for marker in ("激活记录不存在", "激活已取消")
            )
            expired = lease.reusable and self.model.remaining_seconds(lease) <= 0
            if lease.reusable and not terminal and not expired:
                self.model.queue_cleanup(lease, force_complete=force_complete)
            return bool(terminal or expired)
        return True

    def _retry_pending_cleanup(self, view: SmsActivationView, client: Any) -> None:
        # Keep new-number acquisition responsive even if several earlier
        # provider transitions are awaiting cleanup.
        for pending in self.model.take_pending_cleanup(limit=1):
            self._finish_provider_lease(
                view,
                client,
                pending.lease,
                force_complete=pending.force_complete,
            )


__all__ = [
    "HERO_SMS_ACTIVATION_TTL_SECONDS",
    "MAX_REUSABLE_PAYMENT_FAILURES",
    "PendingSmsActivationCleanup",
    "REUSABLE_SMS_PROVIDER",
    "ReusableSmsActivationModel",
    "SmsActivationKey",
    "SmsActivationLease",
    "SmsActivationReservation",
    "SmsActivationReusePresenter",
    "SmsActivationView",
]
