import re
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QFontMetrics, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QProgressBar,
    QScrollArea, QStackedWidget, QVBoxLayout, QWidget,
)

from qfluentwidgets import (
    BodyLabel, CaptionLabel, CheckBox, ComboBox, FlowLayout, FluentIcon, InfoBar,
    InfoBarPosition, LineEdit, MessageBox, MessageBoxBase, PushButton,
    SmoothScrollArea, StrongBodyLabel, SubtitleLabel, TitleLabel, TransparentToolButton,
    qconfig,
)

from app.config import MEDIA_NOUNS, PLANS_DIR, READ_LABELS
from app.db import Database
from app.media_meta import book_stats_text, movie_stats_text
from app.plan_export import export_plan_png
from app.profile import effective_name
from app.ui.read_stamp import ReadStamp
from app.ui.segmented import SegmentedSwitch
from app.ui.styles import progress_style, surface_style, primary_button
from app.ui.record_card import _cover_pixmap, _two_line_text

SOURCE_LABELS = {'wish': '想看/想读', 'doulist': '豆列', 'manual': '手动'}


def _read_text(media_type: str, is_read) -> str:
    return READ_LABELS.get(media_type, ('已读', '未读'))[0 if is_read else 1]


def _count_text(media_type: str, read: int, total: int) -> str:
    if total <= 0:
        return '暂无条目'
    done_text, todo_text = READ_LABELS.get(media_type, ('已读', '未读'))
    noun = MEDIA_NOUNS.get(media_type, '条')
    return f'{done_text} {read}/{total} {noun} · {todo_text} {max(0, total - read)}'


def _folder_placeholder(width: int, height: int) -> QPixmap:
    canvas = QPixmap(width, height)
    canvas.fill(QColor('#0f131b'))
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QColor('#5d6474'))
    font = QFont()
    font.setPointSize(12)
    painter.setFont(font)
    painter.drawText(canvas.rect(), Qt.AlignCenter, '暂无封面')
    painter.end()
    return canvas


def _folder_pixmap(folder, width: int, height: int) -> QPixmap:
    cover = folder['cover_path'] or ''
    if cover:
        return _cover_pixmap(cover, width, height, fill=True)
    return _folder_placeholder(width, height)


class PlanFolderCard(QFrame):
    open_requested = Signal(int)
    move_requested = Signal(int, int)
    delete_requested = Signal(int)

    def __init__(self, folder, parent=None):
        super().__init__(parent)
        self.folder = folder
        self.setObjectName('planFolderCard')
        self.setFixedWidth(214)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(surface_style('planFolderCard', 12))
        qconfig.themeChanged.connect(self._apply_theme)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(7)
        cover = QLabel(self)
        cover.setFixedSize(194, 272)
        cover.setPixmap(_folder_pixmap(folder, 194, 272))
        cover.setStyleSheet('background:#0f131b; border-radius:7px;')
        layout.addWidget(cover)

        title = StrongBodyLabel('', self)
        title.setWordWrap(True)
        metrics = QFontMetrics(title.font())
        title.setFixedHeight(metrics.lineSpacing() * 2 + 2)
        title.setText(_two_line_text(folder['title'] or '', title.font(), 194))
        title.setToolTip(folder['title'] or '')
        layout.addWidget(title)

        footer = QHBoxLayout()
        footer.setSpacing(2)
        total = int(folder['item_count'] or 0)
        read = min(total, int(folder['read_count'] or 0))
        stats = CaptionLabel(_count_text(folder['media_type'], read, total), self)
        stats.setTextColor('#4b5563', '#b9b5aa')
        layout.addWidget(stats)
        progress = QProgressBar(self)
        progress.setFixedHeight(7)
        progress.setRange(0, max(1, total))
        progress.setValue(read)
        progress.setTextVisible(False)
        progress.setStyleSheet(progress_style())
        progress.setVisible(total > 0)
        self.progress_bar = progress
        layout.addWidget(progress)
        footer.addStretch(1)
        up_button = TransparentToolButton(FluentIcon.UP, self)
        up_button.setIconSize(QSize(12, 12))
        up_button.setFixedSize(24, 24)
        up_button.setToolTip('上移计划')
        up_button.clicked.connect(lambda: self.move_requested.emit(int(folder['id']), -1))
        footer.addWidget(up_button)
        down_button = TransparentToolButton(FluentIcon.DOWN, self)
        down_button.setIconSize(QSize(12, 12))
        down_button.setFixedSize(24, 24)
        down_button.setToolTip('下移计划')
        down_button.clicked.connect(lambda: self.move_requested.emit(int(folder['id']), 1))
        footer.addWidget(down_button)
        delete_button = TransparentToolButton(FluentIcon.DELETE, self)
        delete_button.setIconSize(QSize(12, 12))
        delete_button.setFixedSize(24, 24)
        delete_button.setToolTip('删除计划')
        delete_button.clicked.connect(
            lambda: self.delete_requested.emit(int(folder['id']))
        )
        footer.addWidget(delete_button)
        layout.addLayout(footer)

    def _apply_theme(self, *_) -> None:
        self.setStyleSheet(surface_style('planFolderCard', 12))
        self.progress_bar.setStyleSheet(progress_style())

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.open_requested.emit(int(self.folder['id']))
        super().mouseReleaseEvent(event)


class PlanFolderDialog(MessageBoxBase):
    def __init__(self, db: Database, media_type: str, folder=None, parent=None):
        super().__init__(parent)
        self.db = db
        self.media_type = media_type
        self.folder = folder
        self.folder_id = int(folder['id']) if folder else 0
        self.widget.setMinimumWidth(520)
        self.yesButton.setText('保存')
        self.cancelButton.setText('取消')
        self.viewLayout.addWidget(SubtitleLabel(
            '重命名计划' if folder else '新建计划', self
        ))
        hint = CaptionLabel('每个计划是一个文件夹，条目添加后可以单独排序和删除。', self)
        hint.setTextColor('#5f6b7a', '#8d95a5')
        self.viewLayout.addWidget(hint)

        self.viewLayout.addWidget(BodyLabel('计划名称', self))
        self.title_edit = LineEdit(self)
        self.title_edit.setPlaceholderText('例如：2026 年想读、科幻补完计划')
        self.title_edit.setClearButtonEnabled(True)
        if folder:
            self.title_edit.setText(folder['title'] or '')
        self.viewLayout.addWidget(self.title_edit)

        self.viewLayout.addWidget(BodyLabel('备注（可选）', self))
        self.note_edit = LineEdit(self)
        self.note_edit.setPlaceholderText('这个计划的说明')
        self.note_edit.setClearButtonEnabled(True)
        if folder:
            self.note_edit.setText(folder['note'] or '')
        self.viewLayout.addWidget(self.note_edit)

    def validate(self) -> bool:
        title = self.title_edit.text().strip()
        if not title:
            InfoBar.error('缺少名称', '请输入计划名称。', parent=self, position=InfoBarPosition.TOP)
            return False
        if self.folder_id:
            ok = self.db.update_plan_folder(self.folder_id, title, self.note_edit.text().strip())
        else:
            self.folder_id = self.db.add_plan_folder(
                self.media_type, title, self.note_edit.text().strip()
            ) or 0
            ok = bool(self.folder_id)
        if not ok:
            if self.db.find_deleted_plan_folder(self.media_type, title):
                InfoBar.warning('计划在回收站', '同名计划已被删除，请先在回收站恢复或彻底删除。',
                                parent=self, position=InfoBarPosition.TOP)
            else:
                InfoBar.warning('名称已存在', '同一类型下已有同名计划，请换一个名称。',
                                parent=self, position=InfoBarPosition.TOP)
            return False
        return True


class PlanRow(QFrame):
    move_requested = Signal(int, int)
    delete_requested = Signal(int)
    open_requested = Signal(str)

    def __init__(self, position: int, item, stats_text: str = '', parent=None):
        super().__init__(parent)
        self.item = item
        self.setObjectName('planRow')
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip('点击在豆瓣打开')
        self.setStyleSheet(surface_style('planRow', 10))
        qconfig.themeChanged.connect(self._apply_theme)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 14, 10)
        layout.setSpacing(12)

        index = CaptionLabel(f'{position:02d}', self)
        index.setFixedWidth(28)
        index.setStyleSheet('color:#e5b45f; font-weight:700;')
        layout.addWidget(index, 0, Qt.AlignVCenter)

        cover = QLabel(self)
        cover.setFixedSize(58, 82)
        cover.setAlignment(Qt.AlignCenter)
        cover.setPixmap(_cover_pixmap(item['cover_path'] or '', 58, 82, fill=True))
        cover.setStyleSheet('background:#0f131b; border-radius:5px;')
        layout.addWidget(cover)

        info = QVBoxLayout()
        info.setSpacing(4)
        title = StrongBodyLabel('', self)
        metrics = QFontMetrics(title.font())
        title.setText(metrics.elidedText(item['title'] or '', Qt.ElideRight, 620))
        title.setToolTip(item['title'] or '')
        info.addWidget(title)
        source_row = QHBoxLayout()
        source_row.setSpacing(6)
        source = CaptionLabel(SOURCE_LABELS.get(item['source'], item['source'] or '计划'), self)
        source.setTextColor('#5f6b7a', '#8d95a5')
        source_row.addWidget(source)
        source_row.addStretch(1)
        info.addLayout(source_row)
        if stats_text:
            stats_label = CaptionLabel(stats_text, self)
            stats_label.setTextColor('#5f6b7a', '#8d95a5')
            info.addWidget(stats_label)
        if item['note']:
            note = CaptionLabel('', self)
            note.setText(metrics.elidedText(item['note'], Qt.ElideRight, 620))
            note.setStyleSheet('color:#c9c5bb;')
            info.addWidget(note)
        info.addStretch(1)

        info_row = QHBoxLayout()
        info_row.setSpacing(14)
        info_row.addLayout(info)
        if item['is_read']:
            stamp = ReadStamp(
                item['media_type'], item['read_date'] or '', item['read_rating'] or 0, 92, self
            )
            info_row.addWidget(stamp, 0, Qt.AlignVCenter)
        info_row.addStretch(1)
        layout.addLayout(info_row, 1)

        up_button = PushButton('上移', self)
        up_button.setFixedWidth(58)
        up_button.clicked.connect(lambda: self.move_requested.emit(int(item['id']), -1))
        layout.addWidget(up_button)
        down_button = PushButton('下移', self)
        down_button.setFixedWidth(58)
        down_button.clicked.connect(lambda: self.move_requested.emit(int(item['id']), 1))
        layout.addWidget(down_button)
        delete_button = PushButton('删除', self)
        delete_button.setFixedWidth(58)
        delete_button.clicked.connect(lambda: self.delete_requested.emit(int(item['id'])))
        layout.addWidget(delete_button)

    def _apply_theme(self, *_) -> None:
        self.setStyleSheet(surface_style('planRow', 10))

    def _open(self) -> None:
        if self.item['url']:
            QDesktopServices.openUrl(QUrl(self.item['url']))

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._open()
        super().mouseReleaseEvent(event)


class AddWishDialog(MessageBoxBase):
    def __init__(self, db: Database, media_type: str, folder_id: int, parent=None):
        super().__init__(parent)
        self.db = db
        self.media_type = media_type
        self.folder_id = folder_id
        self.checkboxes = {}
        self.widget.setMinimumWidth(720)
        self.yesButton.setText('添加选中')
        self.cancelButton.setText('取消')
        label = '想看' if media_type == 'movie' else '想读'
        self.viewLayout.addWidget(SubtitleLabel(f'从{label}添加到计划', self))
        hint = CaptionLabel('勾选要加入当前计划的条目；已在该计划中的条目会自动隐藏。', self)
        hint.setTextColor('#5f6b7a', '#8d95a5')
        self.viewLayout.addWidget(hint)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(380)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet('QScrollArea{background:transparent;border:none;}')
        container = QWidget()
        container.setStyleSheet('background:transparent;')
        layout = QVBoxLayout(container)
        layout.setContentsMargins(2, 8, 2, 0)
        layout.setSpacing(10)
        existing = db.plan_subject_ids(media_type, folder_id)
        read_ids = db.done_subject_ids(media_type)
        rows = [row for row in db.list_records(media_type, status='wish')
                if str(row['subject_id']) not in existing]
        if not rows:
            layout.addWidget(BodyLabel('没有可添加的想读/想看记录。', container))
        for row in rows:
            item_widget = QWidget(container)
            item_layout = QVBoxLayout(item_widget)
            item_layout.setContentsMargins(0, 0, 0, 0)
            item_layout.setSpacing(2)
            checkbox = CheckBox(row['title'], item_widget)
            is_read = str(row['subject_id']) in read_ids
            meta_parts = [
                _read_text(row['media_type'], is_read),
                row['effective_date'] or row['marked_date'] or '未设置',
            ]
            if row['media_type'] == 'book':
                meta_parts.append(book_stats_text(
                    row['subject_id'], int(row['word_count'] or 0)
                ))
            elif row['media_type'] == 'movie':
                meta_parts.append(movie_stats_text(row['intro'] or '', row['subject_id']))
            meta = CaptionLabel(' · '.join(meta_parts), item_widget)
            meta.setContentsMargins(26, 0, 0, 0)
            meta.setStyleSheet('color:#7fae68;' if is_read else 'color:#d98a4a;')
            item_layout.addWidget(checkbox)
            item_layout.addWidget(meta)
            self.checkboxes[int(row['id'])] = (checkbox, row)
            layout.addWidget(item_widget)
        layout.addStretch(1)
        scroll.setWidget(container)
        self.viewLayout.addWidget(scroll)

    def validate(self) -> bool:
        selected = [row for checkbox, row in self.checkboxes.values() if checkbox.isChecked()]
        if not selected:
            return True
        for row in selected:
            self.db.add_plan_item({
                'media_type': self.media_type,
                'subject_id': row['subject_id'],
                'title': row['title'],
                'url': row['url'],
                'cover_url': row['cover_url'],
                'cover_path': row['cover_path'],
                'source': 'wish',
                'source_id': row['id'],
            }, folder_id=self.folder_id)
        return True


class AddDoulistDialog(MessageBoxBase):
    def __init__(self, db: Database, media_type: str, folder_id: int, parent=None):
        super().__init__(parent)
        self.db = db
        self.media_type = media_type
        self.folder_id = folder_id
        self.current_doulist = None
        self.checkboxes = {}
        self.word_counts = db.word_count_map('book')
        self.movie_intros = db.intro_map('movie')
        self.widget.setMinimumWidth(760)
        self.yesButton.setText('添加选中')
        self.cancelButton.setText('取消')
        self.viewLayout.addWidget(SubtitleLabel('从豆列添加到计划', self))
        hint = CaptionLabel('选择豆列后勾选条目，可一键全选；只显示与当前计划类型一致的条目。', self)
        hint.setTextColor('#5f6b7a', '#8d95a5')
        self.viewLayout.addWidget(hint)

        self.doulist_combo = ComboBox(self)
        self._lists = [row for row in db.list_doulists()
                       if row['media_type'] in (media_type, 'mixed')]
        for row in self._lists:
            self.doulist_combo.addItem(row['title'], userData=int(row['id']))
        if not self._lists:
            self.doulist_combo.addItem('暂无可用豆列')
            self.doulist_combo.setEnabled(False)
        self.doulist_combo.currentIndexChanged.connect(self._rebuild_items)
        list_row = QHBoxLayout()
        list_row.setSpacing(10)
        list_row.addWidget(self.doulist_combo, 1)
        self.select_all = CheckBox('全选', self)
        self.select_all.checkStateChanged.connect(self._set_all_checked)
        self.select_all.setEnabled(False)
        list_row.addWidget(self.select_all)
        self.viewLayout.addLayout(list_row)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(380)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet('QScrollArea{background:transparent;border:none;}')
        self.container = QWidget()
        self.container.setStyleSheet('background:transparent;')
        self.items_layout = QVBoxLayout(self.container)
        self.items_layout.setContentsMargins(2, 8, 2, 0)
        self.items_layout.setSpacing(10)
        scroll.setWidget(self.container)
        self.viewLayout.addWidget(scroll)
        self._rebuild_items()

    def _clear(self) -> None:
        while self.items_layout.count():
            item = self.items_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.hide()
                widget.deleteLater()
        self.checkboxes.clear()

    def _rebuild_items(self) -> None:
        self._clear()
        index = self.doulist_combo.currentIndex()
        if index < 0 or index >= len(self._lists):
            return
        self.current_doulist = self._lists[index]
        existing = self.db.plan_subject_ids(self.media_type, self.folder_id)
        items = [item for item in self.db.doulist_items(int(self.current_doulist['id']))
                 if item['media_type'] == self.media_type
                 and str(item['subject_id']) not in existing]
        self.select_all.blockSignals(True)
        self.select_all.setChecked(False)
        self.select_all.blockSignals(False)
        self.select_all.setEnabled(bool(items))
        if not items:
            self.items_layout.addWidget(BodyLabel('这个豆列没有可添加的条目。', self.container))
        for item in items:
            item_widget = QWidget(self.container)
            item_layout = QVBoxLayout(item_widget)
            item_layout.setContentsMargins(0, 0, 0, 0)
            item_layout.setSpacing(2)
            checkbox = CheckBox(item['title'], item_widget)
            meta_parts = [_read_text(item['media_type'], item['is_read'])]
            if item['media_type'] == 'book':
                meta_parts.append(book_stats_text(
                    item['subject_id'], self.word_counts.get(str(item['subject_id']), 0)
                ))
            elif item['media_type'] == 'movie':
                intro = item['intro'] or self.movie_intros.get(str(item['subject_id']), '')
                meta_parts.append(movie_stats_text(intro, item['subject_id']))
            if item['intro']:
                meta_parts.append(item['intro'][:80])
            if meta_parts:
                meta = CaptionLabel(' · '.join(meta_parts), item_widget)
                meta.setContentsMargins(26, 0, 0, 0)
                meta.setStyleSheet('color:#7fae68;' if item['is_read'] else 'color:#d98a4a;')
                item_layout.addWidget(checkbox)
                item_layout.addWidget(meta)
            else:
                item_layout.addWidget(checkbox)
            self.checkboxes[int(item['id'])] = (checkbox, item)
            self.items_layout.addWidget(item_widget)
        self.items_layout.addStretch(1)

    def _set_all_checked(self, state) -> None:
        checked = state == Qt.CheckState.Checked
        for checkbox, _ in self.checkboxes.values():
            checkbox.setChecked(checked)

    def validate(self) -> bool:
        selected = [item for checkbox, item in self.checkboxes.values() if checkbox.isChecked()]
        if not selected or not self.current_doulist:
            return True
        for item in selected:
            self.db.add_plan_item({
                'media_type': self.media_type,
                'subject_id': item['subject_id'],
                'title': item['title'],
                'url': item['url'],
                'cover_url': item['cover_url'],
                'cover_path': item['cover_path'],
                'source': 'doulist',
                'source_id': self.current_doulist['douban_id'],
            }, folder_id=self.folder_id)
        return True


class PlanPage(QWidget):
    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.setObjectName('planPage')
        self.media_type = 'movie'
        self._folders = []
        self._current_folder_id = 0
        self.rows = []
        self._dirty = False

        root = QVBoxLayout(self)
        root.setContentsMargins(32, 26, 32, 26)
        root.setSpacing(12)
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(3)
        title_box.addWidget(TitleLabel('计划', self))
        subtitle = BodyLabel('每个计划是一个文件夹，里面放具体的书或影视；可分别排序和删除。', self)
        subtitle.setTextColor('#5f6b7a', '#8d95a5')
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch(1)
        self.count_label = StrongBodyLabel('共 0 个计划', self)
        self.count_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        header.addWidget(self.count_label)
        self.type_switch = SegmentedSwitch(
            '观影计划', '读书计划', 'movie', 'book', 210, 'planTypeSwitch', self
        )
        self.type_switch.changed.connect(self._set_type)
        if not db.list_plan_folders('movie') and db.list_plan_folders('book'):
            self.media_type = 'book'
            self.type_switch.book_button.setChecked(True)
        header.addWidget(self.type_switch)
        self.new_button = primary_button(FluentIcon.ADD, '新建计划', self)
        self.new_button.clicked.connect(self.create_folder)
        header.addWidget(self.new_button)
        root.addLayout(header)

        self.stack = QStackedWidget(self)
        self.outer_page = self._build_outer_page()
        self.detail_page = self._build_detail_page()
        self.stack.addWidget(self.outer_page)
        self.stack.addWidget(self.detail_page)
        root.addWidget(self.stack, 1)
        self.refresh()

    def _build_outer_page(self) -> QWidget:
        page = QWidget(self)
        page.setStyleSheet('background:transparent;')
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.outer_scroll = SmoothScrollArea(page)
        self.outer_scroll.setWidgetResizable(True)
        self.outer_scroll.enableTransparentBackground()
        self.outer_scroll.setFrameShape(QFrame.NoFrame)
        self.outer_container = QWidget()
        self.outer_container.setStyleSheet('background:transparent;')
        self.outer_layout = QVBoxLayout(self.outer_container)
        self.outer_layout.setContentsMargins(0, 6, 0, 20)
        self.outer_layout.setSpacing(14)
        self.outer_layout.addStretch(1)
        self.outer_scroll.setWidget(self.outer_container)
        layout.addWidget(self.outer_scroll)
        return page

    def _build_detail_page(self) -> QWidget:
        page = QWidget(self)
        page.setStyleSheet('background:transparent;')
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.back_button = TransparentToolButton(FluentIcon.LEFT_ARROW, page)
        self.back_button.setToolTip('返回计划列表')
        self.back_button.clicked.connect(self.show_outer)
        bar.addWidget(self.back_button)
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        self.detail_title = TitleLabel('计划', page)
        self.detail_meta = CaptionLabel('', page)
        self.detail_meta.setTextColor('#4b5563', '#b9b5aa')
        title_box.addWidget(self.detail_title)
        title_box.addWidget(self.detail_meta)
        self.detail_progress = QProgressBar(page)
        self.detail_progress.setFixedWidth(280)
        self.detail_progress.setFixedHeight(7)
        self.detail_progress.setTextVisible(False)
        self.detail_progress.setStyleSheet(progress_style())
        title_box.addWidget(self.detail_progress)
        bar.addLayout(title_box)
        bar.addStretch(1)
        self.rename_button = PushButton('重命名', page)
        self.rename_button.clicked.connect(self.rename_folder)
        bar.addWidget(self.rename_button)
        self.delete_folder_button = PushButton('删除当前计划', page)
        self.delete_folder_button.clicked.connect(lambda: self.delete_folder())
        bar.addWidget(self.delete_folder_button)
        self.wish_button = PushButton(FluentIcon.ADD, '从想读/想看添加', page)
        self.wish_button.clicked.connect(self.add_from_wish)
        bar.addWidget(self.wish_button)
        self.doulist_button = primary_button(FluentIcon.ALBUM, '从豆列添加', page)
        self.doulist_button.clicked.connect(self.add_from_doulist)
        bar.addWidget(self.doulist_button)
        self.export_button = PushButton(FluentIcon.SAVE, '导出 PNG', page)
        self.export_button.setToolTip('把当前计划整体导出为 PNG 图片')
        self.export_button.clicked.connect(self.export_png)
        bar.addWidget(self.export_button)
        layout.addLayout(bar)

        self.detail_scroll = SmoothScrollArea(page)
        self.detail_scroll.setWidgetResizable(True)
        self.detail_scroll.enableTransparentBackground()
        self.detail_scroll.setFrameShape(QFrame.NoFrame)
        self.detail_container = QWidget()
        self.detail_container.setStyleSheet('background:transparent;')
        self.detail_layout = QVBoxLayout(self.detail_container)
        self.detail_layout.setContentsMargins(0, 6, 0, 20)
        self.detail_layout.setSpacing(8)
        self.detail_layout.addStretch(1)
        self.detail_scroll.setWidget(self.detail_container)
        layout.addWidget(self.detail_scroll, 1)
        return page

    def _clear_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.hide()
                widget.deleteLater()

    def _current_folder(self):
        return next((folder for folder in self._folders
                     if int(folder['id']) == int(self._current_folder_id)), None)

    def _reload_folders(self) -> None:
        self._folders = self.db.list_plan_folders(self.media_type)
        total_items = sum(int(folder['item_count']) for folder in self._folders)
        total_read = sum(min(int(folder['item_count']), int(folder['read_count'] or 0))
                         for folder in self._folders)
        self.count_label.setText(
            f'共 {len(self._folders)} 个计划 · '
            + _count_text(self.media_type, total_read, total_items)
        )
        self._build_outer_grid()

    def _build_outer_grid(self) -> None:
        self._clear_layout(self.outer_layout)
        if self._folders:
            total_items = sum(int(folder['item_count']) for folder in self._folders)
            total_read = sum(min(int(folder['item_count']), int(folder['read_count'] or 0))
                             for folder in self._folders)
            info = CaptionLabel(
                f'{"观影计划" if self.media_type == "movie" else "读书计划"} · '
                f'共 {len(self._folders)} 个计划 · '
                + _count_text(self.media_type, total_read, total_items),
                self.outer_container,
            )
            info.setTextColor('#5f6b7a', '#8d95a5')
            self.outer_layout.addWidget(info)
            grid = QWidget(self.outer_container)
            grid.setStyleSheet('background:transparent;')
            flow = FlowLayout(grid, needAni=False, isTight=False)
            flow.setContentsMargins(0, 0, 0, 0)
            flow.setHorizontalSpacing(14)
            flow.setVerticalSpacing(14)
            for folder in self._folders:
                card = PlanFolderCard(folder, grid)
                card.open_requested.connect(self.show_detail)
                card.move_requested.connect(self.move_folder)
                card.delete_requested.connect(self.delete_folder)
                flow.addWidget(card)
            self.outer_layout.addWidget(grid)
        else:
            empty = BodyLabel('还没有计划，点击右上角「新建计划」创建一个文件夹。',
                              self.outer_container)
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet('color:#8d95a5; padding:70px 0;')
            self.outer_layout.addWidget(empty)
        self.outer_layout.addStretch(1)
        self.outer_scroll.verticalScrollBar().setValue(0)

    def _build_detail(self) -> None:
        self.rows = []
        folder = self._current_folder()
        if not folder:
            self.show_outer()
            return
        note = folder['note'] or ''
        total = int(folder['item_count'] or 0)
        read = min(total, int(folder['read_count'] or 0))
        meta = (_count_text(self.media_type, read, total)
                + (f' · {note}' if note else ''))
        self.detail_title.setText(folder['title'])
        self.detail_meta.setText(meta)
        self.detail_progress.setRange(0, max(1, total))
        self.detail_progress.setValue(read)
        self.detail_progress.setVisible(total > 0)
        self._clear_layout(self.detail_layout)
        items = self.db.list_plan_items(self.media_type, int(folder['id']))
        if not items:
            empty = BodyLabel('这个计划还是空的，从想读/想看或豆列里添加条目。',
                              self.detail_container)
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet('color:#8d95a5; padding:60px 0;')
            self.detail_layout.addWidget(empty)
        else:
            word_counts = self.db.word_count_map('book')
            movie_intros = self.db.intro_map('movie')
            for position, item in enumerate(items, 1):
                stats = ''
                if item['media_type'] == 'book':
                    stats = book_stats_text(
                        item['subject_id'], word_counts.get(str(item['subject_id']), 0)
                    )
                elif item['media_type'] == 'movie':
                    stats = movie_stats_text(
                        movie_intros.get(str(item['subject_id']), ''), item['subject_id']
                    )
                else:
                    stats = ''
                row = PlanRow(position, item, stats, self.detail_container)
                row.move_requested.connect(self.move_item)
                row.delete_requested.connect(self.delete_item)
                row.open_requested.connect(self.open_item)
                self.detail_layout.addWidget(row)
                self.rows.append(row)
        self.detail_layout.addStretch(1)
        self.detail_scroll.verticalScrollBar().setValue(0)

    def refresh(self) -> None:
        self._dirty = False
        self._reload_folders()
        if self._current_folder_id and self._current_folder():
            self._build_detail()
            self.stack.setCurrentWidget(self.detail_page)
        else:
            self._current_folder_id = 0
            self.show_outer()

    def mark_dirty(self) -> None:
        self._dirty = True

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._dirty:
            self.refresh()

    def _set_type(self, media_type: str) -> None:
        self.media_type = media_type
        self._current_folder_id = 0
        self.show_outer()
        self.refresh()

    def show_detail(self, folder_id: int) -> None:
        self._current_folder_id = int(folder_id)
        self._build_detail()
        self.stack.setCurrentWidget(self.detail_page)

    def show_outer(self) -> None:
        self.stack.setCurrentWidget(self.outer_page)

    def create_folder(self) -> None:
        dialog = PlanFolderDialog(self.db, self.media_type, parent=self.window())
        if dialog.exec() and dialog.folder_id:
            self._current_folder_id = dialog.folder_id
            self.refresh()

    def rename_folder(self) -> None:
        folder = self._current_folder()
        if not folder:
            return
        dialog = PlanFolderDialog(self.db, self.media_type, folder, self.window())
        if dialog.exec():
            self.refresh()

    def delete_folder(self, folder_id=None) -> None:
        if folder_id is None:
            folder = self._current_folder()
        else:
            folder = next((item for item in self._folders if int(item['id']) == int(folder_id)), None)
        if not folder:
            return
        target_id = int(folder['id'])
        box = MessageBox(
            '删除计划',
            f'将「{folder["title"]}」及其中的 {folder["item_count"]} 条条目移到回收站？\n'
            '可在回收站中恢复或彻底删除。',
            self.window(),
        )
        box.yesButton.setText('删除')
        box.cancelButton.setText('取消')
        if not box.exec():
            return
        self.db.delete_plan_folder(target_id)
        if int(self._current_folder_id) == target_id:
            self._current_folder_id = 0
        self.refresh()

    def add_from_wish(self) -> None:
        folder = self._current_folder()
        if not folder:
            return
        dialog = AddWishDialog(self.db, self.media_type, int(folder['id']), self.window())
        if dialog.exec():
            self.refresh()

    def add_from_doulist(self) -> None:
        folder = self._current_folder()
        if not folder:
            return
        dialog = AddDoulistDialog(self.db, self.media_type, int(folder['id']), self.window())
        if dialog.exec():
            self.refresh()

    def move_folder(self, folder_id: int, offset: int) -> None:
        if not self.db.move_plan_folder(folder_id, offset):
            return
        scroll_value = self.outer_scroll.verticalScrollBar().value()
        self.refresh()
        self.outer_scroll.verticalScrollBar().setValue(scroll_value)

    def move_item(self, item_id: int, offset: int) -> None:
        if not self.db.move_plan_item(item_id, offset):
            return
        scroll_value = self.detail_scroll.verticalScrollBar().value()
        self._build_detail()
        self.detail_scroll.verticalScrollBar().setValue(scroll_value)

    def export_png(self) -> None:
        folder = self._current_folder()
        if not folder:
            return
        PLANS_DIR.mkdir(parents=True, exist_ok=True)
        safe_title = re.sub(r'[\\/:*?"<>|]+', '_', folder['title'] or '').strip() or '计划'
        default_path = PLANS_DIR / f"{safe_title}_{datetime.now().strftime('%Y%m%d_%H%M')}.png"
        path, _ = QFileDialog.getSaveFileName(
            self, '导出计划 PNG', str(default_path), 'PNG 图片 (*.png)'
        )
        if not path:
            return
        if not path.lower().endswith('.png'):
            path += '.png'
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            result = export_plan_png(
                self.db, folder, self.media_type, Path(path),
                effective_name(self.db),
            )
        except Exception as exc:
            InfoBar.error('导出失败', str(exc), parent=self,
                          position=InfoBarPosition.TOP, duration=6000)
            return
        finally:
            QApplication.restoreOverrideCursor()
        InfoBar.success(
            '导出完成',
            f"已导出「{folder['title']}」共 {result['items']} 条到 {Path(result['path']).name}。",
            parent=self, position=InfoBarPosition.TOP, duration=6000,
        )

    def delete_item(self, item_id: int) -> None:
        box = MessageBox(
            '删除计划条目',
            '将这条记录移到回收站？\n可在回收站中恢复或彻底删除。',
            self.window(),
        )
        box.yesButton.setText('删除')
        box.cancelButton.setText('取消')
        if box.exec():
            self.db.remove_plan_item(item_id)
            self.refresh()

    def open_item(self, url: str) -> None:
        if url:
            QDesktopServices.openUrl(QUrl(url))
