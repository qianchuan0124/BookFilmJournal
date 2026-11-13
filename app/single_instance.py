"""同一用户会话内只允许运行一个实例。

主实例持有 QLockFile；重复启动时通过锁文件中记录的 PID 找到主窗口，
将其从最小化/隐藏状态还原并置于前台。进程异常退出后 QLockFile 会发现
原 PID 已不存在，锁被视为过期并被接管，不会残留锁阻塞下次启动。
"""
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QLockFile

_LOCK_PATH = Path(tempfile.gettempdir()) / 'shuyingjilu.lock'
_lock: Optional[QLockFile] = None


def acquire(activate_timeout: float = 8.0) -> bool:
    """尝试成为唯一实例。

    返回 True 表示当前进程是主实例；返回 False 表示已有实例在运行，
    已尝试把它的窗口还原并置前，当前进程应尽快退出。
    """
    global _lock
    lock = QLockFile(str(_LOCK_PATH))
    if lock.tryLock(0):
        _lock = lock
        return True
    if lock.error() != QLockFile.LockError.LockFailedError:
        # 锁文件无法创建（如临时目录不可写）时，不能因此阻断启动。
        return True
    pid, _hostname, _appname = lock.getLockInfo()
    _activate_window(int(pid or 0), activate_timeout)
    return False


def _activate_window(pid: int, timeout: float) -> bool:
    if not pid or sys.platform != 'win32':
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
    ]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]

    def find_main_window() -> int:
        handles = []

        @enum_proc
        def callback(hwnd, _lparam):
            window_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
            if window_pid.value != pid or not user32.IsWindowVisible(hwnd):
                return True
            if user32.GetWindowTextLengthW(hwnd) <= 0:
                return True
            handles.append(hwnd)
            return False

        user32.EnumWindows(callback, 0)
        return int(handles[0]) if handles else 0

    sw_restore = 9
    hwnd_top = 0
    swp_nosize, swp_nomove, swp_showwindow = 0x0001, 0x0002, 0x0040
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        hwnd = find_main_window()
        if hwnd:
            user32.ShowWindow(hwnd, sw_restore)
            user32.SetWindowPos(
                hwnd, hwnd_top, 0, 0, 0, 0,
                swp_nomove | swp_nosize | swp_showwindow,
            )
            user32.SetForegroundWindow(hwnd)
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
