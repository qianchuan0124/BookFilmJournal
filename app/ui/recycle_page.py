from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from qfluentwidgets import (
    BodyLabel, CaptionLabel, ComboBox, InfoBar, InfoBarPosition, MessageBox,
    PushButton, SmoothScrollArea, StrongBodyLabel, TitleLabel,
    qconfig,
)

from app.config import MEDIA_LABELS, MEDIA_NOUNS, READ_LABELS
from app.db import Database
from app.ui.record_card import _cover_pixmap
from app.ui.styles import surface_style, primary_button

RELATION_LABELS = {'created': '我创建的', 'followed': '我关注的'}


def _deleted_text(value: str) -> str:
    text = (value or '').replace('T', ' ')
    return text[:16]


class RecycleRow(QFrame):
    restore_requested = Signal(str, int)
    purge_requested = Signal(str, int)

    def __init__(self, kind: str, row, cover_path: str, subtitle: str,
                 meta: str, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.row_id = int(row['id'])
        self.setObjectName('recycleRow')
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(surface_style('recycleRow', 10))
        qconfig.themeChanged.connect(self._apply_theme)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 14, 10)
        layout.setSpacing(14)

        cover = QLabel(self)
        cover.setFixedSize(48, 68)
        cover.setAlignment(Qt.AlignCenter)
        cover.setPixmap(_cover_pixmap(cover_path or '', 48, 68, fill=True))
        cover.setStyleSheet('background:#0f131b; border-radius:5px;')
        layout.addWidget(cover)

        info = QVBoxLayout()
        info.setSpacing(4)
        title = StrongBodyLabel('', self)
        metrics = QFontMetrics(title.font())
        title.setText(metrics.elidedText(subtitle, Qt.ElideRight, 680))
        title.setToolTip(subtitle)
        info.addWidget(title)
        meta_label = CaptionLabel(meta, self)
        meta_label.setTextColor('#5f6b7a', '#8d95a5')
        info.addWidget(meta_label)
        deleted_label = CaptionLabel(
            f"删除于 {_deleted_text(row['deleted_at'])}", self
        )
        deleted_label.setTextColor('#4b5563', '#5d6474')
        info.addWidget(deleted_label)
        info.addStretch(1)
        layout.addLayout(info, 1)

        restore_button = primary_button('恢复', self)
        restore_button.setFixedWidth(76)
        restore_button.clicked.connect(
            lambda: self.restore_requested.emit(self.kind, self.row_id)
        )
        layout.addWidget(restore_button)
        purge_button = PushButton('彻底删除', self)
        purge_button.setFixedWidth(88)
        purge_button.setToolTip('永久删除，不可恢复')
        purge_button.clicked.connect(
            lambda: self.purge_requested.emit(self.kind, self.row_id)
        )
        layout.addWidget(purge_button)

    def _apply_theme(self, *_) -> None:
        self.setStyleSheet(surface_style('recycleRow', 10))


class RecyclePage(QWidget):
    changed = Signal()

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.setObjectName('recyclePage')

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 26, 32, 0)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(3)
        title_box.addWidget(TitleLabel('回收站', self))
        subtitle = BodyLabel('删除的记录、豆列和计划会先移到这里，可随时恢复或彻底删除。', self)
        subtitle.setTextColor('#5f6b7a', '#8d95a5')
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch(1)
        self.type_combo = ComboBox(self)
        self.type_combo.addItems(['全部', '观影记录', '读书记录', '豆列', '计划文件夹', '计划条目'])
        self.type_combo.setFixedWidth(150)
        self.type_combo.currentIndexChanged.connect(self.refresh)
        header.addWidget(self.type_combo)
        self.count_label = StrongBodyLabel('共 0 项', self)
        header.addWidget(self.count_label)
        self.clear_all_button = PushButton('全部清理', self)
        self.clear_all_button.setToolTip('彻底删除回收站中的所有条目，不可恢复')
        self.clear_all_button.clicked.connect(self.purge_all)
        header.addWidget(self.clear_all_button)
        layout.addLayout(header)

        self.scroll = SmoothScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.enableTransparentBackground()
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.container = QWidget()
        self.container.setStyleSheet('background:transparent;')
        self.content_layout = QVBoxLayout(self.container)
        self.content_layout.setContentsMargins(0, 8, 0, 28)
        self.content_layout.setSpacing(8)
        self.content_layout.addStretch(1)
        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll, 1)

    def _clear(self) -> None:
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.hide()
                widget.deleteLater()

    def _current_index(self) -> int:
        return self.type_combo.currentIndex()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh()

    def _add_record_rows(self, media_type: str, rows: list) -> None:
        done_text, wish_text = READ_LABELS.get(media_type, ('已读', '未读'))
        for row in self.db.list_deleted_records(media_type):
            status = wish_text if row['status'] == 'wish' else done_text
            meta_parts = [status, MEDIA_LABELS.get(media_type, media_type)]
            if row['deleted_by'] == 'sync':
                meta_parts.append('豆瓣同步移除')
            if row['rating']:
                meta_parts.append('★' * int(row['rating']))
            date = row['effective_date'] or row['marked_date']
            if date:
                meta_parts.append(date.replace('-', '.'))
            comment = (row['comment'] or '').strip()
            subtitle = row['title']
            if comment:
                subtitle += f'　{comment[:40]}'
            rows.append(('record', row, row['cover_path'] or '',
                         subtitle, ' · '.join(meta_parts)))

    def _add_doulist_rows(self, rows: list) -> None:
        for row in self.db.list_deleted_doulists():
            media = MEDIA_LABELS.get(row['media_type'], '混合')
            noun = MEDIA_NOUNS.get(row['media_type'], '条')
            meta = (f"{RELATION_LABELS.get(row['relation'], '豆列')} · {media} · "
                    f"{int(row['local_count'] or 0)} {noun}")
            rows.append(('doulist', row, row['cover_path'] or '', row['title'], meta))

    def _add_plan_folder_rows(self, rows: list) -> None:
        for row in self.db.list_deleted_plan_folders():
            media = MEDIA_LABELS.get(row['media_type'], '')
            noun = MEDIA_NOUNS.get(row['media_type'], '条')
            meta_parts = [f'{media}计划', f"{int(row['item_count'] or 0)} {noun}"]
            if row['note']:
                meta_parts.append(row['note'])
            rows.append(('plan_folder', row, row['cover_path'] or '',
                         row['title'], ' · '.join(meta_parts)))

    def _add_plan_item_rows(self, rows: list) -> None:
        for row in self.db.list_deleted_plan_items():
            media = MEDIA_LABELS.get(row['media_type'], '')
            meta_parts = [f'{media}计划']
            if row['folder_title']:
                meta_parts.append(f"所属：{row['folder_title']}")
            if row['note']:
                meta_parts.append(row['note'])
            rows.append(('plan_item', row, row['cover_path'] or '',
                         row['title'], ' · '.join(meta_parts)))

    def refresh(self) -> None:
        self._clear()
        index = self._current_index()
        rows: list = []
        if index in (0, 1):
            self._add_record_rows('movie', rows)
        if index in (0, 2):
            self._add_record_rows('book', rows)
        if index in (0, 3):
            self._add_doulist_rows(rows)
        if index in (0, 4):
            self._add_plan_folder_rows(rows)
        if index in (0, 5):
            self._add_plan_item_rows(rows)

        self.count_label.setText(f'共 {len(rows)} 项')
        if not rows:
            empty = BodyLabel('回收站是空的。', self.container)
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet('color:#8d95a5; padding:80px 0;')
            self.content_layout.addWidget(empty)
            self.content_layout.addStretch(1)
            self.scroll.verticalScrollBar().setValue(0)
            return
        for kind, row, cover_path, subtitle, meta in rows:
            widget = RecycleRow(kind, row, cover_path, subtitle, meta, self.container)
            widget.restore_requested.connect(self.restore_item)
            widget.purge_requested.connect(self.purge_item)
            self.content_layout.addWidget(widget)
        self.content_layout.addStretch(1)
        self.scroll.verticalScrollBar().setValue(0)

    def restore_item(self, kind: str, item_id: int) -> None:
        if kind == 'record':
            self.db.restore_record(item_id)
        elif kind == 'doulist':
            self.db.restore_doulist(item_id)
        elif kind == 'plan_folder':
            self.db.restore_plan_folder(item_id)
        else:
            self.db.restore_plan_item(item_id)
        self.refresh()
        self.changed.emit()
        InfoBar.success('已恢复', '条目已恢复到原列表。', parent=self,
                        position=InfoBarPosition.TOP)

    def purge_all(self) -> None:
        total = self._recycle_total()
        if total == 0:
            InfoBar.info('回收站为空', '没有需要清理的条目。', parent=self,
                         position=InfoBarPosition.TOP)
            return
        box = MessageBox(
            '全部清理',
            f'将彻底删除回收站中的全部 {total} 项，此操作无法恢复，确定继续吗？',
            self.window(),
        )
        box.yesButton.setText('全部清理')
        box.cancelButton.setText('取消')
        if not box.exec():
            return
        counts = self.db.purge_all_recycle()
        self.refresh()
        self.changed.emit()
        parts = [f'记录 {counts["records"]} 条', f'豆列 {counts["doulists"]} 个',
                 f'计划文件夹 {counts["plan_folders"]} 个',
                 f'计划条目 {counts["plan_items"]} 条']
        InfoBar.success('已全部清理', '，'.join(parts) + '。', parent=self,
                        position=InfoBarPosition.TOP, duration=6000)

    def _recycle_total(self) -> int:
        found = 0
        for media_type in ('movie', 'book'):
            found += len(self.db.list_deleted_records(media_type))
        found += len(self.db.list_deleted_doulists())
        found += len(self.db.list_deleted_plan_folders())
        found += len(self.db.list_recycle_plan_items())
        return found

    def purge_item(self, kind: str, item_id: int) -> None:
        box = MessageBox(
            '彻底删除',
            '彻底删除后无法恢复，确定继续吗？',
            self.window(),
        )
        box.yesButton.setText('彻底删除')
        box.cancelButton.setText('取消')
        if not box.exec():
            return
        if kind == 'record':
            self.db.hard_delete_record(item_id)
        elif kind == 'doulist':
            self.db.hard_delete_doulist(item_id)
        elif kind == 'plan_folder':
            self.db.hard_delete_plan_folder(item_id)
        else:
            self.db.hard_delete_plan_item(item_id)
        self.refresh()
        self.changed.emit()
        InfoBar.success('已彻底删除', '该条目已从本地数据库移除。', parent=self,
                        position=InfoBarPosition.TOP)
