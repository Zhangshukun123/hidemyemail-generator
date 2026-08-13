import asyncio
import unittest

from hidemyemail_generator.registration_tasks import (
    ConcurrentRegistrationTaskManager,
    RegistrationTaskManager,
)
from hidemyemail_generator.registration_tasks import generate_openai_password


class FakeBrowserManager:
    def __init__(
        self,
        *,
        password_confirmed=True,
        two_factor_enabled=True,
        password_confirmed_sequence=None,
        two_factor_enabled_sequence=None,
    ):
        self.started_accounts = []
        self.start_options = {}
        self.started_batches = []
        self.start_options_history = []
        self.reset_count = 0
        self.password_confirmed = password_confirmed
        self.two_factor_enabled = two_factor_enabled
        self.password_confirmed_sequence = list(
            password_confirmed_sequence or []
        )
        self.two_factor_enabled_sequence = list(two_factor_enabled_sequence or [])
        self.state = {
            "status": "idle",
            "running": False,
            "succeeded": 0,
            "accounts": [],
        }

    def availability(self):
        return {"available": True, "errors": []}

    def snapshot(self):
        return dict(self.state)

    def reset(self):
        self.reset_count += 1
        self.state.update(
            status="idle",
            running=False,
            succeeded=0,
            accounts=[],
        )
        return self.snapshot()

    def start(
        self,
        accounts,
        *,
        headless,
        concurrency,
        use_registration_proxy=False,
        browser_engine="camoufox",
    ):
        self.started_accounts = accounts
        self.start_options = {
            "headless": headless,
            "concurrency": concurrency,
            "use_registration_proxy": use_registration_proxy,
            "browser_engine": browser_engine,
        }
        self.started_batches.append([dict(item) for item in accounts])
        self.start_options_history.append(dict(self.start_options))
        self.state.update(
            status="running",
            running=True,
            headless=headless,
            useRegistrationProxy=use_registration_proxy,
        )
        return self.snapshot()

    async def wait(self):
        await asyncio.sleep(0)
        attempt = max(0, len(self.started_batches) - 1)
        password_confirmed = (
            self.password_confirmed_sequence[
                min(attempt, len(self.password_confirmed_sequence) - 1)
            ]
            if self.password_confirmed_sequence
            else self.password_confirmed
        )
        two_factor_enabled = (
            self.two_factor_enabled_sequence[
                min(attempt, len(self.two_factor_enabled_sequence) - 1)
            ]
            if self.two_factor_enabled_sequence
            else self.two_factor_enabled
        )
        self.state.update(
            status="completed",
            running=False,
            succeeded=len(self.started_accounts),
            accounts=[
                {
                    "email": account["email"],
                    "status": "success",
                    "passwordConfirmed": password_confirmed,
                    "twoFactorEnabled": two_factor_enabled,
                }
                for account in self.started_accounts
            ],
        )
        return self.snapshot()

    async def stop(self):
        self.state.update(status="cancelled", running=False)
        return self.snapshot()


class FakeRegistrationProcess:
    def __init__(self, process_number):
        self.process_number = process_number
        self.codes = {}
        self.state = {
            "id": "",
            "status": "idle",
            "running": False,
            "phase": "idle",
            "provider": "manual",
            "email": "",
            "emails": [],
            "awaitingCode": False,
            "awaitingCodeEmails": [],
            "requested": 0,
            "effectiveConcurrency": 0,
            "claimed": 0,
            "message": "idle",
            "logs": [],
            "startedAt": "",
            "finishedAt": "",
        }

    def start(
        self,
        *,
        label,
        headless,
        concurrency,
        email,
        provider,
        browser_engine="camoufox",
    ):
        del label, headless
        process_id = f"process-{self.process_number}"
        self.state.update(
            id=process_id,
            status="running",
            running=True,
            phase="registering_openai",
            provider=provider,
            browserEngine=browser_engine,
            email=email,
            emails=[email],
            requested=concurrency,
            effectiveConcurrency=1,
            claimed=1,
            message=f"正在注册 {email}",
            logs=[
                {
                    "at": f"2026-08-11T00:00:0{self.process_number}+00:00",
                    "message": f"开始 {email}",
                }
            ],
            startedAt=f"2026-08-11T00:00:0{self.process_number}+00:00",
        )
        return self.snapshot()

    def snapshot(self):
        return {**self.state, "logs": list(self.state["logs"])}

    def submit_verification_code(self, email, code):
        self.codes[email] = code
        return self.snapshot()

    def poll_verification_code(self, email):
        return self.codes.pop(email, "")

    async def poll_verification_code_async(self, email, *, request_id=""):
        del request_id
        return self.poll_verification_code(email)

    async def stop(self):
        self.state.update(status="cancelled", running=False, phase="cancelled")
        return self.snapshot()

    def fail(self, message="registration failed"):
        self.state.update(
            status="failed",
            running=False,
            phase="failed",
            message=message,
            finishedAt="2026-08-11T00:01:00+00:00",
        )


class RegistrationTaskManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_manager_allows_only_one_roxy_profile_task(self):
        processes = []

        def process_factory():
            process = FakeRegistrationProcess(len(processes) + 1)
            processes.append(process)
            return process

        coordinator = ConcurrentRegistrationTaskManager(
            process_factory=process_factory,
            max_processes=3,
        )
        state = coordinator.start(
            label="roxy",
            headless=True,
            concurrency=4,
            email="first@icloud.com",
            provider="manual",
            browser_engine="roxy",
        )

        self.assertEqual(state["tasks"][0]["browserEngine"], "roxy")
        with self.assertRaisesRegex(RuntimeError, "Roxy 专用指纹环境正在注册"):
            coordinator.start(
                label="roxy second",
                headless=False,
                concurrency=1,
                email="second@icloud.com",
                provider="manual",
                browser_engine="roxy",
            )
        await coordinator.stop()

    async def test_concurrent_manager_starts_next_process_while_previous_runs(self):
        processes = []

        def process_factory():
            process = FakeRegistrationProcess(len(processes) + 1)
            processes.append(process)
            return process

        coordinator = ConcurrentRegistrationTaskManager(
            process_factory=process_factory,
            shared_browser_manager=FakeBrowserManager(),
            max_processes=3,
        )

        coordinator.start(
            label="first",
            headless=False,
            concurrency=1,
            email="first@icloud.com",
            provider="manual",
        )
        state = coordinator.start(
            label="second",
            headless=False,
            concurrency=1,
            email="second@icloud.com",
            provider="manual",
        )

        self.assertEqual(state["runningCount"], 2)
        self.assertEqual(state["processCount"], 2)
        self.assertTrue(state["canStartNext"])
        self.assertEqual(
            [task["email"] for task in state["tasks"]],
            ["first@icloud.com", "second@icloud.com"],
        )
        self.assertIn("[进程 1 · first@icloud.com]", state["logs"][0]["message"])
        coordinator.submit_verification_code("first@icloud.com", "123456")
        self.assertEqual(processes[0].codes["first@icloud.com"], "123456")

        state = coordinator.start(
            label="third",
            headless=False,
            concurrency=1,
            email="third@icloud.com",
            provider="manual",
        )
        self.assertEqual(state["runningCount"], 3)
        self.assertFalse(state["canStartNext"])
        with self.assertRaisesRegex(RuntimeError, "达到上限 3"):
            coordinator.start(
                label="fourth",
                headless=False,
                concurrency=1,
                email="fourth@icloud.com",
                provider="manual",
            )

        stopped = await coordinator.stop()
        self.assertEqual(stopped["runningCount"], 0)
        self.assertFalse(stopped["running"])

    async def test_concurrent_manager_rejects_duplicate_active_email(self):
        processes = []

        def process_factory():
            process = FakeRegistrationProcess(len(processes) + 1)
            processes.append(process)
            return process

        coordinator = ConcurrentRegistrationTaskManager(
            process_factory=process_factory,
            max_processes=2,
        )
        coordinator.start(
            label="first",
            headless=False,
            concurrency=1,
            email="same@icloud.com",
            provider="manual",
        )

        with self.assertRaisesRegex(RuntimeError, "已有正在运行"):
            coordinator.start(
                label="duplicate",
                headless=False,
                concurrency=1,
                email="same@icloud.com",
                provider="manual",
            )

    async def test_failed_email_is_recorded_and_can_be_registered_again(self):
        processes = []
        recorded = []

        def process_factory():
            process = FakeRegistrationProcess(len(processes) + 1)
            processes.append(process)
            return process

        coordinator = ConcurrentRegistrationTaskManager(
            process_factory=process_factory,
            record_failure=lambda failure: recorded.append(failure),
            max_processes=2,
        )
        coordinator.start(
            label="first attempt",
            headless=False,
            concurrency=1,
            email="retry@icloud.com",
            provider="manual",
        )
        processes[0].fail("验证码失败")
        await asyncio.sleep(0.3)

        state = coordinator.snapshot()
        self.assertEqual(state["recordedFailureCount"], 1)
        self.assertTrue(state["tasks"][0]["failureRecorded"])
        self.assertEqual(recorded[0]["email"], "retry@icloud.com")
        self.assertEqual(recorded[0]["message"], "验证码失败")

        restarted = coordinator.start(
            label="retry",
            headless=False,
            concurrency=1,
            email="retry@icloud.com",
            provider="manual",
        )
        self.assertEqual(restarted["runningCount"], 1)
        self.assertEqual(restarted["tasks"][-1]["email"], "retry@icloud.com")
        await coordinator.stop()

    async def test_generated_password_contains_required_character_groups(self):
        password = generate_openai_password()
        self.assertGreaterEqual(len(password), 16)
        self.assertRegex(password, r"[A-Z]")
        self.assertRegex(password, r"[a-z]")
        self.assertRegex(password, r"\d")
        self.assertRegex(password, r"[!@#$%^&*_+=-]")

    async def test_stop_during_provider_finalization_clears_running_state(self):
        browser = FakeBrowserManager()
        finalization_started = asyncio.Event()
        finalization_cancelled = asyncio.Event()

        async def acquire_provider(_label):
            return "cancel.finalization@gmail.com"

        async def complete_provider(_email, _success, _message):
            finalization_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finalization_cancelled.set()

        manager = RegistrationTaskManager(
            browser_manager=browser,
            acquire_email=lambda _label: None,
            confirm_email=lambda _email: None,
            acquire_provider_email=acquire_provider,
            complete_provider_email=complete_provider,
        )
        manager.start(
            label="SMSBower Gmail 注册",
            provider="smsbower",
            headless=False,
        )
        await asyncio.wait_for(finalization_started.wait(), timeout=2)

        snapshot = await asyncio.wait_for(manager.stop(), timeout=2)

        self.assertTrue(finalization_cancelled.is_set())
        self.assertTrue(manager._task.done())
        self.assertEqual(snapshot["status"], "cancelled")
        self.assertEqual(snapshot["phase"], "cancelled")
        self.assertFalse(snapshot["running"])
        self.assertTrue(snapshot["finishedAt"])

    async def test_stop_repairs_stale_completed_task_state(self):
        manager = RegistrationTaskManager(
            browser_manager=FakeBrowserManager(),
            acquire_email=lambda _label: None,
            confirm_email=lambda _email: None,
        )
        manager._task = asyncio.create_task(asyncio.sleep(0))
        await manager._task
        manager._state.update(
            status="cancelling",
            phase="cancelling",
            running=True,
            finishedAt="",
        )

        snapshot = await manager.stop()

        self.assertEqual(snapshot["status"], "cancelled")
        self.assertFalse(snapshot["running"])
        self.assertTrue(snapshot["finishedAt"])

    async def test_claims_inventory_email_then_runs_browser_registration(self):
        browser = FakeBrowserManager()
        events = []

        async def acquire(label):
            events.append(("acquire", label))
            return "new-alias@icloud.com"

        async def confirm(email):
            events.append(("confirm", email))

        async def save_password(email, password):
            events.append(("save_password", email, password))

        async def complete(email, success, message):
            events.append(("complete", email, success, message))

        manager = RegistrationTaskManager(
            browser_manager=browser,
            acquire_email=acquire,
            confirm_email=confirm,
            save_password=save_password,
            complete_email=complete,
        )
        state = manager.start(label="OpenAI 一键注册", headless=True, concurrency=1)
        self.assertTrue(state["running"])
        await asyncio.wait_for(manager._task, timeout=5)

        snapshot = manager.snapshot()
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["phase"], "completed")
        self.assertEqual(snapshot["email"], "new-alias@icloud.com")
        self.assertEqual(events[0], ("acquire", "OpenAI 一键注册"))
        self.assertEqual(events[1], ("confirm", "new-alias@icloud.com"))
        self.assertEqual(events[2][0:2], ("save_password", "new-alias@icloud.com"))
        self.assertEqual(events[3][0:3], ("complete", "new-alias@icloud.com", True))
        self.assertEqual(browser.started_accounts[0]["email"], "new-alias@icloud.com")
        self.assertTrue(browser.started_accounts[0]["enable_2fa"])
        self.assertTrue(browser.started_accounts[0]["ensure_password"])
        self.assertFalse(browser.started_accounts[0]["force_reset_password"])
        self.assertEqual(browser.started_accounts[0]["password"], events[2][2])
        self.assertIn("成功 1/1", snapshot["message"])
        self.assertIn("密码已确认 1/1", snapshot["message"])
        self.assertIn("2FA 已开启 1/1", snapshot["message"])
        self.assertTrue(browser.state["headless"])
        self.assertTrue(browser.state["useRegistrationProxy"])
        self.assertEqual(browser.start_options["concurrency"], 1)
        self.assertEqual(browser.reset_count, 1)

    async def test_partial_batch_success_keeps_successful_inventory_consumed(self):
        class PartialBrowserManager(FakeBrowserManager):
            async def wait(self):
                await asyncio.sleep(0)
                self.state.update(
                    status="completed",
                    running=False,
                    succeeded=1,
                    accounts=[
                        {
                            "email": self.started_accounts[0]["email"],
                            "status": "success",
                            "passwordConfirmed": True,
                            "twoFactorEnabled": True,
                        },
                        {
                            "email": self.started_accounts[1]["email"],
                            "status": "failed",
                            "message": "验证码校验失败",
                        },
                    ],
                )
                return self.snapshot()

        emails = iter(["success@icloud.com", "failed@icloud.com"])
        completions = []

        async def acquire(_label):
            return next(emails)

        async def noop(*_args):
            return None

        async def complete(email, success, _message):
            completions.append((email, success))

        manager = RegistrationTaskManager(
            browser_manager=PartialBrowserManager(),
            acquire_email=acquire,
            confirm_email=noop,
            save_password=noop,
            complete_email=complete,
        )
        manager.start(label="partial", headless=True, concurrency=2)
        await asyncio.wait_for(manager._task, timeout=5)

        self.assertEqual(manager.snapshot()["status"], "failed")
        self.assertEqual(
            completions,
            [("success@icloud.com", True), ("failed@icloud.com", False)],
        )

    async def test_browser_log_relay_tracks_rolling_buffer_by_entry(self):
        manager = RegistrationTaskManager(
            browser_manager=FakeBrowserManager(),
            acquire_email=lambda _label: None,
            confirm_email=lambda _email: None,
        )
        first_logs = [
            {"at": f"2026-08-13T00:00:0{index}Z", "message": f"line-{index}"}
            for index in range(3)
        ]
        task_id, cursor = manager._relay_browser_logs(
            {"id": "rolling", "logs": first_logs},
            task_id="",
            cursor=0,
        )
        rolling_logs = first_logs[1:] + [
            {"at": "2026-08-13T00:00:03Z", "message": "line-3"}
        ]

        _, next_cursor = manager._relay_browser_logs(
            {"id": "rolling", "logs": rolling_logs},
            task_id=task_id,
            cursor=cursor,
        )

        self.assertEqual(next_cursor, 3)
        self.assertEqual(manager.snapshot()["logs"][-1]["message"], "line-3")

    async def test_manual_email_skips_inventory_and_starts_single_registration(self):
        browser = FakeBrowserManager()
        events = []

        async def acquire(_label):
            events.append("acquire")
            return "unused@icloud.com"

        async def confirm(_email):
            events.append("confirm")

        async def save_password(email, _password):
            events.append(("save", email))

        async def complete(*_args):
            events.append("complete")

        manager = RegistrationTaskManager(
            browser_manager=browser,
            acquire_email=acquire,
            confirm_email=confirm,
            save_password=save_password,
            complete_email=complete,
        )
        state = manager.start(
            label="手动邮箱注册",
            email="Manual.User@QQ.COM",
            headless=False,
            concurrency=8,
        )
        self.assertEqual(state["email"], "manual.user@qq.com")
        self.assertEqual(state["requested"], 1)
        await asyncio.wait_for(manager._task, timeout=5)

        self.assertEqual(events, [("save", "manual.user@qq.com")])
        self.assertEqual(browser.start_options["concurrency"], 1)
        self.assertFalse(browser.start_options["headless"])
        self.assertEqual(browser.started_accounts[0]["email"], "manual.user@qq.com")
        self.assertTrue(browser.started_accounts[0]["manual_otp_entry"])
        self.assertTrue(browser.started_accounts[0]["foreground_required"])

    async def test_manual_icloud_email_uses_automatic_inbox_code_reader(self):
        browser = FakeBrowserManager()

        async def save_password(_email, _password):
            return None

        manager = RegistrationTaskManager(
            browser_manager=browser,
            acquire_email=lambda _label: None,
            confirm_email=lambda _email: None,
            save_password=save_password,
        )
        manager.start(
            label="iCloud 自有邮箱注册",
            email="relay.address@icloud.com",
            headless=False,
        )
        await asyncio.wait_for(manager._task, timeout=5)

        account = browser.started_accounts[0]
        self.assertFalse(account["manual_otp_entry"])
        self.assertFalse(account["foreground_required"])
        self.assertTrue(account["password_first_required"])
        self.assertTrue(
            any("垃圾邮件" in item["message"] for item in manager.snapshot()["logs"])
        )

    async def test_retry_reuses_first_saved_password_without_overwriting_it(self):
        browser = FakeBrowserManager()
        saved_password = "FirstAttempt!A7Password"
        saved_two_factor = {
            "enabled": False,
            "status": "enrolled",
            "secret": "JBSWY3DPEHPK3PXP",
            "factor_id": "factor-1",
            "session_id": "session-1",
        }
        events = []

        async def load_account(email):
            events.append(("load", email))
            return {
                "email": email,
                "password": saved_password,
                "password_confirmed": False,
                "two_factor": saved_two_factor,
            }

        async def save_password(email, password):
            events.append(("save", email, password))

        manager = RegistrationTaskManager(
            browser_manager=browser,
            acquire_email=lambda _label: None,
            confirm_email=lambda _email: None,
            save_password=save_password,
            load_account=load_account,
        )
        manager.start(
            label="iCloud 自有邮箱重试",
            email="Retry.Alias@icloud.com",
            headless=False,
        )
        await asyncio.wait_for(manager._task, timeout=5)

        account = browser.started_batches[0][0]
        self.assertEqual(account["email"], "retry.alias@icloud.com")
        self.assertEqual(account["password"], saved_password)
        self.assertFalse(account["password_confirmed"])
        self.assertEqual(account["two_factor"], saved_two_factor)
        self.assertEqual(events, [("load", "retry.alias@icloud.com")])
        self.assertTrue(
            any(
                "登录遇到动态码页面时将自动生成并提交当前验证码"
                in item["message"]
                for item in manager.snapshot()["logs"]
            )
        )
        self.assertTrue(
            any(
                "本次继续使用原密码，不生成或覆盖新密码" in item["message"]
                for item in manager.snapshot()["logs"]
            )
        )

    async def test_browser_page_recognition_is_relayed_to_registration_logs(self):
        class LoggingBrowserManager(FakeBrowserManager):
            def __init__(self):
                super().__init__()
                self.log_ready = asyncio.Event()
                self.release = asyncio.Event()

            async def wait(self):
                self.state.update(
                    id="browser-recognition-task",
                    logs=[
                        {
                            "message": (
                                "[界面识别] 当前=邮箱验证码页；语言=日文；"
                                "目标邮箱=匹配；验证码输入框=可见；"
                                "使用密码继续=可见；决策=点击“使用密码继续”并进入密码设置"
                            )
                        }
                    ],
                )
                self.log_ready.set()
                await self.release.wait()
                return await super().wait()

        browser = LoggingBrowserManager()
        manager = RegistrationTaskManager(
            browser_manager=browser,
            acquire_email=lambda _label: None,
            confirm_email=lambda _email: None,
        )
        manager.start(
            label="iCloud 自有邮箱注册",
            email="relay.address@icloud.com",
            headless=False,
        )
        await asyncio.wait_for(browser.log_ready.wait(), timeout=2)
        await asyncio.sleep(0.5)

        running = manager.snapshot()
        recognition_logs = [
            item["message"]
            for item in running["logs"]
            if item["message"].startswith("[界面识别]")
        ]
        self.assertEqual(len(recognition_logs), 1)
        self.assertEqual(running["currentStage"], "password")
        self.assertEqual(running["currentLocation"], "OpenAI 密码页")
        self.assertIn("点击", running["message"])

        browser.release.set()
        await asyncio.wait_for(manager._task, timeout=5)
        self.assertTrue(
            any(
                item["message"].startswith("[界面识别]")
                for item in manager.snapshot()["logs"]
            )
        )

    async def test_manual_verification_code_wait_and_submit_lifecycle(self):
        manager = RegistrationTaskManager(
            browser_manager=FakeBrowserManager(),
            acquire_email=lambda _label: None,
            confirm_email=lambda _email: None,
        )
        manager._state.update(
            running=True,
            status="running",
            email="manual@qq.com",
            emails=["manual@qq.com"],
        )

        self.assertEqual(manager.poll_verification_code("manual@qq.com"), "")
        self.assertTrue(manager.snapshot()["awaitingCode"])
        manager.submit_verification_code("manual@qq.com", " 12 34 56 ")
        self.assertEqual(manager.poll_verification_code("manual@qq.com"), "123456")
        self.assertFalse(manager.snapshot()["awaitingCode"])

    async def test_smsbower_provider_acquires_gmail_and_reports_completion(self):
        browser = FakeBrowserManager()
        events = []

        async def acquire_provider(label):
            events.append(("acquire_provider", label))
            return "api.gmail@gmail.com"

        async def poll_provider(email):
            events.append(("poll_provider", email))
            return "654321"

        async def complete_provider(email, success, message):
            events.append(("complete_provider", email, success, message))

        async def acquire_inventory(_label):
            raise AssertionError("SMSBower flow must not use iCloud inventory")

        manager = RegistrationTaskManager(
            browser_manager=browser,
            acquire_email=acquire_inventory,
            confirm_email=lambda _email: None,
            acquire_provider_email=acquire_provider,
            poll_provider_code=poll_provider,
            complete_provider_email=complete_provider,
        )
        state = manager.start(
            label="SMSBower Gmail 注册",
            provider="smsbower",
            headless=False,
            concurrency=8,
        )
        self.assertEqual(state["provider"], "smsbower")
        self.assertEqual(state["requested"], 1)
        await asyncio.wait_for(manager._task, timeout=5)

        snapshot = manager.snapshot()
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["email"], "api.gmail@gmail.com")
        self.assertEqual(events[0], ("acquire_provider", "SMSBower Gmail 注册"))
        self.assertEqual(events[-1][0:3], ("complete_provider", "api.gmail@gmail.com", True))
        self.assertEqual(browser.started_accounts[0]["email"], "api.gmail@gmail.com")
        self.assertTrue(
            browser.started_accounts[0]["password_first_required"]
        )
        self.assertFalse(browser.started_accounts[0]["foreground_required"])
        self.assertFalse(browser.start_options["headless"])

    async def test_smsbower_provider_poll_returns_code_without_manual_input(self):
        async def poll_provider(email):
            self.assertEqual(email, "auto.code@gmail.com")
            return "246810"

        manager = RegistrationTaskManager(
            browser_manager=FakeBrowserManager(),
            acquire_email=lambda _label: None,
            confirm_email=lambda _email: None,
            poll_provider_code=poll_provider,
        )
        manager._state.update(
            running=True,
            status="running",
            provider="smsbower",
            email="auto.code@gmail.com",
            emails=["auto.code@gmail.com"],
        )

        code = await manager.poll_verification_code_async("auto.code@gmail.com")

        self.assertEqual(code, "246810")
        self.assertFalse(manager.snapshot()["awaitingCode"])
        self.assertIn("SMSBower 已返回验证码", manager.snapshot()["message"])

    async def test_smsbower_code_timeout_cancels_activation_and_marks_failure(self):
        now = [100.0]
        completions = []

        async def poll_provider(_email):
            return ""

        async def cancel_provider(email, message):
            completions.append((email, message))

        manager = RegistrationTaskManager(
            browser_manager=FakeBrowserManager(),
            acquire_email=lambda _label: None,
            confirm_email=lambda _email: None,
            poll_provider_code=poll_provider,
            cancel_provider_email=cancel_provider,
            provider_code_timeout_seconds=30,
            monotonic=lambda: now[0],
        )
        manager._state.update(
            running=True,
            status="running",
            provider="smsbower",
            email="timeout@gmail.com",
            emails=["timeout@gmail.com"],
        )

        self.assertEqual(
            await manager.poll_verification_code_async(
                "timeout@gmail.com", request_id="request-one"
            ),
            "",
        )
        now[0] += 30.1
        with self.assertRaisesRegex(RuntimeError, "已取消邮箱激活"):
            await manager.poll_verification_code_async(
                "timeout@gmail.com", request_id="request-one"
            )
        with self.assertRaisesRegex(RuntimeError, "已取消邮箱激活"):
            await manager.poll_verification_code_async(
                "timeout@gmail.com", request_id="request-one"
            )

        self.assertEqual(len(completions), 1)
        self.assertEqual(completions[0][0], "timeout@gmail.com")
        self.assertIn("30 秒", completions[0][1])
        snapshot = manager.snapshot()
        self.assertEqual(snapshot["status"], "failed")
        self.assertEqual(snapshot["phase"], "failed")
        self.assertIn("已取消邮箱激活", snapshot["message"])

    async def test_smsbower_distinct_browser_requests_wait_for_distinct_codes(self):
        initial_calls = []
        next_calls = []

        async def poll_initial(email):
            initial_calls.append(email)
            return "123456"

        async def poll_next(email):
            next_calls.append(email)
            return "654321"

        manager = RegistrationTaskManager(
            browser_manager=FakeBrowserManager(),
            acquire_email=lambda _label: None,
            confirm_email=lambda _email: None,
            poll_provider_code=poll_initial,
            poll_provider_next_code=poll_next,
        )
        manager._state.update(
            running=True,
            status="running",
            provider="smsbower",
            email="auto.code@gmail.com",
            emails=["auto.code@gmail.com"],
        )

        first = await manager.poll_verification_code_async(
            "auto.code@gmail.com", request_id="request-one"
        )
        first_retry = await manager.poll_verification_code_async(
            "auto.code@gmail.com", request_id="request-one"
        )
        second = await manager.poll_verification_code_async(
            "auto.code@gmail.com", request_id="request-two"
        )
        second_retry = await manager.poll_verification_code_async(
            "auto.code@gmail.com", request_id="request-two"
        )

        self.assertEqual((first, first_retry), ("123456", "123456"))
        self.assertEqual((second, second_retry), ("654321", "654321"))
        self.assertEqual(initial_calls, ["auto.code@gmail.com"])
        self.assertEqual(next_calls, ["auto.code@gmail.com"])

    async def test_missing_two_factor_stays_visible_before_provider_completion(self):
        browser = FakeBrowserManager(two_factor_enabled_sequence=[False, True])
        events = []
        partial_two_factor = {
            "enabled": False,
            "status": "enrolled",
            "secret": "JBSWY3DPEHPK3PXP",
            "factor_id": "factor-1",
            "session_id": "session-1",
        }

        async def acquire_provider(label):
            events.append(("acquire", label))
            return "retry.2fa@gmail.com"

        async def save_password(email, password):
            events.append(("save", email, password))

        async def load_account(email):
            events.append(("load", email))
            if not browser.started_batches:
                return {}
            return {
                "password": browser.started_batches[0][0]["password"],
                "password_confirmed": True,
                "two_factor": partial_two_factor,
            }

        async def complete_provider(email, success, message):
            events.append(("complete", email, success, message))

        manager = RegistrationTaskManager(
            browser_manager=browser,
            acquire_email=lambda _label: None,
            confirm_email=lambda _email: None,
            save_password=save_password,
            acquire_provider_email=acquire_provider,
            complete_provider_email=complete_provider,
            load_account=load_account,
        )
        manager.start(
            label="SMSBower Gmail 注册",
            provider="smsbower",
            headless=False,
        )
        await asyncio.wait_for(manager._task, timeout=5)

        snapshot = manager.snapshot()
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(len(browser.started_batches), 2)
        self.assertFalse(browser.start_options_history[1]["headless"])
        self.assertTrue(browser.start_options_history[1]["use_registration_proxy"])
        retry_account = browser.started_batches[1][0]
        self.assertFalse(retry_account["foreground_required"])
        self.assertTrue(retry_account["password_confirmed"])
        self.assertTrue(retry_account["enable_2fa"])
        self.assertEqual(retry_account["two_factor"], partial_two_factor)
        self.assertEqual(events[-1][0:3], ("complete", "retry.2fa@gmail.com", True))
        self.assertTrue(any("2FA 补做完成" in item["message"] for item in snapshot["logs"]))

    async def test_rejects_passwordless_icloud_result(self):
        browser = FakeBrowserManager(
            password_confirmed=False,
            two_factor_enabled=False,
        )

        async def acquire(_label):
            return "pending-password@icloud.com"

        async def confirm(_email):
            return None

        manager = RegistrationTaskManager(
            browser_manager=browser,
            acquire_email=acquire,
            confirm_email=confirm,
        )
        manager.start(label="OpenAI 一键注册", headless=True)
        await asyncio.wait_for(manager._task, timeout=5)

        snapshot = manager.snapshot()
        self.assertEqual(snapshot["status"], "failed")
        self.assertIn("拒绝免密码账号", snapshot["message"])
        self.assertEqual(len(browser.started_batches), 1)

    async def test_smsbower_gmail_rejects_passwordless_result(self):
        browser = FakeBrowserManager(
            password_confirmed=False,
            two_factor_enabled=False,
        )
        completions = []

        async def acquire_provider(_label):
            return "strict-result@gmail.com"

        async def complete_provider(email, success, message):
            completions.append((email, success, message))

        manager = RegistrationTaskManager(
            browser_manager=browser,
            acquire_email=lambda _label: None,
            confirm_email=lambda _email: None,
            acquire_provider_email=acquire_provider,
            complete_provider_email=complete_provider,
        )
        manager.start(
            label="SMSBower Gmail 严格注册",
            provider="smsbower",
            headless=True,
        )
        await asyncio.wait_for(manager._task, timeout=5)

        snapshot = manager.snapshot()
        self.assertEqual(snapshot["status"], "failed")
        self.assertIn("拒绝免密码账号", snapshot["message"])
        self.assertEqual(completions[-1][0:2], ("strict-result@gmail.com", False))
        self.assertEqual(len(browser.started_batches), 1)

    async def test_empty_inventory_fails_without_starting_browser(self):
        browser = FakeBrowserManager()

        async def acquire(_label):
            return ""

        async def confirm(_email):
            raise AssertionError("empty inventory must not confirm an email")

        manager = RegistrationTaskManager(
            browser_manager=browser,
            acquire_email=acquire,
            confirm_email=confirm,
        )
        manager.start(label="OpenAI 一键注册", headless=True)
        await asyncio.wait_for(manager._task, timeout=5)

        snapshot = manager.snapshot()
        self.assertEqual(snapshot["status"], "failed")
        self.assertIn("邮箱库存不足", snapshot["message"])
        self.assertEqual(browser.started_accounts, [])

    async def test_inventory_failure_is_reported_to_remote_inventory(self):
        browser = FakeBrowserManager()
        completed = []

        async def acquire(_label):
            return "retry@icloud.com"

        async def confirm(_email):
            raise RuntimeError("confirmation failed")

        async def complete(email, success, message):
            completed.append((email, success, message))

        manager = RegistrationTaskManager(
            browser_manager=browser,
            acquire_email=acquire,
            confirm_email=confirm,
            complete_email=complete,
        )
        manager.start(label="OpenAI 一键注册", headless=True)
        await asyncio.wait_for(manager._task, timeout=5)

        self.assertEqual(manager.snapshot()["status"], "failed")
        self.assertEqual(completed[0][0:2], ("retry@icloud.com", False))
        self.assertIn("confirmation failed", completed[0][2])
        self.assertTrue(
            any(
                "注册失败，已释放，可再次注册" in item["message"]
                for item in manager.snapshot()["logs"]
            )
        )

    async def test_concurrency_three_claims_three_accounts_and_starts_three_browsers(self):
        browser = FakeBrowserManager()
        inventory = [
            "first@icloud.com",
            "second@icloud.com",
            "third@icloud.com",
        ]
        released = []

        async def acquire(_label):
            return inventory.pop(0) if inventory else ""

        async def confirm(_email):
            return None

        async def release(email):
            released.append(email)

        manager = RegistrationTaskManager(
            browser_manager=browser,
            acquire_email=acquire,
            confirm_email=confirm,
            release_email=release,
        )
        state = manager.start(
            label="OpenAI 一键注册",
            headless=False,
            concurrency=3,
        )
        self.assertEqual(state["requested"], 3)
        await asyncio.wait_for(manager._task, timeout=5)

        snapshot = manager.snapshot()
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["claimed"], 3)
        self.assertEqual(len(snapshot["emails"]), 3)
        self.assertEqual(len(browser.started_accounts), 3)
        self.assertEqual(browser.start_options["concurrency"], 3)
        self.assertEqual(len({item["password"] for item in browser.started_accounts}), 3)
        self.assertEqual(len(released), 3)
        self.assertIn("成功 3/3", snapshot["message"])

    async def test_roxy_concurrency_five_claims_five_accounts(self):
        browser = FakeBrowserManager()
        inventory = [f"roxy-{index}@icloud.com" for index in range(1, 6)]

        async def acquire(_label):
            return inventory.pop(0) if inventory else ""

        manager = RegistrationTaskManager(
            browser_manager=browser,
            acquire_email=acquire,
            confirm_email=lambda _email: asyncio.sleep(0),
            release_email=lambda _email: asyncio.sleep(0),
        )
        state = manager.start(
            label="Roxy 五并发注册",
            headless=False,
            concurrency=5,
            browser_engine="roxy",
        )
        self.assertEqual(state["requested"], 5)
        await asyncio.wait_for(manager._task, timeout=5)

        snapshot = manager.snapshot()
        self.assertEqual(snapshot["claimed"], 5)
        self.assertEqual(browser.start_options["concurrency"], 5)
        self.assertEqual(browser.start_options["browser_engine"], "roxy")
        self.assertEqual(len(browser.started_accounts), 5)

    async def test_roxy_target_count_claims_all_accounts_but_keeps_five_windows(self):
        browser = FakeBrowserManager()
        inventory = [f"target-{index}@icloud.com" for index in range(1, 13)]

        async def acquire(_label):
            return inventory.pop(0) if inventory else ""

        manager = RegistrationTaskManager(
            browser_manager=browser,
            acquire_email=acquire,
            confirm_email=lambda _email: asyncio.sleep(0),
            release_email=lambda _email: asyncio.sleep(0),
        )
        state = manager.start(
            label="Roxy 目标注册",
            headless=True,
            concurrency=5,
            target_count=12,
            browser_engine="roxy",
        )
        self.assertEqual(state["requested"], 12)
        self.assertEqual(state["targetCount"], 12)
        self.assertEqual(state["concurrency"], 5)
        await asyncio.wait_for(manager._task, timeout=5)

        snapshot = manager.snapshot()
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["claimed"], 12)
        self.assertEqual(snapshot["effectiveConcurrency"], 5)
        self.assertEqual(browser.start_options["concurrency"], 5)
        self.assertEqual(len(browser.started_accounts), 12)

    async def test_inventory_shortage_uses_available_minimum(self):
        browser = FakeBrowserManager()
        inventory = ["only@icloud.com"]
        released = []

        async def acquire(_label):
            return inventory.pop(0) if inventory else ""

        async def confirm(_email):
            return None

        async def release(email):
            released.append(email)

        manager = RegistrationTaskManager(
            browser_manager=browser,
            acquire_email=acquire,
            confirm_email=confirm,
            release_email=release,
        )
        manager.start(
            label="OpenAI 一键注册",
            headless=True,
            concurrency=3,
        )
        await asyncio.wait_for(manager._task, timeout=5)

        snapshot = manager.snapshot()
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["requested"], 3)
        self.assertEqual(snapshot["effectiveConcurrency"], 1)
        self.assertEqual(snapshot["claimed"], 1)
        self.assertEqual(len(browser.started_accounts), 1)
        self.assertEqual(browser.start_options["concurrency"], 1)
        self.assertIn("成功 1/1", snapshot["message"])
        self.assertTrue(
            any("从 3 自动降为 1" in item["message"] for item in snapshot["logs"])
        )
        self.assertEqual(released, ["only@icloud.com"])


if __name__ == "__main__":
    unittest.main()
