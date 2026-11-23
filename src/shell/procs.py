# -*- coding: utf-8 -*-
"""procs.py —— 纯 ctypes 查命令行 / 结束进程（不启动 PowerShell）。

原来这两件事都走 `powershell -Command Get-CimInstance Win32_Process`：
冷启动 powershell.exe 要 1～3 秒，而且会闪一个控制台黑框。启动流程里要跑两次
（清遗留外壳进程、登录成功后关登录窗口），用户看到的就是
「打开要等好久，还要弹两个 PowerShell 框」。

改用 Toolhelp32 快照 + NtQueryInformationProcess(ProcessCommandLineInformation)
+ TerminateProcess，单次 10 毫秒级，且完全不出窗口。
"""
import ctypes
import ctypes.wintypes as wt
import os
import time

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_ntdll = ctypes.WinDLL("ntdll")


class _ENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * 260),
    ]


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wt.USHORT),
                ("MaximumLength", wt.USHORT),
                ("Buffer", ctypes.c_void_p)]


_k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
_k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
_k32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(_ENTRY32W)]
_k32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(_ENTRY32W)]
_k32.OpenProcess.restype = wt.HANDLE
_k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_k32.CloseHandle.argtypes = [wt.HANDLE]
_k32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
_ntdll.NtQueryInformationProcess.argtypes = [wt.HANDLE, wt.ULONG, ctypes.c_void_p,
                                             wt.ULONG, ctypes.POINTER(wt.ULONG)]


def list_pids():
    """[(pid, exe 名)]，一次快照。"""
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return []
    out = []
    e = _ENTRY32W()
    e.dwSize = ctypes.sizeof(e)
    try:
        ok = _k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out.append((int(e.th32ProcessID), e.szExeFile))
            ok = _k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        _k32.CloseHandle(snap)
    return out


def cmdline(pid):
    """进程完整命令行；拿不到返回 ""（系统进程、权限不足都很正常）。"""
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return ""
    try:
        need = wt.ULONG(0)
        st = _ntdll.NtQueryInformationProcess(h, 60, None, 0, ctypes.byref(need))
        if (st & 0xFFFFFFFF) != STATUS_INFO_LENGTH_MISMATCH or not need.value:
            return ""
        buf = ctypes.create_string_buffer(need.value)
        st = _ntdll.NtQueryInformationProcess(h, 60, buf, need.value, ctypes.byref(need))
        if (st & 0xFFFFFFFF) != 0:
            return ""
        us = ctypes.cast(buf, ctypes.POINTER(_UNICODE_STRING)).contents
        if not us.Buffer or not us.Length:
            return ""
        return ctypes.wstring_at(us.Buffer, us.Length // 2)
    except Exception:
        return ""
    finally:
        _k32.CloseHandle(h)


def kill(pid):
    h = _k32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
    if not h:
        return False
    try:
        return bool(_k32.TerminateProcess(h, 1))
    finally:
        _k32.CloseHandle(h)


def reap(profile_dir, exe_names=("msedge.exe", "chrome.exe"), settle=0.5):
    """结束命令行里带这个 profile 目录名的一切浏览器进程，返回被杀的 pid 列表。

    只匹配 profile 目录名（ui_profile / login_profile），不碰用户自己的 Edge。
    """
    tag = os.path.basename(str(profile_dir).rstrip("\\/")).lower()
    if not tag:
        return []
    me = os.getpid()
    killed = []
    for pid, name in list_pids():
        if pid == me or name.lower() not in exe_names:
            continue
        if tag in cmdline(pid).lower():
            if kill(pid):
                killed.append(pid)
    if killed and settle:
        time.sleep(settle)
    return killed


def find_by_profile(profile_dir, exe_names=("msedge.exe", "chrome.exe")):
    """还没被杀掉、仍带这个 profile 的浏览器进程数。"""
    tag = os.path.basename(str(profile_dir).rstrip("\\/")).lower()
    n = 0
    for pid, name in list_pids():
        if name.lower() not in exe_names:
            continue
        if tag in cmdline(pid).lower():
            n += 1
    return n
