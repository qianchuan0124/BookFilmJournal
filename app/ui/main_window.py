from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QWidget

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


# 页面首次点开时才实例化：一次性建 8 个页面要 200 多毫秒，多数页面启动时用不上。
_PAGE_FACTORIES = {
    'book_page': lambda window: RecordsPage(window.db, 'book', window),
    'doulist_page': lambda window: DoulistPage(window.db, window),
    'plan_page': lambda window: PlanPage(window.db, window),
    'report_page': lambda window: ReportPage(window.db, window),
    'explore_page': lambda window: ExplorePage(window.db, window),
    'data_page': lambda window: DataPage(window.db, window),
    'recycle_page': lambda window: RecyclePage(window.db, window),
}


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
        self._pages = {}
        self._factories = {}
        self._placeholders = {}
        self._register_page('movie_page', self.movie_page)
        self.addSubInterface(
            self.movie_page, FluentIcon.MOVIE, '观影记录',
            NavigationItemPosition.TOP, isTransparent=True,
        )

        # 其余页面首次点开时再建：整窗口一次性建 8 个页面要 200 多毫秒，且大多用不上。
        for name, route, icon, text in (
            ('book_page', 'recordsBook', FluentIcon.LIBRARY, '读书记录'),
            ('doulist_page', 'doulistPage', FluentIcon.ALBUM, '我的豆列'),
            ('plan_page', 'planPage', FluentIcon.CALENDAR, '我的计划'),
            ('report_page', 'reportPage', FluentIcon.DOCUMENT, '报告中心'),
            ('explore_page', 'explorePage', FluentIcon.GLOBE, '探索空间'),
            ('data_page', 'dataPage', FluentIcon.SETTING, '应用设置'),
            ('recycle_page', 'recyclePage', FluentIcon.DELETE, '回收站'),
        ):
            self._factories[name] = _PAGE_FACTORIES[name]
            placeholder = QWidget(self)
            placeholder.setObjectName(route)
            placeholder.setProperty('isStackedTransparent', True)
            self.stackedWidget.addWidget(placeholder)
            self._placeholders[name] = placeholder
            self.navigationInterface.addItem(
                routeKey=route, icon=icon, text=text,
                onClick=lambda _=False, key=name: self._open_page(key),
                position=NavigationItemPosition.TOP,
            )

        self.version_label = CaptionLabel(APP_VERSION_TEXT, self)
        self.version_label.setTextColor('#64748b', '#7f8899')
        self.version_label.setContentsMargins(22, 8, 16, 14)
        self.navigationInterface.panel.bottomLayout.addWidget(self.version_label)
        self.setCustomBackgroundColor('#e9eef5', '#202020')
        self.navigationInterface.setExpandWidth(200)
        self.navigationInterface.expand(useAni=False)

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
        for name in ('movie_page', 'book_page'):
            page = self._pages.get(name)
            if page is not None:
                page.apply_theme()

    def page(self, name: str):
        """取页面；没建过就现在建（导航点击走的是 _open_page）。"""
        return self._open_page(name)

    def _register_page(self, name: str, page) -> None:
        self._pages[name] = page
        setattr(self, name, page)
        if name in ('movie_page', 'book_page'):
            page.record_changed.connect(self._on_records_changed)
            page.synced.connect(self._on_synced)
        elif name == 'doulist_page':
            page.record_changed.connect(self._on_other_pages_records_changed)
            page.synced.connect(self._on_synced)
        elif name == 'plan_page':
            page.record_changed.connect(self._on_other_pages_records_changed)
        elif name == 'data_page':
            page.records_changed.connect(self._on_all_records_changed)
        elif name == 'recycle_page':
            page.changed.connect(self._on_recycle_changed)

    def _open_page(self, name: str):
        page = self._pages.get(name)
        if page is None:
            page = _PAGE_FACTORIES[name](self)
            page.setProperty('isStackedTransparent', True)
            placeholder = self._placeholders.pop(name, None)
            if placeholder is not None:
                placeholder.hide()
                self.stackedWidget.removeWidget(placeholder)
                placeholder.deleteLater()
            self.stackedWidget.addWidget(page)
            self._register_page(name, page)
        self.switchTo(page)
        return page

    def mark_pages_dirty(self, *names: str) -> None:
        """只标记已经建出来的页面，没建的下次显示时本来就会重建。"""
        for name in names:
            page = self._pages.get(name)
            if page is not None:
                page.mark_dirty()

    def _schedule_auto_backup(self, delay: int = 30000) -> None:
        if self.db.get_setting('cloud_sync_auto_backup') != '1':
            return
        self._auto_backup_timer.start(delay)

    def _run_auto_backup(self) -> None:
        result = auto_backup(self.db)
        if result and not result.get('skipped'):
            page = self._pages.get('data_page')
            if page is not None:
                page.refresh_sync()

    def _run_auto_sync(self) -> None:
        result = auto_restore(self.db)
        if not result:
            return
        counts = result.get('counts') or {}
        self.mark_pages_dirty(
            'movie_page', 'book_page', 'doulist_page', 'plan_page', 'report_page', 'data_page',
        )
        data_page = self._pages.get('data_page')
        if data_page is not None:
            data_page.refresh_sync()
            # 恢复后自动补全缺失的封面 / 页数 / 时长（后台线程 + 悬浮进度）。
            data_page.auto_backfill(restored=True)
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
        self.mark_pages_dirty(
            'movie_page', 'book_page', 'doulist_page', 'plan_page', 'data_page', 'report_page',
        )

    def _on_records_changed(self, media_type: str = '') -> None:
        self._schedule_auto_backup()
        # 记录页在改动后已自行刷新；指定类型时另一类型页面不受影响，无需重建。
        if not media_type:
            self.mark_pages_dirty('movie_page', 'book_page')
        self.mark_pages_dirty('doulist_page', 'plan_page', 'report_page', 'data_page')

    def _on_all_records_changed(self) -> None:
        self._on_records_changed()

    def _on_other_pages_records_changed(self, media_type: str = '') -> None:
        """从计划/豆列里改记录时，记录页本身没参与，也要标记为待刷新。"""
        self.mark_pages_dirty('movie_page', 'book_page')
        self._on_records_changed(media_type)

    def _on_recycle_changed(self) -> None:
        self.mark_pages_dirty(
            'movie_page', 'book_page', 'doulist_page', 'plan_page', 'report_page', 'data_page',
        )
