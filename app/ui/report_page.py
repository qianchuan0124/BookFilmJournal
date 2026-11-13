from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QDate, QThread, Qt, Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QVBoxLayout, QWidget,
)

from qfluentwidgets import (
    BodyLabel, CalendarPicker, CaptionLabel, CardWidget, CheckBox, ComboBox,
    InfoBar, InfoBarPosition, MessageBox, ProgressBar,
    PushButton, SmoothScrollArea, StrongBodyLabel, TitleLabel,
)

from app.config import DB_PATH, REPORTS_DIR
from app.db import Database
from app.profile import effective_name
from app.ui.styles import primary_button

SCOPE_MONTH = '指定月份'
SCOPE_YEAR = '指定年份'
SCOPE_RANGE = '自定义时间范围'
SCOPE_ALL = '全部记录'


class ReportWorker(QThread):
    succeeded = Signal(dict)
    failed = Signal(str)

    def __init__(self, media_type: str, scope: str, month: str, name: str,
                 start_date: str = '', end_date: str = '',
                 year_value: str = '', theme: str = 'dark', parent=None):
        super().__init__(parent)
        self.media_type = media_type
        self.scope = scope
        self.month = month
        self.name = name
        self.start_date = start_date
        self.end_date = end_date
        self.year_value = year_value
        self.theme = theme

    def run(self) -> None:
        from app.reports import generate_report

        try:
            db = Database(DB_PATH)
            result = generate_report(
                db, self.media_type, self.scope, self.month, self.name,
                False, self.start_date, self.end_date, self.year_value,
                self.theme,
            )
            self.succeeded.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class BatchReportWorker(QThread):
    progress = Signal(str, int, int)
    succeeded = Signal(list)
    failed = Signal(str)

    def __init__(self, media_type: str, months: list[str], name: str,
                 theme: str = 'dark', parent=None):
        super().__init__(parent)
        self.media_type = media_type
        self.months = months
        self.name = name
        self.theme = theme

    def run(self) -> None:
        from app.reports import generate_report

        try:
            db = Database(DB_PATH)
            results = []
            for index, month in enumerate(self.months, 1):
                self.progress.emit(f'生成 {month} 月报…', index, len(self.months))
                results.append(generate_report(
                    db, self.media_type, 'month', month, self.name, theme=self.theme,
                ))
            self.succeeded.emit(results)
        except Exception as exc:
            self.failed.emit(str(exc))


class ReportFileRow(QFrame):
    open_requested = Signal(str)
    delete_requested = Signal(list)
    open_dir_requested = Signal(str)

    def __init__(self, title: str, meta: str, html_path: str, png_path: str,
                 files: list[str] = None, parent=None):
        super().__init__(parent)
        self.html_path = html_path
        self.png_path = png_path
        self.files = files or []
        self.setObjectName('reportFileRow')
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet("""
            #reportFileRow { background-color: rgba(255,255,255,0.03); border-radius: 9px; }
            #reportFileRow:hover { background-color: rgba(255,255,255,0.06); }
        """)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(12)
        badge = CaptionLabel('HTML' if html_path else ('PNG' if png_path else '海报'), self)
        badge.setStyleSheet('color:#e5b45f; font-weight:700;')
        badge.setFixedWidth(42)
        layout.addWidget(badge)
        info = QVBoxLayout()
        info.setSpacing(2)
        title_label = StrongBodyLabel(title, self)
        title_label.setToolTip(title)
        info.addWidget(title_label)
        meta_label = CaptionLabel(meta, self)
        meta_label.setTextColor('#5f6b7a', '#8d95a5')
        info.addWidget(meta_label)
        layout.addLayout(info, 1)
        if html_path:
            open_html = PushButton('打开 HTML', self)
            open_html.setFixedWidth(96)
            open_html.setToolTip('打开完整 HTML 报告')
            open_html.clicked.connect(lambda: self.open_requested.emit(html_path))
            layout.addWidget(open_html)
        if png_path:
            open_png = PushButton('打开 PNG', self)
            open_png.setFixedWidth(96)
            open_png.setToolTip('打开完整报告 PNG')
            open_png.clicked.connect(lambda: self.open_requested.emit(png_path))
            layout.addWidget(open_png)
        open_dir = PushButton('打开目录', self)
        open_dir.setFixedWidth(96)
        open_dir.clicked.connect(lambda: self.open_dir_requested.emit(png_path or html_path))
        layout.addWidget(open_dir)
        delete_button = PushButton('删除', self)
        delete_button.setFixedWidth(58)
        delete_button.clicked.connect(lambda: self.delete_requested.emit(self.files))
        layout.addWidget(delete_button)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.open_requested.emit(self.html_path or self.png_path)
        super().mouseReleaseEvent(event)


class ReportPage(QWidget):
    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.setObjectName('reportPage')
        self.worker = None
        self.recent_path = ''
        self._dirty = False
        self._initialized = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 26, 32, 26)
        layout.setSpacing(14)

        title = TitleLabel('报告中心', self)
        subtitle = BodyLabel('按月、自定义时间范围或全部记录生成报告；下方清单可直接打开或删除记录。', self)
        subtitle.setTextColor('#5f6b7a', '#8d95a5')
        layout.addWidget(title)
        layout.addWidget(subtitle)

        card = CardWidget(self)
        card.setBorderRadius(14)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(22, 20, 22, 20)
        card_layout.setSpacing(12)

        row1 = QHBoxLayout()
        row1.setSpacing(10)
        row1.addWidget(BodyLabel('类型', card))
        self.type_combo = ComboBox(card)
        self.type_combo.addItems(['观影记录', '读书记录'])
        self.type_combo.setFixedWidth(150)
        self.type_combo.currentIndexChanged.connect(self.refresh_months)
        row1.addWidget(self.type_combo)
        row1.addSpacing(12)
        row1.addWidget(BodyLabel('范围', card))
        self.scope_combo = ComboBox(card)
        self.scope_combo.addItems([SCOPE_MONTH, SCOPE_YEAR, SCOPE_RANGE, SCOPE_ALL])
        self.scope_combo.setFixedWidth(170)
        self.scope_combo.currentIndexChanged.connect(self._on_scope_changed)
        row1.addWidget(self.scope_combo)
        row1.addSpacing(12)
        row1.addWidget(BodyLabel('月份', card))
        self.month_combo = ComboBox(card)
        self.month_combo.setFixedWidth(150)
        row1.addWidget(self.month_combo)
        row1.addSpacing(12)
        row1.addWidget(BodyLabel('年份', card))
        self.year_combo = ComboBox(card)
        self.year_combo.setFixedWidth(120)
        row1.addWidget(self.year_combo)
        row1.addStretch(1)
        card_layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(10)
        row2.addWidget(BodyLabel('开始', card))
        self.start_picker = CalendarPicker(card)
        row2.addWidget(self.start_picker)
        row2.addWidget(BodyLabel('结束', card))
        self.end_picker = CalendarPicker(card)
        row2.addWidget(self.end_picker)
        row2.addStretch(1)
        card_layout.addLayout(row2)

        row3 = QHBoxLayout()
        row3.setSpacing(12)
        row3.addWidget(BodyLabel('主题', card))
        self.theme_combo = ComboBox(card)
        self.theme_combo.addItems(['暗色', '亮色'])
        self.theme_combo.setFixedWidth(110)
        saved_theme = self.db.get_setting('report_theme', 'dark')
        self.theme_combo.setCurrentIndex(1 if saved_theme == 'light' else 0)
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        row3.addWidget(self.theme_combo)
        row3.addSpacing(12)
        self.png_checkbox = CheckBox('同时导出 PNG（高清长图）', card)
        row3.addWidget(self.png_checkbox)
        row3.addStretch(1)
        self.generate_button = primary_button('生成报告', card)
        self.generate_button.clicked.connect(self.generate)
        row3.addWidget(self.generate_button)
        self.batch_button = PushButton('生成全部月报', card)
        self.batch_button.clicked.connect(self.generate_all_months)
        row3.addWidget(self.batch_button)
        card_layout.addLayout(row3)

        self.progress = ProgressBar(card)
        self.progress.setRange(0, 100)
        self.progress.hide()
        card_layout.addWidget(self.progress)

        self.status = CaptionLabel('选择类型与范围后生成报告。', card)
        self.status.setTextColor('#5f6b7a', '#8d95a5')
        card_layout.addWidget(self.status)

        recent_row = QHBoxLayout()
        recent_row.addWidget(BodyLabel('最近生成：', card))
        self.recent_label = CaptionLabel('暂无', card)
        self.recent_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        recent_row.addWidget(self.recent_label, 1)
        self.open_button = PushButton('打开报告', card)
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self.open_recent)
        recent_row.addWidget(self.open_button)
        self.open_dir_button = PushButton('打开目录', card)
        self.open_dir_button.clicked.connect(self.open_output_dir)
        recent_row.addWidget(self.open_dir_button)
        card_layout.addLayout(recent_row)
        layout.addWidget(card)

        reports_card = CardWidget(self)
        reports_card.setBorderRadius(14)
        reports_layout = QVBoxLayout(reports_card)
        reports_layout.setContentsMargins(18, 14, 18, 14)
        reports_layout.setSpacing(8)
        reports_header = QHBoxLayout()
        self.reports_title = StrongBodyLabel('已生成报告', reports_card)
        reports_header.addWidget(self.reports_title)
        reports_hint = CaptionLabel('这里列出 output/reports 下的全部报告，文件名相同的 HTML 和 PNG 会合并显示。', reports_card)
        reports_hint.setTextColor('#5f6b7a', '#8d95a5')
        reports_header.addWidget(reports_hint)
        reports_header.addStretch(1)
        refresh_reports_button = PushButton('刷新列表', reports_card)
        refresh_reports_button.clicked.connect(self.refresh_reports)
        reports_header.addWidget(refresh_reports_button)
        reports_layout.addLayout(reports_header)
        self.reports_scroll = SmoothScrollArea(reports_card)
        self.reports_scroll.setWidgetResizable(True)
        self.reports_scroll.enableTransparentBackground()
        self.reports_scroll.setFrameShape(QFrame.NoFrame)
        self.reports_scroll.setMinimumHeight(200)
        self.reports_container = QWidget()
        self.reports_container.setStyleSheet('background:transparent;')
        self.reports_layout = QVBoxLayout(self.reports_container)
        self.reports_layout.setContentsMargins(0, 0, 0, 0)
        self.reports_layout.setSpacing(6)
        self.reports_layout.addStretch(1)
        self.reports_scroll.setWidget(self.reports_container)
        reports_layout.addWidget(self.reports_scroll)
        layout.addWidget(reports_card, 1)

    def _initial_refresh(self) -> None:
        self.refresh_months()
        self._on_scope_changed()
        self._set_default_range()
        self.refresh_reports()

    def current_media_type(self) -> str:
        return 'movie' if self.type_combo.currentIndex() == 0 else 'book'

    def current_scope(self) -> str:
        index = self.scope_combo.currentIndex()
        if index == 0:
            return 'month'
        if index == 1:
            return 'year'
        if index == 2:
            return 'range'
        return 'all'

    def current_month(self) -> str:
        return self.month_combo.currentData() or ''

    def current_year(self) -> str:
        return self.year_combo.currentData() or ''

    def current_range(self) -> tuple[str, str]:
        start = self.start_picker.getDate()
        end = self.end_picker.getDate()
        return start.toString('yyyy-MM-dd'), end.toString('yyyy-MM-dd')

    def current_theme(self) -> str:
        return 'light' if self.theme_combo.currentIndex() == 1 else 'dark'

    def _on_theme_changed(self) -> None:
        self.db.set_setting('report_theme', self.current_theme())

    def refresh_reports(self) -> None:
        while self.reports_layout.count():
            item = self.reports_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.hide()
                widget.deleteLater()
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        groups: dict[str, dict] = {}
        wall_files = []
        for path in REPORTS_DIR.iterdir():
            if not path.is_file():
                continue
            suffix = path.suffix.lower()
            if suffix in ('.html', '.png'):
                groups.setdefault(path.stem, {})[suffix.lstrip('.')] = path
            elif suffix in ('.jpg', '.jpeg') and path.stem.endswith('_wall'):
                wall_files.append(path)
        for path in wall_files:
            key = path.stem[:-5]
            if key in groups:
                groups[key]['wall'] = path
        entries = []
        for stem, files in groups.items():
            mtime = max(path.stat().st_mtime for path in files.values())
            entries.append((mtime, stem, files))
        entries.sort(key=lambda item: item[0], reverse=True)
        self.reports_title.setText(f'已生成报告（{len(entries)} 份）')
        if not entries:
            empty = BodyLabel('还没有生成过报告。', self.reports_container)
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet('color:#8d95a5; padding:30px 0;')
            self.reports_layout.addWidget(empty)
            self.reports_layout.addStretch(1)
            return
        for mtime, stem, files in entries:
            html_path = str(files['html']) if 'html' in files else ''
            png_path = str(files['png']) if 'png' in files else ''
            sizes = []
            if 'html' in files:
                sizes.append(f"HTML {self._file_size(files['html'])}")
            if 'png' in files:
                sizes.append(f"PNG {self._file_size(files['png'])}")
            meta = (f"{datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')} · "
                    + ' · '.join(sizes))
            files = [str(path) for path in files.values()]
            row = ReportFileRow(self._report_name(stem), meta, html_path, png_path,
                                files, self.reports_container)
            row.open_requested.connect(self.open_report_file)
            row.delete_requested.connect(self.delete_report)
            row.open_dir_requested.connect(self.open_report_dir)
            self.reports_layout.addWidget(row)
        self.reports_layout.addStretch(1)

    @staticmethod
    def _file_size(path: Path) -> str:
        size = path.stat().st_size
        if size >= 1024 * 1024:
            return f'{size / 1024 / 1024:.1f} MB'
        return f'{size / 1024:.0f} KB'

    @staticmethod
    def _report_name(stem: str) -> str:
        light = stem.endswith('_light')
        if light:
            stem = stem[:-len('_light')]
        media = '观影' if stem.startswith('movie') else ('读书' if stem.startswith('book') else '')
        if '_month_' in stem:
            name = f'{media}月报 · {stem.split("_month_", 1)[1]}'
        elif '_year_' in stem:
            name = f'{media}年报 · {stem.split("_year_", 1)[1]}'
        elif stem.endswith('_all'):
            name = f'{media}全部报告'
        elif '_range_' in stem:
            name = f'{media}自定义范围 · {stem.split("_range_", 1)[1]}'
        else:
            name = stem
        return f'{name} · 亮色' if light else name

    def open_report_file(self, path: str) -> None:
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).resolve())))

    def delete_report(self, files: list) -> None:
        if not files:
            return
        names = '\n'.join(Path(path).name for path in files[:8])
        if len(files) > 8:
            names += f'\n… 共 {len(files)} 个文件'
        box = MessageBox(
            '删除报告',
            f'确定删除这份报告吗？将删除以下文件且不可撤销：\n{names}',
            self.window(),
        )
        box.yesButton.setText('删除')
        box.cancelButton.setText('取消')
        if not box.exec():
            return
        failed = []
        for path in files:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                failed.append(Path(path).name)
            if self.recent_path and Path(path).resolve() == Path(self.recent_path).resolve():
                self.recent_path = ''
                self.recent_label.setText('暂无')
                self.open_button.setEnabled(False)
        self.refresh_reports()
        if failed:
            InfoBar.warning('部分文件未能删除', '、'.join(failed), parent=self,
                            position=InfoBarPosition.TOP, duration=5000)

    def open_report_dir(self, path: str) -> None:
        target = Path(path).parent if path else REPORTS_DIR
        if target.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target.resolve())))

    def refresh_months(self) -> None:
        media_type = self.current_media_type()
        months = self.db.months(media_type)
        self.month_combo.blockSignals(True)
        self.month_combo.clear()
        if months:
            for month in months:
                year, mon = month.split('-')
                self.month_combo.addItem(f'{year} 年 {int(mon)} 月', userData=month)
        else:
            self.month_combo.addItem('暂无记录')
        self.month_combo.blockSignals(False)
        self.refresh_years()
        self._set_default_range()
        self._on_scope_changed()

    def refresh_years(self) -> None:
        media_type = self.current_media_type()
        years = self.db.years(media_type)
        self.year_combo.blockSignals(True)
        self.year_combo.clear()
        if years:
            for year in years:
                self.year_combo.addItem(f'{year} 年', userData=year)
        else:
            self.year_combo.addItem('暂无记录')
        self.year_combo.blockSignals(False)

    def _set_default_range(self) -> None:
        first, last = self.db.date_bounds(self.current_media_type())
        start = QDate.fromString(first, 'yyyy-MM-dd')
        end = QDate.fromString(last, 'yyyy-MM-dd')
        if not start.isValid():
            start = QDate.currentDate()
            start = QDate(start.year(), start.month(), 1)
        if not end.isValid():
            end = QDate.currentDate()
        self.start_picker.blockSignals(True)
        self.end_picker.blockSignals(True)
        self.start_picker.setDate(start)
        self.end_picker.setDate(end)
        self.start_picker.blockSignals(False)
        self.end_picker.blockSignals(False)

    def _on_scope_changed(self) -> None:
        scope = self.current_scope()
        self.month_combo.setEnabled(scope == 'month')
        self.year_combo.setEnabled(scope == 'year')
        self.start_picker.setEnabled(scope == 'range')
        self.end_picker.setEnabled(scope == 'range')

    def _set_busy(self, busy: bool) -> None:
        self.generate_button.setEnabled(not busy)
        self.batch_button.setEnabled(not busy)
        self.type_combo.setEnabled(not busy)
        self.scope_combo.setEnabled(not busy)
        self.month_combo.setEnabled(not busy and self.current_scope() == 'month')
        self.year_combo.setEnabled(not busy and self.current_scope() == 'year')
        self.start_picker.setEnabled(not busy and self.current_scope() == 'range')
        self.end_picker.setEnabled(not busy and self.current_scope() == 'range')
        self.png_checkbox.setEnabled(not busy)
        self.theme_combo.setEnabled(not busy)
        self.progress.setVisible(busy)

    def generate(self) -> None:
        if self.worker and self.worker.isRunning():
            return
        media_type = self.current_media_type()
        scope = self.current_scope()
        month = self.current_month() if scope == 'month' else ''
        year_value = self.current_year() if scope == 'year' else ''
        start_date = end_date = ''
        if scope == 'month' and not month:
            InfoBar.warning('没有可用月份', '请先同步记录。', parent=self, position=InfoBarPosition.TOP)
            return
        if scope == 'year' and not year_value:
            InfoBar.warning('没有可用年份', '请先同步记录。', parent=self, position=InfoBarPosition.TOP)
            return
        if scope == 'range':
            start_date, end_date = self.current_range()
            if start_date > end_date:
                InfoBar.warning('时间范围无效', '开始日期不能晚于结束日期。', parent=self,
                                position=InfoBarPosition.TOP)
                return
        self._set_busy(True)
        self.progress.setRange(0, 0)
        self.status.setText('正在生成报告…')
        name = effective_name(self.db)
        self.worker = ReportWorker(
            media_type, scope, month, name,
            start_date, end_date, year_value, self.current_theme(), self,
        )
        self.worker.succeeded.connect(self._on_generated)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    def generate_all_months(self) -> None:
        if self.worker and self.worker.isRunning():
            return
        media_type = self.current_media_type()
        months = self.db.months(media_type)
        if not months:
            InfoBar.warning('没有可用月份', '请先同步记录。', parent=self, position=InfoBarPosition.TOP)
            return
        self._set_busy(True)
        self.progress.setRange(0, len(months))
        self.progress.setValue(0)
        self.status.setText('正在批量生成月报…')
        name = effective_name(self.db)
        self.worker = BatchReportWorker(media_type, months, name,
                                        self.current_theme(), self)
        self.worker.progress.connect(self._on_batch_progress)
        self.worker.succeeded.connect(self._on_batch_done)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    def _on_batch_progress(self, message: str, current: int, total: int) -> None:
        self.status.setText(message)
        self.progress.setRange(0, total)
        self.progress.setValue(current)

    def _on_batch_done(self, results: list) -> None:
        self._set_busy(False)
        self.status.setText(f'已生成 {len(results)} 份月报。')
        InfoBar.success('批量生成完成', f'共生成 {len(results)} 份月报。', parent=self,
                        position=InfoBarPosition.TOP)
        if results:
            self._set_recent(results[0]['html'])
        self.refresh_reports()

    def _on_generated(self, result: dict) -> None:
        if self.png_checkbox.isChecked() and result.get('html'):
            from app.reports import render_png

            self.status.setText('正在导出 PNG…')
            QApplication.processEvents()
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                html_path = Path(result['html'])
                png_path = html_path.with_suffix('.png')
                render_png(html_path, png_path)
                result['png'] = str(png_path)
            except Exception as exc:
                result['png_error'] = str(exc)
            finally:
                QApplication.restoreOverrideCursor()
        self._set_busy(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self.progress.hide()
        self._set_recent(result['html'])
        self.refresh_reports()
        if result.get('png'):
            message = '报告与 PNG 已生成。'
        elif result.get('png_error'):
            message = f"报告已生成，但 PNG 导出失败：{result['png_error']}"
        else:
            message = '报告已生成。'
        self.status.setText(message)
        if result.get('png_error'):
            InfoBar.warning('PNG 导出失败', result['png_error'], parent=self,
                            position=InfoBarPosition.TOP, duration=5000)
        else:
            InfoBar.success('生成完成', message, parent=self, position=InfoBarPosition.TOP)

    def _on_failed(self, error: str) -> None:
        self._set_busy(False)
        self.progress.hide()
        self.status.setText(f'生成失败：{error}')
        InfoBar.error('生成失败', error, parent=self, position=InfoBarPosition.TOP, duration=5000)

    def _set_recent(self, path: str) -> None:
        self.recent_path = path
        self.recent_label.setText(Path(path).name)
        self.recent_label.setToolTip(path)
        self.open_button.setEnabled(bool(path))

    def open_recent(self) -> None:
        if self.recent_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self.recent_path).resolve())))

    def open_output_dir(self) -> None:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(REPORTS_DIR.resolve())))

    def mark_dirty(self) -> None:
        if self.isVisible():
            self.refresh_all()
        else:
            self._dirty = True

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._initialized:
            self._initialized = True
            self._initial_refresh()
        elif self._dirty:
            self._dirty = False
            self.refresh_all()

    def refresh_all(self) -> None:
        self.refresh_months()
        self.refresh_reports()
