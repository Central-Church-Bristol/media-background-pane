from __future__ import annotations

import sys
import time
from collections.abc import Callable

# Longest single sleep, so a stop request is noticed within this time.
_SLICE = 0.05
THREAD_PRIORITY_TIME_CRITICAL = 15


def begin_precise_timer() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.winmm.timeBeginPeriod(1)
    except OSError:
        pass


def end_precise_timer() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.winmm.timeEndPeriod(1)
    except OSError:
        pass


def sleep_until(deadline: float, running: Callable[[], bool] | None = None) -> None:
    """Sleep until time.perf_counter() reaches deadline, or running() turns false.

    No busy-wait. On Windows, Python 3.11 and later sleep on a high-resolution
    waitable timer, so a sleep ends within about a millisecond of when it
    should. The spin that used to cover the last 1-2 ms held the GIL in three
    threads at once.
    """
    while running is None or running():
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return
        time.sleep(min(remaining, _SLICE))


def raise_thread_priority() -> bool:
    """Run the calling thread ahead of normal work. True if Windows agreed.

    For the compose thread: it uses little CPU, but when it runs is the
    output's timing. The pane's process stays below normal priority, so this
    does not take time from ProPresenter beyond the few milliseconds per frame
    the compositor needs.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentThread.restype = wintypes.HANDLE
        kernel32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
        kernel32.SetThreadPriority.restype = wintypes.BOOL
        return bool(kernel32.SetThreadPriority(kernel32.GetCurrentThread(), THREAD_PRIORITY_TIME_CRITICAL))
    except (OSError, AttributeError):
        return False
