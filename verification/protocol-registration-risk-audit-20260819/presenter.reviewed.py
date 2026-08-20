from __future__ import annotations

import asyncio
import json
import secrets
from copy import deepcopy
from pathlib import Path
from typing import Any

from hidemyemail_generator.account_plan import AccountPlanPresenter
from hidemyemail_generator.browser_tasks import (
    account_registration_proxy_url,
    account_session_access_token,
)
from hidemyemail_generator.protocol_registration import ProtocolRegistrationManager
from hidemyemail_generator.registration_inventory import (
    complete_generated_inventory_lease,
    lease_generated_inventory_email,
)
from hidemyemail_generator.registration_inventory_sync import export_inventory_record
from hidemyemail_generator.registration_proxy import PROXY_COUNTRIES, RegistrationProxyStore
from hidemyemail_generator.zkgmail import ZkgmailConfigStore, ZkgmailMailClient

from .model import (
    NO_OFFER_POOL,
    OFFER_POOL,
    OfferAccount,
    OfferPoolRepository,
    RegistrationRunRepository,
    SharedAccountRepository,
    utc_now,
)
from .network import (
    OFFER_PROXY_SETTING_KEY,
    REGISTRATION_PROXY_SETTING_KEY,
    CodeServiceClient,
    KookeeyRegistrationProxyStrategy,
    KookeeyOfferView,
    ServerAlternatingProxyStrategy,
)
from .settings import ServerSettings


DEFAULT_OFFER_COUNTRIES = ("US", "GB", "DE")

# Conservative review profile: one self-service registration on one route.
MAX_REVIEW_REGISTRATIONS_PER_TASK = 1
MAX_REVIEW_CONCURRENCY = 1


def normalize_offer_countries(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple)):
        raise ValueError("优惠检查国家必须是数组")
    countries: list[str] = []
    for value in values:
        country = str(value or "").strip().upper()
        if country not in PROXY_COUNTRIES:
            raise ValueError(f"优惠检查国家无效：{country or '<empty>'}")
        if country not in countries:
            countries.append(country)
    if not countries:
        raise ValueError("请至少选择一个优惠检查国家")
    return countries[:10]


class OfferPresenter:
    """MVP Presenter that checks with Kookeey and classifies one account."""

    def __init__(
        self,
        *,
        shared_db: Path,
        repository: OfferPoolRepository,
        view: KookeeyOfferView,
    ) -> None:
        self.shared_db = Path(shared_db)
        self.repository = repository
        self.view = view
        self.account_repository = SharedAccountRepository(self.shared_db)

    @staticmethod
    def _registration_fields(record: dict[str, Any]) -> dict[str, str]:
        environment = record.get("registration_environment")
        environment = environment if isinstance(environment, dict) else {}
        return {
            "registration_ip": str(environment.get("exit_ip") or ""),
            "registration_country": str(
                environment.get("exit_country")
                or environment.get("proxy_country")
                or ""
            ),
            "registration_proxy_mode": str(environment.get("proxy_mode") or ""),
        }

    def _new_item(
        self,
        *,
        email: str,
        record: dict[str, Any],
        status: str,
        **values: Any,
    ) -> OfferAccount:
        registration = self._registration_fields(record)
        previous = self.repository.get(email)
        if previous is not None:
            registration["registration_ip"] = (
                registration["registration_ip"] or previous.registration_ip
            )
            registration["registration_country"] = (
                registration["registration_country"] or previous.registration_country
            )
            registration["registration_proxy_mode"] = (
                registration["registration_proxy_mode"]
                or previous.registration_proxy_mode
            )
        return OfferAccount(
            email=email,
            status=status,
            checked_at=utc_now(),
            **registration,
            **values,
        )

    def _save_account_metadata(self, item: OfferAccount) -> None:
        record = self.account_repository.load(item.email)
        if item.plan_status in {"free", "plus"}:
            record["account_type"] = item.plan_status
            record["account_type_source"] = "protocol_server_kookeey_check"
        record["protocol_server_offer"] = {
            "status": item.status,
            "pool": item.pool,
            "eligible": item.eligible,
            "checkout_submitted": item.checkout_submitted,
            "checkout_url": item.checkout_url,
            "checkout_country": item.checkout_country,
            "checkout_currency": item.checkout_currency,
            "checkout_amount_minor": item.checkout_amount_minor,
            "paypal_available": item.paypal_available,
            "checkout_evidence": json.loads(item.checkout_evidence_json or "[]"),
            "detail": item.detail,
            "checked_at": item.checked_at,
            "registration_ip": item.registration_ip,
            "registration_country": item.registration_country,
            "registration_proxy_mode": item.registration_proxy_mode,
        }
        record["updated_at"] = utc_now()
        self.account_repository.save(item.email, record)

    def _persist(self, item: OfferAccount) -> dict[str, Any]:
        saved = self.repository.save(item)
        result = saved.to_dict()
        try:
            self._save_account_metadata(saved)
        except Exception as error:
            result["metadata_sync_error"] = str(error)
        return result

    def process(
        self,
        email: str,
        countries: list[str] | tuple[str, ...] = DEFAULT_OFFER_COUNTRIES,
    ) -> dict[str, Any]:
        target = str(email or "").strip().lower()
        record: dict[str, Any] = {}
        try:
            selected_countries = normalize_offer_countries(countries)
            record = self.account_repository.load(target)
            access_token = account_session_access_token(record)
            if not access_token:
                return self._persist(
                    self._new_item(
                        email=target,
                        record=record,
                        status="error",
                        detail="账号尚未保存 Access Token",
                    )
                )
            evidence: list[dict[str, Any]] = []
            failures = 0
            for country in selected_countries:
                try:
                    probe = self.view.check_checkout(access_token, country)
                except Exception as error:
                    failures += 1
                    evidence.append(
                        {
                            "exitCountry": country,
                            "status": "error",
                            "detail": str(error),
                        }
                    )
                    continue
                result = probe.to_dict()
                result["status"] = "checked"
                evidence.append(result)
                if probe.eligible:
                    return self._persist(
                        self._new_item(
                            email=target,
                            record=record,
                            status=OFFER_POOL,
                            pool=OFFER_POOL,
                            eligible=True,
                            checkout_submitted=True,
                            checkout_url=probe.checkout_url,
                            checkout_country=probe.checkout_country,
                            checkout_currency=probe.currency,
                            checkout_amount_minor=probe.amount_minor,
                            paypal_available=True,
                            checkout_evidence_json=json.dumps(
                                evidence, ensure_ascii=False, separators=(",", ":")
                            ),
                            detail=(
                                f"出口 {probe.exit_country} / 账单 "
                                f"{probe.checkout_country} Checkout 已确认 "
                                "PayPal 可用且应付金额为 0"
                            ),
                        )
                    )
            evidence_json = json.dumps(
                evidence, ensure_ascii=False, separators=(",", ":")
            )
            if failures:
                return self._persist(
                    self._new_item(
                        email=target,
                        record=record,
                        status="error",
                        checkout_evidence_json=evidence_json,
                        detail=(
                            f"Checkout 多国检查未全部完成：{failures}/"
                            f"{len(selected_countries)} 个国家失败"
                        ),
                    )
                )
            return self._persist(
                self._new_item(
                    email=target,
                    record=record,
                    status=NO_OFFER_POOL,
                    pool=NO_OFFER_POOL,
                    checkout_evidence_json=evidence_json,
                    detail=(
                        "已检查 "
                        + "/".join(selected_countries)
                        + " Checkout，均未同时满足 PayPal 可用且应付金额为 0"
                    ),
                )
            )
        except Exception as error:
            return self._persist(
                self._new_item(
                    email=target,
                    record=record,
                    status="error",
                    detail=f"优惠检查失败：{error}",
                )
            )


class AccountVerificationPresenter:
    """MVP Presenter that verifies a new Session immediately after registration."""

    def __init__(
        self,
        shared_db: Path,
        *,
        plan_presenter: AccountPlanPresenter | None = None,
    ) -> None:
        self.shared_db = Path(shared_db)
        self.plan_presenter = plan_presenter or AccountPlanPresenter()
        self.account_repository = SharedAccountRepository(self.shared_db)

    def _save(self, email: str, result: dict[str, Any]) -> None:
        record = self.account_repository.load(email)
        plan_status = str(result.get("planStatus") or "").lower()
        if result.get("verified") and plan_status in {"free", "plus"}:
            record["account_type"] = plan_status
            record["account_type_source"] = "protocol_server_post_registration"
        record["protocol_server_verification"] = result
        record["updated_at"] = utc_now()
        self.account_repository.save(email, record)

    def _save_result(self, email: str, result: dict[str, Any]) -> dict[str, Any]:
        try:
            self._save(email, result)
        except Exception as error:
            result["metadataSyncError"] = str(error)
        return result

    def process(self, email: str) -> dict[str, Any]:
        target = str(email or "").strip().lower()
        checked_at = utc_now()
        try:
            record = self.account_repository.load(target)
        except Exception as error:
            return {
                "email": target,
                "verified": False,
                "planStatus": "error",
                "detail": f"注册后验证读取账号失败：{error}",
                "proxyMode": "unknown",
                "checkedAt": checked_at,
            }
        access_token = account_session_access_token(record)
        proxy_url = account_registration_proxy_url(record)
        if not access_token:
            result = {
                "email": target,
                "verified": False,
                "planStatus": "error",
                "detail": "注册后验证缺少 Access Token",
                "checkedAt": checked_at,
            }
            return self._save_result(target, result)
        try:
            plan = self.plan_presenter.check(
                access_token,
                proxy_url=proxy_url,
                language="en-US",
                timezone_offset_min="-",
            )
            status = str(plan.status or "").strip().lower()
            result = {
                "email": target,
                "verified": status in {"free", "plus"},
                "planStatus": status,
                "detail": str(plan.detail or ""),
                "proxyMode": "registration" if proxy_url else "direct",
                "checkedAt": checked_at,
            }
        except Exception as error:
            result = {
                "email": target,
                "verified": False,
                "planStatus": "error",
                "detail": f"注册后验证失败：{error}",
                "proxyMode": "registration" if proxy_url else "direct",
                "checkedAt": checked_at,
            }
        return self._save_result(target, result)


class ServerRegistrationPresenter:
    """Standalone registration Presenter with inventory and offer orchestration."""

    def __init__(self, settings: ServerSettings) -> None:
        self.settings = settings
        self.settings.service_db.parent.mkdir(parents=True, exist_ok=True)
        self.offer_repository = OfferPoolRepository(settings.service_db)
        self.run_repository = RegistrationRunRepository(settings.service_db)
        self.registration_store = RegistrationProxyStore(
            settings.service_db,
            setting_key=REGISTRATION_PROXY_SETTING_KEY,
        )
        self.registration_store.configure(
            enabled=True,
            mode="clash",
            country="JP",
            clash_controller=settings.clash_controller,
            clash_selector=settings.clash_selector,
            clash_proxy_url=settings.clash_proxy_url,
            max_latency_ms=settings.clash_max_latency_ms,
        )
        self.registration_proxy = ServerAlternatingProxyStrategy(
            self.registration_store
        )
        self.active_registration_proxy = self.registration_proxy
        self.offer_store = RegistrationProxyStore(
            settings.service_db,
            setting_key=OFFER_PROXY_SETTING_KEY,
        )
        self.offer_view = KookeeyOfferView(self.offer_store)
        self.offer_presenter = OfferPresenter(
            shared_db=settings.shared_db,
            repository=self.offer_repository,
            view=self.offer_view,
        )
        self.verification_presenter = AccountVerificationPresenter(
            settings.shared_db
        )
        self.code_client = CodeServiceClient(
            settings.code_service_url,
            settings.code_service_token,
        )
        self.zkgmail = ZkgmailMailClient(ZkgmailConfigStore(settings.shared_db))
        self._verification_lock = asyncio.Lock()
        self.manager = self._new_manager()
        self._leases: dict[str, dict[str, Any]] = {}
        self._providers: dict[str, str] = {}
        self._supervisor: asyncio.Task[None] | None = None
        self._offer_refresh_lock = asyncio.Lock()
        self._stop_requested = False
        restored = self.run_repository.latest()
        self._service_state = {
            **self._idle_service_state(),
            **(
                {key: value for key, value in restored.items() if key != "persistedAt"}
                if restored
                else {}
            ),
        }
        if self._service_state.get("running"):
            self._service_state.update(
                status="interrupted",
                phase="interrupted",
                running=False,
                message="服务重启，上一任务状态已保留但执行已中断",
                finishedAt=utc_now(),
            )
            self.run_repository.save(self._service_state)

    def _new_manager(self, proxy_store: Any | None = None) -> ProtocolRegistrationManager:
        return ProtocolRegistrationManager(
            base_dir=Path(__file__).resolve().parents[2],
            db_file=self.settings.shared_db,
            proxy_store=proxy_store or self.active_registration_proxy,
            max_concurrency=MAX_REVIEW_CONCURRENCY,
        )

    def _persist_state(self) -> None:
        state = deepcopy(self._service_state)
        task = self.manager.snapshot()
        state["taskSummary"] = {
            key: deepcopy(task.get(key))
            for key in (
                "id",
                "status",
                "phase",
                "total",
                "completed",
                "succeeded",
                "failed",
                "accounts",
            )
        }
        self.run_repository.save(state)

    @staticmethod
    def _idle_service_state() -> dict[str, Any]:
        return {
            "id": "",
            "status": "idle",
            "phase": "idle",
            "running": False,
            "provider": "inventory",
            "requested": 0,
            "acquired": 0,
            "concurrency": 1,
            "useRegistrationKookeey": False,
            "registrationCountry": "JP",
            "checkOffer": False,
            "offerCountries": list(DEFAULT_OFFER_COUNTRIES),
            "setupCredentials": False,
            "verificationCompleted": 0,
            "verificationVerified": 0,
            "offerCompleted": 0,
            "message": "等待服务器注册任务",
            "startedAt": "",
            "finishedAt": "",
            "offerResults": [],
            "verificationResults": [],
        }

    async def _acquire(self, provider: str, count: int) -> list[str]:
        emails: list[str] = []
        for index in range(count):
            if provider == "inventory":
                lease = await asyncio.to_thread(
                    lease_generated_inventory_email,
                    self.settings.shared_db,
                    client_id="protocol-registration-server",
                    label=f"服务器协议注册 {index + 1}/{count}",
                    lease_seconds=3600,
                )
                if not lease:
                    break
                email = str(lease.get("email") or "").strip().lower()
                self._leases[email] = dict(lease)
            else:
                email = str(
                    await self.zkgmail.acquire_email(
                        f"服务器协议注册 {index + 1}/{count}"
                    )
                ).strip().lower()
            if email and email not in emails:
                emails.append(email)
                self._providers[email] = provider
        return emails

    async def _complete_email(self, email: str, success: bool, message: str) -> None:
        provider = self._providers.get(email, "")
        if provider == "inventory":
            lease = self._leases.get(email, {})
            record = (
                await asyncio.to_thread(
                    export_inventory_record,
                    self.settings.shared_db,
                    email,
                )
                if success
                else None
            )
            await asyncio.to_thread(
                complete_generated_inventory_lease,
                self.settings.shared_db,
                lease_id=str(lease.get("leaseId") or ""),
                email=email,
                success=success,
                message=message,
                record=record,
            )
        elif provider == "zkgmail":
            await self.zkgmail.complete_email(email, success, message)

    async def _complete_email_and_verify(
        self,
        email: str,
        success: bool,
        message: str,
    ) -> None:
        try:
            if success:
                result = await asyncio.to_thread(
                    self.verification_presenter.process,
                    email,
                )
                async with self._verification_lock:
                    previous = [
                        item
                        for item in self._service_state.get("verificationResults", [])
                        if item.get("email") != email
                    ]
                    results = [*previous, result][-100:]
                    self._service_state.update(
                        phase="verification",
                        message=f"{email} 注册完成，已立即验证账号",
                        verificationCompleted=len(results),
                        verificationVerified=sum(
                            1 for item in results if item.get("verified")
                        ),
                        verificationResults=results,
                    )
                    self._persist_state()
        finally:
            await self._complete_email(email, success, message)

    async def _release_all(self, message: str) -> None:
        for email in list(self._providers):
            try:
                await self._complete_email(email, False, message)
            except Exception:
                pass

    async def start(
        self,
        *,
        count: int,
        provider: str,
        concurrency: int,
        use_registration_kookeey: bool,
        registration_country: str,
        offer_countries: list[str],
        check_offer: bool,
        setup_credentials: bool,
    ) -> dict[str, Any]:
        if self._supervisor is not None and not self._supervisor.done():
            raise RuntimeError("服务器协议注册任务正在运行")
        count = int(count)
        if not 1 <= count <= MAX_REVIEW_REGISTRATIONS_PER_TASK:
            raise ValueError("审查配置每次仅允许注册 1 个账号")
        concurrency = int(concurrency)
        if not 1 <= concurrency <= MAX_REVIEW_CONCURRENCY:
            raise ValueError("审查配置并发注册数必须为 1")
        if use_registration_kookeey:
            raise ValueError("审查配置固定使用既有单一路由")
        if check_offer:
            raise ValueError("审查配置不自动创建或跨地区探测优惠 Checkout")
        provider = str(provider or "").strip().lower()
        if provider not in {"inventory", "zkgmail"}:
            raise ValueError("邮箱来源仅支持 inventory 或 zkgmail")
        country = str(registration_country or "JP").strip().upper()
        if country not in PROXY_COUNTRIES:
            raise ValueError("注册出口国家无效")
        selected_offer_countries = normalize_offer_countries(offer_countries)
        if use_registration_kookeey:
            self.active_registration_proxy = KookeeyRegistrationProxyStrategy(
                self.offer_store,
                country,
            )
            if not self.active_registration_proxy.public_state().get("configured"):
                raise RuntimeError("Kookeey 注册代理尚未配置")
        else:
            self.active_registration_proxy = self.registration_proxy
        self.manager = self._new_manager(self.active_registration_proxy)
        runtime = self.manager.snapshot()["runtime"]
        if not runtime.get("available"):
            raise RuntimeError(f"协议运行环境未就绪：{runtime.get('error') or 'unknown'}")
        self._leases.clear()
        self._providers.clear()
        self._stop_requested = False
        emails = await self._acquire(provider, count)
        if not emails:
            raise RuntimeError("服务器邮箱库存为空")
        task_id = secrets.token_hex(8)
        self._service_state = {
            **self._idle_service_state(),
            "id": task_id,
            "status": "running",
            "phase": "registration",
            "running": True,
            "provider": provider,
            "requested": count,
            "acquired": len(emails),
            "concurrency": concurrency,
            "useRegistrationKookeey": bool(use_registration_kookeey),
            "registrationCountry": country,
            "offerCountries": selected_offer_countries,
            "checkOffer": bool(check_offer),
            "setupCredentials": bool(setup_credentials),
            "message": f"已领取 {len(emails)} 个邮箱，开始服务器协议注册",
            "startedAt": utc_now(),
        }
        self._persist_state()
        try:
            self.manager.start(
                emails=emails,
                base_url=self.settings.internal_base_url,
                concurrency=concurrency,
                setup_credentials=bool(setup_credentials),
                on_account_finished=self._complete_email_and_verify,
            )
            self._persist_state()
        except Exception:
            await self._release_all("协议任务启动失败")
            self._service_state.update(
                status="failed",
                phase="failed",
                running=False,
                message="服务器协议注册任务启动失败",
                finishedAt=utc_now(),
            )
            self._persist_state()
            raise
        self._supervisor = asyncio.create_task(
            self._monitor(
                check_offer=bool(check_offer),
                offer_countries=selected_offer_countries,
            ),
            name=f"server-protocol-registration-{task_id}",
        )
        return self.snapshot()

    async def _monitor(
        self,
        *,
        check_offer: bool,
        offer_countries: list[str],
    ) -> None:
        try:
            final = await self.manager.wait()
            if self._stop_requested or final.get("status") == "cancelled":
                self._service_state.update(
                    status="cancelled",
                    phase="cancelled",
                    message="服务器协议注册已停止",
                )
                return
            successful = [
                str(item.get("email") or "")
                for item in final.get("accounts", [])
                if isinstance(item, dict) and item.get("status") == "success"
            ]
            if check_offer and successful:
                self._service_state.update(
                    phase="offer_check",
                    message=f"注册完成，正在使用 Kookeey 检查 {len(successful)} 个账号",
                )
                results = []
                for email in successful:
                    if self._stop_requested:
                        break
                    result = await asyncio.to_thread(
                        self.offer_presenter.process,
                        email,
                        offer_countries,
                    )
                    results.append(result)
                    self._service_state.update(
                        offerCompleted=len(results),
                        offerResults=results[-100:],
                    )
                    self._persist_state()
            self._service_state.update(
                status="completed" if final.get("succeeded") else "failed",
                phase="completed",
                message=(
                    f"服务器任务完成：注册成功 {final.get('succeeded', 0)}，"
                    f"验证通过 {self._service_state.get('verificationVerified', 0)}，"
                    f"优惠检查 {self._service_state.get('offerCompleted', 0)}"
                ),
            )
        finally:
            self._service_state["running"] = False
            self._service_state["finishedAt"] = utc_now()
            self._persist_state()

    async def stop(self) -> dict[str, Any]:
        self._stop_requested = True
        if self.manager.snapshot().get("running"):
            await self.manager.stop()
        if self._supervisor is not None and not self._supervisor.done():
            await self._supervisor
        return self.snapshot()

    async def refresh_offer(
        self,
        email: str,
        countries: list[str],
    ) -> dict[str, Any]:
        target = str(email or "").strip().lower()
        if not target or self.offer_repository.get(target) is None:
            raise ValueError("优惠记录不存在")
        selected_countries = normalize_offer_countries(countries)
        async with self._offer_refresh_lock:
            return await asyncio.to_thread(
                self.offer_presenter.process,
                target,
                selected_countries,
            )

    async def close(self) -> None:
        if self._supervisor is not None and not self._supervisor.done():
            await self.stop()

    def token_record(self, token: str) -> dict[str, str] | None:
        return self.manager.token_record(token)

    def snapshot(self) -> dict[str, Any]:
        if self._service_state.get("id"):
            self._persist_state()
        return {
            "service": deepcopy(self._service_state),
            "task": self.manager.snapshot(),
            "offerPool": self.offer_repository.snapshot(limit=100),
            "registrationProxy": self.active_registration_proxy.public_state(),
            "offerProxy": self.offer_view.public_state(),
            "registrationCountries": [
                {"code": code, "label": label}
                for code, label in PROXY_COUNTRIES.items()
            ],
            "runHistory": self.run_repository.recent(limit=20),
        }
