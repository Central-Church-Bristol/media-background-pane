from __future__ import annotations

import sys
import time
from collections.abc import Callable


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
    while running is None or running():
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return
        if remaining > 0.002:
            time.sleep(remaining - 0.001)
            continue
        while (running is None or running()) and time.perf_counter() < deadline:
            pass
        return
