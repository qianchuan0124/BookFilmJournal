from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, Signal, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QFontMetrics, QPainter, QPixmap
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QProgressBar, QStackedWidget,
    QVBoxLayout, QWidget,
)

from qfluentwidgets import (
    BodyLabel, CaptionLabel, FlowLayout, FluentIcon, InfoBar, InfoBarPosition, MessageBox,
    PushButton, StrongBodyLabel, TitleLabel,
    TransparentToolButton, qconfig,
)

from app.config import MEDIA_LABELS, MEDIA_NOUNS, READ_LABELS, ROOT
from app.db import Database
from app.media_meta import book_stats_text_full, movie_stats_text
from app.ui.dialogs import (
    MediaMetaDialog, SyncDialog, open_item_detail, open_record_detail,
)
from app.ui.read_stamp import ReadStamp
from app.ui.record_card import _apply_tone, _cover_pixmap, _elide_to_lines, _text_label
from app.ui.scroll_area import SmoothScrollArea
from app.ui.segmented import SegmentedSwitch
from app.ui.styles import progress_style, surface_style, primary_button

RELATION_LABELS = {'created': '我创建的', 'followed': '我关注的'}
RELATION_COLORS = {'created': ('#b07c20', '#e5b45f'),
                   'followed': ('#1f8a70', '#55b7a5')}


def _count_text(media_type: str, read: int, total: int) -> str:
    if total <= 0:
        return '暂无条目'
    done_text, todo_text = READ_LABELS.get(media_type, ('已读', '未读'))
    noun = MEDIA_NOUNS.get(media_type, '条')
    return f'{done_text} {read}/{total} {noun} · {todo_text} {max(0, total - read)}'


def _placeholder_pixmap(width: int, height: int) -> QPixmap:
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


def _doulist_cover_pixmap(items, width: int, height: int) -> QPixmap:
    if not items:
        return _placeholder_pixmap(width, height)
    for item in items:
        cover = item['cover_path'] or ''
        if not cover:
            continue
        full = Path(cover)
        if not full.is_absolute():
            full = ROOT / full
        if full.exists():
            return _cover_pixmap(cover, width, height, fill=True)
    return _placeholder_pixmap(width, height)


class DoulistListCard(QFrame):
    open_requested = Signal(int)
    move_requested = Signal(int, int)
    delete_requested = Signal(int)

    def __init__(self, doulist, items, parent=None):
        super().__init__(parent)
        self.doulist = doulist
        self.setObjectName('doulistListCard')
        self.setFixedWidth(214)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(surface_style('doulistListCard', 12))
        qconfig.themeChanged.connect(self._apply_theme)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        collage = QLabel(self)
        collage.setFixedSize(194, 272)
        collage.setPixmap(_doulist_cover_pixmap(items, 194, 272))
        collage.setStyleSheet('background:#0f131b; border-radius:7px;')
        layout.addWidget(collage)
        title = _text_label(self, '', size=14, weight=QFont.Weight.DemiBold, tone='title')
        title.setWordWrap(True)
        title.setText(_elide_to_lines(title, doulist['title'] or '', 194, 2))
        title.setToolTip(doulist['title'] or '')
        layout.addWidget(title)
        footer = QHBoxLayout()
        relation = _text_label(
            self, RELATION_LABELS.get(doulist['relation'], '豆列'),
            colors=RELATION_COLORS.get(doulist['relation'], ('#5f6b7a', '#8d95a5')))
        footer.addWidget(relation)
        if doulist['media_type'] not in ('movie', 'book'):
            mixed = _text_label(self, '混合')
            footer.addWidget(mixed)
        footer.addStretch(1)
        total = int(doulist['local_count'] or 0)
        read = min(total, int(doulist['read_count'] or 0))
        stats = _text_label(self, _count_text(doulist['media_type'], read, total),
                            colors=('#4b5563', '#b9b5aa'))
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
        if total and read == total:
            complete = _text_label(
                self, '已看完' if doulist['media_type'] == 'movie' else '已读完',
                colors=('#4f7d3a', '#7fae68'))
            footer.addWidget(complete)
        up_button = TransparentToolButton(FluentIcon.UP, self)
        up_button.setIconSize(QSize(12, 12))
        up_button.setFixedSize(20, 20)
        up_button.setToolTip('上移豆列')
        up_button.clicked.connect(lambda: self.move_requested.emit(int(doulist['id']), -1))
        footer.addWidget(up_button)
        down_button = TransparentToolButton(FluentIcon.DOWN, self)
        down_button.setIconSize(QSize(12, 12))
        down_button.setFixedSize(20, 20)
        down_button.setToolTip('下移豆列')
        down_button.clicked.connect(lambda: self.move_requested.emit(int(doulist['id']), 1))
        footer.addWidget(down_button)
        delete_button = TransparentToolButton(FluentIcon.DELETE, self)
        delete_button.setIconSize(QSize(12, 12))
        delete_button.setFixedSize(20, 20)
        delete_button.setToolTip('删除豆列（移到回收站）')
        delete_button.clicked.connect(
            lambda: self.delete_requested.emit(int(doulist['id']))
        )
        footer.addWidget(delete_button)
        layout.addLayout(footer)
        self._tone_labels = [
            label for label in self.findChildren(QLabel)
            if label.property('cardTone') or label.property('cardColors')
        ]

    def _apply_theme(self, *_) -> None:
        self.setStyleSheet(surface_style('doulistListCard', 12))
        self.progress_bar.setStyleSheet(progress_style())
        for label in self._tone_labels:
            _apply_tone(label)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.open_requested.emit(int(self.doulist['id']))
        super().mouseReleaseEvent(event)


class DoulistDetailRow(QFrame):
    move_requested = Signal(int, int)
    edit_requested = Signal(int)
    open_requested = Signal(object)

    def __init__(self, item, word_counts=None, movie_intros=None, parent=None):
        super().__init__(parent)
        self.item = item
        word_counts = word_counts or {}
        movie_intros = movie_intros or {}
        self.setObjectName('doulistDetailRow')
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip('点击查看条目详情')
        self.setStyleSheet(surface_style('doulistDetailRow', 10))
        qconfig.themeChanged.connect(self._apply_theme)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 12, 14, 12)
        layout.setSpacing(14)
        cover = QLabel(self)
        cover.setFixedSize(86, 122)
        cover.setAlignment(Qt.AlignCenter)
        cover.setPixmap(_cover_pixmap(item['cover_path'] or '', 86, 122, fill=True))
        cover.setStyleSheet('background:#0f131b; border-radius:6px;')
        layout.addWidget(cover)

        info = QVBoxLayout()
        info.setSpacing(5)
        title = StrongBodyLabel('', self)
        metrics = QFontMetrics(title.font())
        title.setText(metrics.elidedText(item['title'] or '', Qt.ElideRight, 760))
        title.setToolTip(item['title'] or '')
        info.addWidget(title)
        subtitle = CaptionLabel('', self)
        subtitle.setText(metrics.elidedText(item['intro'] or '暂无简介', Qt.ElideRight, 760))
        subtitle.setTextColor('#5f6b7a', '#8d95a5')
        info.addWidget(subtitle)
        if item['media_type'] == 'book':
            stats = book_stats_text_full(item['subject_id'],
                                         word_counts.get(str(item['subject_id']), 0))
        elif item['media_type'] == 'movie':
            intro = item['intro'] or movie_intros.get(str(item['subject_id']), '')
            stats = movie_stats_text(intro, item['subject_id'])
        else:
            stats = ''
        if stats:
            stats_label = CaptionLabel(stats, self)
            stats_label.setTextColor('#5f6b7a', '#8d95a5')
            info.addWidget(stats_label)
        if item['comment']:
            comment = CaptionLabel('', self)
            comment.setText(metrics.elidedText(item['comment'], Qt.ElideRight, 760))
            comment.setTextColor('#7a7466', '#c9c5bb')
            info.addWidget(comment)
        info.addStretch(1)
        footer = QHBoxLayout()
        footer.setSpacing(2)
        media = CaptionLabel(MEDIA_LABELS.get(item['media_type'], item['media_type']), self)
        media.setTextColor('#b07c20', '#e5b45f')
        footer.addWidget(media)
        edit_button = TransparentToolButton(FluentIcon.EDIT, self)
        edit_button.setIconSize(QSize(12, 12))
        edit_button.setFixedSize(20, 20)
        edit_button.setToolTip('修改时长 / 页数 / 字数')
        edit_button.clicked.connect(lambda: self.edit_requested.emit(int(item['id'])))
        footer.addWidget(edit_button)
        up_button = TransparentToolButton(FluentIcon.UP, self)
        up_button.setIconSize(QSize(12, 12))
        up_button.setFixedSize(20, 20)
        up_button.setToolTip('上移条目')
        up_button.clicked.connect(lambda: self.move_requested.emit(int(item['id']), -1))
        footer.addWidget(up_button)
        down_button = TransparentToolButton(FluentIcon.DOWN, self)
        down_button.setIconSize(QSize(12, 12))
        down_button.setFixedSize(20, 20)
        down_button.setToolTip('下移条目')
        down_button.clicked.connect(lambda: self.move_requested.emit(int(item['id']), 1))
        footer.addWidget(down_button)
        footer.addStretch(1)
        info.addLayout(footer)
        layout.addLayout(info, 1)

        if item['is_read']:
            stamp = ReadStamp(
                item['media_type'], item['read_date'] or '', item['read_rating'] or 0, 96, self
            )
            layout.addWidget(stamp, 0, Qt.AlignVCenter)

    def _apply_theme(self, *_) -> None:
        self.setStyleSheet(surface_style('doulistDetailRow', 10))

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.open_requested.emit(self.item)
        super().mouseReleaseEvent(event)


class DoulistPage(QWidget):
    synced = Signal(dict)
    record_changed = Signal(str)

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.setObjectName('doulistPage')
        self._lists = []
        self._selected_type = 'movie'
        self._current_doulist = None
        self._cards = []
        self._flow = None
        self._detail_rows = {}
        self._dirty = False
        # 卡片分批创建：一次性建几十张卡片会让切换页面卡住几百毫秒。
        self._card_queue: list = []
        self._card_items: dict[int, list] = {}
        self._card_parent = None
        self._card_generation = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(32, 26, 32, 26)
        root.setSpacing(12)
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(3)
        title_box.addWidget(TitleLabel('豆列', self))
        subtitle = BodyLabel('自己创建和关注的片单/书单，按影视与书籍拆分。', self)
        subtitle.setTextColor('#5f6b7a', '#8d95a5')
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch(1)
        self.count_label = StrongBodyLabel('共 0 个', self)
        self.count_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        header.addWidget(self.count_label)
        self.type_switch = SegmentedSwitch(
            '影视豆列', '书籍豆列', 'movie', 'book', 210, 'doulistTypeSwitch', self
        )
        self.type_switch.changed.connect(self._set_type)
        header.addWidget(self.type_switch)
        self.sync_button = primary_button(FluentIcon.SYNC, '同步豆瓣', self)
        self.sync_button.clicked.connect(self.open_sync_dialog)
        header.addWidget(self.sync_button)
        root.addLayout(header)

        self.stack = QStackedWidget(self)
        self.outer_page = self._build_outer_page()
        self.detail_page = self._build_detail_page()
        self.stack.addWidget(self.outer_page)
        self.stack.addWidget(self.detail_page)
        root.addWidget(self.stack, 1)

        # 首次显示时再加载豆列，避免启动阶段构建全部卡片。
        self._dirty = True

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
        self.back_button.setToolTip('返回豆列列表')
        self.back_button.clicked.connect(self.show_outer)
        bar.addWidget(self.back_button)
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        self.detail_title = TitleLabel('豆列', page)
        self.detail_meta = CaptionLabel('', page)
        self.detail_meta.setTextColor('#5f6b7a', '#b9b5aa')
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
        self.detail_open_button = PushButton('打开豆列', page)
        self.detail_open_button.setToolTip('在豆瓣打开当前豆列')
        self.detail_open_button.clicked.connect(self._open_current_in_douban)
        bar.addWidget(self.detail_open_button)
        self.detail_delete_button = PushButton('删除豆列', page)
        self.detail_delete_button.setToolTip('删除当前豆列（移到回收站）')
        self.detail_delete_button.clicked.connect(lambda: self.delete_doulist())
        bar.addWidget(self.detail_delete_button)
        layout.addLayout(bar)

        self.detail_scroll = SmoothScrollArea(page)
        self.detail_scroll.setWidgetResizable(True)
        self.detail_scroll.enableTransparentBackground()
        self.detail_scroll.setFrameShape(QFrame.NoFrame)
        self.detail_container = QWidget()
        self.detail_container.setStyleSheet('background:transparent;')
        self.detail_layout = QVBoxLayout(self.detail_container)
        self.detail_layout.setContentsMargins(0, 6, 0, 20)
        self.detail_layout.setSpacing(10)
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

    def _set_type(self, media_type: str) -> None:
        self._selected_type = media_type
        self.show_outer()
        self._build_outer_grid()

    def _build_outer_grid(self) -> None:
        self._clear_layout(self.outer_layout)
        self._cards = []
        self._flow = None
        # 停掉上一轮还在排队建卡的批次。
        self._card_generation += 1
        self._card_queue = []
        lists = [row for row in self._lists
                 if row['media_type'] == self._selected_type
                 or row['media_type'] not in ('movie', 'book')]
        created = sum(1 for row in lists if row['relation'] == 'created')
        followed = sum(1 for row in lists if row['relation'] == 'followed')
        total_items = sum(int(row['local_count'] or 0) for row in lists)
        total_read = sum(min(int(row['local_count'] or 0), int(row['read_count'] or 0))
                         for row in lists)
        title = '影视豆列' if self._selected_type == 'movie' else '书籍豆列'
        if lists:
            info = CaptionLabel(
                f'{title} · 共 {len(lists)} 个（我创建 {created} / 我关注 {followed}）'
                f' · {_count_text(self._selected_type, total_read, total_items)}',
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
            self._flow = flow
            self._card_parent = grid
            # 一次性建完几十张卡会卡住 300ms 以上，这里排队分批建，界面先出来再补卡片。
            self._card_items = {int(row['id']): self.db.doulist_items(int(row['id']))
                                for row in lists}
            self._card_queue = list(lists)
            # 首批少建几张，先把页面显示出来，剩下的交给定时器补。
            self._pump_cards(budget=3)
            self.outer_layout.addWidget(grid)
        else:
            empty = BodyLabel(f'还没有{title}，同步豆瓣时勾选「同步豆列」并填写 Cookie。', self.outer_container)
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet('color:#8d95a5; padding:70px 0;')
            self.outer_layout.addWidget(empty)
        self.outer_layout.addStretch(1)
        self.outer_scroll.verticalScrollBar().setValue(0)

    _CARD_BATCH = 6

    def _pump_cards(self, budget: int | None = None) -> None:
        """分批把排队中的豆列卡片建出来，每批建完就让出事件循环。"""
        if not self.isVisible():
            # 页面已经切走，剩下的卡片留到下次显示时再补，别在后台抢时间。
            return
        generation = self._card_generation
        budget = self._CARD_BATCH if budget is None else budget
        while budget > 0 and self._card_queue:
            doulist = self._card_queue.pop(0)
            items = self._card_items.get(int(doulist['id']), [])
            card = DoulistListCard(doulist, items, self._card_parent)
            card.open_requested.connect(self.show_detail)
            card.move_requested.connect(self.move_doulist)
            card.delete_requested.connect(self.delete_doulist)
            self._flow.addWidget(card)
            self._cards.append(card)
            budget -= 1
        if self._card_queue:
            QTimer.singleShot(
                0, lambda: self._card_generation == generation and self._pump_cards())

    def refresh(self) -> None:
        self._dirty = False
        self.show_outer()
        self._lists = self.db.list_doulists()
        visible = [row for row in self._lists
                   if row['media_type'] == self._selected_type
                   or row['media_type'] not in ('movie', 'book')]
        self.count_label.setText(f'共 {len(visible)} 个')
        self._build_outer_grid()

    def mark_dirty(self) -> None:
        self._dirty = True

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._dirty:
            self.refresh()
        if self._card_queue:
            # 上次切走时没建完，这里接着补。
            self._pump_cards()

    def show_detail(self, doulist_id: int, keep_scroll: bool = False) -> None:
        doulist = next((row for row in self._lists if int(row['id']) == int(doulist_id)), None)
        if not doulist:
            return
        scroll_value = self.detail_scroll.verticalScrollBar().value() if keep_scroll else 0
        self._current_doulist = doulist
        relation = RELATION_LABELS.get(doulist['relation'], '豆列')
        media = MEDIA_LABELS.get(doulist['media_type'], '混合')
        self.detail_title.setText(doulist['title'])
        total = int(doulist['local_count'] or 0)
        read = min(total, int(doulist['read_count'] or 0))
        self.detail_meta.setText(
            f'{relation} · {media} · {_count_text(doulist["media_type"], read, total)}'
        )
        self.detail_progress.setRange(0, max(1, total))
        self.detail_progress.setValue(read)
        self.detail_progress.setVisible(total > 0)
        items = self.db.doulist_items(int(doulist_id))
        self._clear_layout(self.detail_layout)
        self._detail_rows = {}
        if not items:
            empty = BodyLabel('这个豆列没有可展示的影视/图书条目。', self.detail_container)
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet('color:#8d95a5; padding:60px 0;')
            self.detail_layout.addWidget(empty)
        else:
            word_counts = self.db.word_count_map('book')
            movie_intros = self.db.intro_map('movie')
            existing = {(r['media_type'], str(r['subject_id']))
                        for r in self.db.list_all_records()}
            for item in items:
                row = DoulistDetailRow(item, word_counts, movie_intros, self.detail_container)
                row.move_requested.connect(self.move_doulist_item)
                row.edit_requested.connect(self.edit_item_meta)
                row.open_requested.connect(self.open_item)
                if (item['media_type'], str(item['subject_id'])) in existing:
                    row.setToolTip('点击查看记录详情')
                else:
                    row.setToolTip('点击查看条目详情')
                self.detail_layout.addWidget(row)
                self._detail_rows[int(item['id'])] = row
        self.detail_layout.addStretch(1)
        self.detail_scroll.verticalScrollBar().setValue(scroll_value)
        self.stack.setCurrentWidget(self.detail_page)

    def open_item(self, item) -> None:
        record = self.db.find_record_any(item['media_type'], str(item['subject_id']))
        if record is None:
            # 豆列里有、记录里还没有的条目：先看条目详情，别直接把人扔进浏览器。
            source = (f'来自豆列「{self._current_doulist["title"]}」'
                      if self._current_doulist else '豆列')
            open_item_detail(self.db, item, self.window(), source)
            return
        if open_record_detail(self.db, record, self.window()):
            if self._current_doulist:
                self.show_detail(int(self._current_doulist['id']), keep_scroll=True)
            self.record_changed.emit(record['media_type'])

    def edit_item_meta(self, item_id: int) -> None:
        row = self._detail_rows.get(int(item_id))
        if row is None:
            return
        item = row.item
        dialog = MediaMetaDialog(
            self.db, item['media_type'], str(item['subject_id']), item['title'] or '',
            item['intro'] or '', self.window(),
        )
        if dialog.exec() and self._current_doulist:
            self.show_detail(int(self._current_doulist['id']), keep_scroll=True)

    def move_doulist(self, doulist_id: int, offset: int) -> None:
        if not self.db.move_doulist(
            doulist_id, offset, (self._selected_type, 'mixed')
        ):
            return
        self._lists = self.db.list_doulists()
        if self._flow is None or not self._cards:
            return
        cards = {int(card.doulist['id']): card for card in self._cards}
        lists = [row for row in self._lists
                 if row['media_type'] == self._selected_type
                 or row['media_type'] not in ('movie', 'book')]
        for card in self._cards:
            self._flow.removeWidget(card)
        ordered = []
        for doulist in lists:
            card = cards.get(int(doulist['id']))
            if card is not None:
                self._flow.addWidget(card)
                ordered.append(card)
        self._cards = ordered
        self._flow.invalidate()
        self._flow.setGeometry(self._flow.geometry())
        if self._flow.parentWidget() is not None:
            self._flow.parentWidget().update()

    def delete_doulist(self, doulist_id=None) -> None:
        if doulist_id is None and self._current_doulist:
            doulist_id = int(self._current_doulist['id'])
        if not doulist_id:
            return
        doulist = next((row for row in self._lists if int(row['id']) == int(doulist_id)), None)
        if not doulist:
            return
        box = MessageBox(
            '删除豆列',
            f'将「{doulist["title"]}」移到回收站？\n可在回收站中恢复或彻底删除。',
            self.window(),
        )
        box.yesButton.setText('删除')
        box.cancelButton.setText('取消')
        if not box.exec():
            return
        self.db.soft_delete_doulist(int(doulist_id))
        if self._current_doulist and int(self._current_doulist['id']) == int(doulist_id):
            self._current_doulist = None
        self.refresh()
        InfoBar.success('已移到回收站', f'「{doulist["title"]}」已移到回收站。',
                        parent=self, position=InfoBarPosition.TOP)

    def move_doulist_item(self, item_id: int, offset: int) -> None:
        if not self.db.move_doulist_item(item_id, offset):
            return
        if not self._current_doulist:
            return
        items = self.db.doulist_items(int(self._current_doulist['id']))
        order = [int(item['id']) for item in items]
        if not order or any(value not in self._detail_rows for value in order):
            self.show_detail(int(self._current_doulist['id']), keep_scroll=True)
            return
        for row in self._detail_rows.values():
            self.detail_layout.removeWidget(row)
        for index, value in enumerate(order):
            self.detail_layout.insertWidget(index, self._detail_rows[value])
        self.detail_layout.invalidate()
        self.detail_layout.activate()
        self.detail_container.update()

    def show_outer(self) -> None:
        self.stack.setCurrentWidget(self.outer_page)

    def _open_current_in_douban(self) -> None:
        if self._current_doulist and self._current_doulist['url']:
            QDesktopServices.openUrl(QUrl(self._current_doulist['url']))

    def open_sync_dialog(self) -> None:
        dialog = SyncDialog(self.db, self.window())
        dialog.synced.connect(self._on_synced)
        dialog.exec()

    def _on_synced(self, result: dict) -> None:
        self.refresh()
        self.synced.emit(result)
