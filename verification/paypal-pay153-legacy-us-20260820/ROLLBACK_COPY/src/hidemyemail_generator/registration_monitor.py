from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, Mapping, Protocol

from .inbox import connect_db


REGISTRATION_PROCESS_FAILURE_PREFIX = "registration_process_failure:"
DEFAULT_JSONL_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_JSONL_BACKUPS = 3
MAX_PERSISTED_LOGS = 80
MAX_NESTED_ITEMS = 200
REDACTED = "[REDACTED]"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp_sort_key(value: Any) -> tuple[float, str]:
    text = str(value or "").strip()
    if not text:
        return (float("-inf"), "")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (parsed.astimezone(timezone.utc).timestamp(), text)
    except (OverflowError, TypeError, ValueError):
        return (float("-inf"), text)


@dataclass(frozen=True, slots=True)
class FailureDecision:
    reason_code: str
    category: str
    failure_reason: str
    suggested_action: str
    retryable: bool
    failed_stage: str


class FailureClassificationStrategy(Protocol):
    def classify(self, task: Mapping[str, Any]) -> FailureDecision: ...


@dataclass(frozen=True, slots=True)
class _FailureProfile:
    category: str
    default_reason: str
    suggested_action: str
    retryable: bool
    failed_stage: str


class RegistrationFailureClassifier:
    """Strategy that turns volatile worker messages into stable failure codes."""

    _PROFILES: dict[str, _FailureProfile] = {
        "inventory_auth": _FailureProfile(
            "inventory",
            "邮箱库存认证失败",
            "检查库存服务账号、访问令牌和本机时间后重试。",
            False,
            "inventory",
        ),
        "inventory_empty": _FailureProfile(
            "inventory",
            "邮箱库存没有可用地址",
            "补充可用邮箱库存或释放占用中的邮箱后重试。",
            True,
            "inventory",
        ),
        "inventory_network": _FailureProfile(
            "network",
            "邮箱库存服务网络异常",
            "检查库存服务地址、TLS 和网络连通性后重试。",
            True,
            "inventory",
        ),
        "proxy_unavailable": _FailureProfile(
            "network",
            "注册代理不可用",
            "检测或更换注册代理出口后重试。",
            True,
            "proxy",
        ),
        "network_error": _FailureProfile(
            "network",
            "注册网络连接异常",
            "检查本机网络、DNS、TLS 和目标站点连通性后重试。",
            True,
            "network",
        ),
        "resource_exhausted": _FailureProfile(
            "capacity",
            "上游资源、额度或并发已耗尽",
            "等待限流恢复、补充额度或降低并发后重试。",
            True,
            "resource",
        ),
        "email_verification": _FailureProfile(
            "verification",
            "邮箱验证码步骤失败",
            "确认验证码邮箱可收信，并使用本轮最新验证码重试。",
            True,
            "email_verification",
        ),
        "password": _FailureProfile(
            "authentication",
            "密码设置或校验失败",
            "检查密码规则和当前认证页面后重试。",
            True,
            "password",
        ),
        "two_factor": _FailureProfile(
            "security",
            "两步验证配置失败",
            "检查 TOTP 页面状态和系统时间后继续或重试。",
            True,
            "two_factor",
        ),
        "session": _FailureProfile(
            "authentication",
            "登录 Session 获取或校验失败",
            "重新登录并获取新的 Session 后重试。",
            True,
            "session",
        ),
        "browser_closed": _FailureProfile(
            "browser",
            "浏览器或页面意外关闭",
            "重新启动浏览器注册进程，并检查浏览器稳定性。",
            True,
            "browser",
        ),
        "page_navigation": _FailureProfile(
            "browser",
            "页面导航或控件识别失败",
            "检查失败页面和选择器日志，更新页面识别规则后重试。",
            True,
            "page_navigation",
        ),
        "worker_exit": _FailureProfile(
            "runtime",
            "注册工作进程异常退出",
            "检查工作进程退出码和末尾日志后重新启动。",
            True,
            "worker",
        ),
        "protocol_auth_failed": _FailureProfile(
            "protocol",
            "Mail Auth 协议注册失败",
            "检查协议响应、邮箱验证步骤和末尾事件日志后重试。",
            True,
            "protocol_auth",
        ),
        "unknown": _FailureProfile(
            "unknown",
            "未识别的注册失败",
            "查看末尾日志和失败页面信息，补充稳定的失败分类规则。",
            False,
            "unknown",
        ),
    }

    _RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
        (
            "inventory_auth",
            re.compile(
                r"(?:inventory|库存).{0,50}(?:auth|login|token|unauthori[sz]ed|forbidden|401|403|认证|鉴权|登录|令牌|凭据)",
                re.I | re.S,
            ),
        ),
        (
            "inventory_empty",
            re.compile(
                r"(?:inventory\s+(?:is\s+)?empty|no\s+(?:available\s+)?(?:inventory\s+)?(?:email|address)|库存.{0,24}(?:为空|空了|没有|无可用)|没有可(?:协议)?注册|无可用邮箱)",
                re.I | re.S,
            ),
        ),
        (
            "inventory_network",
            re.compile(
                r"(?:(?:inventory|库存).{0,60}(?:network|connect|timeout|timed out|tls|dns|https|request failed|网络|连接|超时|证书|请求失败)|(?:network|connect|timeout|tls|dns|网络|连接|超时|证书).{0,60}(?:inventory|库存))",
                re.I | re.S,
            ),
        ),
        (
            "proxy_unavailable",
            re.compile(
                r"(?:proxy|代理|tunnel).{0,60}(?:unavailable|failed|error|refused|timeout|closed|不可用|失败|错误|拒绝|超时|关闭)|(?:无法连接|连接失败).{0,30}(?:proxy|代理)",
                re.I | re.S,
            ),
        ),
        (
            "resource_exhausted",
            re.compile(
                r"(?:resource exhausted|quota|rate.?limit|too many requests|http\s*429|\b429\b|out of memory|memory usage|额度不足|余额不足|资源耗尽|频率限制|请求过多|内存.{0,20}(?:不足|超出|过高)|并发.{0,12}(?:上限|已满)|号段用尽)",
                re.I | re.S,
            ),
        ),
        (
            "network_error",
            re.compile(
                r"(?:err_(?:connection|network|name|timed)|connection (?:closed|reset|refused)|connect(?:ion)? timed out|net::|dns|tls handshake|网络连接|连接被关闭|连接重置)",
                re.I | re.S,
            ),
        ),
        (
            "browser_closed",
            re.compile(
                r"(?:browser|page|target|context).{0,36}(?:has been )?closed|(?:browser|page).{0,24}(?:crash|disconnected)|浏览器.{0,20}(?:关闭|崩溃|断开)|页面.{0,20}(?:关闭|已关闭)",
                re.I | re.S,
            ),
        ),
        (
            "worker_exit",
            re.compile(
                r"(?:worker|subprocess|child process|工作进程|子进程).{0,50}(?:exit|exited|terminated|crash|退出|终止|崩溃)|(?:broken pipe|exit code|退出码)",
                re.I | re.S,
            ),
        ),
        (
            "two_factor",
            re.compile(
                r"(?:\b2fa\b|\btotp\b|two.?factor|two step|两步验证|双重验证|二次验证)",
                re.I,
            ),
        ),
        (
            "email_verification",
            re.compile(
                r"(?:email.{0,24}(?:verification|verify|otp|code)|verification.{0,16}(?:email|code)|\botp\b|验证码|邮箱验证|邮件验证)",
                re.I | re.S,
            ),
        ),
        (
            "password",
            re.compile(r"(?:password|passcode|密码)", re.I),
        ),
        (
            "session",
            re.compile(
                r"(?:session|oauth.{0,16}(?:callback|state)|登录态|会话|获取.{0,12}session|请先登录)",
                re.I | re.S,
            ),
        ),
        (
            "page_navigation",
            re.compile(
                r"(?:navigation|navigate|redirect|goto|selector|locator|element not found|页面.{0,30}(?:导航|跳转|加载|未变化|没有变化|无响应|仍停留|未严格识别)|找不到.{0,16}(?:控件|元素|按钮|输入框)|(?:按钮|控件).{0,16}(?:点击失败|未找到|不存在)|注册步骤顺序错误)",
                re.I | re.S,
            ),
        ),
        (
            "protocol_auth_failed",
            re.compile(
                r"(?:mail auth|协议注册|protocol auth).{0,80}(?:failed|error|reject|失败|错误|拒绝)|(?:failed|error|reject|失败|错误|拒绝).{0,80}(?:mail auth|协议注册|protocol auth)",
                re.I | re.S,
            ),
        ),
    )

    _STAGE_REASON_CODES = {
        "email_verification": "email_verification",
        "verification": "email_verification",
        "password": "password",
        "two_factor": "two_factor",
        "totp": "two_factor",
        "session": "session",
        "proxy": "proxy_unavailable",
        "network": "network_error",
        "page_navigation": "page_navigation",
        "protocol_auth": "protocol_auth_failed",
    }

    @classmethod
    def profile(cls, reason_code: str) -> _FailureProfile:
        return cls._PROFILES.get(
            str(reason_code or "").strip(), cls._PROFILES["unknown"]
        )

    @staticmethod
    def _failed_account(task: Mapping[str, Any]) -> Mapping[str, Any]:
        accounts = task.get("failedAccounts")
        if not isinstance(accounts, (list, tuple)):
            return {}
        return next(
            (item for item in accounts if isinstance(item, Mapping)),
            {},
        )

    @classmethod
    def _explicit_reason_code(cls, task: Mapping[str, Any]) -> str:
        direct = str(task.get("reasonCode") or "").strip()
        if direct:
            return direct
        account = cls._failed_account(task)
        return str(
            account.get("terminalReasonCode") or account.get("lastRetryCode") or ""
        ).strip()

    @classmethod
    def _search_text(cls, task: Mapping[str, Any]) -> str:
        pieces: list[str] = []
        for key in (
            "failureReason",
            "message",
            "currentLocation",
            "currentAction",
        ):
            value = task.get(key)
            if value is not None:
                pieces.append(str(value))
        account = cls._failed_account(task)
        for key in ("terminalRetryDecision", "message", "latestLog"):
            value = account.get(key)
            if value is not None:
                pieces.append(str(value))
        logs = task.get("logs")
        if isinstance(logs, (list, tuple)):
            recent = list(logs)[-MAX_PERSISTED_LOGS:]
            failed_logs = [
                item
                for item in recent
                if isinstance(item, Mapping)
                and str(item.get("status") or "").casefold()
                in {"error", "failed", "fatal", "warning"}
            ]
            ordered_logs = failed_logs or [
                item for item in recent[-8:] if isinstance(item, Mapping)
            ]
            for item in reversed(ordered_logs):
                value = item.get("message")
                if value is not None:
                    pieces.append(str(value))
        return "\n".join(pieces)

    def classify(self, task: Mapping[str, Any]) -> FailureDecision:
        explicit_reason_code = self._explicit_reason_code(task)
        stage = (
            str(
                task.get("failedStage")
                or task.get("currentStage")
                or task.get("phase")
                or ""
            )
            .strip()
            .casefold()
        )
        reason_code = explicit_reason_code or self._STAGE_REASON_CODES.get(stage, "")
        if not reason_code:
            text = self._search_text(task)
            for candidate, pattern in self._RULES:
                if pattern.search(text):
                    reason_code = candidate
                    break
        reason_code = reason_code or "unknown"
        profile = self.profile(reason_code)
        account = self._failed_account(task)
        supplied_reason = str(
            task.get("failureReason")
            or account.get("terminalRetryDecision")
            or task.get("message")
            or ""
        ).strip()
        failed_stage = str(
            task.get("failedStage") or task.get("currentStage") or ""
        ).strip()
        if not failed_stage or failed_stage in {
            "failed",
            "running",
            "idle",
            "prepare",
        }:
            failed_stage = profile.failed_stage
        return FailureDecision(
            reason_code=reason_code,
            category=profile.category,
            failure_reason=supplied_reason or profile.default_reason,
            suggested_action=profile.suggested_action,
            retryable=profile.retryable,
            failed_stage=failed_stage,
        )


_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>[\"']?(?:password|passwd|pwd|access[_-]?token|refresh[_-]?token|id[_-]?token|token|cookie|authorization|auth[_-]?token|client[_-]?secret|secret|api[_-]?key|apikey|x[_-]?api[_-]?key|otp|totp|verification[_ -]?code)[\"']?\s*[:=]\s*)(?:Bearer\s+|Basic\s+)?(?P<value>\"[^\"]*\"|'[^']*'|[^\r\n,;&]+)",
    re.I,
)
_SENSITIVE_HEADER_RE = re.compile(
    r"(?im)(?P<prefix>\b(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key|api-key)\s*:\s*)(?P<value>[^\r\n]*)"
)
_AUTH_SCHEME_RE = re.compile(
    r"(?i)(?P<prefix>\b(?:bearer|basic)\s+)(?P<value>[A-Za-z0-9._~+/=-]{4,})"
)
_PROXY_CREDENTIAL_RE = re.compile(
    r"(?P<scheme>\b[a-z][a-z0-9+.-]*://)(?P<userinfo>[^/@\s]+@)", re.I
)
_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
)
_OTP_VALUE_RE = re.compile(
    r"(?P<label>\b(?:otp|totp|one[- ]time code|verification code)\b|验证码)(?P<separator>\s*(?:code)?\s*[:=]?\s*)(?P<value>\d{4,10})",
    re.I,
)
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SENSITIVE_KEY_PARTS = {
    "password",
    "passwd",
    "pwd",
    "token",
    "cookie",
    "authorization",
    "secret",
    "otp",
    "totp",
}
_SAFE_SENSITIVE_SUFFIXES = {
    "attempt",
    "attempts",
    "confirmed",
    "count",
    "enabled",
    "method",
    "required",
    "stage",
    "status",
}


def redact_text(value: Any, *, maximum: int = 4000) -> str:
    text = str(value or "")
    text = _SENSITIVE_HEADER_RE.sub(
        lambda match: f"{match.group('prefix')}{REDACTED}", text
    )
    text = _PROXY_CREDENTIAL_RE.sub(
        lambda match: f"{match.group('scheme')}{REDACTED}@", text
    )
    text = _AUTH_SCHEME_RE.sub(lambda match: f"{match.group('prefix')}{REDACTED}", text)
    text = _SENSITIVE_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group('prefix')}{REDACTED}", text
    )
    text = _OTP_VALUE_RE.sub(
        lambda match: f"{match.group('label')}{match.group('separator')}{REDACTED}",
        text,
    )
    text = _JWT_RE.sub(REDACTED, text)
    return text[: max(0, int(maximum))]


def _is_sensitive_key(key: Any) -> bool:
    expanded = _CAMEL_BOUNDARY_RE.sub("_", str(key or ""))
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", expanded.casefold()) if part]
    normalized = "_".join(parts)
    has_api_key = normalized in {"apikey", "api_key", "x_api_key"} or (
        "api" in parts and "key" in parts
    )
    if not has_api_key and not any(part in _SENSITIVE_KEY_PARTS for part in parts):
        return False
    return not any(part in _SAFE_SENSITIVE_SUFFIXES for part in parts[-1:])


def sanitize_value(value: Any, *, _depth: int = 0) -> Any:
    if _depth > 12:
        return "[TRUNCATED]"
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for raw_key, nested in list(value.items())[:MAX_NESTED_ITEMS]:
            key = str(raw_key)
            if _is_sensitive_key(key):
                continue
            sanitized[key] = sanitize_value(nested, _depth=_depth + 1)
        return sanitized
    if isinstance(value, (list, tuple)):
        return [
            sanitize_value(item, _depth=_depth + 1) for item in value[:MAX_NESTED_ITEMS]
        ]
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(value)


class RegistrationFailureRepository:
    """Repository for compatible SQLite settings rows and an audit JSONL file."""

    def __init__(
        self,
        db_file: str | Path,
        log_file: str | Path,
        *,
        jsonl_max_bytes: int = DEFAULT_JSONL_MAX_BYTES,
        jsonl_backups: int = DEFAULT_JSONL_BACKUPS,
        key_prefix: str = REGISTRATION_PROCESS_FAILURE_PREFIX,
    ) -> None:
        self.db_file = Path(db_file)
        self.log_file = Path(log_file)
        self.jsonl_max_bytes = max(1, int(jsonl_max_bytes))
        self.jsonl_backups = max(1, int(jsonl_backups))
        self.key_prefix = str(key_prefix)
        self._lock = threading.RLock()

    def upsert(self, record: Mapping[str, Any]) -> dict[str, Any]:
        saved = dict(record)
        process_id = str(saved.get("processId") or "").strip()
        if not process_id:
            raise ValueError("processId is required")
        payload = json.dumps(saved, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self.db_file.parent.mkdir(parents=True, exist_ok=True)
            connection = connect_db(str(self.db_file))
            try:
                connection.execute(
                    """
                    INSERT INTO settings(key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (f"{self.key_prefix}{process_id}", payload),
                )
                connection.commit()
            finally:
                connection.close()
            self._append_jsonl(payload)
        return saved

    def load_all(self) -> list[dict[str, Any]]:
        connection = connect_db(str(self.db_file))
        try:
            rows = connection.execute(
                """
                SELECT key, value
                FROM settings
                WHERE key LIKE ?
                ORDER BY rowid DESC
                """,
                (f"{self.key_prefix}%",),
            ).fetchall()
        finally:
            connection.close()
        records: list[dict[str, Any]] = []
        for row in rows:
            try:
                item = json.loads(str(row["value"] or ""))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if not isinstance(item, dict):
                continue
            if not str(item.get("processId") or "").strip():
                item["processId"] = str(row["key"])[len(self.key_prefix) :]
            records.append(item)
        return records

    def _append_jsonl(self, payload: str) -> None:
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        encoded_size = len((payload + "\n").encode("utf-8"))
        current_size = self.log_file.stat().st_size if self.log_file.exists() else 0
        if current_size and current_size + encoded_size > self.jsonl_max_bytes:
            self._rotate_jsonl()
        with self.log_file.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _rotate_jsonl(self) -> None:
        oldest = Path(f"{self.log_file}.{self.jsonl_backups}")
        if oldest.exists():
            oldest.unlink()
        for index in range(self.jsonl_backups - 1, 0, -1):
            source = Path(f"{self.log_file}.{index}")
            if source.exists():
                os.replace(source, Path(f"{self.log_file}.{index + 1}"))
        if self.log_file.exists():
            os.replace(self.log_file, Path(f"{self.log_file}.1"))


class RegistrationMonitorPresenter:
    """MVP presenter that freezes, classifies, sanitizes and exposes failures."""

    def __init__(
        self,
        repository: RegistrationFailureRepository,
        classifier: FailureClassificationStrategy | None = None,
    ) -> None:
        self.repository = repository
        self.classifier = classifier or RegistrationFailureClassifier()

    @classmethod
    def from_paths(cls, db_file: Path, log_file: Path) -> RegistrationMonitorPresenter:
        return cls(RegistrationFailureRepository(db_file, log_file))

    @staticmethod
    def _failure_context(task: Mapping[str, Any]) -> Mapping[str, Any]:
        context = task.get("failureContext")
        return context if isinstance(context, Mapping) else {}

    @staticmethod
    def _preferred(
        task: Mapping[str, Any],
        context: Mapping[str, Any],
        *names: str,
        default: Any = None,
    ) -> Any:
        for source in (context, task):
            for name in names:
                if name in source and source[name] is not None:
                    return source[name]
        return default

    def _build_record(
        self, task: Mapping[str, Any], *, legacy: bool = False
    ) -> dict[str, Any] | None:
        context = self._failure_context(task)
        task_status = str(task.get("status") or "").strip().lower()
        if task_status != "failed" and not legacy:
            return None
        process_id = str(
            self._preferred(task, context, "processId", "id", default="")
        ).strip()
        if not process_id:
            return None

        message = self._preferred(task, context, "message", default="注册失败")
        current_stage = self._preferred(
            task, context, "currentStage", "phase", default="failed"
        )
        current_location = self._preferred(
            task, context, "currentLocation", default="注册流程"
        )
        current_action = self._preferred(
            task, context, "currentAction", default="注册失败"
        )
        logs = self._preferred(task, context, "logs", default=[])
        failed_accounts = self._preferred(task, context, "failedAccounts", default=[])
        registration_chain = self._preferred(
            task, context, "registrationChain", default={}
        )
        page_state = self._preferred(task, context, "pageState", default={})
        primary_failed_account = (
            next(
                (item for item in failed_accounts if isinstance(item, Mapping)),
                {},
            )
            if isinstance(failed_accounts, (list, tuple))
            else {}
        )
        if not registration_chain and isinstance(primary_failed_account, Mapping):
            registration_chain = primary_failed_account.get("registrationChain") or {}
        if not page_state and isinstance(primary_failed_account, Mapping):
            page_state = primary_failed_account.get("pageState") or {}
        classification_source = {
            **dict(task),
            **dict(context),
            "message": message,
            "currentStage": current_stage,
            "currentLocation": current_location,
            "currentAction": current_action,
            "logs": logs,
            "failedAccounts": failed_accounts,
        }
        decision = self.classifier.classify(classification_source)

        explicit_reason_code = str(
            self._preferred(task, context, "reasonCode", default="")
        ).strip()
        reason_code = explicit_reason_code or decision.reason_code
        profile = RegistrationFailureClassifier.profile(reason_code)
        category = str(
            self._preferred(task, context, "category", default="")
        ).strip() or (
            decision.category if not explicit_reason_code else profile.category
        )
        failure_reason = self._preferred(
            task, context, "failureReason", default=decision.failure_reason
        )
        suggested_action = self._preferred(
            task,
            context,
            "suggestedAction",
            default=(
                decision.suggested_action
                if not explicit_reason_code
                else profile.suggested_action
            ),
        )
        retryable_value = self._preferred(task, context, "retryable", default=None)
        retryable = (
            retryable_value
            if isinstance(retryable_value, bool)
            else (decision.retryable if not explicit_reason_code else profile.retryable)
        )
        failed_stage = (
            str(self._preferred(task, context, "failedStage", default="")).strip()
            or decision.failed_stage
        )

        email = str(self._preferred(task, context, "email", default="")).strip().lower()
        raw_emails = self._preferred(task, context, "emails", default=[])
        attempted_emails = (
            [
                str(item or "").strip().lower()
                for item in raw_emails
                if str(item or "").strip()
            ]
            if isinstance(raw_emails, (list, tuple))
            else []
        )
        if email and email not in attempted_emails:
            attempted_emails.insert(0, email)
        raw_logs = list(logs) if isinstance(logs, (list, tuple)) else []
        failure_log_index = context.get("failureLogIndex")
        if isinstance(failure_log_index, int) and failure_log_index >= 0:
            raw_logs = raw_logs[: failure_log_index + 1]
        raw_failed_accounts = (
            list(failed_accounts) if isinstance(failed_accounts, (list, tuple)) else []
        )[:20]
        failed_emails = [
            str(account.get("email") or "").strip().lower()
            for account in raw_failed_accounts
            if isinstance(account, Mapping) and str(account.get("email") or "").strip()
        ]
        failed_emails = list(dict.fromkeys(failed_emails))
        if failed_emails:
            email = failed_emails[0]
            emails = failed_emails
        else:
            emails = list(attempted_emails)
        record = {
            "schemaVersion": 1,
            "processId": process_id,
            "status": "failed",
            "mode": redact_text(
                self._preferred(task, context, "mode", default="browser"), maximum=40
            ),
            "provider": redact_text(
                self._preferred(task, context, "provider", default=""), maximum=120
            ),
            "browserEngine": redact_text(
                self._preferred(task, context, "browserEngine", default=""),
                maximum=40,
            ),
            "email": email,
            "emails": emails,
            "attemptedEmails": attempted_emails,
            "message": redact_text(message),
            "currentStage": redact_text(current_stage, maximum=160),
            "currentLocation": redact_text(current_location, maximum=500),
            "currentAction": redact_text(current_action, maximum=1000),
            "startedAt": redact_text(
                self._preferred(task, context, "startedAt", default=""), maximum=80
            ),
            "finishedAt": redact_text(
                self._preferred(task, context, "finishedAt", default=""), maximum=80
            ),
            "recordedAt": redact_text(
                self._preferred(task, context, "recordedAt", default="") or _utc_now(),
                maximum=80,
            ),
            "logs": sanitize_value(raw_logs[-MAX_PERSISTED_LOGS:]),
            "failedAccounts": sanitize_value(raw_failed_accounts),
            "registrationChain": sanitize_value(registration_chain),
            "pageState": sanitize_value(page_state),
            "reasonCode": redact_text(reason_code, maximum=120) or "unknown",
            "category": redact_text(category, maximum=120) or "unknown",
            "failureReason": redact_text(failure_reason),
            "suggestedAction": redact_text(suggested_action),
            "retryable": bool(retryable),
            "failedStage": redact_text(failed_stage, maximum=160) or "unknown",
        }
        return record

    def record_failure(self, task: dict) -> dict[str, Any] | None:
        record = self._build_record(task)
        if record is None:
            return None
        return self.repository.upsert(record)

    def _normalize_legacy(self, item: Mapping[str, Any]) -> dict[str, Any] | None:
        legacy = dict(item)
        legacy.setdefault("status", "failed")
        record = self._build_record(legacy, legacy=True)
        if record is None:
            return None
        return record

    @staticmethod
    def _record_emails(record: Mapping[str, Any]) -> set[str]:
        emails = {
            str(record.get("email") or "").strip().lower(),
            *(
                str(item or "").strip().lower()
                for item in record.get("emails", [])
                if str(item or "").strip()
            ),
        }
        for account in record.get("failedAccounts", []):
            if isinstance(account, Mapping):
                target = str(account.get("email") or "").strip().lower()
                if target:
                    emails.add(target)
        emails.discard("")
        return emails

    def snapshot(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        email: str = "",
        reason_code: str = "",
        include_details: bool = True,
    ) -> dict[str, Any]:
        normalized = [
            record
            for item in self.repository.load_all()
            if (record := self._normalize_legacy(item)) is not None
        ]
        normalized.sort(
            key=lambda item: _timestamp_sort_key(
                item.get("recordedAt")
                or item.get("finishedAt")
                or item.get("startedAt")
            ),
            reverse=True,
        )
        target_email = str(email or "").strip().lower()
        target_reason = str(reason_code or "").strip().casefold()
        filtered = [
            record
            for record in normalized
            if (not target_email or target_email in self._record_emails(record))
            and (
                not target_reason
                or str(record.get("reasonCode") or "").casefold() == target_reason
            )
        ]
        total = len(filtered)
        safe_limit = max(1, min(200, int(limit)))
        safe_offset = max(0, int(offset))
        page = filtered[safe_offset : safe_offset + safe_limit]
        if not include_details:
            detail_fields = {
                "logs",
                "failedAccounts",
                "registrationChain",
                "pageState",
            }
            page = [
                {
                    **{
                        key: value
                        for key, value in record.items()
                        if key not in detail_fields
                    },
                    "logCount": len(record.get("logs") or []),
                    "failedAccountCount": len(record.get("failedAccounts") or []),
                }
                for record in page
            ]
        by_reason = Counter(
            str(item.get("reasonCode") or "unknown") for item in filtered
        )
        by_category = Counter(
            str(item.get("category") or "unknown") for item in filtered
        )
        summary = {
            "total": total,
            "returned": len(page),
            "retryable": sum(bool(item.get("retryable")) for item in filtered),
            "nonRetryable": sum(not bool(item.get("retryable")) for item in filtered),
            "byReason": dict(sorted(by_reason.items())),
            "byCategory": dict(sorted(by_category.items())),
        }
        return {
            "records": page,
            "total": total,
            "summary": summary,
            "logFile": str(self.repository.log_file.resolve()),
        }


# A descriptive alias lets integrations use either monitor- or failure-oriented names.
RegistrationFailurePresenter = RegistrationMonitorPresenter


__all__ = [
    "DEFAULT_JSONL_BACKUPS",
    "DEFAULT_JSONL_MAX_BYTES",
    "FailureClassificationStrategy",
    "FailureDecision",
    "MAX_PERSISTED_LOGS",
    "REDACTED",
    "REGISTRATION_PROCESS_FAILURE_PREFIX",
    "RegistrationFailureClassifier",
    "RegistrationFailurePresenter",
    "RegistrationFailureRepository",
    "RegistrationMonitorPresenter",
    "redact_text",
    "sanitize_value",
]
