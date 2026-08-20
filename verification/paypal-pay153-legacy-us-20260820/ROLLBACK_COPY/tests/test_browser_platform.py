import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hidemyemail_generator.browser_platform import (
    camoufox_window_layout,
    configure_windowed_camoufox,
    focus_camoufox_window_once,
)


class BrowserPlatformTests(unittest.TestCase):
    def test_single_window_is_tracked_without_moving_it(self):
        calls = []

        def launch(_playwright, **kwargs):
            calls.append(kwargs)
            return "browser"

        backend = SimpleNamespace(CamoufoxNewBrowser=launch)
        with (
            patch.dict(
                os.environ,
                {
                    "HME_BROWSER_WINDOW_SLOT": "0",
                    "HME_BROWSER_WINDOW_SLOTS": "1",
                    "HME_BROWSER_FOREGROUND_REQUIRED": "1",
                },
            ),
            patch(
                "hidemyemail_generator.browser_platform.move_camoufox_window"
            ) as mover,
        ):
            self.assertTrue(configure_windowed_camoufox(backend))
            self.assertEqual(
                backend.CamoufoxNewBrowser("playwright", headless=False),
                "browser",
            )
            for _ in range(30):
                if mover.called:
                    break
                time.sleep(0.01)

        mover.assert_called_once()
        self.assertFalse(mover.call_args.kwargs["apply_layout"])
        self.assertIn("window", calls[0])
        prefs = calls[0]["firefox_user_prefs"]
        self.assertFalse(
            prefs["widget.windows.window_occlusion_tracking.enabled"]
        )
        self.assertFalse(prefs["dom.timeout.enable_budget_timer_throttling"])
        self.assertEqual(prefs["dom.min_background_timeout_value"], 4)

    def test_concurrent_windows_start_one_tiling_worker(self):
        backend = SimpleNamespace(
            CamoufoxNewBrowser=lambda _playwright, **_kwargs: "browser"
        )
        with (
            patch.dict(
                os.environ,
                {"HME_BROWSER_WINDOW_SLOT": "1", "HME_BROWSER_WINDOW_SLOTS": "2"},
            ),
            patch(
                "hidemyemail_generator.browser_platform.move_camoufox_window"
            ) as mover,
        ):
            configure_windowed_camoufox(backend)
            backend.CamoufoxNewBrowser("playwright", headless=False)
            for _ in range(30):
                if mover.called:
                    break
                time.sleep(0.01)

        mover.assert_called_once()
        self.assertTrue(mover.call_args.kwargs["apply_layout"])

    def test_layouts_are_pure_and_slot_specific(self):
        layouts = [
            camoufox_window_layout(
                index,
                2,
                screen_size=(2400, 1200),
                randomizer=lambda lower, upper: (lower + upper) // 2,
            )
            for index in range(2)
        ]

        self.assertEqual([item["slot"] for item in layouts], [0, 1])
        self.assertNotEqual(layouts[0]["x"], layouts[1]["x"])

    def test_focus_uses_verified_foreground_activation(self):
        fake_user32 = SimpleNamespace(
            IsWindow=lambda hwnd: hwnd == 202,
            IsWindowVisible=lambda hwnd: hwnd == 202,
        )
        with (
            patch(
                "hidemyemail_generator.browser_platform._CAMOUFOX_WINDOW_HANDLES",
                (101, 202),
            ),
            patch(
                "hidemyemail_generator.browser_platform.ctypes.windll.user32",
                fake_user32,
            ),
            patch(
                "hidemyemail_generator.browser_platform.force_foreground_window",
                return_value=True,
            ) as activate,
        ):
            self.assertTrue(focus_camoufox_window_once())

        activate.assert_called_once_with(202)


if __name__ == "__main__":
    unittest.main()
