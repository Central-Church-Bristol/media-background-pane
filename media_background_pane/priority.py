from __future__ import annotations

import ctypes
from ctypes import wintypes

IDLE_PRIORITY_CLASS = 0x00000040
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
NORMAL_PRIORITY_CLASS = 0x00000020
ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
HIGH_PRIORITY_CLASS = 0x00000080
REALTIME_PRIORITY_CLASS = 0x00000100

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# Stay below the apps that actually run the livestream.
LIVESTREAM_EXES = {
    "vmix.exe",
    "vmix64.exe",
    "vmix32.exe",
    "propresenter.exe",
    "propresenter64.exe",
}

_RANK = {
    IDLE_PRIORITY_CLASS: 0,
    BELOW_NORMAL_PRIORITY_CLASS: 1,
    NORMAL_PRIORITY_CLASS: 2,
    ABOVE_NORMAL_PRIORITY_CLASS: 3,
    HIGH_PRIORITY_CLASS: 4,
    REALTIME_PRIORITY_CLASS: 5,
}
_FROM_RANK = {rank: value for value, rank in _RANK.items()}


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
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.GetPriorityClass.argtypes = [wintypes.HANDLE]
kernel32.GetPriorityClass.restype = wintypes.DWORD
kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.SetPriorityClass.restype = wintypes.BOOL
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


def _livestream_pids() -> list[int]:
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
            if entry.szExeFile.lower() in LIVESTREAM_EXES:
                pids.append(int(entry.th32ProcessID))
            if not kernel32.Process32NextW(snap, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snap)
    return pids


def _priority_class(pid: int) -> int | None:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        value = int(kernel32.GetPriorityClass(handle))
        return value if value in _RANK else None
    finally:
        kernel32.CloseHandle(handle)


def _target_class() -> int:
    ranks = []
    for pid in _livestream_pids():
        value = _priority_class(pid)
        if value is not None:
            ranks.append(_RANK[value])
    # Never run at Normal or above — this pane should lose to the livestream.
    cap = _RANK[BELOW_NORMAL_PRIORITY_CLASS]
    if not ranks:
        return BELOW_NORMAL_PRIORITY_CLASS
    needed = min(ranks) - 1
    return _FROM_RANK[max(0, min(cap, needed))]


def prefer_livestream_apps() -> None:
    try:
        kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), _target_class())
    except OSError:
        pass
