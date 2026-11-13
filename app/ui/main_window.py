from PySide6.QtWidgets import QApplication

from qfluentwidgets import (
    CaptionLabel, FluentIcon, FluentWindow, NavigationItemPosition, qconfig,
)

from app.config import APP_VERSION_TEXT
from app.db import Database
from app.ui.records_page import RecordsPage
from app.ui.doulist_page import DoulistPage
from app.ui.report_page import ReportPage
from app.ui.plan_page import PlanPage
from app.ui.data_page import DataPage
from app.ui.recycle_page import RecyclePage


class MainWindow(FluentWindow):
    def __init__(self, db: Database = None):
        super().__init__()
        self.db = db or Database()
        self.setWindowTitle('书影记录')
        self.resize(1280, 860)

        self.movie_page = RecordsPage(self.db, 'movie', self)
        self.book_page = RecordsPage(self.db, 'book', self)
        self.doulist_page = DoulistPage(self.db, self)
        self.plan_page = PlanPage(self.db, self)
        self.report_page = ReportPage(self.db, self)
        self.data_page = DataPage(self.db, self)
        self.recycle_page = RecyclePage(self.db, self)

        for page, icon, text in (
            (self.movie_page, FluentIcon.MOVIE, '观影记录'),
            (self.book_page, FluentIcon.LIBRARY, '读书记录'),
            (self.doulist_page, FluentIcon.ALBUM, '我的豆列'),
            (self.plan_page, FluentIcon.CALENDAR, '我的计划'),
            (self.report_page, FluentIcon.DOCUMENT, '报告中心'),
            (self.data_page, FluentIcon.SETTING, '应用设置'),
            (self.recycle_page, FluentIcon.DELETE, '回收站'),
        ):
            self.addSubInterface(page, icon, text, NavigationItemPosition.TOP, isTransparent=True)

        self.version_label = CaptionLabel(APP_VERSION_TEXT, self)
        self.version_label.setTextColor('#64748b', '#7f8899')
        self.version_label.setContentsMargins(22, 8, 16, 14)
        self.navigationInterface.panel.bottomLayout.addWidget(self.version_label)
        self.setCustomBackgroundColor('#e9eef5', '#202020')
        self.navigationInterface.setExpandWidth(200)
        self.navigationInterface.expand(useAni=False)

        self.movie_page.synced.connect(self._on_synced)
        self.book_page.synced.connect(self._on_synced)
        self.doulist_page.synced.connect(self._on_synced)
        self.movie_page.record_changed.connect(self._on_records_changed)
        self.book_page.record_changed.connect(self._on_records_changed)
        self.data_page.records_changed.connect(self._on_all_records_changed)
        self.recycle_page.changed.connect(self._on_recycle_changed)

        qconfig.themeChangedFinished.connect(self._on_theme_changed)

        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.center() - self.rect().center())

    def _on_theme_changed(self) -> None:
        self.movie_page.apply_theme()
        self.book_page.apply_theme()

    def _on_synced(self, result: dict) -> None:
        self.movie_page.mark_dirty()
        self.book_page.mark_dirty()
        self.doulist_page.mark_dirty()
        self.plan_page.mark_dirty()
        self.data_page.mark_dirty()
        self.report_page.mark_dirty()

    def _on_records_changed(self, media_type: str = '') -> None:
        # 记录页在改动后已自行刷新；指定类型时另一类型页面不受影响，无需重建。
        if not media_type:
            self.movie_page.mark_dirty()
            self.book_page.mark_dirty()
        self.doulist_page.mark_dirty()
        self.plan_page.mark_dirty()
        self.report_page.mark_dirty()
        self.data_page.mark_dirty()

    def _on_all_records_changed(self) -> None:
        self._on_records_changed()

    def _on_recycle_changed(self) -> None:
        self.movie_page.mark_dirty()
        self.book_page.mark_dirty()
        self.doulist_page.mark_dirty()
        self.plan_page.mark_dirty()
        self.report_page.mark_dirty()
        self.data_page.mark_dirty()
