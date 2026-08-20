from __future__ import annotations

import asyncio
from collections import deque
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from hidemyemail_generator import (
    card_link_bridge_service as bridge_service,
    card_link_runtime,
    openai_card_link_bridge,
)
from hidemyemail_generator.card_link_bridge_service import (
    CardLinkBridgeCommand,
    CardLinkBridgeProcessView,
    CardLinkBridgeResult,
    CardLinkBridgeServiceError,
    SharedCardLinkBridgePresenter,
    WORKER_MESSAGE_PREFIX,
    WORKER_PROTOCOL_VERSION,
)


class MemoryCardLinkBridgeProcessView:
    """Deterministic in-memory process adapter used to verify presenter policy."""

    def __init__(self, outcomes=()) -> None:
        self._outcomes = deque(outcomes)
        self.worker_pid: int | None = None
        self.spawn_count = 0
        self.start_attempts = 0
        self.start_calls = 0
        self.exchange_calls: list[dict[str, object]] = []
        self.close_calls = 0
        self.abort_calls = 0
        self.active_exchanges = 0
        self.max_active_exchanges = 0

    async def start(self) -> None:
        self.start_attempts += 1
        if self.worker_pid is None:
            self.start_calls += 1
            self.spawn_count += 1
            self.worker_pid = 43000 + self.spawn_count

    async def exchange(
        self,
        request_id: str,
        payload: dict[str, object],
        *,
        timeout_seconds: float,
        on_log=None,
    ) -> CardLinkBridgeResult:
        await self.start()
        self.exchange_calls.append(
            {
                "request_id": request_id,
                "payload": deepcopy(payload),
                "timeout": timeout_seconds,
                "worker_pid": self.worker_pid,
            }
        )
        self.active_exchanges += 1
        self.max_active_exchanges = max(
            self.max_active_exchanges,
            self.active_exchanges,
        )
        try:
            # Give a concurrently scheduled generate() call a chance to enter.
            # Without a presenter-level lock this becomes two active exchanges.
            await asyncio.sleep(0.01)
            if self._outcomes:
                outcome = self._outcomes.popleft()
                if callable(outcome):
                    outcome = outcome(request_id, deepcopy(payload))
                if isinstance(outcome, BaseException):
                    raise outcome
                if isinstance(outcome, CardLinkBridgeResult) and on_log is not None:
                    for message in outcome.logs:
                        on_log(message)
                return outcome
            result = CardLinkBridgeResult(
                event={
                    "status": "success",
                    "url": f"https://example.test/{payload['account_email']}",
                    "method": payload["method"],
                    "country": payload["country"],
                    "currency": payload["currency"],
                },
                logs=(f"completed:{payload['account_email']}",),
            )
            if on_log is not None:
                for message in result.logs:
                    on_log(message)
            return result
        finally:
            self.active_exchanges -= 1

    async def close(self) -> None:
        self.close_calls += 1
        self.worker_pid = None

    async def abort(self) -> None:
        self.abort_calls += 1
        self.worker_pid = None


def command(
    *,
    email: str,
    token: str,
    proxy: str,
    method: str = "paypal_gb",
    country: str = "GB",
    currency: str = "GBP",
) -> CardLinkBridgeCommand:
    return CardLinkBridgeCommand(
        access_token=token,
        method=method,
        country=country,
        currency=currency,
        locale="en-GB" if country == "GB" else "en-US",
        account_email=email,
        create_proxy_url=proxy,
        promotion_proxy_url=proxy,
        target_amount="0",
    )


class SharedCardLinkBridgePresenterTests(unittest.IsolatedAsyncioTestCase):
    async def test_nonempty_session_context_is_forwarded_and_redacted(self):
        session_token = "session-context-token-secret"
        device_id = "session-context-device-secret"
        cookie_value = "session-context-cookie-secret"
        session_context = {
            "session_token": session_token,
            "device_id": device_id,
            "storage_state": {
                "cookies": [
                    {
                        "name": "oai-sc",
                        "value": cookie_value,
                        "domain": "chatgpt.com",
                        "path": "/",
                    }
                ]
            },
        }
        bridge_command = CardLinkBridgeCommand(
            access_token="at-session-context",
            method="paypal_us",
            country="US",
            currency="USD",
            locale="en-US",
            account_email="session-context@example.test",
            create_proxy_url="http://session:proxy-secret@proxy.test:8000",
            promotion_proxy_url="http://session:proxy-secret@proxy.test:8000",
            target_amount="0",
            session_context=session_context,
        )
        view = MemoryCardLinkBridgeProcessView()
        presenter = SharedCardLinkBridgePresenter(view)

        await presenter.generate(bridge_command)

        payload = view.exchange_calls[0]["payload"]
        self.assertEqual(payload["session_context"], session_context)
        message = (
            f"session={session_token} device={device_id} cookie={cookie_value}"
        )
        rendered_logs = (
            bridge_service._redact(
                message,
                bridge_service._payload_secrets(payload),
            ),
            openai_card_link_bridge._worker_redact(
                message,
                openai_card_link_bridge._worker_request_secrets(payload),
            ),
        )
        for secret in (session_token, device_id, cookie_value):
            self.assertNotIn(secret, repr(bridge_command))
            for rendered in rendered_logs:
                self.assertNotIn(secret, rendered)
        for rendered in rendered_logs:
            self.assertIn("[REDACTED]", rendered)

        await presenter.close()

    async def test_progress_callback_receives_worker_logs_before_result_hand_off(self):
        view = MemoryCardLinkBridgeProcessView()
        presenter = SharedCardLinkBridgePresenter(view)
        progress: list[str] = []

        result = await presenter.generate(
            command(
                email="live-progress@example.test",
                token="at-live-progress",
                proxy="http://live:secret@live-proxy.test:8000",
            ),
            on_log=progress.append,
        )

        self.assertEqual(progress, ["completed:live-progress@example.test"])
        self.assertEqual(tuple(progress), result.logs)
        await presenter.close()

    async def test_consecutive_requests_start_one_worker_and_keep_payloads_separate(
        self,
    ):
        view = MemoryCardLinkBridgeProcessView()
        presenter = SharedCardLinkBridgePresenter(
            view,
            request_timeout_seconds=17,
        )
        first = command(
            email="first@example.test",
            token="at-first-account",
            proxy="http://first-user:first-pass@first-proxy.test:8000",
        )
        second = command(
            email="second@example.test",
            token="at-second-account",
            proxy="http://second-user:second-pass@second-proxy.test:9000",
            method="paypal_us",
            country="US",
            currency="USD",
        )

        first_result = await presenter.generate(first)
        first_pid = presenter.worker_pid
        second_result = await presenter.generate(second)

        self.assertIsInstance(first_result, CardLinkBridgeResult)
        self.assertIsInstance(second_result, CardLinkBridgeResult)
        self.assertEqual(
            first_result.event["url"],
            "https://example.test/first@example.test",
        )
        self.assertEqual(
            second_result.event["url"],
            "https://example.test/second@example.test",
        )
        self.assertEqual(first_result.logs, ("completed:first@example.test",))
        self.assertEqual(second_result.logs, ("completed:second@example.test",))
        self.assertEqual(view.start_calls, 1)
        self.assertEqual(presenter.spawn_count, 1)
        self.assertEqual(presenter.worker_pid, first_pid)

        first_call, second_call = view.exchange_calls
        self.assertNotEqual(first_call["request_id"], second_call["request_id"])
        self.assertTrue(str(first_call["request_id"]).strip())
        self.assertTrue(str(second_call["request_id"]).strip())
        self.assertEqual(first_call["timeout"], 17)
        self.assertEqual(second_call["timeout"], 17)
        self.assertEqual(first_call["worker_pid"], second_call["worker_pid"])
        self.assertEqual(
            first_call["payload"],
            {
                "access_token": "at-first-account",
                "method": "paypal_gb",
                "country": "GB",
                "currency": "GBP",
                "locale": "en-GB",
                "account_email": "first@example.test",
                "create_proxy_url": (
                    "http://first-user:first-pass@first-proxy.test:8000"
                ),
                "promotion_proxy_url": (
                    "http://first-user:first-pass@first-proxy.test:8000"
                ),
                "target_amount": "0",
                "sentinel_so_enabled": False,
            },
        )
        self.assertEqual(
            second_call["payload"],
            {
                "access_token": "at-second-account",
                "method": "paypal_us",
                "country": "US",
                "currency": "USD",
                "locale": "en-US",
                "account_email": "second@example.test",
                "create_proxy_url": (
                    "http://second-user:second-pass@second-proxy.test:9000"
                ),
                "promotion_proxy_url": (
                    "http://second-user:second-pass@second-proxy.test:9000"
                ),
                "target_amount": "0",
                "sentinel_so_enabled": False,
            },
        )

        await presenter.close()

    async def test_concurrent_generate_calls_are_serialized_and_request_scoped(self):
        view = MemoryCardLinkBridgeProcessView()
        presenter = SharedCardLinkBridgePresenter(
            view,
            request_timeout_seconds=23,
        )

        first_result, second_result = await asyncio.gather(
            presenter.generate(
                command(
                    email="parallel-a@example.test",
                    token="at-parallel-a",
                    proxy="http://a-user:a-pass@a-proxy.test:8000",
                )
            ),
            presenter.generate(
                command(
                    email="parallel-b@example.test",
                    token="at-parallel-b",
                    proxy="http://b-user:b-pass@b-proxy.test:9000",
                )
            ),
        )

        self.assertEqual(view.max_active_exchanges, 1)
        self.assertEqual(view.start_calls, 1)
        self.assertEqual(presenter.spawn_count, 1)
        self.assertEqual(len(view.exchange_calls), 2)
        self.assertEqual(
            len({str(call["request_id"]) for call in view.exchange_calls}),
            2,
        )
        self.assertEqual(
            {first_result.event["url"], second_result.event["url"]},
            {
                "https://example.test/parallel-a@example.test",
                "https://example.test/parallel-b@example.test",
            },
        )
        self.assertEqual(
            {str(call["payload"]["access_token"]) for call in view.exchange_calls},
            {"at-parallel-a", "at-parallel-b"},
        )

        await presenter.close()

    async def test_business_failure_does_not_restart_or_poison_next_request(self):
        failed = CardLinkBridgeServiceError(
            "first checkout was rejected",
            logs=["first-only-log"],
            retryable=True,
        )
        succeeded = CardLinkBridgeResult(
            event={
                "status": "success",
                "url": "https://example.test/second-success",
                "method": "paypal_gb",
                "country": "GB",
                "currency": "GBP",
            },
            logs=("second-only-log",),
        )
        view = MemoryCardLinkBridgeProcessView([failed, succeeded])
        presenter = SharedCardLinkBridgePresenter(view)

        with self.assertRaisesRegex(
            CardLinkBridgeServiceError,
            "first checkout was rejected",
        ):
            await presenter.generate(
                command(
                    email="failed@example.test",
                    token="at-failed",
                    proxy="http://failed:secret@failed-proxy.test:8000",
                )
            )

        pid_after_failure = presenter.worker_pid
        result = await presenter.generate(
            command(
                email="recovered@example.test",
                token="at-recovered",
                proxy="http://recovered:secret@recovered-proxy.test:9000",
            )
        )

        self.assertEqual(result.event["url"], "https://example.test/second-success")
        self.assertEqual(result.logs, ("second-only-log",))
        self.assertNotIn("first-only-log", result.logs)
        self.assertEqual(presenter.worker_pid, pid_after_failure)
        self.assertEqual(view.start_calls, 1)
        self.assertEqual(presenter.spawn_count, 1)
        self.assertEqual(view.abort_calls, 0)
        self.assertEqual(
            view.exchange_calls[1]["payload"]["access_token"],
            "at-recovered",
        )
        self.assertEqual(
            view.exchange_calls[1]["payload"]["create_proxy_url"],
            "http://recovered:secret@recovered-proxy.test:9000",
        )

        await presenter.close()

    async def test_transport_failures_abort_worker_and_next_request_starts_fresh_pid(
        self,
    ):
        for failure in (
            asyncio.TimeoutError(),
            RuntimeError("worker protocol pipe closed"),
        ):
            with self.subTest(failure=type(failure).__name__):
                recovered = CardLinkBridgeResult(
                    event={
                        "status": "success",
                        "url": "https://example.test/recovered-after-abort",
                        "method": "paypal_gb",
                        "country": "GB",
                        "currency": "GBP",
                    },
                    logs=("fresh-worker-log",),
                )
                view = MemoryCardLinkBridgeProcessView([failure, recovered])
                presenter = SharedCardLinkBridgePresenter(
                    view,
                    request_timeout_seconds=0.1,
                )

                with self.assertRaises(CardLinkBridgeServiceError) as raised:
                    await presenter.generate(
                        command(
                            email="crashed@example.test",
                            token="at-before-crash",
                            proxy="http://before:secret@before-crash.test:8000",
                        )
                    )

                self.assertEqual(
                    raised.exception.retryable,
                    not isinstance(failure, asyncio.TimeoutError),
                )
                self.assertEqual(view.abort_calls, 1)
                self.assertIsNone(presenter.worker_pid)
                result = await presenter.generate(
                    command(
                        email="recovered@example.test",
                        token="at-after-crash",
                        proxy="http://after:secret@after-crash.test:9000",
                    )
                )

                self.assertEqual(
                    result.event["url"],
                    "https://example.test/recovered-after-abort",
                )
                self.assertEqual(result.logs, ("fresh-worker-log",))
                self.assertEqual(view.start_calls, 2)
                self.assertEqual(presenter.spawn_count, 2)
                self.assertEqual(
                    [call["worker_pid"] for call in view.exchange_calls],
                    [43001, 43002],
                )
                self.assertEqual(
                    view.exchange_calls[1]["payload"]["access_token"],
                    "at-after-crash",
                )
                self.assertEqual(view.abort_calls, 1)

                await presenter.close()

    async def test_close_is_idempotent(self):
        view = MemoryCardLinkBridgeProcessView()
        presenter = SharedCardLinkBridgePresenter(view)
        await presenter.generate(
            command(
                email="close@example.test",
                token="at-close",
                proxy="http://close:secret@close-proxy.test:8000",
            )
        )

        await presenter.close()
        await presenter.close()

        self.assertEqual(view.close_calls, 1)
        self.assertEqual(view.abort_calls, 0)
        self.assertIsNone(presenter.worker_pid)
        self.assertEqual(presenter.spawn_count, 1)

    def test_process_view_contract_is_explicit(self):
        self.assertTrue(callable(getattr(CardLinkBridgeProcessView, "start", None)))
        self.assertTrue(callable(getattr(CardLinkBridgeProcessView, "exchange", None)))
        self.assertTrue(callable(getattr(CardLinkBridgeProcessView, "close", None)))
        self.assertTrue(callable(getattr(CardLinkBridgeProcessView, "abort", None)))


def worker_request(
    request_id: str,
    *,
    method: str,
    token: str,
    proxy: str,
    email: str,
    promotion_proxy: str = "",
) -> dict[str, object]:
    country, currency = ("GB", "GBP") if method == "paypal_gb" else ("US", "USD")
    return {
        "v": WORKER_PROTOCOL_VERSION,
        "id": request_id,
        "op": "generate",
        "payload": {
            "method": method,
            "access_token": token,
            "country": country,
            "currency": currency,
            "locale": "en-GB" if country == "GB" else "en-US",
            "account_email": email,
            "create_proxy_url": proxy,
            "promotion_proxy_url": promotion_proxy or proxy,
            "target_amount": "0",
        },
    }


def decoded_worker_messages(output: io.StringIO) -> list[dict[str, object]]:
    messages = []
    for line in output.getvalue().splitlines():
        if line.startswith(WORKER_MESSAGE_PREFIX):
            messages.append(json.loads(line[len(WORKER_MESSAGE_PREFIX) :]))
    return messages


class CardLinkBridgeWorkerTests(unittest.TestCase):
    def test_proxy_credentials_are_redacted_even_when_logs_split_the_url(self):
        username = "1234567-AbCdEf1234"
        password = "secret@value"
        proxy = f"http://{username}:secret%40value@proxy.test:8000"
        payload = {
            "access_token": "at-component-test",
            "account_email": "component@example.test",
            "create_proxy_url": proxy,
            "promotion_proxy_url": proxy,
        }
        message = f"proxy user={username} password={password}"

        worker_text = openai_card_link_bridge._worker_redact(
            message,
            openai_card_link_bridge._worker_request_secrets(payload),
        )
        parent_text = bridge_service._redact(
            message,
            bridge_service._payload_secrets(payload),
        )
        endpoint = card_link_runtime.opll_describe_proxy_endpoint(proxy)

        for rendered in (worker_text, parent_text, endpoint):
            self.assertNotIn(username, rendered)
            self.assertNotIn(password, rendered)
        self.assertIn("host=proxy.test:8000", endpoint)

    def test_worker_releases_request_payload_after_each_exchange(self):
        request = worker_request(
            "request-release",
            method="paypal_gb",
            token="at-release",
            proxy="http://release:secret@release-proxy.test:8000",
            email="release@example.test",
        )
        payload = request["payload"]
        output_stream = io.StringIO()
        with mock.patch.object(
            openai_card_link_bridge,
            "_worker_generate",
            return_value={
                "status": "success",
                "url": "https://example.test/released",
            },
        ):
            should_stop = openai_card_link_bridge._handle_worker_request(
                request,
                output_stream=output_stream,
            )

        self.assertFalse(should_stop)
        self.assertEqual(request, {})
        self.assertEqual(payload, {})

    def test_worker_keeps_one_loop_and_isolates_requests_after_business_failure(self):
        first_token = "at-worker-first"
        first_proxy = "http://first:secret@first-worker-proxy.test:8000"
        first_final_proxy = (
            "http://first-final:secret@first-final-worker-proxy.test:8000"
        )
        first_email = "first-worker@example.test"
        second_token = "at-worker-second"
        second_proxy = "http://second:secret@second-worker-proxy.test:9000"
        second_final_proxy = (
            "http://second-final:secret@second-final-worker-proxy.test:9000"
        )
        second_email = "second-worker@example.test"
        requests = [
            worker_request(
                "request-first",
                method="paypal_gb",
                token=first_token,
                proxy=first_proxy,
                email=first_email,
                promotion_proxy=first_final_proxy,
            ),
            worker_request(
                "request-second",
                method="paypal_us",
                token=second_token,
                proxy=second_proxy,
                email=second_email,
                promotion_proxy=second_final_proxy,
            ),
            {
                "v": WORKER_PROTOCOL_VERSION,
                "id": "request-stop",
                "op": "shutdown",
            },
        ]
        input_stream = io.BytesIO(
            b"".join(
                (json.dumps(item, separators=(",", ":")) + "\n").encode("utf-8")
                for item in requests
            )
        )
        output_stream = io.StringIO()
        observed: list[dict[str, str]] = []

        def fail_gb(
            token,
            create_proxy_url,
            promotion_proxy_url,
            target_amount,
            *,
            account_email,
            sentinel_so_enabled,
            diagnostic_log,
        ):
            self.assertFalse(sentinel_so_enabled)
            observed.append(
                {
                    "method": "paypal_gb",
                    "token": token,
                    "create_proxy": create_proxy_url,
                    "promotion_proxy": promotion_proxy_url,
                    "email": account_email,
                    "target_amount": target_amount,
                }
            )
            diagnostic_log(
                f"first log token={token} proxy={create_proxy_url} email={account_email}"
            )
            raise RuntimeError(
                f"first failed token={token} proxy={create_proxy_url} email={account_email}"
            )

        def succeed_us(
            token,
            create_proxy_url,
            promotion_proxy_url,
            target_amount,
            *,
            account_email,
            sentinel_so_enabled,
            diagnostic_log,
        ):
            self.assertFalse(sentinel_so_enabled)
            observed.append(
                {
                    "method": "paypal_us",
                    "token": token,
                    "create_proxy": create_proxy_url,
                    "promotion_proxy": promotion_proxy_url,
                    "email": account_email,
                    "target_amount": target_amount,
                }
            )
            diagnostic_log(
                f"second log token={token} proxy={create_proxy_url} email={account_email}"
            )
            return {
                "status": "success",
                "url": "https://example.test/worker-second-success",
                "method": "paypal_us",
                "country": "US",
                "currency": "USD",
            }

        with (
            mock.patch.object(
                openai_card_link_bridge.card_link_runtime,
                "clear_proxy_exit_cache",
            ) as clear_proxy_cache,
            mock.patch.object(
                openai_card_link_bridge,
                "generate_paypal_gb_event",
                side_effect=fail_gb,
            ),
            mock.patch.object(
                openai_card_link_bridge,
                "generate_paypal_us_event",
                side_effect=succeed_us,
            ),
            mock.patch.object(
                openai_card_link_bridge,
                "card_link_error_is_retryable",
                return_value=True,
            ),
            mock.patch.object(openai_card_link_bridge.os, "getpid", return_value=24680),
        ):
            exit_status = openai_card_link_bridge.worker_main(
                input_stream=input_stream,
                output_stream=output_stream,
            )

        messages = decoded_worker_messages(output_stream)
        ready = [item for item in messages if item.get("type") == "ready"]
        first_messages = [
            item for item in messages if item.get("id") == "request-first"
        ]
        second_messages = [
            item for item in messages if item.get("id") == "request-second"
        ]

        self.assertEqual(exit_status, 0)
        self.assertEqual(ready, [{"v": 1, "type": "ready", "pid": 24680}])
        self.assertEqual(clear_proxy_cache.call_count, 2)
        self.assertEqual(
            [item["method"] for item in observed], ["paypal_gb", "paypal_us"]
        )
        self.assertEqual(
            observed[0],
            {
                "method": "paypal_gb",
                "token": first_token,
                "create_proxy": first_proxy,
                "promotion_proxy": first_final_proxy,
                "email": first_email,
                "target_amount": "0",
            },
        )
        self.assertEqual(
            observed[1],
            {
                "method": "paypal_us",
                "token": second_token,
                "create_proxy": second_proxy,
                "promotion_proxy": second_final_proxy,
                "email": second_email,
                "target_amount": "0",
            },
        )
        self.assertEqual(
            [item["type"] for item in first_messages],
            ["log", "error"],
        )
        self.assertEqual(
            [item["type"] for item in second_messages],
            ["log", "result"],
        )
        self.assertEqual(
            second_messages[-1]["event"]["url"],
            "https://example.test/worker-second-success",
        )
        self.assertIn("[REDACTED]", first_messages[0]["message"])
        self.assertIn("[REDACTED]", first_messages[-1]["detail"])
        protocol_output = output_stream.getvalue()
        for secret in (
            first_token,
            first_proxy,
            first_final_proxy,
            first_email,
            second_token,
            second_proxy,
            second_final_proxy,
            second_email,
        ):
            self.assertNotIn(secret, protocol_output)


class CardLinkRuntimeCacheTests(unittest.TestCase):
    def test_proxy_exit_cache_prunes_expired_entries_and_stays_bounded(self):
        now = 10_000.0
        card_link_runtime.clear_proxy_exit_cache()
        try:
            with card_link_runtime._PROXY_EXIT_CACHE_LOCK:
                card_link_runtime._PROXY_EXIT_CACHE["expired"] = (
                    now - card_link_runtime._PROXY_EXIT_CACHE_TTL - 1,
                    {"country": "GB"},
                )
                for index in range(
                    card_link_runtime._PROXY_EXIT_CACHE_MAX_ENTRIES + 20
                ):
                    card_link_runtime._PROXY_EXIT_CACHE[f"fresh-{index}"] = (
                        now - index / 100,
                        {"country": "GB"},
                    )
                card_link_runtime._prune_proxy_exit_cache(now)

                self.assertNotIn(
                    "expired",
                    card_link_runtime._PROXY_EXIT_CACHE,
                )
                self.assertEqual(
                    len(card_link_runtime._PROXY_EXIT_CACHE),
                    card_link_runtime._PROXY_EXIT_CACHE_MAX_ENTRIES,
                )
                self.assertIn("fresh-0", card_link_runtime._PROXY_EXIT_CACHE)
        finally:
            card_link_runtime.clear_proxy_exit_cache()


class FakeProcessStdin:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None


class FakeStartedProcess:
    def __init__(self) -> None:
        self.pid = 27500
        self.returncode: int | None = None
        self.stdin = FakeProcessStdin()
        self.stdout = None
        self.stderr = None
        self.kill_calls = 0
        self.wait_calls = 0

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9

    async def wait(self) -> int:
        self.wait_calls += 1
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class CardLinkBridgeProcessViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_command_and_environment_do_not_contain_request_secrets(self):
        fake_process = FakeStartedProcess()
        spawn = mock.AsyncMock(return_value=fake_process)
        working_directory = (Path.cwd() / "worker-runtime").resolve()
        view = CardLinkBridgeProcessView(
            python_executable=Path.cwd() / "python-test.exe",
            bridge_file=Path.cwd() / "bridge-test.py",
            working_directory=working_directory,
        )
        sensitive_environment = {
            "HME_OPENAI_ACCESS_TOKEN": "at-environment-secret",
            "HME_CARD_LINK_CREATE_PROXY_URL": "http://create-environment-secret",
            "HME_CARD_LINK_PROMO_PROXY_URL": "http://promo-environment-secret",
            "HME_SAFE_MARKER": "preserved-value",
        }

        with (
            mock.patch.dict(os.environ, sensitive_environment, clear=False),
            mock.patch(
                "hidemyemail_generator.card_link_bridge_service.asyncio.create_subprocess_exec",
                new=spawn,
            ),
            mock.patch.object(
                view,
                "_read_protocol_message",
                new=mock.AsyncMock(
                    return_value={
                        "v": WORKER_PROTOCOL_VERSION,
                        "type": "ready",
                        "pid": fake_process.pid,
                    }
                ),
            ),
        ):
            await view.start()

        command_args = tuple(str(item) for item in spawn.await_args.args)
        environment = spawn.await_args.kwargs["env"]
        serialized_startup = " ".join(command_args) + json.dumps(
            environment,
            sort_keys=True,
        )

        self.assertEqual(command_args[-1], "--worker")
        self.assertEqual(spawn.await_args.kwargs["cwd"], str(working_directory))
        self.assertEqual(environment["HME_SAFE_MARKER"], "preserved-value")
        self.assertEqual(view.worker_pid, fake_process.pid)
        self.assertEqual(view.spawn_count, 1)
        for key, value in sensitive_environment.items():
            if key == "HME_SAFE_MARKER":
                continue
            self.assertNotIn(key, environment)
            self.assertNotIn(value, serialized_startup)

        await view.abort()
        self.assertEqual(fake_process.kill_calls, 1)
        self.assertIsNone(view.worker_pid)

    async def test_close_twice_reaps_process_once_and_leaves_no_background_task(self):
        fake_process = FakeStartedProcess()
        view = CardLinkBridgeProcessView(
            python_executable=Path.cwd() / "python-test.exe",
            bridge_file=Path.cwd() / "bridge-test.py",
            working_directory=Path.cwd(),
        )
        view._process = fake_process

        never_finishes = asyncio.Event()
        stderr_task = asyncio.create_task(never_finishes.wait())
        view._stderr_task = stderr_task
        await asyncio.sleep(0)

        await view.close()
        await view.close()

        self.assertEqual(fake_process.wait_calls, 1)
        self.assertEqual(fake_process.kill_calls, 0)
        self.assertEqual(fake_process.returncode, 0)
        self.assertEqual(len(fake_process.stdin.writes), 1)
        shutdown = json.loads(fake_process.stdin.writes[0].decode("utf-8"))
        self.assertEqual(shutdown["v"], WORKER_PROTOCOL_VERSION)
        self.assertEqual(shutdown["op"], "shutdown")
        self.assertTrue(str(shutdown["id"]).strip())
        self.assertIsNone(view.worker_pid)
        self.assertIsNone(view._process)
        self.assertIsNone(view._stderr_task)
        self.assertTrue(stderr_task.done())


if __name__ == "__main__":
    unittest.main()
