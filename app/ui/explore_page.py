"""探索空间页面：拉取远端站点配置、检测连通性、管理本地站点。"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QThread, QUrl, Signal
from PySide6.QtGui import (
    QColor, QDesktopServices, QPainter, QPainterPath, QPen,
)
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    BodyLabel, CaptionLabel, CardWidget, ComboBox, FluentIcon, InfoBar,
    InfoBarPosition, LineEdit, MessageBoxBase, PrimaryPushButton, PushButton,
    SmoothScrollArea, StrongBodyLabel, SubtitleLabel, TitleLabel,
    TransparentToolButton, isDarkTheme, qconfig,
)

from app import explore
from app.db import Database
from app.ui.connectivity import service as connectivity_service


class _ElidedLabel(QLabel):
    """按可用宽度省略显示的链接文本。颜色由调用方按主题设置。"""

    def __init__(self, text: str = '', parent=None):
        super().__init__(parent)
        self._full = text
        self.setText(text)

    def set_full_text(self, text: str) -> None:
        self._full = text
        self._apply()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply()

    def _apply(self) -> None:
        width = max(0, self.width() - 2)
        self.setText(self.fontMetrics().elidedText(self._full, Qt.ElideRight, width))


class _PullWorker(QThread):
    done = Signal(object, str)
    failed = Signal(str)

    def run(self) -> None:
        try:
            config, source = explore.pull_remote()
            self.done.emit(config, source)
        except Exception as exc:  # noqa: BLE001 - 网络异常统一提示
            self.failed.emit(str(exc))


class _StatusMark(QWidget):
    """连通性状态圆标：勾、叉和未检测都用画笔绘制，保证视觉居中。"""

    COLORS = {
        True: ('#2aa889', QColor(42, 168, 137, 41)),
        False: ('#d9534f', QColor(217, 83, 79, 41)),
        None: ('#8d95a5', QColor(141, 149, 165, 41)),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(22, 22)
        self._state = None

    def set_state(self, reachable) -> None:
        state = None if reachable is None else bool(reachable)
        if state != self._state:
            self._state = state
            self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        color, background = self.COLORS[self._state]
        painter.setPen(Qt.NoPen)
        painter.setBrush(background)
        painter.drawEllipse(QRectF(0.5, 0.5, 21, 21))

        cx, cy = 11.0, 11.0
        if self._state is None:
            painter.setBrush(QColor(color))
            painter.drawEllipse(QPointF(cx, cy), 2.2, 2.2)
            return

        pen = QPen(QColor(color), 2)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        if self._state:
            path = QPainterPath()
            path.moveTo(cx - 4.0, cy + 0.4)
            path.lineTo(cx - 1.2, cy + 3.2)
            path.lineTo(cx + 4.2, cy - 3.0)
            painter.drawPath(path)
        else:
            painter.drawLine(QPointF(cx - 3.4, cy - 3.4), QPointF(cx + 3.4, cy + 3.4))
            painter.drawLine(QPointF(cx + 3.4, cy - 3.4), QPointF(cx - 3.4, cy + 3.4))


class SiteRow(QFrame):
    """单个站点：状态、名称、链接与操作按钮。点击整行复制链接。"""

    copy_requested = Signal(dict)
    open_requested = Signal(dict)
    edit_requested = Signal(dict)
    delete_requested = Signal(dict)
    move_requested = Signal(dict, int)

    def __init__(self, site: dict, reachable, parent=None, can_up: bool = True,
                 can_down: bool = True):
        super().__init__(parent)
        self.site = site
        self.setObjectName('exploreSiteRow')
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(54)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 10, 8)
        layout.setSpacing(10)

        self.status_label = _StatusMark(self)
        layout.addWidget(self.status_label)
        self._apply_status(reachable)

        text_box = QVBoxLayout()
        text_box.setSpacing(1)
        name_row = QHBoxLayout()
        name_row.setSpacing(6)
        name_label = StrongBodyLabel(site['name'], self)
        name_row.addWidget(name_label)
        if site.get('local'):
            tag = CaptionLabel('本地', self)
            tag.setTextColor('#1f8a70', '#2aa889')
            name_row.addWidget(tag)
        name_row.addStretch(1)
        text_box.addLayout(name_row)

        self.url_label = _ElidedLabel(site['url'], self)
        self.url_label.setObjectName('exploreSiteUrl')
        # 纯 QLabel 用应用调色板取色；亮色主题下调色板可能仍是白字，这里显式指定。
        self.url_label.setStyleSheet(
            f"color:{'#8d95a5' if isDarkTheme() else '#5f6b7a'};")
        self.url_label.setFixedHeight(16)
        text_box.addWidget(self.url_label)
        layout.addLayout(text_box, 1)

        up_button = self._tool_button(FluentIcon.UP, '上移一位')
        up_button.setEnabled(can_up)
        up_button.clicked.connect(lambda: self.move_requested.emit(self.site, -1))
        layout.addWidget(up_button)
        down_button = self._tool_button(FluentIcon.DOWN, '下移一位')
        down_button.setEnabled(can_down)
        down_button.clicked.connect(lambda: self.move_requested.emit(self.site, 1))
        layout.addWidget(down_button)

        self.open_button = self._tool_button(FluentIcon.LINK, '打开链接')
        self.open_button.clicked.connect(lambda: self.open_requested.emit(self.site))
        layout.addWidget(self.open_button)

        if site.get('local'):
            edit_button = self._tool_button(FluentIcon.EDIT, '编辑站点')
            edit_button.clicked.connect(lambda: self.edit_requested.emit(self.site))
            layout.addWidget(edit_button)
            delete_button = self._tool_button(FluentIcon.DELETE, '删除站点')
            delete_button.clicked.connect(lambda: self.delete_requested.emit(self.site))
            layout.addWidget(delete_button)

    def _tool_button(self, icon, tooltip: str) -> TransparentToolButton:
        button = TransparentToolButton(icon, self)
        button.setFixedSize(24, 24)
        button.setIconSize(QSize(12, 12))
        button.setToolTip(tooltip)
        return button

    def _apply_status(self, reachable) -> None:
        self.status_label.set_state(reachable)

    def set_status(self, reachable) -> None:
        self._apply_status(reachable)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.copy_requested.emit(self.site)
        super().mouseReleaseEvent(event)


class SiteDialog(MessageBoxBase):
    """新增 / 编辑本地站点。"""

    def __init__(self, parent=None, site: Optional[dict] = None):
        super().__init__(parent)
        self.site = site
        self.widget.setFixedWidth(520)
        self.yesButton.setText('保存')
        self.cancelButton.setText('取消')
        self.viewLayout.setSpacing(10)
        self.viewLayout.addWidget(SubtitleLabel('编辑站点' if site else '新增站点', self))

        hint = CaptionLabel('本地站点标记为「本地」，拉取远端配置时不会被覆盖。', self)
        hint.setWordWrap(True)
        hint.setTextColor('#5f6b7a', '#8d95a5')
        self.viewLayout.addWidget(hint)

        self.kind_combo = ComboBox(self)
        self.kind_combo.addItems(['电影', '书籍'])
        self.kind_combo.setCurrentIndex(
            1 if site and site.get('kind') == explore.BOOK else 0)
        self.viewLayout.addWidget(BodyLabel('类型', self))
        self.viewLayout.addWidget(self.kind_combo)

        self.viewLayout.addWidget(BodyLabel('站点名称', self))
        self.name_edit = LineEdit(self)
        self.name_edit.setPlaceholderText('例如：我的 NAS 片库')
        self.name_edit.setText((site or {}).get('name', ''))
        self.viewLayout.addWidget(self.name_edit)

        self.viewLayout.addWidget(BodyLabel('站点链接', self))
        self.url_edit = LineEdit(self)
        self.url_edit.setPlaceholderText('https://example.com/movies')
        self.url_edit.setText((site or {}).get('url', ''))
        self.viewLayout.addWidget(self.url_edit)

        self.viewLayout.addWidget(BodyLabel('搜索链接（可选）', self))
        self.search_edit = LineEdit(self)
        self.search_edit.setPlaceholderText('https://example.com/search?q={title}')
        self.search_edit.setText((site or {}).get('search_url', ''))
        self.viewLayout.addWidget(self.search_edit)

    def values(self) -> dict:
        return {
            'name': self.name_edit.text().strip(),
            'url': self.url_edit.text().strip(),
            'search_url': self.search_edit.text().strip(),
            'kind': explore.BOOK if self.kind_combo.currentIndex() == 1 else explore.MOVIE,
        }

    def validate(self) -> bool:
        values = self.values()
        if not values['name']:
            InfoBar.warning('缺少名称', '请填写站点名称。', parent=self,
                            position=InfoBarPosition.TOP, duration=4000)
            return False
        if not values['url'].startswith(('http://', 'https://')):
            InfoBar.warning('链接无效', '请填写以 http(s):// 开头的站点链接。', parent=self,
                            position=InfoBarPosition.TOP, duration=4000)
            return False
        return True


class ExplorePage(QWidget):
    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.setObjectName('explorePage')
        self.state = explore.load_state(db)
        # 复用启动时（或上一次）检测到的结果，页面一打开就能显示圆点。
        self.status: dict[str, bool] = {}
        for site in self.state.get('sites') or []:
            reachable = explore.get_reachability(site.get('id'))
            if reachable is not None:
                self.status[site['id']] = reachable
        self.pull_worker = None
        self._pending_check = False
        self._service = connectivity_service()
        self._service.started.connect(self._on_check_started)
        self._service.progress.connect(self._on_check_progress)
        self._service.finished.connect(self._on_checked)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 26, 32, 0)
        outer.setSpacing(12)
        outer.addWidget(TitleLabel('探索空间', self))
        subtitle = BodyLabel('资源站点由远端配置下发，也可以添加自己的本地站点。', self)
        subtitle.setTextColor('#5f6b7a', '#8d95a5')
        outer.addWidget(subtitle)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        self.pull_button = PrimaryPushButton(FluentIcon.CLOUD, '拉取配置', self)
        self.pull_button.clicked.connect(self.pull_config)
        toolbar.addWidget(self.pull_button)
        self.check_button = PushButton(FluentIcon.SYNC, '检查连通性', self)
        self.check_button.clicked.connect(self.check_connectivity)
        toolbar.addWidget(self.check_button)
        self.add_button = PushButton(FluentIcon.ADD, '新增站点', self)
        self.add_button.clicked.connect(self.add_site)
        toolbar.addWidget(self.add_button)
        toolbar.addStretch(1)
        self.hint_label = CaptionLabel('', self)
        self.hint_label.setTextColor('#5f6b7a', '#8d95a5')
        toolbar.addWidget(self.hint_label)
        outer.addLayout(toolbar)

        self.scroll = SmoothScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.enableTransparentBackground()
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.container = QWidget()
        self.container.setStyleSheet('background:transparent;')
        self.body = QVBoxLayout(self.container)
        self.body.setContentsMargins(0, 4, 0, 28)
        self.body.setSpacing(14)
        self.scroll.setWidget(self.container)
        outer.addWidget(self.scroll, 1)

        self._build()
        if self._service.is_running():
            self.check_button.setEnabled(False)
            self.hint_label.setText('检测中…')
        qconfig.themeChangedFinished.connect(self._on_theme_changed)

    # ---------- 界面 ----------

    def _clear(self) -> None:
        while self.body.count():
            item = self.body.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def _build(self) -> None:
        self._clear()
        self.rows: dict[str, SiteRow] = {}
        self.body.addWidget(self._config_card())

        for kind, title in ((explore.MOVIE, '电影'), (explore.BOOK, '书籍')):
            sites = explore.sites_for(self.state, kind)
            self.body.addWidget(self._section_title(f'{title} · {len(sites)} 个站点'))
            card = CardWidget(self.container)
            card.setBorderRadius(14)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(10, 8, 10, 8)
            card_layout.setSpacing(4)
            if not sites:
                empty = CaptionLabel('还没有站点，点击「新增站点」添加。', card)
                empty.setTextColor('#5f6b7a', '#8d95a5')
                card_layout.addWidget(empty)
            for index, site in enumerate(sites):
                row = SiteRow(site, self.status.get(site['id']), card,
                              can_up=index > 0, can_down=index < len(sites) - 1)
                row.copy_requested.connect(self.copy_link)
                row.open_requested.connect(self.open_link)
                row.edit_requested.connect(self.edit_site)
                row.delete_requested.connect(self.delete_site)
                row.move_requested.connect(self.move_site)
                self.rows[site['id']] = row
                card_layout.addWidget(row)
            self.body.addWidget(card)

        tips = CaptionLabel('点击任意站点复制完整链接；点击 ↑ / ↓ 调整顺序；'
                            '「拉取」从远端更新站点并按配置顺序排列（本地站点保持原位），'
                            '「检查连通性」只检测不改配置。', self.container)
        tips.setWordWrap(True)
        tips.setTextColor('#5f6b7a', '#8d95a5')
        self.body.addWidget(tips)
        self.body.addStretch(1)
        self._refresh_hint()

    def _section_title(self, text: str) -> QLabel:
        label = StrongBodyLabel(text, self.container)
        label.setContentsMargins(2, 6, 0, 0)
        return label

    def _config_card(self) -> CardWidget:
        card = CardWidget(self.container)
        card.setBorderRadius(14)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(8)

        source_row = QHBoxLayout()
        source_row.addWidget(BodyLabel('配置来源', card))
        source_row.addStretch(1)
        sources = ' / '.join(source['name'] for source in explore.REMOTE_SOURCES)
        source_label = CaptionLabel(sources, card)
        source_label.setTextColor('#5f6b7a', '#8d95a5')
        source_row.addWidget(source_label)
        layout.addLayout(source_row)

        status_row = QHBoxLayout()
        status_row.addWidget(BodyLabel('配置状态', card))
        status_row.addStretch(1)
        version = self.state.get('version') or '—'
        last_source = self.state.get('last_source') or '等待首次拉取'
        self.config_status = CaptionLabel(f'v{version} · {last_source}', card)
        self.config_status.setTextColor('#5f6b7a', '#8d95a5')
        status_row.addWidget(self.config_status)
        layout.addLayout(status_row)
        return card

    def _refresh_hint(self) -> None:
        sites = self.state.get('sites') or []
        total = len(sites)
        if not self.status:
            self.hint_label.setText(f'{total} 个站点 · 待检测' if total else '暂无站点')
            return
        reachable = sum(1 for value in self.status.values() if value)
        self.hint_label.setText(f'{reachable}/{total} 可访问')

    # ---------- 操作 ----------

    def copy_link(self, site: dict) -> None:
        QApplication.clipboard().setText(site['url'])
        InfoBar.success('已复制', site['url'], parent=self,
                        position=InfoBarPosition.TOP, duration=3000)

    def open_link(self, site: dict) -> None:
        QDesktopServices.openUrl(QUrl(site['url']))

    def add_site(self) -> None:
        dialog = SiteDialog(self.window())
        if dialog.exec():
            values = dialog.values()
            explore.add_site(self.db, self.state, **values)
            self._build()
            InfoBar.success('站点已添加', values['name'], parent=self,
                            position=InfoBarPosition.TOP, duration=3000)
            self.check_connectivity()

    def edit_site(self, site: dict) -> None:
        dialog = SiteDialog(self.window(), site)
        if dialog.exec():
            values = dialog.values()
            explore.update_site(self.db, self.state, site['id'], **values)
            self.status.pop(site['id'], None)
            self._build()

    def move_site(self, site: dict, delta: int) -> None:
        bar = self.scroll.verticalScrollBar()
        position = bar.value()
        if explore.move_site(self.db, self.state, site['id'], delta):
            self._build()
            bar.setValue(position)

    def delete_site(self, site: dict) -> None:
        explore.delete_site(self.db, self.state, site['id'])
        self.status.pop(site['id'], None)
        self._build()
        InfoBar.success('站点已删除', site['name'], parent=self,
                        position=InfoBarPosition.TOP, duration=3000)

    def pull_config(self) -> None:
        if self.pull_worker is not None and self.pull_worker.isRunning():
            return
        self.pull_button.setEnabled(False)
        self.pull_worker = _PullWorker(self)
        self.pull_worker.done.connect(self._on_pulled)
        self.pull_worker.failed.connect(self._on_pull_failed)
        self.pull_worker.finished.connect(lambda: self.pull_button.setEnabled(True))
        self.pull_worker.start()

    def _on_pulled(self, config: dict, source: str) -> None:
        count = explore.merge_remote(self.state, config)
        self.state['last_source'] = f'{source} · sites.json'
        from datetime import datetime
        self.state['pulled_at'] = datetime.now().strftime('%Y-%m-%d %H:%M')
        explore.save_state(self.db, self.state)
        self.status.clear()
        explore.clear_reachability()
        self._build()
        InfoBar.success('配置已更新', f'来自 {source}，共 {len(self.state.get("sites") or [])} 个站点',
                        parent=self, position=InfoBarPosition.TOP, duration=4000)
        self.check_connectivity()

    def _on_pull_failed(self, message: str) -> None:
        InfoBar.error('拉取失败', message, parent=self,
                      position=InfoBarPosition.TOP, duration=6000)

    def check_connectivity(self) -> None:
        """手动检测：交给全局服务；若已有检测在跑，等它结束后用最新站点补测一次。"""
        sites = self.state.get('sites') or []
        if not sites:
            return
        self.check_button.setEnabled(False)
        self.hint_label.setText('检测中…')
        if self._service.is_running():
            self._pending_check = True
            return
        self._service.start(sites=sites)

    def _on_check_started(self) -> None:
        self.check_button.setEnabled(False)
        self.hint_label.setText('检测中…')

    def _on_check_progress(self, done: int, total: int) -> None:
        self.hint_label.setText(f'检测中… {done}/{total}')

    def _on_checked(self, result: dict) -> None:
        self.status.update({key: bool(value) for key, value in result.items()})
        for site_id, row in self.rows.items():
            if site_id in self.status:
                row.set_status(self.status[site_id])
        self.check_button.setEnabled(True)
        self._refresh_hint()
        if self._pending_check:
            # 检测期间站点被增删或重新拉取过：用最新站点再补测一次。
            self._pending_check = False
            self.check_connectivity()

    def _on_theme_changed(self) -> None:
        # 行内链接颜色跟随主题，重建一次即可
        self._build()
