from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from qfluentwidgets import (
    CaptionLabel, FluentIcon, FluentWindow, InfoBar, InfoBarPosition,
    NavigationItemPosition, qconfig,
)

from app.cloud_sync import auto_backup, auto_restore
from app.config import APP_VERSION_TEXT
from app.db import Database
from app.media_meta import seed_from_doulist_items
from app.ui.records_page import RecordsPage
from app.ui.doulist_page import DoulistPage
from app.ui.report_page import ReportPage
from app.ui.plan_page import PlanPage
from app.ui.data_page import DataPage
from app.ui.recycle_page import RecyclePage
from app.ui.explore_page import ExplorePage


class MainWindow(FluentWindow):
    def __init__(self, db: Database = None):
        super().__init__()
        self.db = db or Database()
        # 历史数据里计划/豆列条目可能缺封面（尤其从 iOS 快照恢复后），启动时顺手补一次。
        self.db.fill_item_covers_from_records()
        # 豆列条目自带的时长/页数先补进本机元数据，避免重复联网补全。
        seed_from_doulist_items(self.db)
        self.setWindowTitle('书影记录')
        self.resize(1280, 860)
        # 关闭页面切换时的上滑动画，避免切换后页面先偏下再跳到顶部。
        self.stackedWidget.setAnimationEnabled(False)

        self.movie_page = RecordsPage(self.db, 'movie', self)
        self.book_page = RecordsPage(self.db, 'book', self)
        self.doulist_page = DoulistPage(self.db, self)
        self.plan_page = PlanPage(self.db, self)
        self.report_page = ReportPage(self.db, self)
        self.data_page = DataPage(self.db, self)
        self.recycle_page = RecyclePage(self.db, self)
        self.explore_page = ExplorePage(self.db, self)

        for page, icon, text in (
            (self.movie_page, FluentIcon.MOVIE, '观影记录'),
            (self.book_page, FluentIcon.LIBRARY, '读书记录'),
            (self.doulist_page, FluentIcon.ALBUM, '我的豆列'),
            (self.plan_page, FluentIcon.CALENDAR, '我的计划'),
            (self.report_page, FluentIcon.DOCUMENT, '报告中心'),
            (self.explore_page, FluentIcon.GLOBE, '探索空间'),
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

        # 自动备份：数据变动后合并成一次，延迟生成自动快照（手动备份不受影响）。
        self._auto_backup_timer = QTimer(self)
        self._auto_backup_timer.setSingleShot(True)
        self._auto_backup_timer.timeout.connect(self._run_auto_backup)

        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.center() - self.rect().center())
        # 启动后稍等界面就绪，再尝试自动同步云端最新快照。
        QTimer.singleShot(1800, self._run_auto_sync)

    def _on_theme_changed(self) -> None:
        self.movie_page.apply_theme()
        self.book_page.apply_theme()

    def _schedule_auto_backup(self, delay: int = 30000) -> None:
        if self.db.get_setting('cloud_sync_auto_backup') != '1':
            return
        self._auto_backup_timer.start(delay)

    def _run_auto_backup(self) -> None:
        result = auto_backup(self.db)
        if result and not result.get('skipped'):
            self.data_page.refresh_sync()

    def _run_auto_sync(self) -> None:
        result = auto_restore(self.db)
        if not result:
            return
        counts = result.get('counts') or {}
        self.movie_page.mark_dirty()
        self.book_page.mark_dirty()
        self.doulist_page.mark_dirty()
        self.plan_page.mark_dirty()
        self.report_page.mark_dirty()
        self.data_page.mark_dirty()
        self.data_page.refresh_sync()
        # 恢复后自动补全缺失的封面 / 页数 / 时长（后台线程 + 悬浮进度）。
        self.data_page.auto_backfill(restored=True)
        InfoBar.success(
            '已自动同步云端数据',
            f"启动时检测到更新的快照，已恢复 {counts.get('records', 0)} 条记录。",
            parent=self, position=InfoBarPosition.TOP_RIGHT, duration=8000,
        )

    def closeEvent(self, event) -> None:
        # 退出前落一次自动备份，避免刚改完就退出导致云端落后。
        if self.db.get_setting('cloud_sync_auto_backup') == '1':
            self._auto_backup_timer.stop()
            auto_backup(self.db)
        super().closeEvent(event)

    def _on_synced(self, result: dict) -> None:
        self._schedule_auto_backup(delay=5000)
        self.movie_page.mark_dirty()
        self.book_page.mark_dirty()
        self.doulist_page.mark_dirty()
        self.plan_page.mark_dirty()
        self.data_page.mark_dirty()
        self.report_page.mark_dirty()

    def _on_records_changed(self, media_type: str = '') -> None:
        self._schedule_auto_backup()
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
