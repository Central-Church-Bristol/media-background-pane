from __future__ import annotations

import ctypes
import json
import re
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PATH_SETTINGS = (
    Path.home()
    / "AppData"
    / "Roaming"
    / "RenewedVision"
    / "ProPresenter"
    / "PathSettings.proPaths"
)
# ProPresenter's own logs. Log.log is the run going on now; the nine before it
# are kept as Log.1.log ... Log.9.log.
PP_LOGS = Path.home() / "AppData" / "Local" / "RenewedVision" / "ProPresenter" / "Logs"
NETWORK_PREFS = (
    Path.home()
    / "AppData"
    / "Roaming"
    / "RenewedVision"
    / "ProPresenter"
    / "Preferences"
    / "NetworkPreferences.proPref"
)
PROCESS_NAMES = {"propresenter.exe", "propresenter64.exe"}
_API_PORTS = (1025, 50000, 50001)


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32FirstW.restype = wintypes.BOOL
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


user32 = ctypes.WinDLL("user32", use_last_error=True)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [
    wintypes.HWND,
    wintypes.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_uint,
]
user32.SetWindowPos.restype = wintypes.BOOL
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL

dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
dwmapi.DwmGetWindowAttribute.argtypes = [
    wintypes.HWND,
    ctypes.c_uint,
    ctypes.c_void_p,
    ctypes.c_uint,
]
dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SW_SHOWNOACTIVATE = 4
SW_SHOWNA = 8
GW_HWNDNEXT = 2
GW_HWNDPREV = 3
GWLP_HWNDPARENT = -8
HWND_TOP = wintypes.HWND(0)
HWND_NOTOPMOST = wintypes.HWND(-2)
_Z_ORDER_WALK_LIMIT = 256


def _visible_window_rect(hwnd: int) -> wintypes.RECT | None:
    rect = wintypes.RECT()
    status = dwmapi.DwmGetWindowAttribute(
        hwnd,
        DWMWA_EXTENDED_FRAME_BOUNDS,
        ctypes.byref(rect),
        ctypes.sizeof(rect),
    )
    if status == 0:
        return rect
    if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return rect
    return None


def native_window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    rect = _visible_window_rect(hwnd)
    if rect is None:
        return None
    return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))


def set_visible_window_rect(hwnd: int, left: int, top: int, right: int, bottom: int) -> None:
    visible = _visible_window_rect(hwnd)
    frame = wintypes.RECT()
    if visible is None or not user32.GetWindowRect(hwnd, ctypes.byref(frame)):
        user32.SetWindowPos(
            hwnd,
            None,
            left,
            top,
            max(1, right - left),
            max(1, bottom - top),
            SWP_NOZORDER | SWP_NOACTIVATE,
        )
        return
    user32.SetWindowPos(
        hwnd,
        None,
        left + (int(frame.left) - int(visible.left)),
        top + (int(frame.top) - int(visible.top)),
        max(1, (right - left) + (int(frame.right) - int(visible.right)) - (int(frame.left) - int(visible.left))),
        max(1, (bottom - top) + (int(frame.bottom) - int(visible.bottom)) - (int(frame.top) - int(visible.top))),
        SWP_NOZORDER | SWP_NOACTIVATE,
    )


def native_cursor_pos() -> tuple[int, int]:
    point = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    return int(point.x), int(point.y)


def propresenter_pids() -> list[int]:
    TH32CS_SNAPPROCESS = 0x00000002
    INVALID = ctypes.c_void_p(-1).value
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID:
        return []
    pids: list[int] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snap, ctypes.byref(entry)):
            return []
        while True:
            if entry.szExeFile.lower() in PROCESS_NAMES:
                pids.append(int(entry.th32ProcessID))
            if not kernel32.Process32NextW(snap, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snap)
    return pids


def is_propresenter_running() -> bool:
    return bool(propresenter_pids())


def propresenter_main_window() -> tuple[int, tuple[int, int, int, int]] | None:
    pids = set(propresenter_pids())
    if not pids:
        return None
    best_hwnd = 0
    best_rect: tuple[int, int, int, int] | None = None
    best_area = 0

    def _enum(hwnd: int, _lparam: int) -> bool:
        nonlocal best_hwnd, best_rect, best_area
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if int(pid.value) not in pids:
            return True
        ex = int(user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE))
        if ex & WS_EX_TOOLWINDOW:
            return True
        rect = _visible_window_rect(hwnd)
        if rect is None:
            return True
        width = int(rect.right) - int(rect.left)
        height = int(rect.bottom) - int(rect.top)
        if width < 400 or height < 300:
            return True
        area = width * height
        if area > best_area:
            best_area = area
            best_hwnd = int(hwnd)
            best_rect = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
        return True

    cb = WNDENUMPROC(_enum)
    user32.EnumWindows(cb, 0)
    if not best_hwnd or best_rect is None:
        return None
    return best_hwnd, best_rect


def propresenter_window_rect() -> tuple[int, int, int, int] | None:
    found = propresenter_main_window()
    return found[1] if found else None


def propresenter_main_hwnd() -> int | None:
    found = propresenter_main_window()
    return found[0] if found else None


def window_owner(hwnd: int) -> int:
    if not hwnd:
        return 0
    return int(user32.GetWindowLongPtrW(hwnd, GWLP_HWNDPARENT) or 0)


def native_window_visible(hwnd: int) -> bool:
    return bool(hwnd and user32.IsWindow(hwnd) and user32.IsWindowVisible(hwnd))


def show_window_no_activate(hwnd: int) -> None:
    if hwnd and user32.IsWindow(hwnd):
        user32.ShowWindow(hwnd, SW_SHOWNA)


def window_is_minimized(hwnd: int) -> bool:
    return bool(hwnd and user32.IsWindow(hwnd) and user32.IsIconic(hwnd))


def window_is_cloaked(hwnd: int) -> bool:
    """True when Windows keeps the window off screen though it counts as visible
    (another virtual desktop, or hidden by the shell)."""
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    cloaked = wintypes.DWORD(0)
    status = dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    return status == 0 and bool(cloaked.value)


def restore_window_no_activate(hwnd: int) -> None:
    """Un-minimise a window without taking the focus from ProPresenter."""
    if window_is_minimized(hwnd):
        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)


def set_window_owner(hwnd: int, owner_hwnd: int) -> None:
    if not hwnd or not user32.IsWindow(hwnd):
        return
    if owner_hwnd and not user32.IsWindow(owner_hwnd):
        return
    if window_owner(hwnd) == int(owner_hwnd or 0):
        return
    user32.SetWindowLongPtrW(hwnd, GWLP_HWNDPARENT, owner_hwnd or 0)
    user32.SetWindowPos(
        hwnd,
        None,
        0,
        0,
        0,
        0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
    )


def apply_tool_window_style(hwnd: int) -> None:
    if not hwnd or not user32.IsWindow(hwnd):
        return
    WS_EX_APPWINDOW = 0x00040000
    ex = int(user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE))
    new_ex = (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
    if ex == new_ex:
        return
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, new_ex)
    user32.SetWindowPos(
        hwnd,
        None,
        0,
        0,
        0,
        0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
    )


def _window_is_topmost(hwnd: int) -> bool:
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    return bool(int(user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)) & WS_EX_TOPMOST)


def window_is_above(hwnd: int, other: int) -> bool:
    """True when hwnd is closer to the user than other."""
    if not hwnd or not other or hwnd == other:
        return False
    current = int(other)
    for _ in range(_Z_ORDER_WALK_LIMIT):
        current = int(user32.GetWindow(current, GW_HWNDPREV) or 0)
        if not current:
            return False
        if current == int(hwnd):
            return True
    return False


def _place_window_above(hwnd: int, below_hwnd: int) -> None:
    """Seat hwnd immediately above below_hwnd without activating it.

    hWndInsertAfter is the window that stays in front. Passing the window
    directly above ProPresenter lands the pane between that window and
    ProPresenter. HWND_TOP is only the top of the normal band, so a topmost
    audience output stays in front.
    """
    predecessor = int(user32.GetWindow(below_hwnd, GW_HWNDPREV) or 0)
    if predecessor == int(hwnd):
        return
    insert_after = HWND_TOP if not predecessor or _window_is_topmost(predecessor) else predecessor
    user32.SetWindowPos(
        hwnd,
        insert_after,
        0,
        0,
        0,
        0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
    )


def stack_window_above(hwnd: int, below_hwnd: int) -> None:
    if not hwnd or not below_hwnd or hwnd == below_hwnd:
        return
    if not user32.IsWindow(hwnd) or not user32.IsWindow(below_hwnd):
        return
    # Ownership alone does not move the pane. ProPresenter can already be
    # fullscreen, and SWP_NOZORDER leaves that stack unchanged.
    set_window_owner(hwnd, below_hwnd)
    if not window_is_above(hwnd, below_hwnd):
        _place_window_above(hwnd, below_hwnd)


# Which workspace ProPresenter has open.
#
# A few seconds after it starts, each ProPresenter run logs exactly one line
# naming the workspace it opened, tagged with its process id:
#   04/11/2026 18:54:27 <tab> 0:00:04.389 <tab> P23256 T14 [R] <tab> INFO - Opened
#   [C:\Users\...\LocalWorkspaces\Evening Service\Configuration\Workspace]
# The default workspace opens ...\Documents\ProPresenter\Configuration\Workspace.
# Switching workspace restarts ProPresenter, so the line holds for the whole run.
# Every log from 31 Oct to 4 Nov 2026 has exactly one such line per run, about
# 140 KB in. Nothing here asks ProPresenter over the network: this runs every
# second on the GUI thread.
_LOG_FILES = ("Log.log", "Log.1.log")
_LOG_READ_LIMIT = 4_000_000
_LOG_WAIT = 30.0  # seconds to wait for a new run's line before using Base=
_LOG_OPENED = re.compile(r"\tP(\d+) T\d+ +\[R\]\tINFO - Opened \[([^\]\r\n]*\\Configuration\\Workspace)\]")
_LOG_STARTED = re.compile(r"\tP(\d+) T\d+ +\[R\]\tINFO - Main Started")


@dataclass(frozen=True)
class OpenWorkspace:
    """Which workspace ProPresenter has open, and how that is known.

    source is "log" (ProPresenter's own log line), "Base=" (the Base= line in
    PathSettings.proPaths, used when the log has no answer), "unknown"
    (ProPresenter is running but has not logged its workspace yet), or
    "not running".
    """

    name: str | None
    source: str
    pid: int | None
    detail: str

    @property
    def running(self) -> bool:
        return self.source != "not running"

    def is_named(self, wanted: str) -> bool:
        return bool(self.name and wanted) and self.name.casefold() == wanted.casefold()


# Per ProPresenter run (process id). Dropped when the run ends.
_log_workspaces: dict[int, tuple[str, str]] = {}  # pid -> (name, folder) from its log line
_first_seen: dict[int, float] = {}  # pid -> when it was first seen running
_no_log_line: dict[int, str] = {}  # pid -> why its log gave no answer, so Base= is used


def open_workspace(pids: list[int] | None = None, *, clock=time.monotonic) -> OpenWorkspace:
    """Which workspace the running ProPresenter has open, from its log."""
    running = sorted(set(propresenter_pids() if pids is None else pids))
    now = clock()
    for pid in list(_first_seen):
        if pid not in running:
            _first_seen.pop(pid, None)
            _log_workspaces.pop(pid, None)
            _no_log_line.pop(pid, None)
    if not running:
        return OpenWorkspace(None, "not running", None, "ProPresenter is not running")
    for pid in running:
        _first_seen.setdefault(pid, now)
    # Normally there is one. While one closes and the next starts, go by the newest.
    pid = max(running, key=lambda p: (_first_seen[p], p))
    found = _log_workspaces.get(pid)
    if found is None and pid not in _no_log_line:
        state, info = _read_log_workspace(pid)
        if state == "opened":
            folder = str(PureWindowsPath(info).parent.parent)
            found = (PureWindowsPath(folder).name, folder)
            _log_workspaces[pid] = found
        elif state == "unreadable" or now - _first_seen[pid] >= _LOG_WAIT:
            _no_log_line[pid] = info or f"no workspace line for P{pid} in its log after {_LOG_WAIT:.0f} s"
        else:
            return OpenWorkspace(None, "unknown", pid, f"ProPresenter P{pid} has not logged its workspace yet")
    if found is not None:
        name, folder = found
        return OpenWorkspace(name, "log", pid, f"ProPresenter P{pid} opened {_workspace_label(name, folder)}")
    why = _no_log_line.get(pid, "")
    base = _base_workspace()
    if base is None:
        return OpenWorkspace(None, "unknown", pid, f"ProPresenter P{pid}: {why}, and no Base= line")
    name, folder = base
    return OpenWorkspace(name, "Base=", pid, f"ProPresenter P{pid}: {why}; Base= says {_workspace_label(name, folder)}")


def workspace_is_open(workspace_name: str) -> bool:
    return open_workspace().is_named(workspace_name)


def _read_log_workspace(pid: int) -> tuple[str, str]:
    """For one run: ("opened", path), ("starting", ""), ("absent", "") or ("unreadable", why)."""
    started = False
    problems: list[str] = []
    found_a_log = False
    for name in _LOG_FILES:
        path = PP_LOGS / name
        try:
            with path.open("rb") as handle:
                raw = handle.read(_LOG_READ_LIMIT)
        except FileNotFoundError:
            continue
        except OSError as exc:
            problems.append(f"could not read {name} ({type(exc).__name__}: {exc})")
            continue
        found_a_log = True
        text = _log_text(raw)
        for match in _LOG_OPENED.finditer(text):
            if int(match.group(1)) == pid:
                return "opened", match.group(2)
        if not started:
            started = any(int(m.group(1)) == pid for m in _LOG_STARTED.finditer(text))
    if started:
        return "starting", ""
    if problems:
        return "unreadable", "; ".join(problems)
    if not found_a_log:
        return "unreadable", f"no ProPresenter log in {PP_LOGS}"
    return "absent", ""


def _log_text(raw: bytes) -> str:
    """ProPresenter's log as text. It starts with a byte-order mark and can hold NUL bytes."""
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return raw.replace(b"\x00", b"").decode("utf-8", errors="replace")


def _base_workspace() -> tuple[str, str] | None:
    """The workspace folder on the Base= line of PathSettings.proPaths, as (name, folder)."""
    try:
        text = PATH_SETTINGS.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("Base="):
            base = line[5:].rstrip(";").strip()
            if base:
                folder = PureWindowsPath(base)
                return folder.name, str(folder)
    return None


def _workspace_label(name: str, folder: str) -> str:
    if PureWindowsPath(folder).parent.name.casefold() == "localworkspaces":
        return f"workspace {name}"
    return f"the default workspace ({name})"


def _ports_to_try() -> list[int]:
    ports: list[int] = []
    if NETWORK_PREFS.is_file():
        try:
            raw = NETWORK_PREFS.read_bytes()
        except OSError:
            raw = b""
        text = "".join(chr(b) if 32 <= b < 127 else " " for b in raw)
        for match in re.finditer(r"\b(\d{4,5})\b", text):
            port = int(match.group(1))
            if 1024 <= port <= 65535 and port not in ports:
                ports.append(port)
    for port in _API_PORTS:
        if port not in ports:
            ports.append(port)
    return ports


def _request(url: str, timeout: float = 0.8):
    req = Request(url, headers={"Accept": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        status = getattr(resp, "status", 200)
        return status, body


def _video_input_duration(duration_seconds: float | None) -> float:
    if duration_seconds is None:
        return 5.0
    return max(0.0, float(duration_seconds))


# ProPresenter's API address, per run (process id). The Video Input button
# asks ProPresenter for its status every second from the GUI thread; probing
# every port each time made that several requests a second.
_api_bases: dict[int, str] = {}


def find_api_base() -> str | None:
    pids = propresenter_pids()
    for pid in list(_api_bases):
        if pid not in pids:
            del _api_bases[pid]
    if not pids:
        return None
    for pid in pids:
        if pid in _api_bases:
            return _api_bases[pid]
    for port in _ports_to_try():
        url = f"http://127.0.0.1:{port}/version"
        try:
            status, body = _request(url)
        except (URLError, HTTPError, TimeoutError, OSError):
            continue
        if status != 200 or not body:
            continue
        try:
            data = json.loads(body.decode("utf-8", errors="ignore"))
        except json.JSONDecodeError:
            continue
        host = str(data.get("host_description", "")).lower()
        name = str(data.get("name", "")).lower()
        if "propresenter" in host or name:
            base = f"http://127.0.0.1:{port}"
            _api_bases[max(pids)] = base
            return base
    return None


def _forget_api_base(base: str, error: BaseException) -> None:
    """A request to base could not connect: probe the ports again next time."""
    if isinstance(error, HTTPError):
        return  # it answered, so the address is right
    for pid, known in list(_api_bases.items()):
        if known == base:
            del _api_bases[pid]


def list_video_inputs(base: str | None = None) -> list[dict]:
    root = base or find_api_base()
    if not root:
        return []
    try:
        status, body = _request(f"{root}/v1/video_inputs")
    except (URLError, HTTPError, TimeoutError, OSError) as exc:
        _forget_api_base(root, exc)
        return []
    if status != 200 or not body:
        return []
    try:
        data = json.loads(body.decode("utf-8", errors="ignore"))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _get_ok(url: str) -> bool:
    try:
        status, _body = _request(url)
    except HTTPError as err:
        if err.code == 400 and "?" in url:
            try:
                status, _body = _request(url.split("?", 1)[0])
            except (URLError, HTTPError, TimeoutError, OSError):
                return False
        else:
            return False
    except (URLError, TimeoutError, OSError):
        return False
    return 200 <= status < 300


def trigger_first_video_input(duration_seconds: float | None = None) -> bool:
    """Trigger the first ProPresenter video input. Returns True on success."""
    base = find_api_base()
    if not base:
        return False
    inputs = list_video_inputs(base)
    if not inputs:
        return False
    first = inputs[0]
    key = first.get("uuid") or first.get("id")
    if key is None:
        key = first.get("index", 0)
    seconds = _video_input_duration(duration_seconds)
    return _get_ok(f"{base}/v1/video_inputs/{key}/trigger?duration={seconds:g}")


def clear_video_input_layer(duration_seconds: float | None = None) -> bool:
    """Clear only the ProPresenter video input layer. Returns True on success."""
    base = find_api_base()
    if not base:
        return False
    seconds = _video_input_duration(duration_seconds)
    return _get_ok(f"{base}/v1/clear/layer/video_input?duration={seconds:g}")


def video_input_layer_active() -> bool | None:
    """Return whether ProPresenter's video input layer is live, or None if unknown."""
    base = find_api_base()
    if not base:
        return None
    try:
        status, body = _request(f"{base}/v1/status/layers")
    except (URLError, HTTPError, TimeoutError, OSError) as exc:
        _forget_api_base(base, exc)
        return None
    if status != 200 or not body:
        return None
    try:
        data = json.loads(body.decode("utf-8", errors="ignore"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "video_input" not in data:
        return None
    return bool(data["video_input"])
