import re
from collections import OrderedDict
from functools import lru_cache
from html import escape
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import (
    QColor, QFontMetrics, QImage, QPainter, QPen, QPixmap, QTextLayout,
)
from PySide6.QtGui import QTextOption
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout

from qfluentwidgets import (
    BodyLabel, CaptionLabel, CardWidget, FluentIcon, StrongBodyLabel,
    TransparentToolButton, ToolTipFilter, isDarkTheme,
)

from app.config import CACHE_DIR, MEDIA_LABELS, ROOT
from app.cover_sync import is_custom_cover
from app.media_meta import book_stats_text, movie_stats_text


def _two_line_text(text: str, font, width: int) -> str:
    text = re.sub(r'\s+', ' ', text or '').strip()
    if not text:
        return ''
    metrics = QFontMetrics(font)
    if metrics.horizontalAdvance(text) <= width:
        return text
    layout = QTextLayout(text, font)
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
    layout.setTextOption(option)
    layout.beginLayout()
    first = layout.createLine()
    if not first.isValid():
        return text
    first.setLineWidth(width)
    second = layout.createLine()
    if not second.isValid():
        return text
    second.setLineWidth(width)
    layout.endLayout()
    first_text = text[first.textStart():first.textStart() + first.textLength()].rstrip()
    rest = text[second.textStart():]
    second_text = metrics.elidedText(rest, Qt.ElideRight, width)
    return first_text + '\n' + second_text


def _elided_lines(text: str, font, width: int, max_lines: int) -> str:
    text = re.sub(r'\s+', ' ', text or '').strip()
    if not text:
        return ''
    metrics = QFontMetrics(font)
    layout = QTextLayout(text, font)
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
    layout.setTextOption(option)
    layout.beginLayout()
    ranges = []
    for _ in range(max_lines):
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(width)
        ranges.append((line.textStart(), line.textLength()))
    layout.endLayout()
    if not ranges:
        return text
    lines = [text[start:start + length].rstrip() for start, length in ranges]
    last_start, last_length = ranges[-1]
    if last_start + last_length < len(text):
        lines[-1] = metrics.elidedText(text[last_start:], Qt.ElideRight, width)
    return '\n'.join(lines)


def _highlight_html(text: str, keyword: str) -> str:
    if not keyword:
        return escape(text).replace('\n', '<br>')
    parts = []
    last = 0
    for match in re.finditer(re.escape(keyword), text, re.IGNORECASE):
        parts.append(escape(text[last:match.start()]))
        parts.append(
            '<span style="background-color:#e5b45f;color:#171a20;border-radius:2px;">'
            + escape(match.group(0)) + '</span>'
        )
        last = match.end()
    parts.append(escape(text[last:]))
    return ''.join(parts).replace('\n', '<br>')


def _cover_pixmap(path: str, width: int, height: int, fill: bool = False) -> QPixmap:
    if not path:
        return _empty_pixmap(width, height)
    full = Path(path)
    if not full.is_absolute():
        full = ROOT / full
    if not full.exists():
        return _empty_pixmap(width, height)
    try:
        stamp = full.stat().st_mtime_ns
    except OSError:
        stamp = 0
    thumb = _thumb_path(full, width, height, fill)
    try:
        if thumb.exists() and thumb.stat().st_mtime_ns >= stamp:
            cached = QPixmap(str(thumb))
            if not cached.isNull():
                return cached
    except OSError:
        pass
    return _scaled_cover(str(full), width, height, fill, stamp)


def _empty_pixmap(width: int, height: int) -> QPixmap:
    canvas = QPixmap(width, height)
    canvas.fill(QColor('#0f131b'))
    return canvas


_THUMB_DIR = CACHE_DIR / 'cover_thumbs'
_PIXMAP_CACHE_LIMIT = 400


def _thumb_path(full: Path, width: int, height: int, fill: bool) -> Path:
    tag = f'{width}x{height}{"f" if fill else ""}'
    return _THUMB_DIR / tag / f'{full.stem}.jpg'


def _render_cover_image(full: Path, width: int, height: int, fill: bool) -> QImage:
    """在工作线程中生成（或读取已缓存的）缩略图。"""
    thumb = _thumb_path(full, width, height, fill)
    try:
        source_stamp = full.stat().st_mtime_ns
    except OSError:
        return QImage()
    try:
        if thumb.exists() and thumb.stat().st_mtime_ns >= source_stamp:
            cached = QImage(str(thumb))
            if not cached.isNull():
                return cached
    except OSError:
        pass
    image = QImage(str(full))
    if image.isNull():
        return image
    mode = Qt.KeepAspectRatioByExpanding if fill else Qt.KeepAspectRatio
    scaled = image.scaled(width, height, mode, Qt.SmoothTransformation)
    canvas = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    canvas.fill(QColor('#0f131b'))
    painter = QPainter(canvas)
    painter.drawImage((width - scaled.width()) // 2, (height - scaled.height()) // 2,
                      scaled)
    painter.end()
    try:
        thumb.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(str(thumb), 'JPEG', 88)
    except OSError:
        pass
    return canvas


class _CoverSignals(QObject):
    ready = Signal(str, int, int, QImage)


class _CoverTask(QRunnable):
    def __init__(self, key: str, full: Path, width: int, height: int, fill: bool,
                 signals: _CoverSignals):
        super().__init__()
        self.key = key
        self.full = full
        self.width = width
        self.height = height
        self.fill = fill
        self.signals = signals

    def run(self) -> None:
        try:
            image = _render_cover_image(self.full, self.width, self.height, self.fill)
            self.signals.ready.emit(self.key, self.width, self.height, image)
        except RuntimeError:
            pass


_cover_signals = None
_cover_pool = None
_pixmap_cache: OrderedDict = OrderedDict()
_pending_requests: dict[str, list] = {}


def _ensure_cover_loader() -> None:
    global _cover_signals, _cover_pool
    if _cover_signals is None:
        _cover_signals = _CoverSignals()
        _cover_signals.ready.connect(_on_cover_ready)
        _cover_pool = QThreadPool.globalInstance()


def _on_cover_ready(key: str, width: int, height: int, image: QImage) -> None:
    callbacks = _pending_requests.pop(key, [])
    pixmap = _empty_pixmap(width, height) if image.isNull() else QPixmap.fromImage(image)
    _pixmap_cache[key] = pixmap
    _pixmap_cache.move_to_end(key)
    while len(_pixmap_cache) > _PIXMAP_CACHE_LIMIT:
        _pixmap_cache.popitem(last=False)
    for callback in callbacks:
        try:
            callback(pixmap)
        except RuntimeError:
            pass


def request_cover(path: str, width: int, height: int, fill, callback) -> None:
    """异步获取封面缩略图；先返回占位图，解码完成后回调。"""
    if not path:
        callback(_empty_pixmap(width, height))
        return
    full = Path(path)
    if not full.is_absolute():
        full = ROOT / full
    if not full.exists():
        callback(_empty_pixmap(width, height))
        return
    key = f'{full}|{width}x{height}|{int(bool(fill))}'
    cached = _pixmap_cache.get(key)
    if cached is not None:
        _pixmap_cache.move_to_end(key)
        callback(cached)
        return
    callbacks = _pending_requests.setdefault(key, [])
    callbacks.append(callback)
    if len(callbacks) == 1:
        _ensure_cover_loader()
        _cover_pool.start(_CoverTask(key, full, width, height, fill, _cover_signals))


@lru_cache(maxsize=600)
def _scaled_cover(full_path: str, width: int, height: int, fill: bool,
                  mtime_ns: int) -> QPixmap:
    canvas = _empty_pixmap(width, height)
    pixmap = QPixmap(full_path)
    if pixmap.isNull():
        return canvas
    if fill:
        scaled = pixmap.scaled(width, height, Qt.KeepAspectRatioByExpanding,
                               Qt.SmoothTransformation)
        painter = QPainter(canvas)
        painter.drawPixmap((width - scaled.width()) // 2,
                           (height - scaled.height()) // 2, scaled)
        painter.end()
        return canvas
    scaled = pixmap.scaled(width, height, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    painter = QPainter(canvas)
    painter.drawPixmap((width - scaled.width()) // 2, (height - scaled.height()) // 2, scaled)
    painter.end()
    return canvas


class RecordCard(CardWidget):
    edit_requested = Signal(int)
    detail_requested = Signal(int)
    link_requested = Signal(str)
    delete_requested = Signal(int)
    cover_requested = Signal(int)

    def __init__(self, record, parent=None, highlight: str = ''):
        super().__init__(parent)
        self.record = record
        self.highlight = (highlight or '').strip()
        self.setObjectName('recordCard')
        self.comment = record['comment'] or ''
        self.setFixedWidth(214)
        self.setBorderRadius(12)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setClickEnabled(True)
        self.setCursor(Qt.PointingHandCursor)
        self.clicked.connect(lambda: self.detail_requested.emit(int(self.record['id'])))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(7)

        cover = QLabel(self)
        cover.setFixedSize(194, 272)
        cover.setAlignment(Qt.AlignCenter)
        cover.setPixmap(_empty_pixmap(194, 272))
        cover.setStyleSheet('background:#0f131b; border-radius:7px;')
        self.cover_label = cover
        self.cover_sync_tag = QLabel(' 同步 ', cover)
        self.cover_sync_tag.setStyleSheet(
            'color:#241804; background:rgba(229,180,95,0.92); border-radius:9px;'
            ' font-size:10px; font-weight:600; padding:0 2px;')
        self.cover_sync_tag.setToolTip('自定义封面，豆瓣无法重新获取，会随云同步保存')
        self.cover_sync_tag.adjustSize()
        self.cover_sync_tag.move(194 - self.cover_sync_tag.width() - 8, 8)
        self.cover_sync_tag.setVisible(is_custom_cover(
            record['media_type'], record['subject_id'], record['cover_path'] or ''))
        layout.addWidget(cover)
        self._request_cover(record['cover_path'] or '')

        title = StrongBodyLabel('', self)
        title.setWordWrap(True)
        metrics = QFontMetrics(title.font())
        title.setFixedHeight(metrics.lineSpacing() * 2 + 2)
        self.title_label = title
        self.title_text = _elided_lines(record['title'] or '', title.font(), 194, 2)
        title.setToolTip(record['title'] or '')
        layout.addWidget(title)

        if record['media_type'] == 'book':
            stats = book_stats_text(record['subject_id'], int(record['word_count'] or 0))
        else:
            stats = movie_stats_text(record['intro'] or '', record['subject_id'])
        stats_label = CaptionLabel(stats, self)
        stats_label.setTextColor('#5f6b7a', '#8d95a5')
        if record['book_file']:
            stats_label.setToolTip(record['book_file'])
        self.stats_label = stats_label
        layout.addWidget(stats_label)

        meta = QHBoxLayout()
        meta.setSpacing(5)
        stars = int(record['rating'] or 0)
        star_label = BodyLabel('★' * stars + '☆' * (5 - stars) if stars else '未评分', self)
        star_font = star_label.font()
        star_font.setPixelSize(12)
        star_label.setFont(star_font)
        star_label.setTextColor('#b07c20', '#e5b45f')
        self.star_label = star_label
        meta.addWidget(star_label)
        meta.addStretch(1)
        date_text = record['effective_date'] or record['marked_date'] or '未设置'
        date_label = CaptionLabel(date_text, self)
        self.date_label = date_label
        meta.addWidget(date_label)
        local = CaptionLabel('本地', self)
        local.setTextColor('#b07c20', '#e5b45f')
        local.setToolTip('本地修改过的评分/短评/日期，同步时不会覆盖')
        local.setVisible(bool(
            record['date_overridden'] or record['rating_overridden']
            or record['comment_overridden']
        ))
        self.local_label = local
        meta.addWidget(local)
        layout.addLayout(meta)

        comment_label = CaptionLabel('', self)
        comment_label.setWordWrap(True)
        comment_metrics = QFontMetrics(comment_label.font())
        comment_label.setFixedHeight(comment_metrics.lineSpacing() * 3 + 2)
        self.comment_text = _elided_lines(
            self.comment, comment_label.font(), 194, 3
        )
        comment_label.setTextColor('#5f6b7a', '#8d95a5')
        comment_label.setToolTip(self.comment)
        # 没有短评就不占位：无短评卡片整体更矮，中间不留空白。
        comment_label.setVisible(bool(self.comment))
        self.comment_label = comment_label
        layout.addWidget(comment_label)
        layout.addStretch(1)
        self.box = layout

        actions = QHBoxLayout()
        actions.setSpacing(2)
        if record['status'] == 'wish':
            type_text = '想看' if record['media_type'] == 'movie' else '想读'
        else:
            type_text = MEDIA_LABELS[record['media_type']]
        type_label = CaptionLabel(type_text, self)
        type_label.setTextColor('#b07c20', '#e5b45f')
        self.type_label = type_label
        actions.addWidget(type_label)
        actions.addStretch(1)
        cover_button = TransparentToolButton(FluentIcon.PHOTO, self)
        cover_button.setIconSize(QSize(12, 12))
        cover_button.setFixedSize(20, 20)
        cover_button.setToolTip('更换封面')
        cover_button.installEventFilter(ToolTipFilter(cover_button))
        cover_button.clicked.connect(lambda: self.cover_requested.emit(int(record['id'])))
        actions.addWidget(cover_button)
        link_button = TransparentToolButton(FluentIcon.LINK, self)
        link_button.setIconSize(QSize(12, 12))
        link_button.setFixedSize(20, 20)
        link_button.setToolTip('打开豆瓣条目')
        link_button.installEventFilter(ToolTipFilter(link_button))
        link_button.clicked.connect(lambda: self.link_requested.emit(record['url'] or ''))
        actions.addWidget(link_button)
        edit_button = TransparentToolButton(FluentIcon.EDIT, self)
        edit_button.setIconSize(QSize(12, 12))
        edit_button.setFixedSize(20, 20)
        edit_button.setToolTip('编辑日期、评分与短评')
        edit_button.installEventFilter(ToolTipFilter(edit_button))
        edit_button.clicked.connect(lambda: self.edit_requested.emit(int(record['id'])))
        actions.addWidget(edit_button)
        delete_button = TransparentToolButton(FluentIcon.DELETE, self)
        delete_button.setIconSize(QSize(12, 12))
        delete_button.setFixedSize(20, 20)
        delete_button.setToolTip('删除（移到回收站）')
        delete_button.installEventFilter(ToolTipFilter(delete_button))
        delete_button.clicked.connect(lambda: self.delete_requested.emit(int(record['id'])))
        actions.addWidget(delete_button)
        layout.addLayout(actions)
        self.set_highlight(self.highlight)
        # 固定高度，避免卡片刚加入布局时先以默认高度显示、随后再被撑开。
        self.setFixedHeight(self.sizeHint().height())

    def _request_cover(self, path: str) -> None:
        request_cover(
            path, 194, 272, False,
            lambda pixmap, target=path: self._apply_cover(target, pixmap),
        )

    def _apply_cover(self, path: str, pixmap: QPixmap) -> None:
        if (self.record['cover_path'] or '') != path:
            return
        try:
            self.cover_label.setPixmap(pixmap)
        except RuntimeError:
            pass

    def update_record(self, record, highlight: str = '', force_cover: bool = False) -> None:
        old_cover = self.record['cover_path'] or ''
        self.record = record
        self.comment = record['comment'] or ''
        self.box.setSpacing(7)
        if force_cover or (record['cover_path'] or '') != old_cover:
            self._request_cover(record['cover_path'] or '')
        self.title_text = _elided_lines(
            record['title'] or '', self.title_label.font(), 194, 2
        )
        self.title_label.setToolTip(record['title'] or '')
        if record['media_type'] == 'book':
            stats = book_stats_text(
                record['subject_id'], int(record['word_count'] or 0)
            )
        else:
            stats = movie_stats_text(record['intro'] or '', record['subject_id'])
        self.stats_label.setText(stats)
        self.stats_label.setToolTip(record['book_file'] or '')
        stars = int(record['rating'] or 0)
        self.star_label.setText(
            '★' * stars + '☆' * (5 - stars) if stars else '未评分'
        )
        self.date_label.setText(
            record['effective_date'] or record['marked_date'] or '未设置'
        )
        self.local_label.setVisible(bool(
            record['date_overridden'] or record['rating_overridden']
            or record['comment_overridden']
        ))
        self.cover_sync_tag.setVisible(is_custom_cover(
            record['media_type'], record['subject_id'], record['cover_path'] or ''))
        if record['status'] == 'wish':
            type_text = '想看' if record['media_type'] == 'movie' else '想读'
        else:
            type_text = MEDIA_LABELS[record['media_type']]
        self.type_label.setText(type_text)
        if self.comment:
            self.comment_text = _elided_lines(
                self.comment, self.comment_label.font(), 194, 3
            )
            self.comment_label.setToolTip(self.comment)
        else:
            self.comment_text = ''
        self.comment_label.setVisible(bool(self.comment))
        self._sync_height()
        self.set_highlight(highlight)

    def _sync_height(self) -> None:
        """短评出现/消失后重新计算卡片高度；没有短评的卡片不再预留三行。"""
        self.layout().activate()
        height = self.sizeHint().height()
        if height > 0 and height != self.height():
            self.setFixedHeight(height)
            self.updateGeometry()

    def set_highlight(self, keyword: str) -> None:
        self.highlight = (keyword or '').strip()
        self._set_label_text(self.title_label, self.title_text)
        if self.comment_label is not None:
            self._set_label_text(self.comment_label, self.comment_text)

    def _set_label_text(self, label, text: str) -> None:
        if self.highlight:
            label.setTextFormat(Qt.RichText)
            label.setText(_highlight_html(text, self.highlight))
        else:
            label.setTextFormat(Qt.PlainText)
            label.setText(text)

    def _normalBackgroundColor(self):
        return QColor(255, 255, 255, 13 if isDarkTheme() else 235)

    def _backdrop_color(self) -> QColor:
        """卡片下方（窗口）的底色，用于把半透明卡片压成不透明色。"""
        color = getattr(self.window(), 'backgroundColor', None)
        if isinstance(color, QColor):
            return QColor(color)
        return QColor(32, 32, 32) if isDarkTheme() else QColor(243, 243, 243)

    @staticmethod
    def _flatten(color: QColor, backdrop: QColor) -> QColor:
        alpha = color.alphaF()
        return QColor(
            round(color.red() * alpha + backdrop.red() * (1 - alpha)),
            round(color.green() * alpha + backdrop.green() * (1 - alpha)),
            round(color.blue() * alpha + backdrop.blue() * (1 - alpha)),
        )

    def paintEvent(self, event) -> None:
        # 卡片背景本身是半透明的，直接绘制会和上一帧像素叠加，在 macOS 上
        # 局部重绘后留下文字残影。先铺一层不透底的颜色再画圆角卡片。
        backdrop = self._backdrop_color()
        painter = QPainter(self)
        painter.setRenderHints(QPainter.Antialiasing)
        painter.fillRect(self.rect(), backdrop)
        rect = self.rect().adjusted(0, 0, -1, -1)
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._flatten(self.backgroundColor, backdrop))
        painter.drawRoundedRect(rect, self.borderRadius, self.borderRadius)
        if not isDarkTheme():
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(15, 23, 42, 28), 1))
            painter.drawRoundedRect(rect, self.borderRadius, self.borderRadius)
