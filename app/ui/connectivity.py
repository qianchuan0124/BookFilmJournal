"""探索空间连通性检测的全局服务。

应用启动时由 MainWindow 触发一次后台检测，结果写进 explore 的会话内可达性缓存；
探索空间页面复用同一份结果与进度，避免重复检测。

检测跑在守护线程里（不是 QThread）：应用在检测中途退出时线程随进程结束，
不会触发 QThread 销毁告警，更不会让进程中止。
"""
from __future__ import annotations

import threading
from typing import Optional

from PySide6.QtCore import QObject, Signal

from app import explore


class ConnectivityService(QObject):
    """同一时刻只跑一次检测；检测中再次请求会被忽略。"""

    started = Signal()
    progress = Signal(int, int)
    finished = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._db = None
        self._thread: Optional[threading.Thread] = None
        self._stopping = False

    def bind(self, db) -> None:
        self._db = db

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, sites: Optional[list[dict]] = None, db=None) -> None:
        if db is not None:
            self._db = db
        if self.is_running():
            return
        if sites is None:
            if self._db is None:
                return
            sites = explore.load_state(self._db).get('sites') or []
        if not sites:
            return
        self._stopping = False
        self._thread = threading.Thread(target=self._run, args=(list(sites),), daemon=True)
        self._thread.start()
        self.started.emit()

    def _run(self, sites: list[dict]) -> None:
        try:
            result = explore.check_all(
                sites,
                progress=lambda done, total: self.progress.emit(done, total),
                should_stop=lambda: self._stopping,
            )
            if self._stopping:
                return
            explore.set_reachability(result)
            self.finished.emit(result)
        finally:
            self._thread = None

    def shutdown(self) -> None:
        """只置停止标记：守护线程随进程退出，不阻塞也不强杀。"""
        self._stopping = True


_service: Optional[ConnectivityService] = None


def service() -> ConnectivityService:
    """全局单例：启动检测与页面手动检测共用同一份状态。"""
    global _service
    if _service is None:
        _service = ConnectivityService()
    return _service
