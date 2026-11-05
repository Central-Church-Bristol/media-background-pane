from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from pathlib import Path

from .config import APP_NAME

# Re-entrant, because a hook can fire while this thread is already writing:
# garbage collection can run a __del__ that raises in the middle of a write.
_lock = threading.RLock()
_last_written: dict[str, float] = {}
_LOG_INTERVAL = 5.0
_MAX_BYTES = 512_000
# Entries that reached no file at all. They are kept here, up to a limit, and
# written with the next entry that gets through, so a gap loses nothing.
_lost = 0
_lost_reason = ""
_unwritten: list[str] = []
_KEEP_UNWRITTEN = 200
_dropped = 0


def session_log_path() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    root = Path(local) / APP_NAME if local else Path.home() / "AppData" / "Local" / APP_NAME
    return root / "session.log"


def fallback_log_path() -> Path:
    """Where entries go when session.log cannot be written: the Windows temp folder.

    Taken from TEMP or TMP directly. tempfile.gettempdir() runs a regular
    expression the first time (os.path.expandvars), and on 4 Nov 2026 that
    raised TypeError in the running pane.
    """
    temp = os.environ.get("TEMP") or os.environ.get("TMP")
    root = Path(temp) if temp else Path.home() / "AppData" / "Local" / "Temp"
    return root / f"{APP_NAME} session.log"


def log_event(context: str, message: str, *, path: Path | None = None) -> None:
    """Append one line to the session log. Never raises."""
    try:
        _write(f"{_stamp()} {context}: {message}\n", path)
    except Exception:
        pass


def log_exception(context: str, exc: BaseException | None = None, *, path: Path | None = None) -> None:
    """Append one traceback to the session log. Repeated errors are throttled.

    Never raises. It runs inside the handlers that keep threads alive. If it
    raised there, the thread would end, and under pythonw nothing would say why.
    """
    try:
        if exc is None:
            exc = sys.exc_info()[1]
            if exc is None:
                return
        key = f"{context}:{type(exc).__name__}"
        now = time.monotonic()
        with _lock:
            last = _last_written.get(key)
            if last is not None and now - last < _LOG_INTERVAL:
                return
            _last_written[key] = now
            _write(f"{_stamp()} {context}\n{_format(exc)}\n", path)
    except Exception:
        pass


def thread_stack(thread: threading.Thread) -> str:
    """Where a running thread is right now, as traceback lines. Never raises."""
    try:
        frame = sys._current_frames().get(thread.ident or -1)
        if frame is None:
            return "  (no stack: the thread is not running Python code)\n"
        return "".join(traceback.format_stack(frame))
    except Exception as exc:
        return f"  (no stack: {_describe(exc)})\n"


def install_error_logging() -> None:
    """Send errors that would go to stderr into the session log instead.

    pythonw has no console, so stderr is lost. This covers background threads,
    the GUI thread (Qt reports errors in slots through sys.excepthook), and
    errors raised in __del__ or during garbage collection.
    """
    if getattr(install_error_logging, "_installed", False):
        return
    setattr(install_error_logging, "_installed", True)
    previous_thread_hook = threading.excepthook
    previous_sys_hook = sys.excepthook
    previous_unraisable_hook = sys.unraisablehook

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        try:
            name = args.thread.name if args.thread is not None else "?"
            log_exception(f"thread {name}", args.exc_value)
        except Exception:
            pass
        try:
            previous_thread_hook(args)
        except Exception:
            pass

    def sys_hook(exc_type, exc, tb) -> None:
        log_exception("main thread", exc)
        try:
            previous_sys_hook(exc_type, exc, tb)
        except Exception:
            pass

    def unraisable_hook(unraisable) -> None:
        try:
            context = f"unraisable ({unraisable.err_msg or 'error'})"
            if unraisable.exc_value is not None:
                log_exception(context, unraisable.exc_value)
            else:
                log_event(context, repr(unraisable.exc_type))
        except Exception:
            pass
        try:
            previous_unraisable_hook(unraisable)
        except Exception:
            pass

    threading.excepthook = thread_hook
    sys.excepthook = sys_hook
    sys.unraisablehook = unraisable_hook


class Throttle:
    """Session-log lines for things that can happen many times a second.

    Each kind of event (its key) gets up to burst lines per interval, so
    ordinary changes go in at once. Beyond that they are counted, and flush(),
    called regularly, writes the last of them with the count when there is
    room again. A flickering window cannot flood the log, and still shows up.
    """

    def __init__(self, interval: float = 10.0, burst: int = 5, *, write=None, clock=time.monotonic) -> None:
        self.interval = interval
        self.burst = burst
        self._write = write or log_event
        self._clock = clock
        self._recent: dict[str, list[float]] = {}
        self._held: dict[str, tuple[int, str, str, float]] = {}

    def event(self, key: str, context: str, message: str) -> bool:
        """Write the line now, unless this kind has used up its lines for now."""
        now = self._clock()
        if not self._room(key, now):
            count, _context, _message, since = self._held.get(key, (0, "", "", now))
            self._held[key] = (count + 1, context, message, since)
            return False
        self._write_held(key, now)
        self._recent[key].append(now)
        self._write(context, message)
        return True

    def flush(self) -> None:
        """Write the count of held-back events whose kind has room again."""
        now = self._clock()
        for key in list(self._held):
            if self._room(key, now):
                self._write_held(key, now)
                self._recent[key].append(now)

    def _room(self, key: str, now: float) -> bool:
        recent = [at for at in self._recent.get(key, ()) if now - at < self.interval]
        self._recent[key] = recent
        return len(recent) < self.burst

    def _write_held(self, key: str, now: float) -> None:
        held = self._held.pop(key, None)
        if held is None:
            return
        count, context, message, since = held
        times = "time" if count == 1 else "times"
        self._write(context, f"{message} [{count} more {times} in the last {now - since:.0f} s; this was the last]")


def _write(text: str, path: Path | None) -> None:
    """Write to session.log, or else the fallback. Keep the entry for later if both fail."""
    global _lost, _lost_reason, _dropped
    with _lock:
        backlog = ""
        if _lost:
            entries = "entry" if _lost == 1 else "entries"
            backlog = (
                f"{_stamp()} log: {_lost} earlier {entries} could not be written ({_lost_reason}). "
                "Written now, with the times they happened:\n"
            )
            if _dropped:
                backlog += f"{_stamp()} log: the oldest {_dropped} of them were not kept\n"
            backlog += "".join(_unwritten)
        failures: list[str] = []
        for where in ("session log", "fallback log"):
            try:
                if where == "session log":
                    target = path if path is not None else session_log_path()
                else:
                    target = fallback_log_path()
                notes = "".join(f"{_stamp()} log: could not write the {failure}\n" for failure in failures)
                _append(target, notes + backlog + text)
            except Exception as exc:
                failures.append(f"{where} ({_describe(exc)})")
                continue
            _lost = 0
            _lost_reason = ""
            _unwritten.clear()
            _dropped = 0
            return
        _lost += 1
        _lost_reason = "; ".join(failures)[:300]
        _unwritten.append(text)
        if len(_unwritten) > _KEEP_UNWRITTEN:
            del _unwritten[0]
            _dropped += 1


def _append(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # On 4 Nov 2026 mkdir said the pane's own log folder "already exists"
        # (it did) 54 times in a row, and those entries were lost. Whether the
        # folder is really missing, opening the file below says.
        pass
    _rotate(path)
    # backslashreplace: text that cannot be encoded (a stray surrogate in a
    # file name, say) is written escaped instead of losing the whole entry.
    with path.open("a", encoding="utf-8", errors="backslashreplace") as handle:
        handle.write(text)


def _rotate(path: Path) -> None:
    """Keep about a megabyte. If the file cannot be renamed, keep writing to it."""
    try:
        if path.stat().st_size <= _MAX_BYTES:
            return
        previous = path.with_name(path.name + ".1")
        previous.unlink(missing_ok=True)
        path.replace(previous)
    except OSError:
        return


def _format(exc: BaseException) -> str:
    try:
        return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    except Exception as failure:
        # Keep the error itself even when its traceback cannot be formatted.
        return f"{_describe(exc)}\n(no traceback: {_describe(failure)})\n"


def _describe(exc: BaseException) -> str:
    try:
        return f"{type(exc).__name__}: {exc}"
    except Exception:
        return type(exc).__name__


def _stamp() -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return f"{time.time():.0f}"
