from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping


DEFAULT_RELIABILITY_TARGET_PERCENT = 98.0
DEFAULT_RELIABILITY_MIN_SAMPLE_SIZE = 100


@dataclass(frozen=True, slots=True)
class RegistrationRetryContext:
    error: str
    stage: str = ""
    return_code: int = 0
    retry_count: int = 0
    registration_chain: Mapping[str, Any] | None = None
    page_state: Mapping[str, Any] | None = None
    manual_otp_entry: bool = False
    result_received: bool = False
    two_factor_enrolled: bool = False

    @property
    def chain(self) -> Mapping[str, Any]:
        return self.registration_chain or {}

    @property
    def page(self) -> Mapping[str, Any]:
        return self.page_state or {}


@dataclass(frozen=True, slots=True)
class RegistrationRetryDecision:
    retryable: bool
    reason_code: str
    delay_seconds: float = 0.0
    rotate_proxy: bool = False
    explanation: str = ""


@dataclass(frozen=True, slots=True)
class RegistrationReliabilityReport:
    succeeded: int
    failed: int
    sample_size: int
    success_rate_percent: float
    target_percent: float
    minimum_sample_size: int
    gate: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "succeeded": self.succeeded,
            "failed": self.failed,
            "sampleSize": self.sample_size,
            "successRatePercent": self.success_rate_percent,
            "targetPercent": self.target_percent,
            "minimumSampleSize": self.minimum_sample_size,
            "gate": self.gate,
        }


def build_reliability_report(
    succeeded: int,
    failed: int,
    *,
    target_percent: float = DEFAULT_RELIABILITY_TARGET_PERCENT,
    minimum_sample_size: int = DEFAULT_RELIABILITY_MIN_SAMPLE_SIZE,
) -> RegistrationReliabilityReport:
    safe_succeeded = max(0, int(succeeded))
    safe_failed = max(0, int(failed))
    sample_size = safe_succeeded + safe_failed
    rate = (
        round(safe_succeeded * 100.0 / sample_size, 2)
        if sample_size
        else 0.0
    )
    safe_target = max(0.0, min(100.0, float(target_percent)))
    safe_minimum = max(1, int(minimum_sample_size))
    gate = (
        "collecting"
        if sample_size < safe_minimum
        else "pass"
        if rate >= safe_target
        else "fail"
    )
    return RegistrationReliabilityReport(
        succeeded=safe_succeeded,
        failed=safe_failed,
        sample_size=sample_size,
        success_rate_percent=rate,
        target_percent=safe_target,
        minimum_sample_size=safe_minimum,
        gate=gate,
    )


class RegistrationRetryPolicy:
    """Conservative Strategy for replaying isolated browser workers.

    The policy retries transport/runtime failures only. It deliberately avoids
    replaying manual challenges, rejected credentials, OTP decisions, or a flow
    that already reported complete registration.
    """

    _TERMINAL_PATTERNS = (
        re.compile(
            r"(?:invalid|incorrect|rejected|denied).{0,36}(?:password|otp|code|credential)",
            re.I | re.S,
        ),
        re.compile(
            r"(?:验证码|动态码|密码).{0,24}(?:错误|无效|拒绝|已使用|过期)",
            re.I | re.S,
        ),
        re.compile(
            r"(?:未确认密码|未确认\s*totp|要求已有\s*2fa|本地未保存可用的\s*totp)",
            re.I,
        ),
        re.compile(
            r"(?:security challenge|captcha|人工|手动|安全验证)",
            re.I,
        ),
        re.compile(
            r"(?:inventory|库存|proxy|代理).{0,48}(?:empty|auth|token|unavailable|失败|为空|认证|令牌|不可用)",
            re.I | re.S,
        ),
        re.compile(r"(?:http\s*)?(?:400|401|403|409|422|429)\b", re.I),
    )
    _BROWSER_CLOSED = re.compile(
        r"(?:target|page|context|browser|api\s*request\s*context).{0,56}"
        r"(?:has been |was |is )?(?:closed|disconnected|crashed)|"
        r"(?:浏览器|页面|上下文).{0,24}(?:已关闭|被关闭|断开|崩溃)",
        re.I | re.S,
    )
    _NETWORK_INTERRUPTED = re.compile(
        r"(?:econn(?:reset|aborted)|broken pipe|"
        r"err_connection_(?:closed|reset|aborted|timed_out)|"
        r"err_network_changed|err_name_not_resolved|ns_error_net_reset|failed to fetch|"
        r"networkerror when attempting to fetch|connection reset|"
        r"connection closed|remote protocol error|server disconnected|"
        r"连接被关闭|连接重置|网络连接中断)",
        re.I | re.S,
    )
    _TEMPORARY_CAPACITY = re.compile(
        r"(?:\beagain\b|\benomem\b|\bemfile\b|temporarily unavailable|"
        r"temporary resource|insufficient system resources|系统资源不足|"
        r"临时资源不足|(?:http\s*)?(?:408|502|503|504)\b)",
        re.I | re.S,
    )
    _NAVIGATION_STALLED = re.compile(
        r"(?:page\.goto|navigation).{0,80}(?:timeout|timed out|interrupted)|"
        r"(?:首页邮箱弹窗|免费注册|邮箱已输入并点击继续|邮箱已提交).{0,100}"
        r"(?:没有变化|未完成变化|点击失败|未找到|超时)|"
        r"(?:未找到继续/创建账号按钮|首页尚未完成加载)",
        re.I | re.S,
    )
    _WORKER_EXIT = re.compile(
        r"(?:worker|工作器|工作进程|子进程).{0,40}(?:exit|exited|退出|终止)|"
        r"(?:exit code|退出码)",
        re.I | re.S,
    )
    _CHAIN_ORDER = (
        "site_requested",
        "site_loaded",
        "registration_clicked",
        "registration_entry_ready",
        "email_entered",
        "email_submitted",
        "email_responded",
        "verification_page",
        "verification_requested",
        "verification_code_received",
        "verification_code_entered",
        "verification_submitted",
        "registration_created",
        "profile_verified",
        "profile_submitted",
        "session_ready",
        "password_confirmed",
        "two_factor_enabled",
        "complete",
    )
    _SAFE_PAGE_STAGES = {"", "browser", "openai_auth", "running"}
    _UNSAFE_PAGE_STAGES = {
        "email_verification",
        "password",
        "profile",
        "session",
        "two_factor",
        "completed",
        "security",
        "google_oauth",
        "manual",
    }

    def __init__(
        self,
        *,
        max_retries: int = 2,
        retry_delays: tuple[float, ...] = (0.5, 1.5),
    ) -> None:
        self.max_retries = max(0, min(3, int(max_retries)))
        cleaned = tuple(max(0.0, float(delay)) for delay in retry_delays)
        self.retry_delays = cleaned or (0.0,)

    def _delay(self, retry_count: int) -> float:
        index = max(0, min(int(retry_count), len(self.retry_delays) - 1))
        return self.retry_delays[index]

    @classmethod
    def _furthest_chain_index(cls, chain: Mapping[str, Any]) -> int:
        active_codes: set[str] = set()
        current_code = str(chain.get("currentCode") or "").strip().casefold()
        if current_code:
            active_codes.add(current_code)
        completed_codes = chain.get("completedCodes")
        if isinstance(completed_codes, (list, tuple)):
            active_codes.update(
                str(code or "").strip().casefold() for code in completed_codes
            )
        steps = chain.get("steps")
        if isinstance(steps, (list, tuple)):
            for step in steps:
                if not isinstance(step, Mapping):
                    continue
                status = str(step.get("status") or "").strip().casefold()
                if status not in {"running", "completed", "skipped"}:
                    continue
                active_codes.add(
                    str(step.get("code") or "").strip().casefold()
                )
        indexes = [
            cls._CHAIN_ORDER.index(code)
            for code in active_codes
            if code in cls._CHAIN_ORDER
        ]
        return max(indexes, default=-1)

    def decide(
        self, context: RegistrationRetryContext
    ) -> RegistrationRetryDecision:
        error = str(context.error or "").strip()
        stage = str(context.stage or context.page.get("stage") or "").casefold()
        chain = context.chain
        if context.retry_count >= self.max_retries:
            return RegistrationRetryDecision(
                False,
                "retry_limit",
                explanation="瞬时失败重试次数已用尽",
            )
        if context.manual_otp_entry or stage in {"security", "manual"}:
            return RegistrationRetryDecision(
                False,
                "manual_intervention",
                explanation="当前步骤需要人工继续，不自动重放",
            )
        if context.result_received or context.two_factor_enrolled:
            return RegistrationRetryDecision(
                False,
                "durable_result_received",
                explanation="工作器已产生账号或 2FA 持久结果，不重放注册请求",
            )
        if any(
            bool(chain.get(field))
            for field in (
                "registrationCreated",
                "sessionReady",
                "passwordConfirmed",
                "twoFactorEnabled",
                "fullRegistrationComplete",
            )
        ):
            return RegistrationRetryDecision(
                False,
                "registration_checkpoint_reached",
                explanation="注册已越过不可安全重放的提交点",
            )
        if stage in self._UNSAFE_PAGE_STAGES:
            return RegistrationRetryDecision(
                False,
                "unsafe_page_stage",
                explanation="当前页面可能已经产生不可重复的注册提交",
            )
        verification_index = self._CHAIN_ORDER.index("verification_page")
        if self._furthest_chain_index(chain) >= verification_index:
            return RegistrationRetryDecision(
                False,
                "unsafe_replay_boundary",
                explanation="已进入验证码页，不重启整个注册工作器",
            )
        if stage not in self._SAFE_PAGE_STAGES:
            return RegistrationRetryDecision(
                False,
                "unknown_page_stage",
                explanation="页面阶段未被证明可安全重放",
            )
        if any(pattern.search(error) for pattern in self._TERMINAL_PATTERNS):
            return RegistrationRetryDecision(
                False,
                "terminal_failure",
                explanation="失败属于凭据、验证码、人工验证或业务终态",
            )

        common = {
            "delay_seconds": self._delay(context.retry_count),
            # A retry is still the same registration attempt. Reusing the sticky
            # route avoids an account changing country/IP half way through auth.
            "rotate_proxy": False,
        }
        if self._TEMPORARY_CAPACITY.search(error):
            return RegistrationRetryDecision(
                True,
                "temporary_capacity",
                explanation="运行环境或上游容量出现短暂故障",
                **common,
            )
        if self._BROWSER_CLOSED.search(error):
            return RegistrationRetryDecision(
                True,
                "browser_closed",
                explanation="浏览器上下文意外关闭，启动全新隔离进程恢复",
                **common,
            )
        if self._NETWORK_INTERRUPTED.search(error):
            return RegistrationRetryDecision(
                True,
                "network_interrupted",
                explanation="网络传输中断，启动全新隔离进程恢复",
                **common,
            )
        if self._NAVIGATION_STALLED.search(error):
            return RegistrationRetryDecision(
                True,
                "navigation_stalled",
                explanation="认证页面未推进，重新建立一次干净页面",
                **common,
            )
        if self._WORKER_EXIT.search(error):
            return RegistrationRetryDecision(
                True,
                "worker_exit",
                explanation="工作进程异常退出，启动全新隔离进程恢复",
                **common,
            )
        return RegistrationRetryDecision(
            False,
            "not_retryable",
            explanation="失败不属于已验证可安全重试的瞬时类别",
        )


__all__ = [
    "DEFAULT_RELIABILITY_MIN_SAMPLE_SIZE",
    "DEFAULT_RELIABILITY_TARGET_PERCENT",
    "RegistrationReliabilityReport",
    "RegistrationRetryContext",
    "RegistrationRetryDecision",
    "RegistrationRetryPolicy",
    "build_reliability_report",
]
