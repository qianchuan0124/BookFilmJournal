from datetime import datetime

from PySide6.QtCore import QPoint, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QVBoxLayout, QWidget,
)

from qfluentwidgets import (
    BodyLabel, CaptionLabel, FlowLayout, FluentIcon, InfoBar, InfoBarPosition, LineEdit,
    CalendarPicker, MessageBox, PushButton, StrongBodyLabel,
    TitleLabel, TransparentToolButton, isDarkTheme,
)

from app.config import MEDIA_LABELS, MEDIA_NOUNS
from app.db import Database
from app.ui.dialogs import (
    AddRecordDialog, CoverChangeDialog, EditRecordDialog, RecordDetailDialog, SyncDialog,
    WordCountDialog,
)
from app.ui.record_card import RecordCard
from app.ui.scroll_area import SmoothScrollArea
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

    # 首屏同步创建少量卡片，其余在事件循环空闲时分批补齐。
    _INITIAL_CARDS = 12
    _WARMUP_CARDS = 8
    _BATCH_CARDS = 8

    def __init__(self, db: Database, media_type: str, parent=None):
        super().__init__(parent)
        self.db = db
        self.media_type = media_type
        self.setObjectName(f'records{media_type.title()}')
        self.search_text = ''
        self._saved_scroll = 0
        self._saved_height = 0
        self._had_search = False
        self._restore_scroll = 0
        self._pump_scheduled_for = None
        self._reflow_scheduled = False
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

        # 右下角悬浮的「回到顶部」按钮，只在向下滚动后出现。
        self.back_to_top = TransparentToolButton(FluentIcon.UP, self)
        self.back_to_top.setObjectName('backToTop')
        self.back_to_top.setFixedSize(40, 40)
        self.back_to_top.setIconSize(QSize(16, 16))
        self.back_to_top.setCursor(Qt.PointingHandCursor)
        self.back_to_top.setToolTip('回到顶部')
        self.back_to_top.clicked.connect(self.scroll_to_top)
        shadow = QGraphicsDropShadowEffect(self.back_to_top)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 3)
        shadow.setColor(QColor(0, 0, 0, 90))
        self.back_to_top.setGraphicsEffect(shadow)
        self.back_to_top.setStyleSheet(self._back_to_top_style())
        self.back_to_top.hide()
        self.scroll.verticalScrollBar().valueChanged.connect(self._update_back_to_top)

        # 首次显示时再加载记录，避免启动阶段构建全部卡片。
        self._dirty = True

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

    def _build_view(self, status: str, initial_cards: int = None) -> dict:
        widget = QWidget(self.container)
        widget.setStyleSheet('background:transparent;')
        box = QVBoxLayout(widget)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(18)
        cards: list[RecordCard] = []
        groups = []
        lines = []
        pending = []
        total_rows = 0
        # 日期分组头改为按需创建：记录多时一次性创建上千个控件会卡住切换，导致旧页面残留。
        for date_str, rows in self.db.grouped_records(self.media_type, status):
            total_rows += len(rows)
            pending.append({
                'rows': list(rows), 'total': len(rows), 'cards': [], 'group': None,
                'header': None, 'flow': None, 'date': date_str,
            })

        empty_label = BodyLabel('', widget)
        empty_label.setAlignment(Qt.AlignCenter)
        empty_label.setStyleSheet('color:#8d95a5; padding:80px 0;')
        box.addWidget(empty_label)
        box.addStretch(1)
        self.content_layout.addWidget(widget)
        view = {
            'widget': widget, 'cards': cards, 'groups': groups, 'empty': empty_label,
            'lines': lines, 'pending': pending, 'box': box, 'totalRows': total_rows,
        }
        budget = self._INITIAL_CARDS if initial_cards is None else initial_cards
        self._add_cards(view, budget)
        return view

    def _create_group(self, view: dict, entry: dict) -> None:
        """创建某个日期分组的标题和容器，按需调用。"""
        widget = view['widget']
        box = view['box']
        media_noun = MEDIA_NOUNS[self.media_type]
        header = QWidget(widget)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(10)
        date_label = StrongBodyLabel(_date_heading(entry['date']), header)
        header_layout.addWidget(date_label)
        count = CaptionLabel(f'{entry["total"]} {media_noun}', header)
        count.setTextColor('#5f6b7a', '#8d95a5')
        header_layout.addWidget(count)
        header_layout.addStretch(1)
        line = QFrame(header)
        line.setFixedHeight(1)
        line.setStyleSheet(f'background: {self._divider_color()};')
        header_layout.addWidget(line, 1)
        # 先隐藏分组头，等第一张卡片建好再一起显示，避免出现只有日期和条数的空分组。
        header.setVisible(False)

        group_widget = QWidget(widget)
        group_widget.setStyleSheet('background:transparent;')
        group_widget.setProperty('totalRows', entry['total'])
        group_widget.setVisible(False)
        flow = FlowLayout(group_widget, needAni=False, isTight=True)
        flow.setContentsMargins(0, 0, 0, 0)
        flow.setHorizontalSpacing(14)
        flow.setVerticalSpacing(14)

        # 插到空状态提示与末尾弹簧之前，保持日期先后顺序。
        box.insertWidget(box.count() - 2, header)
        box.insertWidget(box.count() - 2, group_widget)
        entry.update(header=header, group=group_widget, flow=flow)
        view['groups'].append((header, count, group_widget, entry['cards'], entry['date']))
        view['lines'].append(line)
        # 新分组的高度依赖 FlowLayout 的 widthForHeight，不重排会一直保持 0 高度，
        # 表现为只剩日期标题、卡片不显示。
        self._reflow()

    def _add_cards(self, view: dict, budget: int) -> int:
        """按预算补充卡片，返回仍未创建的条目数。"""
        pending = view['pending']
        keyword = self.search_text
        built = 0
        while pending and built < budget:
            entry = pending[0]
            if entry['group'] is None:
                self._create_group(view, entry)
            row = entry['rows'].pop(0)
            card = self._make_card(row, entry['group'])
            entry['flow'].addWidget(card)
            entry['cards'].append((card, row))
            view['cards'].append(card)
            if len(entry['cards']) == 1 and not keyword:
                # 该日期分组有了第一张卡片，才把日期头和内容区一起显示。
                self._set_visible(entry['header'], True)
                self._set_visible(entry['group'], True)
            if keyword:
                matched = keyword in f"{row['title']} {row['comment']}".lower()
                self._set_visible(card, matched)
                if matched:
                    card.set_highlight(keyword)
            if not entry['rows']:
                pending.pop(0)
            built += 1
        return len(pending)

    def _pump(self, view: dict) -> None:
        if self._pump_scheduled_for is view:
            self._pump_scheduled_for = None
        if view is not self.current_view or not self.isVisible():
            return
        if self._add_cards(view, self._BATCH_CARDS):
            self._schedule_pump(view)
            return
        # 卡片都建完了，撤掉加载期间锁定的最小高度，否则排布变宽/变窄后可能永久留白。
        self.container.setMinimumHeight(0)
        self._restore_scroll_if_needed()
        self._schedule_warmup()

    def _schedule_pump(self, view: dict, delay: int = 0) -> None:
        if view is not self.current_view or self._pump_scheduled_for is view:
            return
        self._pump_scheduled_for = view
        QTimer.singleShot(delay, lambda: self._pump(view))

    def _flush_pending(self) -> None:
        view = self.current_view
        if view is None:
            return
        while view['pending']:
            self._add_cards(view, self._BATCH_CARDS * 8)

    def _restore_scroll_if_needed(self) -> None:
        target = self._restore_scroll
        self._restore_scroll = 0
        if not target:
            return
        bar = self.scroll.verticalScrollBar()
        if bar.value() == 0:
            self._set_scroll(min(target, bar.maximum()))

    @staticmethod
    def _divider_color() -> str:
        return 'rgba(255,255,255,0.08)' if isDarkTheme() else 'rgba(0,0,0,0.10)'

    def apply_theme(self) -> None:
        color = self._divider_color()
        for view in self.views.values():
            for line in view['lines']:
                line.setStyleSheet(f'background: {color};')
        self.back_to_top.setStyleSheet(self._back_to_top_style())

    @staticmethod
    def _back_to_top_style() -> str:
        if isDarkTheme():
            return ('#backToTop { background-color: #323232;'
                    ' border: 1px solid rgba(255,255,255,0.10); border-radius: 20px; }'
                    '#backToTop:hover { background-color: #3c3c3c;'
                    ' border: 1px solid rgba(255,255,255,0.18); }')
        return ('#backToTop { background-color: #ffffff;'
                ' border: 1px solid rgba(15,23,42,0.22); border-radius: 20px; }'
                '#backToTop:hover { background-color: #ffffff;'
                ' border: 1px solid rgba(15,23,42,0.40); }')

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_back_to_top()

    def _place_back_to_top(self) -> None:
        button = self.back_to_top
        button.move(self.width() - button.width() - 32,
                    self.height() - button.height() - 24)
        button.raise_()

    def _update_back_to_top(self) -> None:
        shown = self.scroll.verticalScrollBar().value() > 240
        if not shown:
            self.back_to_top.hide()
            return
        self._place_back_to_top()
        self.back_to_top.show()

    def scroll_to_top(self) -> None:
        delegate = getattr(self.scroll, 'delegate', None)
        smooth = getattr(delegate, 'vScrollBar', None)
        if smooth is not None:
            smooth.scrollTo(0)
        else:
            self.scroll.verticalScrollBar().setValue(0)

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
        self._schedule_pump(view, delay=24)

    def _schedule_warmup(self) -> None:
        if len(self.views) >= 2 or not self.isVisible():
            return
        if self.current_view is not None and self.current_view['pending']:
            return
        QTimer.singleShot(600, self._warm_up)

    def _warm_up(self) -> None:
        if len(self.views) >= 2 or not self.isVisible():
            return
        other = 'wish' if self.status == 'done' else 'done'
        if other in self.views:
            return
        view = self._build_view(other, initial_cards=self._WARMUP_CARDS)
        view['widget'].setVisible(False)
        self.views[other] = view

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._dirty:
            self._dirty = False
            self.refresh()
        self._reflow()
        if self.current_view is not None:
            self._schedule_pump(self.current_view)
        self._schedule_warmup()

    def mark_dirty(self) -> None:
        # 即使在后台也立即重建首屏，避免切换到该页时才做耗时构建导致先显示旧内容。
        self._dirty = False
        self.refresh()

    def refresh(self) -> None:
        bar = self.scroll.verticalScrollBar()
        scroll = bar.value()
        self._clear()
        self._restore_scroll = scroll
        self._show_status(self.status, reset_scroll=False)
        if self.current_view is not None and not self.current_view['pending']:
            self._restore_scroll_if_needed()

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
        if keyword or clearing:
            # 搜索/清空搜索需要完整卡片集合，先把分批加载补齐。
            self._restore_scroll = 0
            self._flush_pending()
        if keyword and not was_search:
            self._saved_scroll = self.scroll.verticalScrollBar().value()
            self._saved_height = self.container.height()
        total = 0
        view = self.current_view
        if not keyword and view is not None:
            # 未创建的日期分组不在 self._groups 中，总数直接取视图的完整记录数。
            total = int(view.get('totalRows') or 0)
        for header, count_label, group_widget, cards, _ in self._groups:
            if keyword:
                visible = 0
                for card, row in cards:
                    matched = keyword in f"{row['title']} {row['comment']}".lower()
                    self._set_visible(card, matched)
                    if matched:
                        card.set_highlight(keyword)
                    elif card.highlight:
                        card.set_highlight('')
                    if matched:
                        visible += 1
            else:
                visible = int(group_widget.property('totalRows') or 0)
                for card, _row in cards:
                    self._set_visible(card, True)
                    if card.highlight:
                        card.set_highlight('')
            count_label.setText(f'{visible} {MEDIA_NOUNS[self.media_type]}')
            # 卡片尚未建好的分组先不显示，避免只剩日期和条数的空分组。
            show = visible > 0 and bool(cards)
            self._set_visible(header, show)
            self._set_visible(group_widget, show)
            if keyword:
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
        self._restore_scroll = 0
        self._flush_pending()
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
        # 合并多次请求：补齐卡片时每个新分组都会触发一次，避免排一堆重复的重排。
        if self._reflow_scheduled:
            return
        self._reflow_scheduled = True
        QTimer.singleShot(0, self._reflow_now)

    def _reflow_now(self) -> None:
        self._reflow_scheduled = False
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
            self.content_layout.activate()
            # 分批补卡时滚动区会先按旧内容高度压缩分组，卡片看起来在慢慢变高。
            # 加载期间才锁定最小高度；全部建完后必须放开，否则中途算大的高度会一直留着，
            # 表现为列表底部一大片空白。
            pending = any(entry['total'] > len(entry['cards'])
                          for entry in view['pending'])
            self.container.setMinimumHeight(
                self.content_layout.sizeHint().height() if pending else 0)
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
