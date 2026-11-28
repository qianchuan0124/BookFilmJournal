import re
import shutil
import sqlite3
import time
from pathlib import Path

from PySide6.QtCore import QDate, QModelIndex, QPoint, QRectF, QSize, QThread, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QScrollArea, QSizePolicy, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget,
)

from qfluentwidgets import (
    Action, BodyLabel, CaptionLabel, CheckBox, ComboBox, FluentIcon,
    InfoBar, InfoBarPosition,
    IndicatorMenuItemDelegate, LineEdit, MenuAnimationType, RoundMenu,
    IndeterminateProgressRing, MessageBoxBase, ProgressBar, PushButton,
    StrongBodyLabel, SubtitleLabel,
    TextEdit, CalendarPicker, qconfig, isDarkTheme,
)
from qfluentwidgets.components.widgets.combo_box import ComboBoxMenu
from qfluentwidgets.common.font import getFont
from qfluentwidgets.common.style_sheet import themeColor

from app import explore
from app.calibre_install import install_calibre
from app.config import (
    APP_VERSION, APP_VERSION_TEXT, COVERS_DIR, DB_PATH, MEDIA_LABELS, READ_LABELS, ROOT,
    WISH_LABELS,
)
from app.db import Database
from app.douban import (
    extract_user_id_from_cookie, fetch_cover, fetch_subject_detail, sync_to_db,
)
from app.cover_sync import is_custom_cover
from app.media_meta import (
    book_pages, book_stats_text_full, manual_movie_duration, movie_duration,
    movie_stats_text, parse_duration, set_book_pages, set_movie_duration,
)
from app.ui.record_card import _cover_pixmap
from app.ui.styles import (
    cover_placeholder_style, divider_style, input_style, primary_button,
)
from app.words import find_calibre, format_word_count, run_word_count


def _clean_cookie(text: str) -> str:
    text = (text or '').strip()
    if not text:
        return ''
    match = re.search(r'(?i)cookie:\s*([^\r\n\'\"]+)', text)
    if match:
        text = match.group(1)
    text = text.strip().strip("'\"")
    return re.sub(r'\s+', ' ', text)


def _douban_subject_id(url: str) -> str:
    text = (url or '').strip()
    if not text:
        return ''
    match = re.search(r'/subject/(\d+)', text)
    if match:
        return match.group(1)
    match = re.search(r'uri=/(?:movie|book)/(\d+)', text)
    return match.group(1) if match else ''


def store_cover_file(source: str, media_type: str, copy: bool = False) -> str:
    if not source:
        return ''
    path = Path(source)
    if not path.is_absolute():
        path = ROOT / path
    if not path.is_file():
        return ''
    if not copy:
        try:
            return str(path.resolve().relative_to(ROOT.resolve()))
        except ValueError:
            pass
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower() or '.jpg'
    target = COVERS_DIR / f"manual_{media_type}_{int(time.time() * 1000)}{suffix}"
    try:
        shutil.copyfile(path, target)
    except OSError:
        return ''
    try:
        return str(target.relative_to(ROOT))
    except ValueError:
        return str(target)


def _duration_hint(text: str) -> str:
    text = (text or '').strip()
    if not text:
        return ''
    try:
        minutes = int(float(text))
    except ValueError:
        return ''
    if minutes <= 0:
        return ''
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        value = f'{hours} 小时 {rest} 分钟'
    elif hours:
        value = f'{hours} 小时'
    else:
        value = f'{rest} 分钟'
    return f'≈ {value}'


def _word_hint(text: str) -> str:
    text = (text or '').strip()
    if not text:
        return ''
    try:
        count = int(float(text))
    except ValueError:
        return ''
    value = format_word_count(max(0, count))
    return f'≈ {value}' if value else ''


class SyncWorker(QThread):
    progress = Signal(str, int, int)
    succeeded = Signal(dict)
    failed = Signal(str)

    def __init__(self, media_types: list[str], user_id: str, cookie: str,
                 include_wish: bool = False, include_doulists: bool = False,
                 sync_mode: str = 'full', db_path: Path = DB_PATH, parent=None):
        super().__init__(parent)
        self.media_types = media_types
        self.user_id = user_id
        self.cookie = cookie
        self.include_wish = include_wish
        self.include_doulists = include_doulists
        self.sync_mode = sync_mode
        self.db_path = db_path

    def run(self) -> None:
        try:
            db = Database(self.db_path)
            result = sync_to_db(
                self.media_types, self.user_id, self.cookie, db,
                lambda message, current, total: self.progress.emit(message, current, total),
                include_wish=self.include_wish,
                include_doulists=self.include_doulists,
                sync_mode=self.sync_mode,
            )
            self.succeeded.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class EditRecordDialog(MessageBoxBase):
    def __init__(self, db: Database, record, parent=None):
        super().__init__(parent)
        self.db = db
        self.record = record
        self.media_type = record['media_type']
        self.widget.setMinimumWidth(520)
        self.yesButton.setText('保存')
        self.cancelButton.setText('取消')

        self.viewLayout.addWidget(SubtitleLabel('编辑记录', self))
        self.title_label = BodyLabel(record['title'], self)
        self.title_label.setWordWrap(True)
        self.viewLayout.addWidget(self.title_label)
        self.viewLayout.addWidget(CaptionLabel(
            f"豆瓣记录时间：{record['marked_date'] or '未知'}　当前生效时间：{record['effective_date'] or '未设置'}",
            self,
        ))

        self.source_worker = None
        self.viewLayout.addWidget(BodyLabel('豆瓣来源', self))
        source_row = QHBoxLayout()
        source_row.setSpacing(8)
        self.source_edit = LineEdit(self)
        self.source_edit.setText(record['url'] or '')
        self.source_edit.setPlaceholderText('粘贴新的豆瓣条目链接以更换来源')
        self.source_edit.setClearButtonEnabled(True)
        input_style(self.source_edit)
        source_row.addWidget(self.source_edit, 1)
        self.source_button = PushButton(FluentIcon.SYNC, '更换来源', self)
        self.source_button.clicked.connect(self._change_source)
        source_row.addWidget(self.source_button)
        self.viewLayout.addLayout(source_row)
        source_hint = CaptionLabel(
            '更换来源会按新条目更新标题、封面、简介与评分；本地日期、短评和改过的评分保留，'
            '该记录此后按本地记录处理，不会因远端移除被同步删除。',
            self,
        )
        source_hint.setWordWrap(True)
        source_hint.setTextColor('#5f6b7a', '#8d95a5')
        self.viewLayout.addWidget(source_hint)

        date_row = QHBoxLayout()
        date_row.setSpacing(12)
        date_column = QVBoxLayout()
        date_column.setSpacing(4)
        date_column.addWidget(BodyLabel('本地日期', self))
        self.picker = CalendarPicker(self)
        current = QDate.fromString(record['effective_date'] or record['marked_date'] or '', 'yyyy-MM-dd')
        self.picker.setDate(current if current.isValid() else QDate.currentDate())
        date_column.addWidget(self.picker)
        date_row.addLayout(date_column)

        rating_column = QVBoxLayout()
        rating_column.setSpacing(4)
        rating_column.addWidget(BodyLabel('评分', self))
        self.rating_combo = ComboBox(self)
        self.rating_combo.addItems(['未评分', '★', '★★', '★★★', '★★★★', '★★★★★'])
        self.rating_combo.setCurrentIndex(max(0, min(5, int(record['rating'] or 0))))
        self.rating_combo.setFixedWidth(150)
        rating_column.addWidget(self.rating_combo)
        date_row.addLayout(rating_column)

        status_column = QVBoxLayout()
        status_column.setSpacing(4)
        status_column.addWidget(BodyLabel('状态', self))
        self.status_combo = ComboBox(self)
        self.status_combo.addItems(
            ['看过', '想看'] if self.media_type == 'movie' else ['读过', '想读']
        )
        self.status_combo.setCurrentIndex(0 if record['status'] == 'done' else 1)
        self.status_combo.setFixedWidth(120)
        status_column.addWidget(self.status_combo)
        date_row.addLayout(status_column)
        date_row.addStretch(1)
        self.viewLayout.addLayout(date_row)

        self.reset_checkbox = CheckBox('恢复豆瓣原始时间（允许下次同步覆盖）', self)
        self.viewLayout.addWidget(self.reset_checkbox)

        self.field_override_checkbox = CheckBox('允许下次同步覆盖本地评分与短评', self)
        self.viewLayout.addWidget(self.field_override_checkbox)

        stat_row = QHBoxLayout()
        stat_row.setSpacing(12)
        self.pages_edit = None
        self.duration_edit = None
        self.word_edit = None
        if self.media_type == 'book':
            pages_column = QVBoxLayout()
            pages_column.setSpacing(4)
            pages_column.addWidget(BodyLabel('页数', self))
            self.pages_edit = LineEdit(self)
            self.pages_edit.setPlaceholderText('例如 320')
            self.pages_edit.setClearButtonEnabled(True)
            pages_value = book_pages(record['subject_id'])
            if pages_value:
                self.pages_edit.setText(str(int(pages_value)))
            self.pages_edit.setFixedWidth(140)
            pages_column.addWidget(self.pages_edit)
            stat_row.addLayout(pages_column)
            word_column = QVBoxLayout()
            word_column.setSpacing(4)
            word_column.addWidget(BodyLabel('字数', self))
            self.word_edit = LineEdit(self)
            self.word_edit.setPlaceholderText('例如 120000')
            self.word_edit.setClearButtonEnabled(True)
            if record['word_count']:
                self.word_edit.setText(str(int(record['word_count'])))
            self.word_edit.setFixedWidth(160)
            word_row = QHBoxLayout()
            word_row.setSpacing(8)
            word_row.addWidget(self.word_edit)
            self.word_hint = CaptionLabel('', self)
            self.word_hint.setTextColor('#b07c20', '#e5b45f')
            word_row.addWidget(self.word_hint)
            word_row.addStretch(1)
            word_column.addLayout(word_row)
            self.word_edit.textChanged.connect(self._update_word_hint)
            self._update_word_hint()
            stat_row.addLayout(word_column)
        elif self.media_type == 'movie':
            duration_column = QVBoxLayout()
            duration_column.setSpacing(4)
            duration_column.addWidget(BodyLabel('时长（分钟）', self))
            self.duration_edit = LineEdit(self)
            self.duration_edit.setPlaceholderText('例如 122')
            self.duration_edit.setClearButtonEnabled(True)
            duration_value = (manual_movie_duration(record['subject_id'])
                              or movie_duration(record['subject_id'])
                              or parse_duration(record['intro'] or ''))
            if duration_value:
                self.duration_edit.setText(str(int(duration_value)))
            self.duration_edit.setFixedWidth(140)
            duration_row = QHBoxLayout()
            duration_row.setSpacing(8)
            duration_row.addWidget(self.duration_edit)
            self.duration_hint = CaptionLabel('', self)
            self.duration_hint.setTextColor('#b07c20', '#e5b45f')
            duration_row.addWidget(self.duration_hint)
            duration_row.addStretch(1)
            duration_column.addLayout(duration_row)
            self.duration_edit.textChanged.connect(self._update_duration_hint)
            self._update_duration_hint()
            stat_row.addLayout(duration_column)
        stat_row.addStretch(1)
        self.viewLayout.addLayout(stat_row)
        if self.media_type == 'book':
            stat_hint = '页数与字数留空表示保持原值；0 表示不启用。'
        else:
            stat_hint = '时长留空表示保持原值；单位为分钟，1 小时 50 分请填 110。'
        hint = CaptionLabel(stat_hint, self)
        hint.setTextColor('#5f6b7a', '#8d95a5')
        self.viewLayout.addWidget(hint)

        self.viewLayout.addWidget(BodyLabel('短评', self))
        self.comment_edit = TextEdit(self)
        self.comment_edit.setPlaceholderText('写点短评…')
        self.comment_edit.setPlainText(record['comment'] or '')
        self.comment_edit.setFixedHeight(96)
        input_style(self.comment_edit, 'TextEdit')
        self.viewLayout.addWidget(self.comment_edit)

        for edit in (self.pages_edit, self.word_edit, self.duration_edit):
            if edit is not None:
                input_style(edit)

        tips = CaptionLabel('本地调整后的日期与数值会保存到 SQLite，重新同步豆瓣时不会被覆盖。', self)
        tips.setWordWrap(True)
        tips.setTextColor('#4b5563', '#5d6474')
        self.viewLayout.addWidget(tips)

    def _change_source(self) -> None:
        subject_id = _douban_subject_id(self.source_edit.text())
        if not subject_id:
            InfoBar.warning('链接无效', '请粘贴豆瓣条目链接，例如 https://book.douban.com/subject/1234567/',
                            parent=self, position=InfoBarPosition.TOP)
            return
        if subject_id == str(self.record['subject_id']):
            InfoBar.info('来源未变', '该链接与当前豆瓣来源相同。', parent=self,
                        position=InfoBarPosition.TOP)
            return
        if self.source_worker and self.source_worker.isRunning():
            return
        self.source_button.setEnabled(False)
        self.source_button.setText('解析中…')
        self.source_worker = SubjectParseWorker(subject_id, self.media_type, self)
        self.source_worker.succeeded.connect(self._on_source_parsed)
        self.source_worker.failed.connect(self._on_source_failed)
        self.source_worker.start()

    def _reset_source_button(self) -> None:
        self.source_button.setEnabled(True)
        self.source_button.setText('更换来源')

    def _on_source_failed(self, error: str) -> None:
        self._reset_source_button()
        InfoBar.error('解析失败', error, parent=self, position=InfoBarPosition.TOP, duration=6000)

    def _on_source_parsed(self, detail: dict) -> None:
        self._reset_source_button()
        if not self.db.change_record_source(int(self.record['id']), detail):
            InfoBar.error(
                '无法更换来源',
                '该豆瓣条目已经有对应记录（可能在回收站），请先处理后再试。',
                parent=self, position=InfoBarPosition.TOP, duration=6000,
            )
            return
        self.record = self.db.get_record(int(self.record['id']))
        self.title_label.setText((self.record['title'] if self.record else '') or '')
        self.source_edit.setText((self.record['url'] if self.record else '') or '')
        InfoBar.success('已更换来源', f"豆瓣来源已更新为「{detail.get('title') or ''}」。",
                        parent=self, position=InfoBarPosition.TOP)

    def _update_word_hint(self) -> None:
        self.word_hint.setText(_word_hint(self.word_edit.text()))

    def _update_duration_hint(self) -> None:
        self.duration_hint.setText(_duration_hint(self.duration_edit.text()))

    def validate(self) -> bool:
        record_id = int(self.record['id'])
        word_count = None
        if self.word_edit is not None:
            text = self.word_edit.text().strip()
            if text:
                try:
                    word_count = max(0, int(float(text)))
                except ValueError:
                    InfoBar.error('字数无效', '请输入整数，或留空保持原值。', parent=self,
                                  position=InfoBarPosition.TOP)
                    return False
        pages = None
        if self.pages_edit is not None:
            text = self.pages_edit.text().strip()
            if text:
                try:
                    pages = int(float(text))
                except ValueError:
                    InfoBar.error('页数无效', '请输入整数，或留空保持原值。', parent=self,
                                  position=InfoBarPosition.TOP)
                    return False
        minutes = None
        if self.duration_edit is not None:
            text = self.duration_edit.text().strip()
            if text:
                try:
                    minutes = int(float(text))
                except ValueError:
                    InfoBar.error('时长无效', '请输入整数分钟，或留空保持原值。', parent=self,
                                  position=InfoBarPosition.TOP)
                    return False
        date = self.picker.getDate()
        if not self.reset_checkbox.isChecked() and not date.isValid():
            InfoBar.error('日期无效', '请选择有效的日期。', parent=self, position=InfoBarPosition.TOP)
            return False
        target_status = 'done' if self.status_combo.currentIndex() == 0 else 'wish'
        if not self.db.change_record_status(record_id, target_status):
            noun = '已看' if self.media_type == 'movie' else '已读'
            InfoBar.warning(
                '无法修改状态',
                f'该条目已存在对应的{noun}记录，或目标状态记录在回收站中，请先处理后再修改。',
                parent=self, position=InfoBarPosition.TOP,
            )
            return False

        if self.reset_checkbox.isChecked():
            self.db.reset_effective_date(record_id)
        else:
            self.db.set_effective_date(record_id, date.toString('yyyy-MM-dd'))
        self.db.update_record_fields(
            record_id,
            rating=self.rating_combo.currentIndex(),
            comment=self.comment_edit.toPlainText().strip(),
            word_count=word_count,
        )
        if self.field_override_checkbox.isChecked():
            self.db.clear_field_overrides(record_id)
        if pages:
            set_book_pages(self.record['subject_id'], pages, db=self.db)
        if minutes:
            set_movie_duration(self.record['subject_id'], minutes, db=self.db)
        return True


class _SelectableLabel(BodyLabel):
    def contextMenuEvent(self, event) -> None:
        menu = RoundMenu(parent=self)
        action = Action(FluentIcon.COPY, '复制', self)
        action.setEnabled(bool(self.selectedText()))
        action.triggered.connect(
            lambda: QApplication.clipboard().setText(self.selectedText()))
        menu.addAction(action)
        menu.exec(event.globalPos())


class _CoverLabel(QLabel):
    """封面：宽度固定、高度可随窗口收缩，避免空间不足时压到相邻控件。"""

    def __init__(self, width: int = 180, height: int = 260, parent=None):
        super().__init__(parent)
        self._source = None
        self._preferred = QSize(width, height)
        self.setAlignment(Qt.AlignCenter)
        self.setFixedWidth(width)
        self.setMinimumHeight(100)
        self.setMaximumHeight(height)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)

    def sizeHint(self) -> QSize:
        return self._preferred

    def minimumSizeHint(self) -> QSize:
        return QSize(self._preferred.width(), self.minimumHeight())

    def setSourcePixmap(self, pixmap: QPixmap) -> None:
        self._source = pixmap
        self._fit()

    def _fit(self) -> None:
        if self._source is None or self._source.isNull():
            return
        self.setPixmap(self._source.scaled(
            max(self.width(), 1), max(self.height(), 1),
            Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit()


def _populate_resources(grid: QGridLayout, title_label, box: QWidget, db: Database,
                        media_type: str, title: str) -> None:
    """按条目类型列出探索空间站点并标注最近一次连通性检测结果（两个详情弹窗共用）。"""
    while grid.count():
        item = grid.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()
    sites = explore.sites_for(explore.load_state(db), media_type)
    title_label.setVisible(bool(sites))
    box.setVisible(bool(sites))
    if not sites:
        return
    keyword = (title or '').split(' / ')[0].strip()
    for index, site in enumerate(sites):
        status = explore.get_reachability(site['id'])
        mark = '✓' if status is True else '✕' if status is False else '·'
        state_text = '可访问' if status is True else '不可访问' if status is False else '未检测'
        button = PushButton(f'{mark} {site["name"]}', box)
        button.setMinimumHeight(34)
        button.setToolTip(f'{site["url"]}\n状态：{state_text}')
        button.clicked.connect(
            lambda _checked=False, item=site, text=keyword: QDesktopServices.openUrl(
                QUrl(explore.search_link(item, text))))
        grid.addWidget(button, index // 3, index % 3)
    rows = (len(sites) + 2) // 3
    # 给资源块一个明确的最小高度，避免弹窗变矮时行距被压扁、按钮互相压住。
    box.setMinimumHeight(rows * 34 + (rows - 1) * 12)


class MediaMetaDialog(MessageBoxBase):
    def __init__(self, db: Database, media_type: str, subject_id: str, title: str,
                 intro: str = '', parent=None):
        super().__init__(parent)
        self.db = db
        self.media_type = media_type
        self.subject_id = str(subject_id)
        self.widget.setMinimumWidth(460)
        self.yesButton.setText('保存')
        self.cancelButton.setText('取消')

        name = SubtitleLabel(title or '条目信息', self)
        name.setWordWrap(True)
        self.viewLayout.addWidget(name)
        hint = CaptionLabel('只影响本地展示与统计，不会回写豆瓣。', self)
        hint.setTextColor('#5f6b7a', '#8d95a5')
        self.viewLayout.addWidget(hint)

        if media_type == 'book':
            self.viewLayout.addWidget(BodyLabel('页数', self))
            self.pages_edit = LineEdit(self)
            self.pages_edit.setPlaceholderText('例如 302')
            self.pages_edit.setClearButtonEnabled(True)
            pages = book_pages(self.subject_id)
            if pages:
                self.pages_edit.setText(str(pages))
            input_style(self.pages_edit)
            self.viewLayout.addWidget(self.pages_edit)

            self.viewLayout.addWidget(BodyLabel('字数', self))
            self.word_edit = LineEdit(self)
            self.word_edit.setPlaceholderText('例如 120000')
            self.word_edit.setClearButtonEnabled(True)
            word_count = self.db.word_count_map('book').get(self.subject_id, 0)
            if word_count:
                self.word_edit.setText(str(int(word_count)))
            input_style(self.word_edit)
            self.viewLayout.addWidget(self.word_edit)
            self.word_hint = CaptionLabel(_word_hint(self.word_edit.text()), self)
            self.word_hint.setTextColor('#5f6b7a', '#8d95a5')
            self.viewLayout.addWidget(self.word_hint)
            self.word_edit.textChanged.connect(
                lambda text: self.word_hint.setText(_word_hint(text)))
        else:
            self.viewLayout.addWidget(BodyLabel('时长（分钟）', self))
            self.duration_edit = LineEdit(self)
            self.duration_edit.setPlaceholderText('例如 122')
            self.duration_edit.setClearButtonEnabled(True)
            minutes = manual_movie_duration(self.subject_id) or parse_duration(intro or '')
            if minutes:
                self.duration_edit.setText(str(int(minutes)))
            input_style(self.duration_edit)
            self.viewLayout.addWidget(self.duration_edit)
            self.duration_hint = CaptionLabel(_duration_hint(self.duration_edit.text()), self)
            self.duration_hint.setTextColor('#5f6b7a', '#8d95a5')
            self.viewLayout.addWidget(self.duration_hint)
            self.duration_edit.textChanged.connect(
                lambda text: self.duration_hint.setText(_duration_hint(text)))

    @staticmethod
    def _to_int(text: str) -> int:
        try:
            return max(0, int(float(text.strip())))
        except (TypeError, ValueError):
            return 0

    def validate(self) -> bool:
        if self.media_type == 'book':
            set_book_pages(self.subject_id, self._to_int(self.pages_edit.text()) or None,
                           db=self.db)
            self.db.set_word_count_for_subject(
                self.subject_id, self._to_int(self.word_edit.text()))
        else:
            set_movie_duration(
                self.subject_id, self._to_int(self.duration_edit.text()) or None,
                db=self.db)
        return True


class RecordDetailDialog(MessageBoxBase):
    changed = Signal(int)

    def __init__(self, db: Database, record, parent=None):
        super().__init__(parent)
        self.db = db
        self.record = record
        self.widget.setMinimumWidth(880)
        self.yesButton.setText('编辑记录')
        self.cancelButton.setText('关闭')
        self.viewLayout.setSpacing(12)

        self.title_label = SubtitleLabel(record['title'], self)
        self.title_label.setWordWrap(True)
        self.viewLayout.addWidget(self.title_label)
        self.subtitle_label = CaptionLabel('', self)
        self.subtitle_label.setTextColor('#5f6b7a', '#8d95a5')
        self.viewLayout.addWidget(self.subtitle_label)

        body = QHBoxLayout()
        body.setSpacing(20)
        left_widget = QWidget(self)
        left_widget.setFixedWidth(200)
        left = QVBoxLayout(left_widget)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(10)
        left.addWidget(StrongBodyLabel('记录信息', left_widget))
        self.cover_label = _CoverLabel(180, 260, left_widget)
        self.cover_label.setStyleSheet('background:#0f131b; border-radius:6px;')
        left.addWidget(self.cover_label, 0, Qt.AlignLeft)
        self.change_cover_button = PushButton(FluentIcon.PHOTO, '更换封面', left_widget)
        self.change_cover_button.clicked.connect(self._change_cover)
        left.addWidget(self.change_cover_button, 0, Qt.AlignLeft)
        self.info_layout = QVBoxLayout()
        self.info_layout.setSpacing(8)
        # 信息块贴底，让「时长」的底边与右侧资源区底边齐平。
        left.addStretch(1)
        left.addLayout(self.info_layout)
        body.addWidget(left_widget, 1)

        right_widget = QWidget(self)
        right = QVBoxLayout(right_widget)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(8)
        right.addWidget(StrongBodyLabel('感想', right_widget))
        self.comment_scroll = QScrollArea(right_widget)
        self.comment_scroll.setWidgetResizable(True)
        self.comment_scroll.setFrameShape(QFrame.NoFrame)
        self.comment_scroll.setStyleSheet('QScrollArea{background:transparent;border:none;}')
        holder = QWidget()
        holder.setStyleSheet('background:transparent;')
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        self.comment_label = _SelectableLabel(holder)
        self.comment_label.setWordWrap(True)
        self.comment_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.comment_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        holder_layout.addWidget(self.comment_label)
        holder_layout.addStretch(1)
        self.comment_scroll.setWidget(holder)
        self.comment_scroll.setMinimumHeight(120)
        right.addWidget(self.comment_scroll, 1)

        self.empty_box = QWidget(right_widget)
        empty = QVBoxLayout(self.empty_box)
        empty.setSpacing(12)
        empty.addStretch(1)
        tip = CaptionLabel('暂时还没感想', self.empty_box)
        tip.setAlignment(Qt.AlignCenter)
        tip.setTextColor('#6f7887', '#8d95a5')
        empty.addWidget(tip)
        add_button = primary_button(FluentIcon.EDIT, '前去添加', self.empty_box)
        add_button.clicked.connect(self._edit)
        empty.addWidget(add_button, 0, Qt.AlignHCenter)
        empty.addStretch(1)
        right.addWidget(self.empty_box, 1)

        self.resource_title = StrongBodyLabel('资源', right_widget)
        right.addWidget(self.resource_title)
        self.resource_box = QWidget(right_widget)
        self.resource_box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self.resource_grid = QGridLayout(self.resource_box)
        self.resource_grid.setContentsMargins(0, 0, 0, 0)
        self.resource_grid.setHorizontalSpacing(8)
        self.resource_grid.setVerticalSpacing(12)
        right.addWidget(self.resource_box)

        body.addWidget(right_widget, 2)
        self.viewLayout.addLayout(body)

        self._refresh()
        self._fit_height()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._fit_height()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_height()

    def _fit_height(self) -> None:
        """把弹窗高度限制在窗口可用范围内，剩余空间优先给感想区。"""
        available = self.height()
        if available <= 0:
            return
        # 上限压低到 620：再高的话左侧封面与评分之间会空出一大块。
        target = max(460, min(620, available - 24))
        if self.widget.minimumHeight() != target:
            self.widget.setFixedHeight(target)

    def _refresh(self) -> None:
        record = self.record
        media_type = record['media_type']
        is_done = record['status'] == 'done'
        done_text = READ_LABELS.get(media_type, ('已读', '未读'))[0 if is_done else 1]
        if not is_done:
            done_text = WISH_LABELS.get(media_type, done_text)
        self.title_label.setText(record['title'] or '')
        subtitle = f"{MEDIA_LABELS.get(media_type, '')} · {done_text}"
        if is_custom_cover(media_type, record['subject_id'], record['cover_path'] or ''):
            subtitle += ' · 自定义封面（随云同步）'
        self.subtitle_label.setText(subtitle)

        self.cover_label.setSourcePixmap(_cover_pixmap(record['cover_path'] or '', 180, 260, fill=True))
        self._refresh_resources()
        while self.info_layout.count():
            item = self.info_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

        rating = int(record['rating'] or 0)
        date = record['effective_date'] or record['marked_date'] or '未设置'
        stats = ''
        if media_type == 'book':
            stats = book_stats_text_full(record['subject_id'], int(record['word_count'] or 0))
        else:
            stats = movie_stats_text(record['intro'] or '', record['subject_id'])
        for text in (
            f"我的评分：{'★' * rating}{'☆' * (5 - rating)}" if rating else '我的评分：未评分',
            f"{'日期' if is_done else '标记日期'}：{date}",
            f"{'页数 / 字数' if media_type == 'book' else '时长'}：{stats}",
        ):
            label = BodyLabel(text, self)
            label.setWordWrap(True)
            self.info_layout.addWidget(label)

        comment = (record['comment'] or '').strip()
        self.comment_label.setText(comment)
        self.comment_scroll.setVisible(bool(comment))
        self.empty_box.setVisible(not comment)

    def _refresh_resources(self) -> None:
        """按记录类型展示探索空间站点，并标注最近一次连通性检测结果。"""
        _populate_resources(self.resource_grid, self.resource_title, self.resource_box,
                            self.db, self.record['media_type'], self.record['title'] or '')

    def _change_cover(self) -> None:
        dialog = CoverChangeDialog(self.db, self.record, self)
        if not dialog.exec():
            return
        updated = self.db.get_record(int(self.record['id']))
        if updated is not None:
            self.record = updated
            self._refresh()
            self.changed.emit(int(self.record['id']))

    def _edit(self) -> None:
        dialog = EditRecordDialog(self.db, self.record, self)
        if not dialog.exec():
            return
        updated = self.db.get_record(int(self.record['id']))
        if updated is not None:
            self.record = updated
            self._refresh()
            self.changed.emit(int(self.record['id']))

    def validate(self) -> bool:
        self._edit()
        return False


def open_record_detail(db: Database, record, parent=None) -> bool:
    """打开记录详情弹窗，返回期间是否改过内容（编辑、换封面都算）。"""
    dialog = RecordDetailDialog(db, record, parent)
    changed: list[int] = []
    dialog.changed.connect(changed.append)
    dialog.exec()
    return bool(changed)


class ItemDetailDialog(MessageBoxBase):
    """豆列 / 计划里还没有对应记录时的条目详情：只读展示，可跳到豆瓣。"""

    def __init__(self, db: Database, item, parent=None, source_text: str = ''):
        super().__init__(parent)
        self.db = db
        self.data = dict(item)
        self.url = self.data.get('url') or ''
        self.widget.setMinimumWidth(760)
        self.viewLayout.setSpacing(12)
        self.yesButton.setText('在豆瓣打开')
        self.cancelButton.setText('关闭')
        self.yesButton.setVisible(bool(self.url))

        media_type = self.data.get('media_type') or 'movie'
        status = self.data.get('status') or ''
        done_text = READ_LABELS.get(media_type, ('已读', '未读'))[0 if status == 'done' else 1]
        if status != 'done':
            done_text = WISH_LABELS.get(media_type, done_text)

        title = SubtitleLabel(self.data.get('title') or '', self)
        title.setWordWrap(True)
        self.viewLayout.addWidget(title)
        subtitle_text = f"{MEDIA_LABELS.get(media_type, '')} · {done_text}"
        if source_text:
            subtitle_text += f" · {source_text}"
        subtitle = CaptionLabel(subtitle_text, self)
        subtitle.setTextColor('#5f6b7a', '#8d95a5')
        self.viewLayout.addWidget(subtitle)

        body = QHBoxLayout()
        body.setSpacing(20)
        left_widget = QWidget(self)
        left_widget.setFixedWidth(200)
        left = QVBoxLayout(left_widget)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(10)
        left.addWidget(StrongBodyLabel('条目信息', left_widget))
        cover = _CoverLabel(180, 260, left_widget)
        cover.setStyleSheet('background:#0f131b; border-radius:6px;')
        cover.setSourcePixmap(
            _cover_pixmap(self.data.get('cover_path') or '', 180, 260, fill=True))
        left.addWidget(cover, 0, Qt.AlignLeft)
        left.addStretch(1)
        info = QVBoxLayout()
        info.setSpacing(8)
        for text in self._info_lines(media_type):
            label = BodyLabel(text, left_widget)
            label.setWordWrap(True)
            info.addWidget(label)
        left.addLayout(info)
        body.addWidget(left_widget, 1)

        right_widget = QWidget(self)
        right = QVBoxLayout(right_widget)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(8)
        right.addWidget(StrongBodyLabel('简介', right_widget))
        self.intro_scroll = QScrollArea(right_widget)
        self.intro_scroll.setWidgetResizable(True)
        self.intro_scroll.setFrameShape(QFrame.NoFrame)
        self.intro_scroll.setStyleSheet('QScrollArea{background:transparent;border:none;}')
        holder = QWidget()
        holder.setStyleSheet('background:transparent;')
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        self.intro_label = _SelectableLabel(holder)
        self.intro_label.setWordWrap(True)
        self.intro_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.intro_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.intro_label.setText(
            (self.data.get('intro') or '').strip() or '豆瓣上暂无简介。')
        holder_layout.addWidget(self.intro_label)
        holder_layout.addStretch(1)
        self.intro_scroll.setWidget(holder)
        self.intro_scroll.setMinimumHeight(120)
        right.addWidget(self.intro_scroll, 1)

        note = (self.data.get('comment') or self.data.get('note') or '').strip()
        if note:
            right.addWidget(StrongBodyLabel('备注', right_widget))
            note_label = _SelectableLabel(right_widget)
            note_label.setWordWrap(True)
            note_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            note_label.setText(note)
            right.addWidget(note_label)
        self.resource_title = StrongBodyLabel('资源', right_widget)
        right.addWidget(self.resource_title)
        self.resource_box = QWidget(right_widget)
        self.resource_box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self.resource_grid = QGridLayout(self.resource_box)
        self.resource_grid.setContentsMargins(0, 0, 0, 0)
        self.resource_grid.setHorizontalSpacing(8)
        self.resource_grid.setVerticalSpacing(12)
        right.addWidget(self.resource_box)
        _populate_resources(self.resource_grid, self.resource_title, self.resource_box,
                            self.db, media_type, self.data.get('title') or '')
        body.addWidget(right_widget, 2)
        self.viewLayout.addLayout(body)
        self._fit_height()

    def _info_lines(self, media_type: str) -> list[str]:
        lines: list[str] = []
        rating = int(self.data.get('rating') or 0)
        if rating:
            lines.append(f"豆瓣评分：{'★' * rating}{'☆' * max(0, 5 - rating)}")
        year = self.data.get('year') or ''
        if year:
            lines.append(f'年份：{year}')
        release = self.data.get('release_date') or ''
        if release:
            lines.append(f"{'上映' if media_type == 'movie' else '出版'}：{release}")
        if media_type == 'book':
            pages = int(self.data.get('pages') or 0)
            if pages:
                lines.append(f'页数：{pages}')
            words = int(self.data.get('word_count') or 0)
            if words:
                lines.append(f'字数：{words}')
        else:
            minutes = int(self.data.get('duration_minutes') or 0)
            if minutes:
                hours, rest = divmod(minutes, 60)
                lines.append(
                    f'时长：{hours} 小时 {rest} 分' if hours else f'时长：{rest} 分钟')
        if not lines:
            lines.append('豆瓣上还没有更多信息')
        return lines

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._fit_height()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_height()

    def _fit_height(self) -> None:
        available = self.height()
        if available <= 0:
            return
        target = max(460, min(620, available - 24))
        if self.widget.minimumHeight() != target:
            self.widget.setFixedHeight(target)

    def validate(self) -> bool:
        if self.url:
            QDesktopServices.openUrl(QUrl(self.url))
        return True


def open_item_detail(db: Database, item, parent=None, source_text: str = '') -> None:
    """豆列 / 计划条目：没有本地记录时展示条目详情，而不是直接跳豆瓣。"""
    ItemDetailDialog(db, item, parent, source_text).exec()


class SyncDialog(MessageBoxBase):
    synced = Signal(dict)

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.worker = None
        self.widget.setMinimumWidth(640)
        self.viewLayout.setSpacing(10)
        self.yesButton.setText('开始同步')
        self.cancelButton.setText('关闭')

        self.viewLayout.addWidget(SubtitleLabel('同步豆瓣', self))
        hint = CaptionLabel('同步会抓取豆瓣收藏与封面并写入本地 SQLite；已调整过的本地时间不会被覆盖。', self)
        hint.setWordWrap(True)
        hint.setTextColor('#5f6b7a', '#8d95a5')
        self.viewLayout.addWidget(hint)

        self.id_edit = LineEdit(self)
        self.id_edit.setPlaceholderText('豆瓣 ID（个人主页 URL 中的数字）')
        self.id_edit.setText(self.db.get_setting('douban_user_id'))
        self.id_edit.setClearButtonEnabled(True)
        self.viewLayout.addWidget(BodyLabel('豆瓣 ID', self))
        self.viewLayout.addWidget(self.id_edit)

        self.cookie_edit = LineEdit(self)
        self.cookie_edit.setPlaceholderText('可留空使用已保存登录；私密收藏 / 豆列需要 Cookie')
        self.cookie_edit.setEchoMode(QLineEdit.Password)
        self.cookie_edit.setClearButtonEnabled(True)
        saved_cookie = self.db.get_setting('douban_cookie')
        if saved_cookie:
            self.cookie_edit.setText(saved_cookie)
            if not self.id_edit.text().strip():
                self.id_edit.setText(extract_user_id_from_cookie(saved_cookie))
        self.viewLayout.addWidget(BodyLabel('Cookie（可持久化保存）', self))
        cookie_row = QHBoxLayout()
        cookie_row.setSpacing(8)
        cookie_row.addWidget(self.cookie_edit, 1)
        try:
            from app.ui.login_dialog import WEBENGINE_AVAILABLE
        except Exception:
            WEBENGINE_AVAILABLE = False
        self.login_button = PushButton('扫码登录' if WEBENGINE_AVAILABLE else '登录豆瓣', self)
        self.login_button.clicked.connect(self._open_login)
        cookie_row.addWidget(self.login_button)
        self.paste_cookie_button = PushButton('粘贴 Cookie', self)
        self.paste_cookie_button.clicked.connect(self._paste_cookie)
        cookie_row.addWidget(self.paste_cookie_button)
        self.clear_cookie_button = PushButton('清除登录', self)
        self.clear_cookie_button.clicked.connect(self._clear_login)
        cookie_row.addWidget(self.clear_cookie_button)
        self.viewLayout.addLayout(cookie_row)
        self.remember_checkbox = CheckBox('记住登录（Cookie 保存在本地数据库）', self)
        self.remember_checkbox.setChecked(bool(saved_cookie))
        self.viewLayout.addWidget(self.remember_checkbox)
        cookie_hint = CaptionLabel(
            '推荐点「扫码登录」用豆瓣 App 扫码，自动获取 Cookie 和用户 ID；'
            '勾选记住后下次同步可直接使用，无需重复登录。Cookie 仅保存在本机 SQLite。',
            self,
        )
        cookie_hint.setWordWrap(True)
        cookie_hint.setTextColor('#5f6b7a', '#8d95a5')
        self.viewLayout.addWidget(cookie_hint)

        mode_row = QHBoxLayout()
        mode_row.setSpacing(10)
        mode_row.addWidget(BodyLabel('同步方式', self))
        self.mode_combo = ComboBox(self)
        self.mode_combo.addItems([
            '全量同步（远端全局替换）',
            '增量同步（仅新增）',
            '增量同步（新增并移除）',
        ])
        saved_mode = self.db.get_setting('sync_mode', 'full')
        self.mode_combo.setCurrentIndex({'full': 0, 'incremental_add': 1,
                                         'incremental_sync': 2}.get(saved_mode, 0))
        self.mode_combo.setFixedWidth(240)
        mode_row.addWidget(self.mode_combo)
        mode_row.addStretch(1)
        self.viewLayout.addLayout(mode_row)
        mode_notes = QVBoxLayout()
        mode_notes.setSpacing(3)
        for text in (
            '全量同步（远端全局替换）：以远端为准刷新已有记录（含最新副标题），远端已移除的条目移入回收站',
            '增量同步（仅新增）：只补充远端新增条目，已有记录保持不动',
            '增量同步（新增并移除）：补充新增条目，同时把远端已移除的条目移入回收站',
            '本地改过的日期、评分与短评始终保留；手动新增的记录不受影响',
        ):
            note = CaptionLabel(text, self)
            note.setWordWrap(True)
            note.setTextColor('#5f6b7a', '#8d95a5')
            mode_notes.addWidget(note)
        self.viewLayout.addLayout(mode_notes)

        types = QHBoxLayout()
        types.setSpacing(28)
        self.movie_checkbox = CheckBox('观影记录', self)
        self.movie_checkbox.setChecked(True)
        self.book_checkbox = CheckBox('读书记录', self)
        self.book_checkbox.setChecked(True)
        types.addWidget(self.movie_checkbox)
        types.addWidget(self.book_checkbox)
        types.addStretch(1)
        self.viewLayout.addLayout(types)

        extra_types = QHBoxLayout()
        extra_types.setSpacing(28)
        self.wish_checkbox = CheckBox('同时同步想看/想读', self)
        self.wish_checkbox.setChecked(True)
        self.doulist_checkbox = CheckBox('同步豆列（需要 Cookie）', self)
        extra_types.addWidget(self.wish_checkbox)
        extra_types.addWidget(self.doulist_checkbox)
        extra_types.addStretch(1)
        self.viewLayout.addLayout(extra_types)

        progress_row = QHBoxLayout()
        progress_row.setSpacing(10)
        self.progress = ProgressBar(self)
        self.progress.setRange(0, 0)
        self.progress.hide()
        progress_row.addWidget(self.progress, 1)
        self.loading = IndeterminateProgressRing(self, start=False)
        self.loading.setFixedSize(20, 20)
        self.loading.hide()
        progress_row.addWidget(self.loading)
        self.viewLayout.addLayout(progress_row)

        self.log = TextEdit(self)
        self.log.setReadOnly(True)
        self.log.setFixedHeight(130)
        self.log.setPlaceholderText('同步日志')
        self.viewLayout.addWidget(self.log)

    def _open_login(self) -> None:
        try:
            from app.ui.login_dialog import WEBENGINE_AVAILABLE, DoubanLoginDialog
        except Exception as exc:
            InfoBar.error('无法打开登录页', f'Qt WebEngine 不可用：{exc}', parent=self,
                          position=InfoBarPosition.TOP, duration=5000)
            return
        if not WEBENGINE_AVAILABLE:
            QDesktopServices.openUrl(QUrl('https://accounts.douban.com/passport/login'))
            InfoBar.info(
                '已打开豆瓣登录页',
                '请在浏览器完成登录，复制 Cookie 后回到这里点击「粘贴 Cookie」。',
                parent=self, position=InfoBarPosition.TOP, duration=7000,
            )
            return
        dialog = DoubanLoginDialog(self)
        if dialog.exec() and dialog.cookie:
            self.cookie_edit.setText(dialog.cookie)
            if dialog.user_id:
                self.id_edit.setText(dialog.user_id)
            self.remember_checkbox.setChecked(True)
            self._save_login()
            uid_text = f'，用户 ID：{dialog.user_id}' if dialog.user_id else ''
            InfoBar.success('登录成功', f'已获取 Cookie{uid_text}，下次可直接使用。', parent=self,
                            position=InfoBarPosition.TOP)

    def _paste_cookie(self) -> None:
        text = _clean_cookie(QApplication.clipboard().text())
        if not text:
            InfoBar.warning('剪贴板为空', '请先在浏览器中复制 Cookie。', parent=self, position=InfoBarPosition.TOP)
            return
        self.cookie_edit.setText(text)
        self.remember_checkbox.setChecked(True)
        user_id = extract_user_id_from_cookie(text)
        if user_id and not self.id_edit.text().strip():
            self.id_edit.setText(user_id)
        InfoBar.success('已粘贴 Cookie', '可勾选「记住登录」以便下次继续使用。', parent=self,
                        position=InfoBarPosition.TOP)

    def _save_login(self) -> None:
        cookie = self.cookie_edit.text().strip()
        user_id = self.id_edit.text().strip()
        if user_id:
            self.db.set_setting('douban_user_id', user_id)
        if cookie and self.remember_checkbox.isChecked():
            self.db.set_setting('douban_cookie', cookie)
        else:
            self.db.delete_setting('douban_cookie')

    def _clear_login(self) -> None:
        self.cookie_edit.clear()
        self.remember_checkbox.setChecked(False)
        self.db.delete_setting('douban_cookie')
        InfoBar.success('已清除登录', '本地保存的豆瓣 Cookie 已删除。', parent=self,
                        position=InfoBarPosition.TOP)

    def validate(self) -> bool:
        if self.worker and self.worker.isRunning():
            return False
        cookie = self.cookie_edit.text().strip()
        user_id = self.id_edit.text().strip() or extract_user_id_from_cookie(cookie)
        if not user_id:
            InfoBar.error('缺少豆瓣 ID', '请输入豆瓣用户 ID，或先扫码登录。', parent=self, position=InfoBarPosition.TOP)
            return False
        self.id_edit.setText(user_id)
        media_types = []
        if self.movie_checkbox.isChecked():
            media_types.append('movie')
        if self.book_checkbox.isChecked():
            media_types.append('book')
        if not media_types and not self.doulist_checkbox.isChecked():
            InfoBar.error('未选择类型', '至少选择观影、读书或豆列中的一项。', parent=self, position=InfoBarPosition.TOP)
            return False
        if self.doulist_checkbox.isChecked() and not cookie:
            InfoBar.error('豆列需要 Cookie', '豆列页面需要登录 Cookie，请扫码登录或填写后重试。', parent=self, position=InfoBarPosition.TOP)
            return False

        self.db.set_setting('douban_user_id', user_id)
        sync_mode = ('full', 'incremental_add', 'incremental_sync')[self.mode_combo.currentIndex()]
        self.db.set_setting('sync_mode', sync_mode)
        if self.remember_checkbox.isChecked() and cookie:
            self.db.set_setting('douban_cookie', cookie)
        else:
            self.db.delete_setting('douban_cookie')
        self.log.clear()
        self.progress.show()
        self.progress.setRange(0, 0)
        self.loading.show()
        self.loading.start()
        self.yesButton.setEnabled(False)
        self.cancelButton.setEnabled(False)
        self.id_edit.setEnabled(False)
        self.cookie_edit.setEnabled(False)
        self.login_button.setEnabled(False)
        self.paste_cookie_button.setEnabled(False)
        self.clear_cookie_button.setEnabled(False)
        self.remember_checkbox.setEnabled(False)
        self.mode_combo.setEnabled(False)
        self.movie_checkbox.setEnabled(False)
        self.book_checkbox.setEnabled(False)
        self.wish_checkbox.setEnabled(False)
        self.doulist_checkbox.setEnabled(False)

        self.worker = SyncWorker(
            media_types, user_id, cookie,
            self.wish_checkbox.isChecked(), self.doulist_checkbox.isChecked(),
            sync_mode, DB_PATH, self,
        )
        self.worker.progress.connect(self._on_progress)
        self.worker.succeeded.connect(self._on_succeeded)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()
        return False

    def _append(self, message: str) -> None:
        self.log.append(message)
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def _on_progress(self, message: str, current: int, total: int) -> None:
        self._append(message)
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(min(current, total))

    def _finish_ui(self) -> None:
        self.progress.hide()
        self.loading.stop()
        self.loading.hide()
        self.yesButton.setEnabled(True)
        self.cancelButton.setEnabled(True)
        self.id_edit.setEnabled(True)
        self.cookie_edit.setEnabled(True)
        self.login_button.setEnabled(True)
        self.paste_cookie_button.setEnabled(True)
        self.clear_cookie_button.setEnabled(True)
        self.remember_checkbox.setEnabled(True)
        self.mode_combo.setEnabled(True)
        self.movie_checkbox.setEnabled(True)
        self.book_checkbox.setEnabled(True)
        self.wish_checkbox.setEnabled(True)
        self.doulist_checkbox.setEnabled(True)
        self.yesButton.setText('再次同步')

    def _on_succeeded(self, result: dict) -> None:
        self._finish_ui()
        mode_labels = {
            'full': '全量同步（远端全局替换）', 'incremental_add': '增量同步（仅新增）',
            'incremental_sync': '增量同步（新增并移除）',
        }
        if result.get('mode') in mode_labels:
            self._append(f"方式：{mode_labels[result['mode']]}")
        for media_type, stats in result.items():
            if media_type in ('mode', 'doulists', 'doulists_error') or not isinstance(stats, dict):
                continue
            self._append(
                f"{MEDIA_LABELS[media_type]}：抓取 {stats['fetched']}，"
                f"新增 {stats['inserted']}，更新 {stats['updated']}，"
                f"跳过 {stats.get('skipped', 0)}，移入回收站 {stats.get('deleted', 0)}"
            )
            if stats.get('wish_fetched') or stats.get('wish_deleted'):
                self._append(
                    f"想看/想读：抓取 {stats['wish_fetched']}，"
                    f"新增 {stats['wish_inserted']}，更新 {stats['wish_updated']}，"
                    f"跳过 {stats.get('wish_skipped', 0)}，移入回收站 {stats.get('wish_deleted', 0)}"
                )
        if 'doulists' in result:
            stats = result['doulists']
            detail = ''
            if 'created' in stats or 'followed' in stats:
                detail = f"（我创建 {stats.get('created', 0)} / 我关注 {stats.get('followed', 0)}）"
            self._append(
                f"豆列：同步 {stats['lists']} 个，条目 {stats['items']} 条{detail}"
                f"，删除 {stats.get('deleted_items', 0)} 条"
            )
            if stats.get('failed'):
                self._append(f"豆列失败 {stats['failed']} 个：" + '；'.join(stats.get('failed_lists') or []))
        if 'doulists_error' in result:
            self._append(f"豆列：{result['doulists_error']}")
            InfoBar.warning('同步完成（豆列跳过）', result['doulists_error'], parent=self,
                            position=InfoBarPosition.TOP, duration=5000)
        elif result.get('doulists', {}).get('failed'):
            failed_count = result['doulists']['failed']
            InfoBar.warning('同步完成', f'有 {failed_count} 个豆列同步失败，详见日志。', parent=self,
                            position=InfoBarPosition.TOP, duration=5000)
        else:
            InfoBar.success('同步完成', '数据已写入本地 SQLite。', parent=self, position=InfoBarPosition.TOP)
        self.synced.emit(result)

    def _on_failed(self, error: str) -> None:
        self._finish_ui()
        self._append(f'同步失败：{error}')
        InfoBar.error('同步失败', error, parent=self, position=InfoBarPosition.TOP, duration=5000)

    def reject(self) -> None:
        if self.worker and self.worker.isRunning():
            InfoBar.warning('正在同步', '请等待同步完成后再关闭窗口。', parent=self, position=InfoBarPosition.TOP)
            return
        super().reject()


class CoverPicker(QLabel):
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._source = ''
        self.setMinimumWidth(180)
        self.setFixedHeight(240)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(cover_placeholder_style())
        qconfig.themeChanged.connect(self._apply_theme)
        self.setText('点击选择封面')

    def _apply_theme(self, *_) -> None:
        self.setStyleSheet(cover_placeholder_style())

    def set_cover(self, path: str) -> None:
        self._source = path
        self._render()

    def _render(self) -> None:
        if not self._source:
            self.clear()
            self.setText('点击选择封面')
            return
        pixmap = QPixmap(self._source)
        if pixmap.isNull():
            self.clear()
            self.setText('图片无法读取')
            return
        canvas = QPixmap(self.size())
        canvas.fill(QColor('#0f131b'))
        small = pixmap.scaled(
            max(1, self.width() // 10), max(1, self.height() // 10),
            Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation,
        )
        background = small.scaled(self.size(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        foreground = pixmap.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        painter = QPainter(canvas)
        painter.setOpacity(0.38)
        painter.drawPixmap(0, 0, background)
        painter.setOpacity(1)
        painter.fillRect(canvas.rect(), QColor(0, 0, 0, 80))
        painter.drawPixmap((canvas.width() - foreground.width()) // 2,
                           (canvas.height() - foreground.height()) // 2, foreground)
        painter.end()
        self.setPixmap(canvas)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class RemoteCoverWorker(QThread):
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(self, record, parent=None):
        super().__init__(parent)
        self.record = record

    def run(self) -> None:
        try:
            media_type = self.record['media_type']
            subject_id = str(self.record['subject_id'])
            cover_url = self.record['cover_url'] or ''
            referer = self.record['url'] or ''
            if not cover_url:
                detail = fetch_subject_detail(subject_id, media_type)
                cover_url = detail.get('cover_url') or ''
                referer = detail.get('url') or referer
            if not cover_url:
                self.failed.emit('没有找到可用的豆瓣封面。')
                return
            path = fetch_cover(cover_url, media_type, subject_id, referer, force=True)
            if not path:
                self.failed.emit('豆瓣封面下载失败，请稍后重试。')
                return
            self.succeeded.emit(path)
        except Exception as exc:
            self.failed.emit(str(exc))


class CoverLinkWorker(QThread):
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(self, url: str, media_type: str, parent=None):
        super().__init__(parent)
        self.url = (url or '').strip()
        self.media_type = media_type

    def run(self) -> None:
        try:
            subject_id = _douban_subject_id(self.url)
            if not subject_id:
                self.failed.emit('链接无效，请粘贴豆瓣条目链接。')
                return
            detail = fetch_subject_detail(subject_id, self.media_type)
            cover_url = detail.get('cover_url') or ''
            if not cover_url:
                self.failed.emit('该链接没有可用封面。')
                return
            path = fetch_cover(cover_url, self.media_type, subject_id,
                               detail.get('url') or self.url, force=True)
            if not path:
                self.failed.emit('封面下载失败，请稍后重试。')
                return
            self.succeeded.emit(path)
        except Exception as exc:
            self.failed.emit(str(exc))


class CoverChangeDialog(MessageBoxBase):
    def __init__(self, db: Database, record, parent=None):
        super().__init__(parent)
        self.db = db
        self.record = record
        self.media_type = record['media_type']
        self.cover_source = record['cover_path'] or ''
        self.cover_worker = None
        self.link_worker = None
        self.link_cover = ''
        self.widget.setFixedWidth(600)
        self.yesButton.setText('保存')
        self.cancelButton.setText('取消')
        self.viewLayout.setSpacing(12)

        self.viewLayout.addWidget(SubtitleLabel('更换封面', self))
        hint = CaptionLabel('仅更换本地封面，其他信息不变。', self)
        hint.setTextColor('#5f6b7a', '#8d95a5')
        self.viewLayout.addWidget(hint)

        row = QHBoxLayout()
        row.setSpacing(22)
        self.cover_preview = CoverPicker(self)
        self.cover_preview.setFixedSize(230, 306)
        self.cover_preview.clicked.connect(self._choose_cover)
        row.addWidget(self.cover_preview, 0, Qt.AlignTop)

        column = QVBoxLayout()
        column.setSpacing(0)
        self.cover_name = CaptionLabel('', self)
        self.cover_name.setFixedWidth(230)
        self.cover_name.setTextColor('#5f6b7a', '#8d95a5')
        column.addWidget(self.cover_name)
        column.addStretch(1)
        local_button = PushButton(FluentIcon.PHOTO, '本地图片', self)
        local_button.setFixedSize(230, 36)
        local_button.setToolTip('从本机选择一张图片')
        local_button.clicked.connect(self._choose_cover)
        column.addWidget(local_button)
        column.addStretch(1)
        self.refresh_button = PushButton(FluentIcon.SYNC, '刷新封面', self)
        self.refresh_button.setFixedSize(230, 36)
        self.refresh_button.setToolTip('重新下载这条记录的豆瓣封面')
        self.refresh_button.setEnabled(bool(record['cover_url'] or record['subject_id']))
        self.refresh_button.clicked.connect(self._fetch_remote)
        column.addWidget(self.refresh_button)
        column.addStretch(1)
        clear_button = PushButton(FluentIcon.DELETE, '清除封面', self)
        clear_button.setFixedSize(230, 36)
        clear_button.setToolTip('移除本地封面')
        clear_button.clicked.connect(self._clear_cover)
        column.addWidget(clear_button)
        column.addStretch(1)
        row.addLayout(column)
        row.addStretch(1)
        self.viewLayout.addLayout(row)

        link_row = QHBoxLayout()
        link_row.setSpacing(8)
        self.link_edit = LineEdit(self)
        self.link_edit.setPlaceholderText('粘贴其他豆瓣条目的链接')
        self.link_edit.setClearButtonEnabled(True)
        self.link_edit.textChanged.connect(self._on_link_changed)
        input_style(self.link_edit)
        link_row.addWidget(self.link_edit, 1)
        self.parse_button = PushButton(FluentIcon.SYNC, '解析', self)
        self.parse_button.setFixedSize(88, 34)
        self.parse_button.clicked.connect(self._parse_link)
        self.parse_button.hide()
        link_row.addWidget(self.parse_button)
        self.viewLayout.addLayout(link_row)

        tip = CaptionLabel('支持 jpg / png / webp / gif，自动保存到本地。', self)
        tip.setTextColor('#4b5563', '#5d6474')
        self.viewLayout.addWidget(tip)

        self._set_source(self.cover_source)

    def _set_source(self, source: str, from_link: bool = False) -> None:
        self.cover_source = (source or '').strip()
        self.link_cover = self.cover_source if from_link else ''
        if not self.cover_source:
            self.cover_preview.set_cover('')
            self.cover_name.setText('当前没有封面')
            self.cover_name.setToolTip('')
            return
        full = Path(self.cover_source)
        if not full.is_absolute():
            full = ROOT / full
        self.cover_preview.set_cover(str(full))
        self.cover_name.setText(full.name)
        self.cover_name.setToolTip(str(full))

    def _choose_cover(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, '选择封面图片', '', 'Images (*.jpg *.jpeg *.png *.webp *.gif)'
        )
        if path:
            self._set_source(path)

    def _clear_cover(self) -> None:
        self._set_source('')

    def _reset_refresh_button(self) -> None:
        self.refresh_button.setEnabled(True)
        self.refresh_button.setText('刷新封面')

    def _reset_parse_button(self) -> None:
        self.parse_button.setEnabled(True)
        self.parse_button.setText('解析')

    def _on_link_changed(self, text: str) -> None:
        self.parse_button.setVisible(bool(_douban_subject_id(text)))

    def _fetch_remote(self) -> None:
        if self.cover_worker and self.cover_worker.isRunning():
            return
        self.refresh_button.setEnabled(False)
        self.refresh_button.setText('刷新中…')
        self.cover_worker = RemoteCoverWorker(self.record, self)
        self.cover_worker.succeeded.connect(self._on_remote_succeeded)
        self.cover_worker.failed.connect(self._on_remote_failed)
        self.cover_worker.start()

    def _parse_link(self) -> None:
        url = self.link_edit.text().strip()
        if not _douban_subject_id(url):
            InfoBar.warning('链接无效', '请粘贴豆瓣条目链接。', parent=self,
                            position=InfoBarPosition.TOP)
            self.link_edit.setFocus()
            return
        if self.link_worker and self.link_worker.isRunning():
            return
        self.parse_button.setEnabled(False)
        self.parse_button.setText('解析中…')
        self.link_worker = CoverLinkWorker(url, self.media_type, self)
        self.link_worker.succeeded.connect(self._on_link_succeeded)
        self.link_worker.failed.connect(self._on_link_failed)
        self.link_worker.start()

    def _on_remote_succeeded(self, path: str) -> None:
        self._reset_refresh_button()
        self._set_source(path)
        InfoBar.success('已获取封面', '保存后生效。', parent=self, position=InfoBarPosition.TOP)

    def _on_remote_failed(self, error: str) -> None:
        self._reset_refresh_button()
        InfoBar.error('获取失败', error, parent=self, position=InfoBarPosition.TOP,
                      duration=6000)

    def _on_link_succeeded(self, path: str) -> None:
        self._reset_parse_button()
        self._set_source(path, from_link=True)
        InfoBar.success('已获取封面', '保存后生效。', parent=self, position=InfoBarPosition.TOP)

    def _on_link_failed(self, error: str) -> None:
        self._reset_parse_button()
        InfoBar.error('获取失败', error, parent=self, position=InfoBarPosition.TOP,
                      duration=6000)

    def validate(self) -> bool:
        record_id = int(self.record['id'])
        old_path = self.record['cover_path'] or ''
        source = self.cover_source
        if not source:
            self.db.set_record_cover(record_id, '')
            return True
        copy = bool(self.link_cover) and source == self.link_cover
        path = store_cover_file(source, self.media_type, copy=copy)
        if not path:
            if source == old_path:
                path = old_path
            else:
                InfoBar.error('封面不可用', '无法读取图片，请重新选择。', parent=self,
                              position=InfoBarPosition.TOP)
                return False
        self.db.set_record_cover(record_id, path)
        return True


class SubjectParseWorker(QThread):
    succeeded = Signal(dict)
    failed = Signal(str)

    def __init__(self, subject_id: str, media_type: str, parent=None):
        super().__init__(parent)
        self.subject_id = subject_id
        self.media_type = media_type

    def run(self) -> None:
        try:
            detail = fetch_subject_detail(self.subject_id, self.media_type)
            cover_path = fetch_cover(
                detail.get('cover_url') or '', self.media_type,
                self.subject_id, detail.get('url') or '',
            )
            if cover_path:
                detail['cover_path'] = cover_path
            self.succeeded.emit(detail)
        except Exception as exc:
            self.failed.emit(str(exc))


class AddRecordDialog(MessageBoxBase):
    def __init__(self, db: Database, media_type: str, status: str = 'done', parent=None):
        super().__init__(parent)
        self.db = db
        self.media_type = media_type
        self.record_id = 0
        self.cover_source = ''
        self.parsed_detail = {}
        self.parse_worker = None
        self.widget.setMinimumWidth(860)
        self.yesButton.setText('保存')
        self.cancelButton.setText('取消')
        self.viewLayout.setSpacing(10)
        type_label = MEDIA_LABELS[media_type]

        header = QVBoxLayout()
        header.setSpacing(3)
        header.addWidget(SubtitleLabel(f'新增{type_label}记录', self))
        hint = CaptionLabel('只有标题必填；保存后会写入本地数据库，并参与列表与报告统计。', self)
        hint.setWordWrap(True)
        hint.setTextColor('#5f6b7a', '#8d95a5')
        header.addWidget(hint)
        self.viewLayout.addLayout(header)

        main_row = QHBoxLayout()
        main_row.setSpacing(18)

        cover_column = QVBoxLayout()
        cover_column.setSpacing(6)
        cover_column.addWidget(StrongBodyLabel('本地封面', self))
        self.cover_preview = CoverPicker(self)
        self.cover_preview.clicked.connect(self._choose_cover)
        cover_column.addWidget(self.cover_preview)
        self.cover_name = CaptionLabel('未选择图片', self)
        self.cover_name.setTextColor('#5f6b7a', '#8d95a5')
        self.cover_name.setWordWrap(True)
        cover_column.addWidget(self.cover_name)
        cover_buttons = QHBoxLayout()
        cover_buttons.setSpacing(8)
        cover_button = PushButton('选择图片', self)
        cover_button.clicked.connect(self._choose_cover)
        cover_buttons.addWidget(cover_button)
        clear_cover_button = PushButton('清除', self)
        clear_cover_button.clicked.connect(self._clear_cover)
        cover_buttons.addWidget(clear_cover_button)
        cover_buttons.addStretch(1)
        cover_column.addLayout(cover_buttons)
        cover_tip = CaptionLabel('点击预览图即可选图', self)
        cover_tip.setTextColor('#4b5563', '#5d6474')
        cover_column.addWidget(cover_tip)
        cover_column.addStretch(1)
        main_row.addLayout(cover_column, 1)

        form_column = QVBoxLayout()
        form_column.setSpacing(6)
        form_column.addWidget(BodyLabel('标题', self))
        self.title_edit = LineEdit(self)
        self.title_edit.setPlaceholderText(f'输入{type_label}标题')
        self.title_edit.setClearButtonEnabled(True)
        input_style(self.title_edit)
        form_column.addWidget(self.title_edit)
        form_column.addSpacing(6)
        fields = QHBoxLayout()
        fields.setSpacing(14)
        self.status_combo = ComboBox(self)
        self.status_combo.addItems(['看过' if media_type == 'movie' else '读过',
                                    '想看' if media_type == 'movie' else '想读'])
        self.status_combo.setCurrentIndex(0 if status == 'done' else 1)
        self.date_picker = CalendarPicker(self)
        self.date_picker.setDate(QDate.currentDate())
        self.rating_combo = ComboBox(self)
        self.rating_combo.addItems(['未评分', '★', '★★', '★★★', '★★★★', '★★★★★'])
        for label, widget in (('状态', self.status_combo), ('日期', self.date_picker),
                              ('评分', self.rating_combo)):
            column = QVBoxLayout()
            column.setSpacing(4)
            column.addWidget(BodyLabel(label, self))
            column.addWidget(widget)
            fields.addLayout(column, 1)
        form_column.addLayout(fields)
        form_column.addSpacing(6)

        self.pages_edit = None
        self.word_edit = None
        self.duration_edit = None
        stats_row = QHBoxLayout()
        stats_row.setSpacing(14)
        if media_type == 'book':
            pages_column = QVBoxLayout()
            pages_column.setSpacing(4)
            pages_column.addWidget(BodyLabel('页数', self))
            self.pages_edit = LineEdit(self)
            self.pages_edit.setPlaceholderText('例如 320')
            self.pages_edit.setClearButtonEnabled(True)
            self.pages_edit.setFixedWidth(140)
            pages_column.addWidget(self.pages_edit)
            stats_row.addLayout(pages_column)
            word_column = QVBoxLayout()
            word_column.setSpacing(4)
            word_column.addWidget(BodyLabel('字数', self))
            self.word_edit = LineEdit(self)
            self.word_edit.setPlaceholderText('例如 120000')
            self.word_edit.setClearButtonEnabled(True)
            self.word_edit.setFixedWidth(160)
            word_row = QHBoxLayout()
            word_row.setSpacing(8)
            word_row.addWidget(self.word_edit)
            self.word_hint = CaptionLabel('', self)
            self.word_hint.setTextColor('#b07c20', '#e5b45f')
            word_row.addWidget(self.word_hint)
            word_row.addStretch(1)
            word_column.addLayout(word_row)
            self.word_edit.textChanged.connect(
                lambda text: self.word_hint.setText(_word_hint(text))
            )
            stats_row.addLayout(word_column)
        elif media_type == 'movie':
            duration_column = QVBoxLayout()
            duration_column.setSpacing(4)
            duration_column.addWidget(BodyLabel('时长（分钟）', self))
            self.duration_edit = LineEdit(self)
            self.duration_edit.setPlaceholderText('例如 122')
            self.duration_edit.setClearButtonEnabled(True)
            self.duration_edit.setFixedWidth(140)
            duration_row = QHBoxLayout()
            duration_row.setSpacing(8)
            duration_row.addWidget(self.duration_edit)
            self.duration_hint = CaptionLabel('', self)
            self.duration_hint.setTextColor('#b07c20', '#e5b45f')
            duration_row.addWidget(self.duration_hint)
            duration_row.addStretch(1)
            duration_column.addLayout(duration_row)
            self.duration_edit.textChanged.connect(
                lambda text: self.duration_hint.setText(_duration_hint(text))
            )
            stats_row.addLayout(duration_column)
        stats_row.addStretch(1)
        form_column.addLayout(stats_row)
        form_column.addStretch(1)
        main_row.addLayout(form_column, 1)
        self.viewLayout.addLayout(main_row)
        self.viewLayout.addWidget(self._separator())

        self.viewLayout.addWidget(BodyLabel('豆瓣链接（可选）', self))
        self.url_edit = LineEdit(self)
        host = 'movie' if media_type == 'movie' else 'book'
        self.url_edit.setPlaceholderText(f'https://{host}.douban.com/subject/xxxx/')
        self.url_edit.setClearButtonEnabled(True)
        input_style(self.url_edit)
        self.url_edit.textChanged.connect(self._on_url_changed)
        url_row = QHBoxLayout()
        url_row.setSpacing(8)
        url_row.addWidget(self.url_edit, 1)
        self.parse_button = PushButton(FluentIcon.SYNC, '解析豆瓣链接', self)
        self.parse_button.clicked.connect(self._parse_link)
        self.parse_button.hide()
        url_row.addWidget(self.parse_button)
        self.viewLayout.addLayout(url_row)
        self.parse_status = CaptionLabel('', self)
        self.parse_status.setTextColor('#5f6b7a', '#8d95a5')
        self.parse_status.hide()
        self.viewLayout.addWidget(self.parse_status)
        self.viewLayout.addWidget(self._separator())

        self.viewLayout.addWidget(BodyLabel('短评（可选）', self))
        self.comment_edit = TextEdit(self)
        self.comment_edit.setPlaceholderText('写点短评…')
        self.comment_edit.setFixedHeight(96)
        input_style(self.comment_edit, 'TextEdit')
        self.viewLayout.addWidget(self.comment_edit)

        for edit in (self.pages_edit, self.word_edit, self.duration_edit):
            if edit is not None:
                input_style(edit)

    def _separator(self) -> QFrame:
        line = QFrame(self)
        line.setFixedHeight(1)
        line.setStyleSheet(divider_style())
        return line

    def _choose_cover(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, '选择封面图片', '', 'Images (*.jpg *.jpeg *.png *.webp *.gif)'
        )
        if path:
            self.cover_source = path
            self.cover_preview.set_cover(path)
            self.cover_name.setText(Path(path).name)
            self.cover_name.setToolTip(path)

    def _clear_cover(self) -> None:
        self.cover_source = ''
        self.cover_preview.set_cover('')
        self.cover_name.setText('未选择图片')
        self.cover_name.setToolTip('')

    def _on_url_changed(self, text: str) -> None:
        if _douban_subject_id(text):
            self.parse_button.show()
        else:
            self.parse_button.hide()
            self.parse_status.hide()

    def _parse_link(self) -> None:
        subject_id = _douban_subject_id(self.url_edit.text())
        if not subject_id:
            InfoBar.warning('链接无效', '请粘贴豆瓣条目链接，例如 https://movie.douban.com/subject/1234567/',
                            parent=self, position=InfoBarPosition.TOP)
            return
        if self.parse_worker and self.parse_worker.isRunning():
            return
        self.parse_button.setEnabled(False)
        self.parse_button.setText('解析中…')
        self.parse_status.setText('正在解析豆瓣页面…')
        self.parse_status.show()
        self.parse_worker = SubjectParseWorker(subject_id, self.media_type, self)
        self.parse_worker.succeeded.connect(self._on_parse_succeeded)
        self.parse_worker.failed.connect(self._on_parse_failed)
        self.parse_worker.start()

    def _on_parse_succeeded(self, detail: dict) -> None:
        self.parsed_detail = detail
        self.parse_button.setEnabled(True)
        self.parse_button.setText('重新解析')
        if detail.get('title'):
            self.title_edit.setText(detail['title'])
        rating = int(detail.get('rating') or 0)
        if rating:
            self.rating_combo.setCurrentIndex(max(0, min(5, rating)))
        cover_path = detail.get('cover_path') or ''
        if cover_path and not self.cover_source:
            self.cover_source = cover_path
            preview_path = Path(cover_path)
            if not preview_path.is_absolute():
                preview_path = ROOT / preview_path
            self.cover_preview.set_cover(str(preview_path))
            self.cover_name.setText('已从豆瓣获取')
            self.cover_name.setToolTip(str(preview_path))
        if self.pages_edit is not None and detail.get('pages'):
            self.pages_edit.setText(str(detail['pages']))
        if self.duration_edit is not None and detail.get('duration'):
            self.duration_edit.setText(str(detail['duration']))
        self.parse_status.setText(
            f"解析成功：{detail.get('title', '')}" +
            (f" · {detail['pages']} 页" if detail.get('pages') else '') +
            (f" · {detail['duration']} 分钟" if detail.get('duration') else '')
        )
        InfoBar.success('解析成功', '已填入标题、评分与封面，保存后即可使用。', parent=self,
                        position=InfoBarPosition.TOP)

    def _on_parse_failed(self, error: str) -> None:
        self.parse_button.setEnabled(True)
        self.parse_button.setText('解析豆瓣链接')
        self.parse_status.setText(f'解析失败：{error}')
        InfoBar.error('解析失败', error, parent=self, position=InfoBarPosition.TOP, duration=5000)

    def _store_cover(self) -> str:
        return store_cover_file(self.cover_source, self.media_type)

    def _read_optional_int(self, edit, label: str):
        if edit is None:
            return True, None
        text = edit.text().strip()
        if not text:
            return True, None
        try:
            return True, max(0, int(float(text)))
        except ValueError:
            InfoBar.error(f'{label}无效', f'请输入整数{label}，或留空。', parent=self,
                          position=InfoBarPosition.TOP)
            return False, None

    def validate(self) -> bool:
        if self.parse_worker and self.parse_worker.isRunning():
            InfoBar.info('正在解析', '请等待豆瓣链接解析完成后再保存。', parent=self,
                         position=InfoBarPosition.TOP)
            return False
        title = self.title_edit.text().strip()
        if not title:
            InfoBar.error('缺少标题', '请输入标题。', parent=self, position=InfoBarPosition.TOP)
            return False
        date = self.date_picker.getDate()
        if not date.isValid():
            InfoBar.error('日期无效', '请选择有效日期。', parent=self, position=InfoBarPosition.TOP)
            return False
        ok, pages = self._read_optional_int(self.pages_edit, '页数')
        if not ok:
            return False
        ok, word_count = self._read_optional_int(self.word_edit, '字数')
        if not ok:
            return False
        ok, minutes = self._read_optional_int(self.duration_edit, '时长')
        if not ok:
            return False
        url = self.url_edit.text().strip()
        detail = self.parsed_detail or {}
        if detail and detail.get('subject_id') and _douban_subject_id(url) == detail['subject_id']:
            url = detail.get('url') or url
        parsed_id = detail.get('subject_id') if detail else ''
        subject_id = _douban_subject_id(url) or parsed_id or f'local-{int(time.time() * 1000)}'
        marked_date = date.toString('yyyy-MM-dd')
        status = 'done' if self.status_combo.currentIndex() == 0 else 'wish'
        if self.db.find_record(self.media_type, subject_id, status):
            InfoBar.warning('记录已存在', '同一豆瓣条目和状态已经存在，请直接编辑原记录。',
                            parent=self, position=InfoBarPosition.TOP)
            return False
        if self.db.find_deleted_record(self.media_type, subject_id, status):
            InfoBar.warning('条目在回收站', '该条目已被删除，请先在回收站恢复或彻底删除。',
                            parent=self, position=InfoBarPosition.TOP)
            return False
        record = {
            'media_type': self.media_type,
            'subject_id': subject_id,
            'title': title,
            'url': url,
            'cover_url': detail.get('cover_url', '') if parsed_id == subject_id else '',
            'cover_path': self._store_cover(),
            'rating': self.rating_combo.currentIndex(),
            'marked_date': marked_date,
            'effective_date': marked_date,
            'date_overridden': 1,
            'comment': self.comment_edit.toPlainText().strip(),
            'intro': detail.get('intro', '') if parsed_id == subject_id else '',
            'word_count': word_count or 0,
            'status': status,
            'source': 'manual',
        }
        try:
            self.record_id = self.db.add_manual_record(record)
        except sqlite3.IntegrityError:
            InfoBar.warning('记录已存在', '同一豆瓣条目和状态已经存在，请直接编辑原记录。',
                            parent=self, position=InfoBarPosition.TOP)
            return False
        if pages:
            set_book_pages(subject_id, pages, db=self.db)
        if minutes:
            set_movie_duration(subject_id, minutes, db=self.db)
        return True

    def reject(self) -> None:
        if self.parse_worker and self.parse_worker.isRunning():
            InfoBar.warning('正在解析', '请等待豆瓣链接解析完成后再关闭窗口。', parent=self,
                            position=InfoBarPosition.TOP)
            return
        super().reject()


class WordCountWorker(QThread):
    progress = Signal(str, int, int)
    succeeded = Signal(dict)
    failed = Signal(str)

    def __init__(self, library_dir: str, force: bool, db_path: Path = DB_PATH, parent=None):
        super().__init__(parent)
        self.library_dir = library_dir
        self.force = force
        self.db_path = db_path

    def run(self) -> None:
        try:
            db = Database(self.db_path)
            result = run_word_count(
                db, self.library_dir, self.force,
                lambda message, current, total: self.progress.emit(message, current, total),
            )
            self.succeeded.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class CalibreInstallWorker(QThread):
    progress = Signal(str, int, int)
    succeeded = Signal(str)
    failed = Signal(str)

    def run(self) -> None:
        try:
            path = install_calibre(
                lambda message, current, total: self.progress.emit(message, current, total)
            )
            self.succeeded.emit(path)
        except Exception as exc:
            self.failed.emit(str(exc))


class TwoLineSnapshotDelegate(IndicatorMenuItemDelegate):
    """快照下拉项的两行委托：第一行主信息，第二行小字数据概况。"""

    @staticmethod
    def _text_color(option) -> QColor:
        """菜单项文字颜色。

        不能用 option.palette：qfluentwidgets 的菜单项颜色只写在 menu.qss 里，
        控件调色板跟随的是系统外观，和应用内的亮暗主题无关。亮色主题下选中项
        会取到系统调色板的白色 HighlightedText，压在浅色高亮条上完全看不见；
        暗色主题下普通项又会拿到黑字。这里直接按主题给出与 menu.qss 一致的颜色。
        """
        dark = isDarkTheme()
        if not option.state & QStyle.State_Enabled:
            return QColor(255, 255, 255, 102) if dark else QColor(0, 0, 0, 112)
        if option.state & QStyle.State_Selected:
            # 对应 QSS 的 ::item:selected / ::item:selected:active
            alpha = 178 if option.state & QStyle.State_Active else 255
            return QColor(255, 255, 255, alpha) if dark else QColor(0, 0, 0, alpha)
        return QColor(255, 255, 255) if dark else QColor(0, 0, 0)

    def paint(self, painter: QPainter, option, index: QModelIndex):
        # 绘制背景（选中 / 悬停高亮）
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawPrimitive(QStyle.PE_PanelItemViewItem, option, painter, option.widget)

        action = index.data(Qt.UserRole)
        snapshot = action.data() if isinstance(action, QAction) else None
        selected = bool(option.state & QStyle.State_Selected)

        painter.save()
        painter.setRenderHints(QPainter.TextAntialiasing)

        # 主文字颜色按亮暗主题取，避免亮色下选中项白字压浅色高亮
        text_color = self._text_color(option)

        # 第一行：主信息
        painter.setFont(getFont(14))
        painter.setPen(text_color)
        main_rect = QRectF(option.rect.x() + 16, option.rect.y() + 7,
                           option.rect.width() - 32, 20)
        painter.drawText(main_rect, Qt.AlignLeft | Qt.AlignVCenter,
                         index.data(Qt.DisplayRole) or '')

        # 第二行：数据概况（小字），用主文字色的半透明版本，保证两种主题下都清晰
        summary = SnapshotPickerDialog._brief_counts(snapshot) if snapshot else ''
        age = SnapshotPickerDialog._age_text(snapshot) if snapshot else ''
        summary = ' · '.join(part for part in (summary, age) if part)
        if summary:
            painter.setFont(getFont(11))
            sub_color = QColor(text_color)
            sub_color.setAlpha(min(160, text_color.alpha()))
            painter.setPen(sub_color)
            sub_rect = QRectF(option.rect.x() + 16, option.rect.y() + 28,
                              option.rect.width() - 32, 16)
            painter.drawText(sub_rect, Qt.AlignLeft | Qt.AlignVCenter, summary)

        painter.restore()

        # 选中指示条
        if selected:
            painter.save()
            painter.setRenderHints(QPainter.Antialiasing)
            painter.setPen(Qt.NoPen)
            painter.setBrush(themeColor())
            cy = option.rect.y() + option.rect.height() // 2
            painter.drawRoundedRect(6, cy - 8, 3, 16, 1.5, 1.5)
            painter.restore()


class SnapshotComboBox(ComboBox):
    """快照选择下拉框：下拉项两行显示，主信息 + 小字数据概况。"""

    def _createComboMenu(self):
        menu = ComboBoxMenu(self)
        # 两行显示需要更高的菜单项，并替换为两行委托
        menu.setItemHeight(56)
        menu.view.setItemDelegate(TwoLineSnapshotDelegate(menu.view))
        return menu

    def _showComboMenu(self):
        if not self.items:
            return
        menu = self._createComboMenu()
        for item in self.items:
            action = QAction(item.icon, item.text)
            action.setEnabled(item.isEnabled)
            action.setData(item.userData)
            menu.addAction(action)

        menu.view.itemClicked.connect(
            lambda i: self._onItemClicked(self.findText(i.text().lstrip())))

        if menu.view.width() < self.width():
            menu.view.setMinimumWidth(self.width())
            menu.adjustSize()

        menu.setMaxVisibleItems(self.maxVisibleItems())
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.closedSignal.connect(self._onDropMenuClosed)
        self.dropMenu = menu

        if self.currentIndex() >= 0 and self.items:
            menu.setDefaultAction(menu.actions()[self.currentIndex()])

        x = -menu.width() // 2 + menu.layout().contentsMargins().left() + self.width() // 2
        pd = self.mapToGlobal(QPoint(x, self.height()))
        hd = menu.view.heightForAnimation(pd, MenuAnimationType.DROP_DOWN)
        pu = self.mapToGlobal(QPoint(x, 0))
        hu = menu.view.heightForAnimation(pu, MenuAnimationType.PULL_UP)

        if hd >= hu:
            menu.view.adjustSize(pd, MenuAnimationType.DROP_DOWN)
            menu.exec(pd, aniType=MenuAnimationType.DROP_DOWN)
        else:
            menu.view.adjustSize(pu, MenuAnimationType.PULL_UP)
            menu.exec(pu, aniType=MenuAnimationType.PULL_UP)


class SnapshotPickerDialog(MessageBoxBase):
    """列出同步目录中的快照，让用户自己选择要从哪一份恢复。"""

    def __init__(self, snapshots: list[dict], warning: str = '', local: dict = None,
                 parent=None):
        super().__init__(parent)
        self.snapshots = snapshots
        self.local = local or {}
        self.widget.setFixedWidth(600)
        self.yesButton.setText('恢复此备份')
        self.cancelButton.setText('取消')
        self.viewLayout.setSpacing(10)
        self.viewLayout.addWidget(SubtitleLabel('从同步目录恢复', self))
        hint = CaptionLabel(
            '每台设备保留 3 份自动备份 + 2 份手动备份。恢复会用所选快照覆盖本机数据库，'
            '覆盖前会把当前数据库自动备份到 data/backups。',
            self,
        )
        hint.setWordWrap(True)
        hint.setTextColor('#5f6b7a', '#8d95a5')
        self.viewLayout.addWidget(hint)

        self.combo = SnapshotComboBox(self)
        for snapshot in snapshots:
            self.combo.addItem(self._label(snapshot), userData=snapshot)
        self.combo.setCurrentIndex(0)
        self.combo.setFixedWidth(500)
        self.viewLayout.addWidget(self.combo)

        self.detail = CaptionLabel('', self)
        self.detail.setWordWrap(True)
        self.detail.setTextColor('#4b5563', '#5d6474')
        self.viewLayout.addWidget(self.detail)

        if warning:
            warn_label = CaptionLabel(warning, self)
            warn_label.setWordWrap(True)
            warn_label.setTextColor('#b45309', '#e5b45f')
            self.viewLayout.addWidget(warn_label)

        self.combo.currentIndexChanged.connect(self._refresh_detail)
        self._refresh_detail()

    @staticmethod
    def _brief_counts(snapshot: dict) -> str:
        """从快照的 counts 字段生成一行简短的数据概况，给下拉选项做小字说明。"""
        counts = snapshot.get('counts')
        if not isinstance(counts, dict):
            if not snapshot.get('downloaded', True):
                return '未下载'
            return ''
        movie, book = counts.get('records_movie'), counts.get('records_book')
        if movie is not None and book is not None:
            parts = [f"影视 {movie} · 读书 {book}"]
        else:
            parts = [f"{counts.get('records', 0)}条记录"]
        doulists = counts.get('doulists', 0)
        if doulists:
            parts.append(f"{doulists}个豆列")
        plan_items = counts.get('plan_items', 0)
        if plan_items:
            parts.append(f"{plan_items}项计划")
        book_words = counts.get('book_words', 0)
        if book_words:
            parts.append(f"{book_words}本字数")
        return ' · '.join(parts)

    @staticmethod
    def _label(snapshot: dict) -> str:
        """下拉项第一行：时间 · 类型 · 设备（最新标记）。数据概况由委托另起一行小字绘制。"""
        created = snapshot.get('created_at') or '未知时间'
        device = snapshot.get('device') or '未知设备'
        kind = '自动' if snapshot.get('kind') == 'auto' else '手动'
        marker = '（最新）' if snapshot.get('is_latest') else ''
        return f'{created} · {kind} · {device}{marker}'

    @staticmethod
    def _fmt_time(value: str) -> str:
        """把 ISO 时间（2026-11-19T10:20:00）显示成易读的分钟精度。"""
        return (value or '').replace('T', ' ')[:16]

    @staticmethod
    def _age_text(snapshot: dict) -> str:
        """快照距今多久，帮助判断新旧（时间取自文件名/清单）。"""
        created_ts = snapshot.get('created_ts') or 0
        if not created_ts:
            return ''
        diff = int(time.time() - created_ts)
        if diff < 0:
            return ''
        if diff < 60:
            return '刚刚'
        if diff < 3600:
            return f'{diff // 60} 分钟前'
        if diff < 86400:
            return f'{diff // 3600} 小时前'
        return f'{diff // 86400} 天前'

    @staticmethod
    def _size_text(size: int) -> str:
        if size >= 1024 * 1024:
            return f'{size / 1048576:.1f} MB'
        return f'{max(1, round(size / 1024))} KB'

    def selected_snapshot(self) -> dict:
        index = max(0, self.combo.currentIndex())
        return self.snapshots[index]

    def _refresh_detail(self) -> None:
        item = self.selected_snapshot()
        lines: list[str] = []
        counts = item.get('counts')
        if isinstance(counts, dict):
            movie, book = counts.get('records_movie'), counts.get('records_book')
            if movie is not None and book is not None:
                data_parts = [f"影视 {movie} 条 · 读书 {book} 条"
                              f"（共 {counts.get('records', 0)} 条）"]
            else:
                data_parts = [f"{counts.get('records', 0)} 条记录"]
            doulists = counts.get('doulists', 0)
            doulist_items = counts.get('doulist_items', 0)
            if doulists or doulist_items:
                data_parts.append(f"{doulists} 个豆列（{doulist_items} 条）")
            plan_folders = counts.get('plan_folders', 0)
            plan_items = counts.get('plan_items', 0)
            if plan_folders or plan_items:
                data_parts.append(f"{plan_items} 项计划（{plan_folders} 个清单）")
            book_words = counts.get('book_words', 0)
            if book_words:
                data_parts.append(f"{book_words} 本字数统计")
            book_meta = counts.get('book_meta', 0)
            if book_meta:
                data_parts.append(f"{book_meta} 条书籍元数据")
            ignored = counts.get('ignored_subjects', 0)
            purged = counts.get('purged_records', 0)
            if ignored or purged:
                data_parts.append(f"忽略 {ignored} 条 · 删除标记 {purged} 条")
            updated = item.get('records_updated') or ''
            if updated:
                data_parts.append(f"最近更新 {self._fmt_time(updated)}")
            lines.append('数据：' + ' · '.join(data_parts))
        elif not item.get('downloaded', True):
            lines.append('数据：尚未下载到本机，恢复时可能要先等待同步')

        compare = self._compare_line(item)
        if compare:
            lines.append(compare)

        meta_parts = [f"文件 {item.get('snapshot', '')}"]
        if item.get('downloaded', True):
            meta_parts.append(self._size_text(int(item.get('size') or 0)))
        else:
            meta_parts.append('未下载')
        version = item.get('app_version') or ''
        if version:
            meta_parts.append(f'由 v{version} 创建')
            if version != APP_VERSION:
                meta_parts.append(f'与当前 {APP_VERSION_TEXT} 不同，可能不完全兼容')
        if item.get('snapshot') and item.get('snapshot') == (self.local or {}).get('last_snapshot'):
            meta_parts.append('本机上次备份')
        meta_parts.append('校验值已核对' if item.get('sha256') else '旧快照（无校验值）')
        lines.append(' · '.join(meta_parts))
        lines.append('提示：封面图片不随快照同步，恢复后可按提示从豆瓣补全。')
        self.detail.setText('\n'.join(lines))

    def _compare_line(self, item: dict) -> str:
        """把快照和本机当前数据做规模对比，直观判断哪份更完整/更新。"""
        local_counts = (self.local or {}).get('counts') or {}
        snapshot_counts = item.get('counts')
        if not isinstance(local_counts, dict) or not isinstance(snapshot_counts, dict):
            return ''
        if not local_counts or not snapshot_counts:
            return ''
        if 'records_movie' in snapshot_counts and 'records_book' in snapshot_counts:
            labels = (('records_movie', '影视'), ('records_book', '读书'),
                      ('doulists', '豆列'), ('plan_items', '计划'))
        else:
            labels = (('records', '记录'), ('doulists', '豆列'), ('plan_items', '计划'))
        parts = []
        for key, label in labels:
            diff = int(snapshot_counts.get(key, 0)) - int(local_counts.get(key, 0))
            if diff:
                parts.append(f'{label} {"+" if diff > 0 else ""}{diff}')
        text = '与本机相同' if not parts else '与本机相比：' + '、'.join(parts)
        snapshot_updated = self._fmt_time(item.get('records_updated') or '')
        local_updated = self._fmt_time((self.local or {}).get('records_updated') or '')
        if snapshot_updated and local_updated:
            text += f'；最近更新 {snapshot_updated}（本机 {local_updated}）'
        elif snapshot_updated:
            text += f'；最近更新 {snapshot_updated}'
        return text


class CalibreInstallDialog(MessageBoxBase):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker = None
        self.installed_path = ''
        self.widget.setMinimumWidth(580)
        self.yesButton.setText('开始安装')
        self.cancelButton.setText('取消')
        self.viewLayout.addWidget(SubtitleLabel('安装 Calibre', self))
        hint = CaptionLabel(
            'Calibre 安装包约 200–400 MB，将自动下载并校验签名。macOS 会安装到用户目录，'
            'Windows 会调用 winget 或系统安装器；系统可能要求确认权限。',
            self,
        )
        hint.setWordWrap(True)
        hint.setTextColor('#5f6b7a', '#8d95a5')
        self.viewLayout.addWidget(hint)
        self.status = CaptionLabel('准备下载…', self)
        self.status.setWordWrap(True)
        self.viewLayout.addWidget(self.status)
        self.progress = ProgressBar(self)
        self.progress.setRange(0, 0)
        self.progress.hide()
        self.viewLayout.addWidget(self.progress)

    def validate(self) -> bool:
        if self.worker and self.worker.isRunning():
            return False
        self._set_busy(True)
        self.worker = CalibreInstallWorker(self)
        self.worker.progress.connect(self._on_progress)
        self.worker.succeeded.connect(self._on_succeeded)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()
        return False

    def _set_busy(self, busy: bool) -> None:
        self.yesButton.setEnabled(not busy)
        self.cancelButton.setEnabled(not busy)
        self.progress.setVisible(busy)

    def _on_progress(self, message: str, current: int, total: int) -> None:
        self.status.setText(message)
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(min(current, total))
        else:
            self.progress.setRange(0, 0)

    def _on_succeeded(self, path: str) -> None:
        self.installed_path = path
        self.status.setText(f'安装完成：{path}')
        self._set_busy(False)
        self.accept()

    def _on_failed(self, error: str) -> None:
        self._set_busy(False)
        self.status.setText(error)
        InfoBar.error('Calibre 安装失败', error, parent=self, position=InfoBarPosition.TOP,
                      duration=8000)

    def reject(self) -> None:
        if self.worker and self.worker.isRunning():
            InfoBar.warning('正在安装', '请等待 Calibre 安装完成后再关闭窗口。', parent=self,
                            position=InfoBarPosition.TOP)
            return
        super().reject()


class WordCountDialog(MessageBoxBase):
    words_updated = Signal(dict)

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.worker = None
        self.widget.setMinimumWidth(680)
        self.yesButton.setText('开始统计')
        self.cancelButton.setText('关闭')

        self.viewLayout.addWidget(SubtitleLabel('统计书籍字数', self))
        hint = CaptionLabel(
            '扫描本地 Calibre 书库或电子书目录，按书名匹配记录与豆列中的书籍，并通过 calibre 的 '
            'ebook-convert 提取文本统计字数。统计结果会写入本地记录并在卡片和海报中展示。',
            self,
        )
        hint.setWordWrap(True)
        hint.setTextColor('#5f6b7a', '#8d95a5')
        self.viewLayout.addWidget(hint)

        self.viewLayout.addWidget(BodyLabel('本地书库目录', self))
        dir_row = QHBoxLayout()
        dir_row.setSpacing(8)
        self.dir_edit = LineEdit(self)
        self.dir_edit.setText(self.db.get_setting('book_library_dir'))
        self.dir_edit.setPlaceholderText('包含 metadata.db 的 Calibre 书库，或存放电子书的文件夹')
        dir_row.addWidget(self.dir_edit, 1)
        choose_button = PushButton('选择目录', self)
        choose_button.clicked.connect(self._choose_dir)
        dir_row.addWidget(choose_button)
        self.viewLayout.addLayout(dir_row)

        self.calibre = find_calibre()
        self.calibre_label = CaptionLabel('', self)
        self.calibre_label.setWordWrap(True)
        self.viewLayout.addWidget(self.calibre_label)
        self.calibre_download_button = PushButton(FluentIcon.DOWNLOAD, '安装 Calibre', self)
        self.calibre_download_button.clicked.connect(self._open_calibre_install)
        self.viewLayout.addWidget(self.calibre_download_button)
        self._refresh_calibre()

        self.force_checkbox = CheckBox('强制重新统计（忽略已有字数与缓存）', self)
        self.viewLayout.addWidget(self.force_checkbox)

        progress_row = QHBoxLayout()
        progress_row.setSpacing(10)
        self.progress = ProgressBar(self)
        self.progress.setRange(0, 0)
        self.progress.hide()
        progress_row.addWidget(self.progress, 1)
        self.loading = IndeterminateProgressRing(self, start=False)
        self.loading.setFixedSize(20, 20)
        self.loading.hide()
        progress_row.addWidget(self.loading)
        self.viewLayout.addLayout(progress_row)
        self.log = TextEdit(self)
        self.log.setReadOnly(True)
        self.log.setFixedHeight(130)
        self.log.setPlaceholderText('统计日志')
        self.viewLayout.addWidget(self.log)

    def _choose_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, '选择本地书库目录')
        if path:
            self.dir_edit.setText(path)

    def _refresh_calibre(self) -> None:
        self.calibre = find_calibre()
        if self.calibre:
            self.calibre_label.setText(f'已检测到 calibre：{self.calibre}')
            self.calibre_label.setTextColor('#4f7d3a', '#7fae68')
            self.calibre_download_button.hide()
        else:
            self.calibre_label.setText(
                '未检测到 calibre：仅能直接统计 EPUB / TXT / DOCX 的字数。'
            )
            self.calibre_label.setTextColor('#b4661f', '#d98a4a')
            self.calibre_download_button.show()

    def _open_calibre_install(self) -> None:
        dialog = CalibreInstallDialog(self)
        if dialog.exec() and dialog.installed_path:
            self._refresh_calibre()
            InfoBar.success('Calibre 安装完成', '现在可以统计更多电子书格式的字数。',
                            parent=self, position=InfoBarPosition.TOP, duration=6000)

    def _append(self, message: str) -> None:
        self.log.append(message)
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def validate(self) -> bool:
        if self.worker and self.worker.isRunning():
            return False
        library_dir = self.dir_edit.text().strip()
        if not library_dir or not Path(library_dir).expanduser().is_dir():
            InfoBar.error('目录无效', '请选择存在的本地书库目录。', parent=self,
                          position=InfoBarPosition.TOP)
            return False
        self.db.set_setting('book_library_dir', library_dir)
        self.log.clear()
        self.progress.show()
        self.progress.setRange(0, 0)
        self.loading.show()
        self.loading.start()
        self.yesButton.setEnabled(False)
        self.cancelButton.setEnabled(False)
        self.dir_edit.setEnabled(False)
        self.force_checkbox.setEnabled(False)
        self.worker = WordCountWorker(library_dir, self.force_checkbox.isChecked(), DB_PATH, self)
        self.worker.progress.connect(self._on_progress)
        self.worker.succeeded.connect(self._on_succeeded)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()
        return False

    def _on_progress(self, message: str, current: int, total: int) -> None:
        self._append(message)
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(min(current, total))

    def _finish_ui(self) -> None:
        self.progress.hide()
        self.loading.stop()
        self.loading.hide()
        self.yesButton.setEnabled(True)
        self.cancelButton.setEnabled(True)
        self.dir_edit.setEnabled(True)
        self.force_checkbox.setEnabled(True)
        self.yesButton.setText('再次统计')

    def _on_succeeded(self, stats: dict) -> None:
        self._finish_ui()
        message = (
            f"匹配 {stats['matched']}/{stats['total']} 本，新统计 {stats['counted']} 本，"
            f"缓存 {stats['cached']} 本，未匹配 {stats['missing']} 本，失败 {stats['failed']} 本。"
        )
        self._append(message)
        self.status_message = message
        InfoBar.success('字数统计完成', message, parent=self, position=InfoBarPosition.TOP,
                        duration=6000)
        self.words_updated.emit(stats)

    def _on_failed(self, error: str) -> None:
        self._finish_ui()
        self._append(f'统计失败：{error}')
        InfoBar.error('统计失败', error, parent=self, position=InfoBarPosition.TOP, duration=6000)

    def reject(self) -> None:
        if self.worker and self.worker.isRunning():
            InfoBar.warning('正在统计', '请等待统计完成后再关闭窗口。', parent=self,
                            position=InfoBarPosition.TOP)
            return
        super().reject()
