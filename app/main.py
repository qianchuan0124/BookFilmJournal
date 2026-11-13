import ctypes
import faulthandler
import os
import sys
import traceback


def _relax_webengine_sandbox_when_elevated() -> None:
    """以管理员身份运行时，Chromium 渲染进程的沙箱无法启动，
    内嵌的豆瓣登录页会白屏甚至让进程直接退出；这种环境下只能关掉它的沙箱。
    普通权限运行不受影响，保持默认沙箱。"""
    if sys.platform != 'win32':
        return
    try:
        elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return
    if not elevated:
        return
    flags = os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', '').strip()
    if '--no-sandbox' in flags.split():
        return
    os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = f'{flags} --no-sandbox'.strip()


# 必须在导入 QtWebEngine（登录弹窗依赖它）之前设置。
_relax_webengine_sandbox_when_elevated()

from PySide6.QtCore import QLocale, Qt
from PySide6.QtWidgets import QApplication, QLabel
from qfluentwidgets import Theme, setTheme, setThemeColor
from qfluentwidgets.components.date_time.calendar_view import DayScrollView, MonthScrollView

from app.db import Database
from app.profile import migrate_legacy_profile
from app.ui.main_window import MainWindow
from app.ui.styles import install_flat_buttons


CN_MONTHS = [f'{month} 月' for month in range(1, 13)]
CN_WEEKDAYS = ['一', '二', '三', '四', '五', '六', '日']


def _patch_calendar_months() -> None:
    """qfluentwidgets 的月份网格使用硬编码英文，这里替换成中文。"""
    if getattr(MonthScrollView, '_cn_months_patched', False):
        return
    original_months = MonthScrollView._initItems

    def patched_months(self):
        original_months(self)
        for index in range(self.count()):
            item = self.item(index)
            if item is not None:
                item.setText(CN_MONTHS[index % 12])

    MonthScrollView._initItems = patched_months
    MonthScrollView._cn_months_patched = True

    original_day = DayScrollView.__init__

    def patched_day(self, *args, **kwargs):
        original_day(self, *args, **kwargs)
        labels = self.weekDayGroup.findChildren(QLabel)
        for label, text in zip(labels, CN_WEEKDAYS):
            label.setText(text)

    DayScrollView.__init__ = patched_day


def _log_uncaught(exc_type, exc_value, exc_tb) -> None:
    traceback.print_exception(exc_type, exc_value, exc_tb)
    sys.stderr.flush()


def main() -> None:
    faulthandler.enable()
    sys.excepthook = _log_uncaught
    QLocale.setDefault(QLocale(QLocale.Language.Chinese, QLocale.Country.China))
    _patch_calendar_months()
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv)
    app.setApplicationName('书影记录')
    db = Database()
    migrate_legacy_profile(db)
    setTheme(Theme.LIGHT if db.get_setting('theme') == 'light' else Theme.DARK)
    install_flat_buttons()
    setThemeColor('#e5b45f')
    window = MainWindow(db)
    window.show()
    sys.exit(app.exec())
