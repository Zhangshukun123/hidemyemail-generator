from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hidemyemail_generator.browser_tasks import (
    account_saved_cookies,
    account_session,
    account_session_access_token,
)


OFFER_POOL = "offer"
NO_OFFER_POOL = "no_offer"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class OfferAccount:
    email: str
    status: str
    pool: str = ""
    eligible: bool = False
    checkout_submitted: bool = False
    checkout_url: str = ""
    checkout_country: str = ""
    checkout_currency: str = ""
    checkout_amount_minor: str = ""
    paypal_available: bool = False
    checkout_evidence_json: str = "[]"
    plan_status: str = ""
    detail: str = ""
    checked_at: str = ""
    registration_ip: str = ""
    registration_country: str = ""
    registration_proxy_mode: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OfferPoolRepository:
    """Repository model owned only by the standalone server project."""

    def __init__(self, db_file: Path) -> None:
        self.db_file = Path(db_file)
        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_file, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS offer_accounts (
                    email TEXT PRIMARY KEY COLLATE NOCASE,
                    status TEXT NOT NULL,
                    pool TEXT NOT NULL DEFAULT '',
                    eligible INTEGER NOT NULL DEFAULT 0,
                    checkout_submitted INTEGER NOT NULL DEFAULT 0,
                    checkout_url TEXT NOT NULL DEFAULT '',
                    plan_status TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '',
                    checked_at TEXT NOT NULL
                )
                """
            )
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(offer_accounts)").fetchall()
            }
            migrations = {
                "registration_ip": "TEXT NOT NULL DEFAULT ''",
                "registration_country": "TEXT NOT NULL DEFAULT ''",
                "registration_proxy_mode": "TEXT NOT NULL DEFAULT ''",
                "checkout_country": "TEXT NOT NULL DEFAULT ''",
                "checkout_currency": "TEXT NOT NULL DEFAULT ''",
                "checkout_amount_minor": "TEXT NOT NULL DEFAULT ''",
                "paypal_available": "INTEGER NOT NULL DEFAULT 0",
                "checkout_evidence_json": "TEXT NOT NULL DEFAULT '[]'",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    conn.execute(
                        f"ALTER TABLE offer_accounts ADD COLUMN {name} {definition}"
                    )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_offer_accounts_pool
                ON offer_accounts(pool, checked_at DESC)
                """
            )

    def save(self, item: OfferAccount) -> OfferAccount:
        pool = str(item.pool or "").strip().lower()
        if pool not in {"", OFFER_POOL, NO_OFFER_POOL}:
            raise ValueError("优惠池类型无效")
        try:
            evidence = json.loads(str(item.checkout_evidence_json or "[]"))
        except (json.JSONDecodeError, TypeError, ValueError):
            evidence = []
        evidence = evidence if isinstance(evidence, list) else []
        normalized = OfferAccount(
            email=str(item.email or "").strip().lower(),
            status=str(item.status or "error").strip().lower(),
            pool=pool,
            eligible=bool(item.eligible),
            checkout_submitted=bool(item.checkout_submitted),
            checkout_url=str(item.checkout_url or "").strip(),
            checkout_country=str(item.checkout_country or "").strip().upper()[:8],
            checkout_currency=str(item.checkout_currency or "").strip().upper()[:8],
            checkout_amount_minor=str(item.checkout_amount_minor or "").strip()[:32],
            paypal_available=bool(item.paypal_available),
            checkout_evidence_json=json.dumps(
                evidence[:20], ensure_ascii=False, separators=(",", ":")
            ),
            plan_status=str(item.plan_status or "").strip().lower(),
            detail=str(item.detail or "").strip()[:1000],
            checked_at=str(item.checked_at or "").strip() or utc_now(),
            registration_ip=str(item.registration_ip or "").strip()[:128],
            registration_country=str(item.registration_country or "")
            .strip()
            .upper()[:8],
            registration_proxy_mode=str(item.registration_proxy_mode or "")
            .strip()
            .lower()[:32],
        )
        if not normalized.email:
            raise ValueError("优惠池邮箱不能为空")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO offer_accounts(
                    email, status, pool, eligible, checkout_submitted,
                    checkout_url, checkout_country, checkout_currency,
                    checkout_amount_minor, paypal_available,
                    checkout_evidence_json, plan_status, detail, checked_at
                    , registration_ip, registration_country,
                    registration_proxy_mode
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    status=excluded.status,
                    pool=excluded.pool,
                    eligible=excluded.eligible,
                    checkout_submitted=excluded.checkout_submitted,
                    checkout_url=excluded.checkout_url,
                    checkout_country=excluded.checkout_country,
                    checkout_currency=excluded.checkout_currency,
                    checkout_amount_minor=excluded.checkout_amount_minor,
                    paypal_available=excluded.paypal_available,
                    checkout_evidence_json=excluded.checkout_evidence_json,
                    plan_status=excluded.plan_status,
                    detail=excluded.detail,
                    checked_at=excluded.checked_at,
                    registration_ip=excluded.registration_ip,
                    registration_country=excluded.registration_country,
                    registration_proxy_mode=excluded.registration_proxy_mode
                """,
                (
                    normalized.email,
                    normalized.status,
                    normalized.pool,
                    int(normalized.eligible),
                    int(normalized.checkout_submitted),
                    normalized.checkout_url,
                    normalized.checkout_country,
                    normalized.checkout_currency,
                    normalized.checkout_amount_minor,
                    int(normalized.paypal_available),
                    normalized.checkout_evidence_json,
                    normalized.plan_status,
                    normalized.detail,
                    normalized.checked_at,
                    normalized.registration_ip,
                    normalized.registration_country,
                    normalized.registration_proxy_mode,
                ),
            )
        return normalized

    def get(self, email: str) -> OfferAccount | None:
        target = str(email or "").strip().lower()
        if not target:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT email, status, pool, eligible, checkout_submitted,
                       checkout_url, checkout_country, checkout_currency,
                       checkout_amount_minor, paypal_available,
                       checkout_evidence_json, plan_status, detail, checked_at,
                       registration_ip, registration_country,
                       registration_proxy_mode
                FROM offer_accounts WHERE email = ? COLLATE NOCASE
                """,
                (target,),
            ).fetchone()
        if row is None:
            return None
        return OfferAccount(
            email=str(row["email"]),
            status=str(row["status"]),
            pool=str(row["pool"]),
            eligible=bool(row["eligible"]),
            checkout_submitted=bool(row["checkout_submitted"]),
            checkout_url=str(row["checkout_url"]),
            checkout_country=str(row["checkout_country"]),
            checkout_currency=str(row["checkout_currency"]),
            checkout_amount_minor=str(row["checkout_amount_minor"]),
            paypal_available=bool(row["paypal_available"]),
            checkout_evidence_json=str(row["checkout_evidence_json"]),
            plan_status=str(row["plan_status"]),
            detail=str(row["detail"]),
            checked_at=str(row["checked_at"]),
            registration_ip=str(row["registration_ip"]),
            registration_country=str(row["registration_country"]),
            registration_proxy_mode=str(row["registration_proxy_mode"]),
        )

    def snapshot(self, *, pool: str = "", limit: int = 100) -> dict[str, Any]:
        selected_pool = str(pool or "").strip().lower()
        if selected_pool not in {"", "all", OFFER_POOL, NO_OFFER_POOL, "pending"}:
            raise ValueError("优惠池筛选无效")
        where = ""
        params: list[Any] = []
        if selected_pool in {OFFER_POOL, NO_OFFER_POOL}:
            where = "WHERE pool = ?"
            params.append(selected_pool)
        elif selected_pool == "pending":
            where = "WHERE pool = ''"
        params.append(max(1, min(500, int(limit))))
        with self._connect() as conn:
            counts = {
                str(row["pool"] or ""): int(row["count"] or 0)
                for row in conn.execute(
                    "SELECT pool, COUNT(*) AS count FROM offer_accounts GROUP BY pool"
                ).fetchall()
            }
            rows = conn.execute(
                f"""
                SELECT email, status, pool, eligible, checkout_submitted,
                       checkout_url, checkout_country, checkout_currency,
                       checkout_amount_minor, paypal_available,
                       checkout_evidence_json, plan_status, detail, checked_at,
                       registration_ip, registration_country,
                       registration_proxy_mode
                FROM offer_accounts {where}
                ORDER BY checked_at DESC LIMIT ?
                """,
                params,
            ).fetchall()
        items = []
        for row in rows:
            try:
                evidence = json.loads(str(row["checkout_evidence_json"] or "[]"))
            except (json.JSONDecodeError, TypeError, ValueError):
                evidence = []
            items.append({
                "email": str(row["email"]),
                "status": str(row["status"]),
                "pool": str(row["pool"]),
                "eligible": bool(row["eligible"]),
                "checkoutSubmitted": bool(row["checkout_submitted"]),
                "checkoutUrl": str(row["checkout_url"]),
                "checkoutCountry": str(row["checkout_country"]),
                "checkoutCurrency": str(row["checkout_currency"]),
                "checkoutAmountMinor": str(row["checkout_amount_minor"]),
                "paypalAvailable": bool(row["paypal_available"]),
                "checkoutEvidence": evidence if isinstance(evidence, list) else [],
                "planStatus": str(row["plan_status"]),
                "detail": str(row["detail"]),
                "checkedAt": str(row["checked_at"]),
                "registrationIp": str(row["registration_ip"]),
                "registrationCountry": str(row["registration_country"]),
                "registrationProxyMode": str(row["registration_proxy_mode"]),
            })
        return {
            "offerCount": counts.get(OFFER_POOL, 0),
            "noOfferCount": counts.get(NO_OFFER_POOL, 0),
            "pendingCount": counts.get("", 0),
            "total": sum(counts.values()),
            "items": items,
        }


class SharedAccountRepository:
    """Read/write account records without running shared-database migrations."""

    def __init__(self, db_file: Path) -> None:
        self.db_file = Path(db_file)

    def _connect(self, busy_timeout_ms: int = 30000) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_file,
            timeout=max(0.1, busy_timeout_ms / 1000),
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        return connection

    @staticmethod
    def _retry_locked(error: sqlite3.OperationalError, attempt: int) -> bool:
        return "locked" in str(error).lower() and attempt < 2

    def load(self, email: str) -> dict[str, Any]:
        target = str(email or "").strip().lower()
        for attempt in range(3):
            try:
                with self._connect(2000) as connection:
                    row = connection.execute(
                        "SELECT value FROM settings WHERE key = ?",
                        (f"gpt_account:{target}",),
                    ).fetchone()
                if row is None:
                    return {}
                try:
                    payload = json.loads(str(row["value"] or ""))
                except (json.JSONDecodeError, TypeError, ValueError):
                    return {}
                return payload if isinstance(payload, dict) else {}
            except sqlite3.OperationalError as error:
                if not self._retry_locked(error, attempt):
                    raise
                time.sleep(0.25 * (attempt + 1))
        return {}

    def save(self, email: str, record: dict[str, Any]) -> None:
        target = str(email or "").strip().lower()
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        for attempt in range(3):
            try:
                with self._connect(2000) as connection:
                    connection.execute(
                        """
                        INSERT INTO settings(key, value) VALUES (?, ?)
                        ON CONFLICT(key) DO UPDATE SET value=excluded.value
                        """,
                        (f"gpt_account:{target}", encoded),
                    )
                return
            except sqlite3.OperationalError as error:
                if not self._retry_locked(error, attempt):
                    raise
                time.sleep(0.25 * (attempt + 1))


class RegistrationRunRepository:
    """Repository that keeps the current and recent server task states."""

    def __init__(self, db_file: Path) -> None:
        self.db_file = Path(db_file)
        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS registration_runs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    requested INTEGER NOT NULL DEFAULT 0,
                    acquired INTEGER NOT NULL DEFAULT 0,
                    concurrency INTEGER NOT NULL DEFAULT 1,
                    use_kookeey INTEGER NOT NULL DEFAULT 0,
                    registration_country TEXT NOT NULL DEFAULT '',
                    state_json TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_registration_runs_updated
                ON registration_runs(updated_at DESC)
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_file, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def save(self, state: dict[str, Any]) -> None:
        run_id = str(state.get("id") or "").strip()
        if not run_id:
            return
        updated_at = utc_now()
        encoded = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO registration_runs(
                    id, status, requested, acquired, concurrency, use_kookeey,
                    registration_country, state_json, started_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    requested=excluded.requested,
                    acquired=excluded.acquired,
                    concurrency=excluded.concurrency,
                    use_kookeey=excluded.use_kookeey,
                    registration_country=excluded.registration_country,
                    state_json=excluded.state_json,
                    started_at=excluded.started_at,
                    updated_at=excluded.updated_at
                """,
                (
                    run_id,
                    str(state.get("status") or "idle"),
                    int(state.get("requested") or 0),
                    int(state.get("acquired") or 0),
                    int(state.get("concurrency") or 1),
                    int(bool(state.get("useRegistrationKookeey"))),
                    str(state.get("registrationCountry") or "").upper(),
                    encoded,
                    str(state.get("startedAt") or ""),
                    updated_at,
                ),
            )

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT state_json, updated_at FROM registration_runs
                ORDER BY updated_at DESC LIMIT ?
                """,
                (max(1, min(100, int(limit))),),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                state = json.loads(str(row["state_json"] or ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(state, dict):
                result.append({**state, "persistedAt": str(row["updated_at"])})
        return result

    def latest(self) -> dict[str, Any] | None:
        values = self.recent(limit=1)
        return values[0] if values else None


class AccountExportRepository:
    """Read registered account information for authenticated local clients."""

    def __init__(self, shared_db: Path, offer_repository: OfferPoolRepository) -> None:
        self.shared_db = Path(shared_db)
        self.offer_repository = offer_repository
        self.account_repository = SharedAccountRepository(self.shared_db)

    def export(
        self,
        *,
        pool: str = "",
        limit: int = 100,
        include_credentials: bool = False,
    ) -> dict[str, Any]:
        snapshot = self.offer_repository.snapshot(pool=pool, limit=limit)
        accounts = []
        for offer in snapshot["items"]:
            record = self.account_repository.load(offer["email"])
            session = account_session(record)
            registration = record.get("registration_environment")
            registration = registration if isinstance(registration, dict) else {}
            two_factor = record.get("two_factor")
            two_factor = two_factor if isinstance(two_factor, dict) else {}
            item = {
                "email": offer["email"],
                "offer": offer,
                "accountType": str(record.get("account_type") or ""),
                "hasSession": bool(session and account_session_access_token(record)),
                "hasPassword": bool(record.get("password")),
                "hasTwoFactor": bool(two_factor.get("enabled")),
                "updatedAt": str(record.get("updated_at") or ""),
                "registration": {
                    "proxyMode": str(
                        registration.get("proxy_mode")
                        or offer.get("registrationProxyMode")
                        or ""
                    ),
                    "country": str(
                        registration.get("exit_country")
                        or registration.get("proxy_country")
                        or offer.get("registrationCountry")
                        or ""
                    ),
                    "ip": str(
                        registration.get("exit_ip")
                        or offer.get("registrationIp")
                        or ""
                    ),
                    "capturedAt": str(registration.get("captured_at") or ""),
                },
            }
            if include_credentials:
                item["credentials"] = {
                    "password": str(record.get("password") or ""),
                    "totpSecret": str(two_factor.get("secret") or ""),
                    "accessToken": account_session_access_token(record),
                    "sessionToken": str(
                        session.get("sessionToken")
                        or session.get("session_token")
                        or ""
                    ),
                    "cookies": account_saved_cookies(record),
                    "session": session,
                }
            accounts.append(item)
        return {**{key: value for key, value in snapshot.items() if key != "items"}, "accounts": accounts}
