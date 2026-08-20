from __future__ import annotations

import ctypes
import os
import secrets
import sys
import threading
import time
from ctypes import wintypes
from typing import Any


CAMOUFOX_PERSISTENT_STORAGE_PREFS = {
    "dom.storageManager.prompt.testing": True,
    "dom.storageManager.prompt.testing.allow": True,
}
CAMOUFOX_AUTH_RESOURCE_CACHE_PREFS = {
    "browser.cache.memory.enable": True,
    "network.http.use-cache": True,
}
CAMOUFOX_BACKGROUND_EXECUTION_PREFS = {
    # Firefox marks fully covered/minimized Windows as occluded and can stop
    # painting or heavily throttle their documents. Registration automation
    # must keep progressing even while another application is in front.
    "widget.windows.window_occlusion_tracking.enabled": False,
    "dom.timeout.enable_budget_timer_throttling": False,
    "dom.min_background_timeout_value": 4,
    "dom.min_background_timeout_value_without_budget_throttling": 4,
    "dom.animations.offscreen-throttling": False,
    "network.http.throttle.enable": False,
}
CAMOUFOX_WINDOW_DISCOVERY_SECONDS = 15.0
_CAMOUFOX_WINDOW_HANDLES: tuple[int, ...] = ()
_REGISTRATION_CLIPBOARD_LOCK = threading.RLock()
_REGISTRATION_CLIPBOARD_OPEN_ATTEMPTS = 10
_REGISTRATION_CLIPBOARD_RETRY_DELAY_SECONDS = 0.05


def registration_clipboard_lock() -> threading.RLock:
    return _REGISTRATION_CLIPBOARD_LOCK


def copy_registration_clipboard_text(value: str) -> None:
    """Write Unicode text to the Windows clipboard without logging it."""

    if sys.platform != "win32":
        raise RuntimeError("注册邮箱剪贴板粘贴目前仅支持 Windows")
    text = str(value or "")
    encoded = text.encode("utf-16-le") + b"\x00\x00"
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.CloseClipboard.restype = wintypes.BOOL

    with _REGISTRATION_CLIPBOARD_LOCK:
        memory_handle = None
        if text:
            memory_handle = kernel32.GlobalAlloc(0x0002, len(encoded))
            if not memory_handle:
                raise ctypes.WinError()
            pointer = kernel32.GlobalLock(memory_handle)
            if not pointer:
                kernel32.GlobalFree(memory_handle)
                raise ctypes.WinError()
            try:
                ctypes.memmove(pointer, encoded, len(encoded))
            finally:
                kernel32.GlobalUnlock(memory_handle)

        opened = False
        for attempt in range(_REGISTRATION_CLIPBOARD_OPEN_ATTEMPTS):
            if user32.OpenClipboard(None):
                opened = True
                break
            if attempt + 1 < _REGISTRATION_CLIPBOARD_OPEN_ATTEMPTS:
                time.sleep(_REGISTRATION_CLIPBOARD_RETRY_DELAY_SECONDS)
        if not opened:
            if memory_handle:
                kernel32.GlobalFree(memory_handle)
            raise RuntimeError("系统剪贴板正被其他程序占用")

        try:
            if not user32.EmptyClipboard():
                raise ctypes.WinError()
            if memory_handle:
                if not user32.SetClipboardData(13, memory_handle):
                    raise ctypes.WinError()
                memory_handle = None
        finally:
            user32.CloseClipboard()
            if memory_handle:
                kernel32.GlobalFree(memory_handle)


def browser_window_slot_from_environment() -> tuple[int, int]:
    try:
        slot_count = max(
            1,
            min(10, int(os.environ.get("HME_BROWSER_WINDOW_SLOTS") or "1")),
        )
    except (TypeError, ValueError):
        slot_count = 1
    try:
        slot_index = max(
            0,
            min(
                slot_count - 1,
                int(os.environ.get("HME_BROWSER_WINDOW_SLOT") or "0"),
            ),
        )
    except (TypeError, ValueError):
        slot_index = 0
    return slot_index, slot_count


def primary_screen_size() -> tuple[int, int]:
    if os.name == "nt":
        try:
            width = int(ctypes.windll.user32.GetSystemMetrics(0))
            height = int(ctypes.windll.user32.GetSystemMetrics(1))
            if width >= 1024 and height >= 720:
                return width, height
        except Exception:
            pass
    return 1920, 1080


def camoufox_window_layout(
    slot_index: int,
    slot_count: int,
    *,
    screen_size: tuple[int, int] | None = None,
    randomizer=None,
) -> dict[str, int]:
    screen_width, screen_height = screen_size or primary_screen_size()
    count = max(1, min(10, int(slot_count)))
    index = max(0, min(count - 1, int(slot_index)))

    def random_dimension(minimum: int, maximum: int) -> int:
        lower = max(1, min(int(minimum), int(maximum)))
        upper = max(lower, int(maximum))
        if callable(randomizer):
            return max(lower, min(upper, int(randomizer(lower, upper))))
        if lower == upper:
            return lower
        return lower + secrets.randbelow(upper - lower + 1)

    if count == 1:
        maximum_width = max(900, min(1500, screen_width - 40))
        minimum_width = min(maximum_width, max(960, int(maximum_width * 0.72)))
        maximum_height = max(640, min(960, screen_height - 40))
        minimum_height = min(maximum_height, max(600, int(maximum_height * 0.72)))
        width = random_dimension(minimum_width, maximum_width)
        height = random_dimension(minimum_height, maximum_height)
        return {
            "slot": 0,
            "slots": 1,
            "x": max(0, (screen_width - width) // 2),
            "y": max(0, (screen_height - height) // 2),
            "width": width,
            "height": height,
        }
    columns = min(3, count)
    rows = (count + columns - 1) // columns
    tile_width = max(1, screen_width // columns)
    usable_height = max(720, screen_height - 60)
    tile_height = max(1, usable_height // rows)
    margin = 10
    maximum_width = max(480, tile_width - margin * 2)
    minimum_width = min(maximum_width, max(420, int(maximum_width * 0.78)))
    maximum_height = max(480, min(920, tile_height - margin * 2))
    minimum_height = min(maximum_height, max(420, int(maximum_height * 0.72)))
    width = random_dimension(minimum_width, maximum_width)
    height = random_dimension(minimum_height, maximum_height)
    column = index % columns
    row = index // columns
    return {
        "slot": index,
        "slots": count,
        "x": column * tile_width + max(margin, (tile_width - width) // 2),
        "y": row * tile_height + max(margin, (tile_height - height) // 2),
        "width": width,
        "height": height,
    }


def windows_descendant_process_ids(root_pid: int) -> set[int]:
    if os.name != "nt" or root_pid <= 0:
        return {root_pid} if root_pid > 0 else set()
    try:
        class ProcessEntry32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel32 = ctypes.windll.kernel32
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        if snapshot in {0, -1}:
            return {root_pid}
        parents: dict[int, int] = {}
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                parents[int(entry.th32ProcessID)] = int(
                    entry.th32ParentProcessID
                )
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
        kernel32.CloseHandle(snapshot)
        descendants = {int(root_pid)}
        changed = True
        while changed:
            changed = False
            for process_id, parent_id in parents.items():
                if parent_id in descendants and process_id not in descendants:
                    descendants.add(process_id)
                    changed = True
        return descendants
    except Exception:
        return {root_pid}


def remember_camoufox_window(hwnd: int) -> None:
    global _CAMOUFOX_WINDOW_HANDLES
    normalized = int(hwnd)
    if normalized not in _CAMOUFOX_WINDOW_HANDLES:
        _CAMOUFOX_WINDOW_HANDLES = (*_CAMOUFOX_WINDOW_HANDLES, normalized)


def force_foreground_window(hwnd: int) -> bool:
    """Activate a visible Windows window and verify that Windows accepted it."""

    if os.name != "nt":
        return False
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        target = int(hwnd)
        if not user32.IsWindow(target) or not user32.IsWindowVisible(target):
            return False
        if int(user32.GetForegroundWindow() or 0) == target:
            return True

        current_thread = int(kernel32.GetCurrentThreadId())
        foreground_window = int(user32.GetForegroundWindow() or 0)
        foreground_process = wintypes.DWORD()
        foreground_thread = (
            int(
                user32.GetWindowThreadProcessId(
                    foreground_window,
                    ctypes.byref(foreground_process),
                )
            )
            if foreground_window
            else 0
        )
        target_process = wintypes.DWORD()
        target_thread = int(
            user32.GetWindowThreadProcessId(
                target,
                ctypes.byref(target_process),
            )
        )
        attached_threads = []
        for thread_id in {foreground_thread, target_thread}:
            if not thread_id or thread_id == current_thread:
                continue
            try:
                if user32.AttachThreadInput(current_thread, thread_id, True):
                    attached_threads.append(thread_id)
            except Exception:
                pass

        try:
            try:
                user32.LockSetForegroundWindow(2)  # LSFW_UNLOCK
            except Exception:
                pass
            try:
                user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
            except Exception:
                pass
            user32.ShowWindow(target, 9)  # SW_RESTORE
            # A short topmost pulse makes the already visible window surface
            # even when Windows' foreground lock rejects the first request.
            user32.SetWindowPos(target, -1, 0, 0, 0, 0, 0x0043)
            user32.BringWindowToTop(target)
            user32.SetForegroundWindow(target)
            try:
                user32.SetActiveWindow(target)
                user32.SetFocus(target)
            except Exception:
                pass
            user32.SetWindowPos(target, -2, 0, 0, 0, 0, 0x0043)
        finally:
            for thread_id in reversed(attached_threads):
                try:
                    user32.AttachThreadInput(current_thread, thread_id, False)
                except Exception:
                    pass

        if int(user32.GetForegroundWindow() or 0) == target:
            return True

        # Windows may require a user-input transition before granting focus.
        # Pulse Alt without typing text, retry once, and verify the result.
        try:
            user32.keybd_event(0x12, 0, 0, 0)  # VK_MENU down
            user32.SetForegroundWindow(target)
        finally:
            try:
                user32.keybd_event(0x12, 0, 0x0002, 0)  # KEYEVENTF_KEYUP
            except Exception:
                pass
        return int(user32.GetForegroundWindow() or 0) == target
    except Exception:
        return False


def move_camoufox_window(
    browser: Any,
    layout: dict[str, int],
    *,
    apply_layout: bool = True,
) -> bool:
    if os.name != "nt":
        return False
    try:
        process = browser._impl_obj._connection._transport._proc
        root_pid = int(process.pid)
        user32 = ctypes.windll.user32
        foreground_required = str(
            os.environ.get("HME_BROWSER_FOREGROUND_REQUIRED") or ""
        ).strip().lower() in {"1", "true", "yes", "on"}
        discovered = False
        focused = not foreground_required
        deadline = time.monotonic() + CAMOUFOX_WINDOW_DISCOVERY_SECONDS
        while time.monotonic() < deadline:
            process_ids = windows_descendant_process_ids(root_pid)
            handles: list[int] = []

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def collect_window(hwnd, _lparam):
                process_id = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
                if (
                    int(process_id.value) in process_ids
                    and user32.IsWindowVisible(hwnd)
                    and int(user32.GetWindowTextLengthW(hwnd) or 0) > 0
                ):
                    handles.append(int(hwnd))
                return True

            user32.EnumWindows(collect_window, 0)
            for hwnd in handles:
                remember_camoufox_window(hwnd)
                discovered = True
                if apply_layout:
                    user32.MoveWindow(
                        hwnd,
                        int(layout["x"]),
                        int(layout["y"]),
                        int(layout["width"]),
                        int(layout["height"]),
                        True,
                    )
                if foreground_required and not focused:
                    focused = force_foreground_window(hwnd)
            time.sleep(0.1)
        return bool(discovered and focused)
    except Exception:
        return False


def focus_camoufox_window_once() -> bool:
    global _CAMOUFOX_WINDOW_HANDLES
    if os.name != "nt":
        return False
    try:
        user32 = ctypes.windll.user32
        valid_handles = []
        for hwnd in reversed(_CAMOUFOX_WINDOW_HANDLES):
            if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
                continue
            valid_handles.append(hwnd)
            if force_foreground_window(hwnd):
                _CAMOUFOX_WINDOW_HANDLES = tuple(reversed(valid_handles))
                return True
        _CAMOUFOX_WINDOW_HANDLES = tuple(reversed(valid_handles))
        return False
    except Exception:
        return False


def configure_windowed_camoufox(app_backend) -> bool:
    """Use one stable visible window, tiling only genuinely concurrent runs."""

    original = getattr(app_backend, "CamoufoxNewBrowser", None)
    if not callable(original):
        return False
    if getattr(original, "_hme_windowed", False):
        return True

    def windowed_camoufox(playwright, *args, **kwargs):
        slot_index, slot_count = browser_window_slot_from_environment()
        layout = camoufox_window_layout(slot_index, slot_count)
        kwargs.setdefault("window", (layout["width"], layout["height"]))
        firefox_user_prefs = dict(kwargs.get("firefox_user_prefs") or {})
        firefox_user_prefs.update(CAMOUFOX_PERSISTENT_STORAGE_PREFS)
        firefox_user_prefs.update(CAMOUFOX_BACKGROUND_EXECUTION_PREFS)
        if not str(os.environ.get("HME_REGISTRATION_PROXY_URL") or "").strip():
            kwargs["enable_cache"] = True
            firefox_user_prefs.update(CAMOUFOX_AUTH_RESOURCE_CACHE_PREFS)
        kwargs["firefox_user_prefs"] = firefox_user_prefs
        browser = original(playwright, *args, **kwargs)
        if not kwargs.get("headless"):
            threading.Thread(
                target=move_camoufox_window,
                args=(browser, layout),
                kwargs={"apply_layout": slot_count > 1},
                name=f"camoufox-window-slot-{slot_index + 1}",
                daemon=True,
            ).start()
        return browser

    windowed_camoufox._hme_windowed = True
    app_backend.CamoufoxNewBrowser = windowed_camoufox
    return True
