from __future__ import annotations

import ctypes
import json
import re
from ctypes import wintypes
from pathlib import Path
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
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL

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
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
GW_HWNDNEXT = 2
GWLP_HWNDPARENT = -8
HWND_NOTOPMOST = wintypes.HWND(-2)


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


def stack_window_above(hwnd: int, below_hwnd: int) -> None:
    if not hwnd or not below_hwnd or hwnd == below_hwnd:
        return
    if not user32.IsWindow(hwnd) or not user32.IsWindow(below_hwnd):
        return
    set_window_owner(hwnd, below_hwnd)


_GENERIC_WORKSPACE_WORDS = {"service", "the", "a", "and", "workspace"}


def _decode_workspace_blob(item: str) -> dict | None:
    blob = item
    if "\\u0022" in blob or "\u0022" in blob:
        blob = blob.encode("utf-8", errors="ignore").decode("unicode_escape", errors="ignore")
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _path_settings_workspaces() -> list[dict]:
    if not PATH_SETTINGS.is_file():
        return []
    try:
        text = PATH_SETTINGS.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    workspaces: list[dict] = []
    match = re.search(r"Workspaces=(.+?);", text, re.DOTALL)
    if match:
        try:
            items = json.loads(match.group(1))
        except json.JSONDecodeError:
            items = []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, str):
                    data = _decode_workspace_blob(item)
                    if data:
                        workspaces.append(data)
    return workspaces


def _path_settings_active_name() -> str | None:
    for data in _path_settings_workspaces():
        if data.get("Active") is True:
            name = data.get("Name")
            if name:
                return str(name)
    if not PATH_SETTINGS.is_file():
        return None
    try:
        text = PATH_SETTINGS.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("Base="):
            base = line[5:].rstrip(";").strip()
            if base:
                return Path(base).name
    return None


def _json_names(data) -> list[str]:
    names: list[str] = []
    if isinstance(data, list):
        for item in data:
            names.extend(_json_names(item))
    elif isinstance(data, dict):
        ident = data.get("id")
        if isinstance(ident, dict) and isinstance(ident.get("name"), str):
            names.append(ident["name"])
        if isinstance(data.get("name"), str):
            names.append(data["name"])
        for key in ("themes", "groups", "children", "items"):
            if key in data:
                names.extend(_json_names(data[key]))
    return names


def _live_content_names(base: str) -> list[str]:
    names: list[str] = []
    for path in ("/v1/media/playlists", "/v1/themes", "/v1/playlists"):
        try:
            status, body = _request(f"{base}{path}")
        except (URLError, HTTPError, TimeoutError, OSError):
            continue
        if status != 200 or not body:
            continue
        try:
            data = json.loads(body.decode("utf-8", errors="ignore"))
        except json.JSONDecodeError:
            continue
        names.extend(_json_names(data))
    return names


def _workspace_tokens(name: str) -> set[str]:
    return {
        part
        for part in name.casefold().replace("-", " ").split()
        if part not in _GENERIC_WORKSPACE_WORDS and len(part) > 2
    }


def _score_workspace(name: str, live_names: list[str]) -> int:
    tokens = _workspace_tokens(name)
    if not tokens:
        return 0
    score = 0
    needle = name.casefold()
    for live in live_names:
        lower = live.casefold()
        if needle in lower:
            score += 10
            continue
        padded = f" {lower} "
        for token in tokens:
            if padded.find(f" {token} ") >= 0 or lower.startswith(token + " ") or lower == token:
                score += 1
    return score


def active_workspace_name() -> str | None:
    configured = [str(item.get("Name")) for item in _path_settings_workspaces() if item.get("Name")]
    base = find_api_base()
    if base and configured:
        live = _live_content_names(base)
        best_name = None
        best_score = 0
        for name in configured:
            score = _score_workspace(name, live)
            if score > best_score:
                best_score = score
                best_name = name
        if best_name and best_score > 0:
            return best_name
    return _path_settings_active_name()


def workspace_is_open(workspace_name: str) -> bool:
    if not workspace_name or not is_propresenter_running():
        return False
    active = active_workspace_name()
    if not active:
        return False
    return active.casefold() == workspace_name.casefold()


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


def find_api_base() -> str | None:
    if not is_propresenter_running():
        return None
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
            return f"http://127.0.0.1:{port}"
    return None


def list_video_inputs(base: str | None = None) -> list[dict]:
    root = base or find_api_base()
    if not root:
        return []
    try:
        status, body = _request(f"{root}/v1/video_inputs")
    except (URLError, HTTPError, TimeoutError, OSError):
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
    except (URLError, HTTPError, TimeoutError, OSError):
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
