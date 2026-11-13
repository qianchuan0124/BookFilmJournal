from datetime import datetime

from PySide6.QtCore import QPoint, QTimer, Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QWidget

from qfluentwidgets import (
    BodyLabel, CaptionLabel, FlowLayout, FluentIcon, InfoBar, InfoBarPosition, LineEdit,
    CalendarPicker, MessageBox, PushButton, SmoothScrollArea, StrongBodyLabel,
    TitleLabel, isDarkTheme,
)

from app.config import MEDIA_LABELS, MEDIA_NOUNS
from app.db import Database
from app.ui.dialogs import (
    AddRecordDialog, CoverChangeDialog, EditRecordDialog, RecordDetailDialog, SyncDialog,
    WordCountDialog,
)
from app.ui.record_card import RecordCard
from app.ui.segmented import SegmentedSwitch
from app.ui.styles import primary_button, search_edit_style


def _date_heading(date_str: str) -> str:
    try:
        day = datetime.strptime(date_str, '%Y-%m-%d')
        return f'{day.year} 年 {day.month} 月 {day.day} 日 · ' + '周' + '一二三四五六日'[day.weekday()]
    except (TypeError, ValueError):
        return '未设置日期'


class RecordsPage(QWidget):
    synced = Signal(dict)
    record_changed = Signal(str)

    def __init__(self, db: Database, media_type: str, parent=None):
        super().__init__(parent)
        self.db = db
        self.media_type = media_type
        self.setObjectName(f'records{media_type.title()}')
        self.search_text = ''
        self._saved_scroll = 0
        self._saved_height = 0
        self._had_search = False
        self.status = 'done'
        self.cards: list[RecordCard] = []
        self._groups = []
        self.empty_label = None
        self.views: dict[str, dict] = {}
        self.current_view = None
        self._dirty = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 26, 32, 0)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(3)
        self.title_label = TitleLabel(f'{MEDIA_LABELS[media_type]}记录', self)
        self.subtitle_label = BodyLabel('按本地生效日期分组展示', self)
        self.subtitle_label.setTextColor('#5f6b7a', '#8d95a5')
        title_box.addWidget(self.title_label)
        title_box.addWidget(self.subtitle_label)
        header.addLayout(title_box)
        header.addStretch(1)
        done_text = '看过' if media_type == 'movie' else '读过'
        wish_text = '想看' if media_type == 'movie' else '想读'
        self.count_label = StrongBodyLabel('共 0 条', self)
        self.count_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        header.addWidget(self.count_label)
        self.segment = SegmentedSwitch(done_text, wish_text, 'done', 'wish', 180, 'statusSwitch', self)
        self.segment.changed.connect(self._set_status)
        header.addWidget(self.segment)
        self.add_button = PushButton(FluentIcon.ADD, '新增', self)
        self.add_button.clicked.connect(self.add_record)
        header.addWidget(self.add_button)
        if media_type == 'book':
            self.words_button = PushButton(FluentIcon.DOCUMENT, '统计字数', self)
            self.words_button.clicked.connect(self.open_word_count)
            header.addWidget(self.words_button)
        self.sync_button = primary_button(FluentIcon.SYNC, '同步豆瓣', self)
        self.sync_button.clicked.connect(self.open_sync_dialog)
        header.addWidget(self.sync_button)
        layout.addLayout(header)

        search_row = QHBoxLayout()
        self.search_edit = LineEdit(self)
        self.search_edit.setPlaceholderText('搜索标题 / 短评')
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setFixedWidth(300)
        self.search_edit.setFixedHeight(36)
        search_edit_style(self.search_edit)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self._apply_search)
        self.search_edit.textChanged.connect(self._on_search)
        search_row.addWidget(self.search_edit)
        self.date_jump = CalendarPicker(self)
        self.date_jump.setFixedWidth(150)
        self.date_jump.setFixedHeight(36)
        self.date_jump.setText('跳转到日期')
        self.date_jump.setToolTip('选择日期后滚动到对应的时间分组')
        self.date_jump.dateChanged.connect(self._jump_to_date)
        search_row.addWidget(self.date_jump)
        search_row.addStretch(1)
        hint = CaptionLabel('点击卡片查看详情与感想；详情内可编辑日期、评分和短评，同步不会覆盖本地修改', self)
        hint.setTextColor('#4b5563', '#5d6474')
        search_row.addWidget(hint)
        layout.addLayout(search_row)

        self.scroll = SmoothScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.enableTransparentBackground()
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.container = QWidget()
        self.container.setStyleSheet('background:transparent;')
        self.content_layout = QVBoxLayout(self.container)
        self.content_layout.setContentsMargins(0, 8, 0, 28)
        self.content_layout.setSpacing(18)
        self.content_layout.addStretch(1)
        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll, 1)

        self.refresh()

    def _on_search(self, text: str) -> None:
        self.search_text = (text or '').strip().lower()
        if not self.search_text:
            self._search_timer.stop()
            self._apply_search()
        else:
            self._search_timer.start()

    def _set_status(self, status: str) -> None:
        if status == self.status and self.current_view is not None:
            return
        self.status = status
        self._show_status(status, reset_scroll=True)

    def _clear(self) -> None:
        self.cards = []
        self._groups = []
        self.empty_label = None
        self.current_view = None
        self.views.clear()
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.hide()
                widget.deleteLater()

    def _make_card(self, row, parent) -> RecordCard:
        card = RecordCard(row, parent, self.search_text)
        card.edit_requested.connect(self.edit_record)
        card.detail_requested.connect(self.open_record_detail)
        card.link_requested.connect(self.open_record_link)
        card.delete_requested.connect(self.delete_record)
        card.cover_requested.connect(self.change_cover)
        return card

    def _build_view(self, status: str) -> dict:
        widget = QWidget(self.container)
        widget.setStyleSheet('background:transparent;')
        box = QVBoxLayout(widget)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(18)
        cards: list[RecordCard] = []
        groups = []
        lines = []
        for date_str, rows in self.db.grouped_records(self.media_type, status):
            header = QWidget(widget)
            header_layout = QHBoxLayout(header)
            header_layout.setContentsMargins(0, 0, 0, 0)
            header_layout.setSpacing(10)
            date_label = StrongBodyLabel(_date_heading(date_str), header)
            header_layout.addWidget(date_label)
            count = CaptionLabel(f'0 {MEDIA_NOUNS[self.media_type]}', header)
            count.setTextColor('#5f6b7a', '#8d95a5')
            header_layout.addWidget(count)
            header_layout.addStretch(1)
            line = QFrame(header)
            line.setFixedHeight(1)
            line.setStyleSheet(f'background: {self._divider_color()};')
            lines.append(line)
            header_layout.addWidget(line, 1)
            box.addWidget(header)

            group_widget = QWidget(widget)
            group_widget.setStyleSheet('background:transparent;')
            flow = FlowLayout(group_widget, needAni=False, isTight=True)
            flow.setContentsMargins(0, 0, 0, 0)
            flow.setHorizontalSpacing(14)
            flow.setVerticalSpacing(14)
            group_cards = []
            for row in rows:
                card = self._make_card(row, group_widget)
                flow.addWidget(card)
                cards.append(card)
                group_cards.append((card, row))
            box.addWidget(group_widget)
            groups.append((header, count, group_widget, group_cards, date_str))

        empty_label = BodyLabel('', widget)
        empty_label.setAlignment(Qt.AlignCenter)
        empty_label.setStyleSheet('color:#8d95a5; padding:80px 0;')
        box.addWidget(empty_label)
        box.addStretch(1)
        self.content_layout.addWidget(widget)
        return {
            'widget': widget, 'cards': cards, 'groups': groups, 'empty': empty_label,
            'lines': lines,
        }

    @staticmethod
    def _divider_color() -> str:
        return 'rgba(255,255,255,0.08)' if isDarkTheme() else 'rgba(0,0,0,0.10)'

    def apply_theme(self) -> None:
        color = self._divider_color()
        for view in self.views.values():
            for line in view['lines']:
                line.setStyleSheet(f'background: {color};')

    def _show_status(self, status: str, reset_scroll: bool = False) -> None:
        view = self.views.get(status)
        if view is None:
            view = self._build_view(status)
            self.views[status] = view
        for key, other in self.views.items():
            other['widget'].setVisible(key == status)
        self.current_view = view
        self.cards = view['cards']
        self._groups = view['groups']
        self.empty_label = view['empty']
        self._apply_search(reset_scroll=reset_scroll)
        self._schedule_warmup()

    def _schedule_warmup(self) -> None:
        if len(self.views) >= 2 or not self.isVisible():
            return
        QTimer.singleShot(600, self._warm_up)

    def _warm_up(self) -> None:
        if len(self.views) >= 2 or not self.isVisible():
            return
        other = 'wish' if self.status == 'done' else 'done'
        if other in self.views:
            return
        view = self._build_view(other)
        view['widget'].setVisible(False)
        self.views[other] = view

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._dirty:
            self._dirty = False
            self.refresh()
        self._reflow()
        self._schedule_warmup()

    def mark_dirty(self) -> None:
        if self.isVisible():
            self.refresh()
        else:
            self._dirty = True

    def refresh(self) -> None:
        bar = self.scroll.verticalScrollBar()
        scroll = bar.value()
        self._clear()
        self._show_status(self.status, reset_scroll=False)
        if scroll:
            self._set_scroll(min(scroll, bar.maximum()))
            QTimer.singleShot(
                0, lambda: self._set_scroll(min(scroll, bar.maximum()))
            )

    def update_record(self, record_id: int, force_cover: bool = False) -> None:
        row = self.db.get_record(record_id)
        view = self.views.get(self.status)
        if (row is None or view is None or row['status'] != self.status
                or row['media_type'] != self.media_type or row['deleted_at']):
            self.refresh()
            return
        for _, _, group_widget, cards, _ in view['groups']:
            for index, (card, old_row) in enumerate(cards):
                if int(old_row['id']) != int(record_id):
                    continue
                if (row['effective_date'] or '未设置') != (old_row['effective_date'] or '未设置'):
                    self.refresh()
                    return
                card.update_record(row, self.search_text, force_cover=force_cover)
                cards[index] = (card, row)
                self._apply_search()
                return
        self.refresh()

    @staticmethod
    def _set_visible(widget, visible: bool) -> None:
        if widget.isHidden() == visible:
            widget.setVisible(visible)

    def _apply_search(self, reset_scroll: bool = False) -> None:
        keyword = self.search_text
        was_search = self._had_search
        clearing = not keyword and was_search
        if keyword and not was_search:
            self._saved_scroll = self.scroll.verticalScrollBar().value()
            self._saved_height = self.container.height()
        total = 0
        for header, count_label, group_widget, cards, _ in self._groups:
            visible = 0
            for card, row in cards:
                matched = not keyword or keyword in f"{row['title']} {row['comment']}".lower()
                self._set_visible(card, matched)
                if keyword and matched:
                    card.set_highlight(keyword)
                elif card.highlight:
                    card.set_highlight('')
                if matched:
                    visible += 1
            count_label.setText(f'{visible} {MEDIA_NOUNS[self.media_type]}')
            self._set_visible(header, visible > 0)
            self._set_visible(group_widget, visible > 0)
            total += visible
        if not clearing:
            self._reflow()
        self._had_search = bool(keyword)
        if self.empty_label is not None:
            if total == 0:
                self.empty_label.setText(
                    '没有匹配的记录。' if keyword
                    else '还没有记录，点击右上角「同步豆瓣」开始。'
                )
            self._set_visible(self.empty_label, total == 0)
        self.count_label.setText(f'共 {total} 条')
        if reset_scroll:
            self._set_scroll(0)
        elif clearing:
            target = self._saved_scroll
            self.scroll.setUpdatesEnabled(False)
            try:
                if self._saved_height:
                    self.container.setMinimumHeight(self._saved_height)
                self._reflow_now()
                view = self.current_view
                if view is not None:
                    view['widget'].layout().activate()
                self.content_layout.activate()
                self.container.adjustSize()
                self._set_scroll(min(target, self.scroll.verticalScrollBar().maximum()))
            finally:
                self.container.setMinimumHeight(0)
                self._saved_height = 0
                self.scroll.setUpdatesEnabled(True)
                self.scroll.viewport().update()

    def _jump_to_date(self, value) -> None:
        target = value.toString('yyyy-MM-dd')
        entries = [group for group in self._groups if not group[0].isHidden()]
        if not entries:
            return
        chosen = entries[-1]
        for group in entries:
            date_str = group[4]
            if date_str and date_str <= target:
                chosen = group
                break
        header = chosen[0]
        self._reflow_now()
        y = header.mapTo(self.container, QPoint(0, 0)).y()
        self._set_scroll(max(0, y - 6))

    def _set_scroll(self, value: int) -> None:
        bar = self.scroll.verticalScrollBar()
        delegate = getattr(self.scroll, 'delegate', None)
        smooth = getattr(delegate, 'vScrollBar', None)
        if smooth is not None:
            smooth.ani.stop()
        bar.setValue(max(bar.minimum(), min(int(value), bar.maximum())))
        if smooth is not None:
            smooth.resetValue(bar.value())

    def _reflow(self) -> None:
        QTimer.singleShot(0, self._reflow_now)

    def _reflow_now(self) -> None:
        view = self.current_view
        if view is None:
            return
        try:
            for _, _, group_widget, _, _ in view['groups']:
                group_widget.layout().invalidate()
                group_widget.updateGeometry()
            view['widget'].layout().invalidate()
            view['widget'].updateGeometry()
            self.content_layout.invalidate()
        except RuntimeError:
            pass

    def add_record(self) -> None:
        dialog = AddRecordDialog(self.db, self.media_type, self.status, self.window())
        if dialog.exec():
            self.refresh()
            self.record_changed.emit(self.media_type)

    def open_word_count(self) -> None:
        dialog = WordCountDialog(self.db, self.window())
        dialog.words_updated.connect(self._on_words_updated)
        dialog.exec()

    def _on_words_updated(self, stats: dict) -> None:
        self.refresh()
        self.record_changed.emit(self.media_type)

    def open_sync_dialog(self) -> None:
        dialog = SyncDialog(self.db, self.window())
        dialog.synced.connect(self._on_synced)
        dialog.exec()

    def _on_synced(self, result: dict) -> None:
        self.refresh()
        self.synced.emit(result)

    def open_record_detail(self, record_id: int) -> None:
        row = self.db.get_record(record_id)
        if row is None:
            return
        if row['status'] != 'done':
            self.edit_record(record_id)
            return
        dialog = RecordDetailDialog(self.db, row, self.window())
        dialog.changed.connect(self._on_detail_changed)
        dialog.exec()

    def _on_detail_changed(self, record_id: int) -> None:
        self.update_record(record_id)
        self.record_changed.emit(self.media_type)

    def change_cover(self, record_id: int) -> None:
        row = self.db.get_record(record_id)
        if not row:
            return
        dialog = CoverChangeDialog(self.db, row, self.window())
        if dialog.exec():
            self.update_record(record_id, force_cover=True)
            self.record_changed.emit(self.media_type)

    def edit_record(self, record_id: int) -> None:
        row = self.db.get_record(record_id)
        if not row:
            return
        dialog = EditRecordDialog(self.db, row, self.window())
        if dialog.exec():
            self.update_record(record_id)
            self.record_changed.emit(self.media_type)

    def delete_record(self, record_id: int) -> None:
        row = self.db.get_record(record_id)
        if not row:
            return
        box = MessageBox(
            '删除记录',
            f'将「{row["title"]}」移到回收站？\n可在回收站中恢复或彻底删除。',
            self.window(),
        )
        box.yesButton.setText('删除')
        box.cancelButton.setText('取消')
        if not box.exec():
            return
        self.db.soft_delete_record(record_id)
        self.refresh()
        self.record_changed.emit(self.media_type)
        InfoBar.success('已移到回收站', f'「{row["title"]}」已移到回收站。',
                        parent=self, position=InfoBarPosition.TOP)

    def open_record_link(self, url: str) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        if url:
            QDesktopServices.openUrl(QUrl(url))
