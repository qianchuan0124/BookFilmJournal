from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QPainter,
    QPainterPath,
    QPixmap,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    MessageBox,
    PushButton,
    SmoothScrollArea,
    SwitchButton,
    Theme,
    TitleLabel,
    isDarkTheme,
    setTheme,
)

from app.cloud_sync import (
    SYNC_SUBDIR,
    SyncError,
    base_dir,
    check_sync,
    create_snapshot,
    detected_dirs,
    restore_snapshot,
)
from app.config import APP_VERSION, APP_VERSION_TEXT, OUTPUT_DIR, ROOT
from app.db import Database
from app.douban import cover_file_ok, fetch_cover
from app.excel import ExcelError, default_export_name, export_excel, import_excel
from app.media_meta import book_pages, ensure_subject_meta
from app.profile import (
    DEFAULT_NAME,
    avatar_source,
    clear_custom_avatar,
    douban_name,
    effective_avatar,
    effective_name,
    name_source,
    save_custom_avatar,
)
from app.ui.dialogs import CalibreInstallDialog, WordCountDialog
from app.ui.styles import primary_button
from app.words import find_calibre, format_word_count

AVATAR_SIZE = 64


def _circular_pixmap(path: str, size: int = AVATAR_SIZE) -> QPixmap:
    source = QPixmap()
    if path:
        source.load(str(path))
    if source.isNull():
        return QPixmap()
    scaled = source.scaled(size, size, Qt.KeepAspectRatioByExpanding,
                           Qt.SmoothTransformation)
    canvas = QPixmap(size, size)
    canvas.fill(Qt.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.Antialiasing)
    clip = QPainterPath()
    clip.addEllipse(0, 0, size, size)
    painter.setClipPath(clip)
    painter.drawPixmap((size - scaled.width()) // 2, (size - scaled.height()) // 2, scaled)
    painter.end()
    return canvas


def _initial_pixmap(text: str, size: int = AVATAR_SIZE) -> QPixmap:
    canvas = QPixmap(size, size)
    canvas.fill(Qt.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.Antialiasing)
    clip = QPainterPath()
    clip.addEllipse(0, 0, size, size)
    painter.fillPath(clip, QColor(229, 180, 95, 46))
    painter.setPen(QColor(229, 180, 95, 130))
    painter.drawPath(clip)
    font = QFont('Songti SC')
    font.setPixelSize(int(size * 0.42))
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QColor('#e5b45f'))
    painter.drawText(canvas.rect(), Qt.AlignCenter, (text or '我')[:1])
    painter.end()
    return canvas


def _missing_cover_rows(db: Database) -> list[tuple[str, object]]:
    """返回封面文件缺失、需要重新下载的记录 / 豆列条目 / 计划条目。"""
    rows = [('record', row) for row in db.records_with_cover_url()]
    rows.extend(('doulist', row) for row in db.doulist_items_with_cover_url())
    rows.extend(('plan', row) for row in db.plan_items_with_cover_url())
    todo = []
    for kind, row in rows:
        cover_path = row['cover_path'] or ''
        if cover_path:
            path = Path(cover_path)
            if not path.is_absolute():
                path = ROOT / path
            if cover_file_ok(path):
                continue
        todo.append((kind, row))
    return todo


def missing_metadata_counts(db: Database) -> tuple[int, int]:
    """统计缺失的封面张数与书籍页数，用于恢复后提示是否补全。"""
    covers = len(_missing_cover_rows(db))
    pages = sum(1 for subject in db.book_subjects_for_meta()
                if book_pages(subject['subject_id']) is None)
    return covers, pages


class BackfillWorker(QThread):
    progress = Signal(str, int, int)
    done = Signal(int, int, int)

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db

    def _cover_rows(self) -> list[tuple[str, object]]:
        return _missing_cover_rows(self.db)

    def _backfill_covers(self) -> int:
        todo = self._cover_rows()
        downloaded = 0
        for index, (kind, row) in enumerate(todo, 1):
            self.progress.emit(f'正在补全封面 {index}/{len(todo)}…', index, len(todo))
            path = fetch_cover(
                row['cover_url'], row['media_type'], row['subject_id'],
                row['url'] or '',
            )
            if not path:
                continue
            if kind == 'record':
                self.db.set_record_cover(int(row['id']), path)
            elif kind == 'doulist':
                self.db.set_doulist_item_cover(int(row['id']), path)
            else:
                self.db.set_plan_item_cover(int(row['id']), path)
            downloaded += 1
        return downloaded

    def _backfill_pages(self) -> tuple[int, int]:
        missing = [subject for subject in self.db.book_subjects_for_meta()
                   if book_pages(subject['subject_id']) is None]
        if not missing:
            return 0, 0
        result = ensure_subject_meta(
            'book', missing,
            progress=lambda message, index, total: self.progress.emit(message, index, total),
        )
        return int(result.get('fetched', 0)), int(result.get('failed', 0))

    def run(self) -> None:
        covers = self._backfill_covers()
        pages, failed = self._backfill_pages()
        self.done.emit(covers, pages, failed)


class DataPage(QWidget):
    records_changed = Signal()

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.setObjectName('dataPage')
        self._dirty = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 26, 32, 0)
        outer.setSpacing(12)
        outer.addWidget(TitleLabel('应用设置', self))
        subtitle = BodyLabel('外观、Excel 备份与恢复、本地书籍字数统计、登录与本机状态管理。', self)
        subtitle.setTextColor('#5f6b7a', '#8d95a5')
        outer.addWidget(subtitle)

        self.scroll = SmoothScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.enableTransparentBackground()
        self.scroll.setFrameShape(QFrame.NoFrame)
        container = QWidget()
        container.setStyleSheet('background:transparent;')
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 26)
        layout.setSpacing(16)
        self.scroll.setWidget(container)
        outer.addWidget(self.scroll, 1)

        self._build_account_card(layout)

        theme_card = CardWidget(self)
        theme_card.setBorderRadius(14)
        theme_layout = QVBoxLayout(theme_card)
        theme_layout.setContentsMargins(22, 20, 22, 20)
        theme_layout.setSpacing(10)
        theme_layout.addWidget(BodyLabel('外观', theme_card))
        theme_hint = CaptionLabel('在暗色与亮色皮肤之间切换，设置会保存并在下次启动时生效。', theme_card)
        theme_hint.setWordWrap(True)
        theme_hint.setTextColor('#5f6b7a', '#8d95a5')
        theme_layout.addWidget(theme_hint)
        theme_row = QHBoxLayout()
        self.light_switch = SwitchButton(theme_card)
        self.light_switch.setOnText('亮色')
        self.light_switch.setOffText('暗色')
        self.light_switch.setChecked(not isDarkTheme())
        self.light_switch.checkedChanged.connect(self._on_theme_switch)
        theme_row.addWidget(self.light_switch)
        theme_row.addStretch(1)
        theme_layout.addLayout(theme_row)
        layout.addWidget(theme_card)

        excel_card = CardWidget(self)
        excel_card.setBorderRadius(14)
        excel_layout = QVBoxLayout(excel_card)
        excel_layout.setContentsMargins(22, 20, 22, 20)
        excel_layout.setSpacing(10)
        excel_layout.addWidget(BodyLabel('Excel 导入 / 导出', excel_card))
        excel_hint = CaptionLabel(
            '导出全部记录、豆列、计划与回收站；导入按「类型 + 豆瓣ID + 状态」更新或新增，不删除现有数据。',
            excel_card,)
        excel_hint.setWordWrap(False)
        excel_hint.setTextColor('#5f6b7a', '#8d95a5')
        excel_layout.addWidget(excel_hint)
        excel_row = QHBoxLayout()
        excel_row.setSpacing(10)
        self.export_button = primary_button(FluentIcon.SAVE, '导出 Excel', excel_card)
        self.export_button.clicked.connect(self.export_data)
        excel_row.addWidget(self.export_button)
        self.import_button = PushButton(FluentIcon.FOLDER, '从 Excel 导入', excel_card)
        self.import_button.clicked.connect(self.import_data)
        excel_row.addWidget(self.import_button)
        excel_row.addStretch(1)
        excel_layout.addLayout(excel_row)
        self.excel_status = CaptionLabel('导出文件默认保存在 output/exports。', excel_card)
        self.excel_status.setWordWrap(True)
        self.excel_status.setTextColor('#5f6b7a', '#8d95a5')
        excel_layout.addWidget(self.excel_status)
        layout.addWidget(excel_card)

        sync_card = CardWidget(self)
        sync_card.setBorderRadius(14)
        sync_layout = QVBoxLayout(sync_card)
        sync_layout.setContentsMargins(22, 20, 22, 20)
        sync_layout.setSpacing(10)
        sync_layout.addWidget(BodyLabel('云同步（iCloud / OneDrive）', sync_card))
        sync_hint = CaptionLabel(
            '应用内不支持登录 iCloud：请先在系统设置登录并开启 iCloud 云盘，'
            'Windows 需安装 iCloud for Windows。备份与恢复都需要手动点击按钮触发，'
            '应用不会自动上传或覆盖数据。快照不包含登录 Cookie；'
            '封面与页数不随快照同步，恢复后会提示从豆瓣补全。',
            sync_card,)
        sync_hint.setWordWrap(True)
        sync_hint.setTextColor('#5f6b7a', '#8d95a5')
        sync_layout.addWidget(sync_hint)
        self.sync_status = CaptionLabel('', sync_card)
        self.sync_status.setWordWrap(True)
        self.sync_status.setTextColor('#5f6b7a', '#8d95a5')
        sync_layout.addWidget(self.sync_status)
        sync_row = QHBoxLayout()
        sync_row.setSpacing(10)
        self.sync_dir_button = PushButton(FluentIcon.FOLDER, '选择同步目录', sync_card)
        self.sync_dir_button.clicked.connect(self.choose_sync_dir)
        sync_row.addWidget(self.sync_dir_button)
        self.sync_backup_button = primary_button(FluentIcon.CLOUD, '立即备份', sync_card)
        self.sync_backup_button.clicked.connect(self.backup_to_sync)
        sync_row.addWidget(self.sync_backup_button)
        self.sync_restore_button = PushButton(FluentIcon.CLOUD_DOWNLOAD, '检查并恢复', sync_card)
        self.sync_restore_button.clicked.connect(self.restore_from_sync)
        sync_row.addWidget(self.sync_restore_button)
        sync_row.addStretch(1)
        sync_layout.addLayout(sync_row)
        layout.addWidget(sync_card)

        word_card = CardWidget(self)
        word_card.setBorderRadius(14)
        word_layout = QVBoxLayout(word_card)
        word_layout.setContentsMargins(22, 20, 22, 20)
        word_layout.setSpacing(10)
        word_layout.addWidget(BodyLabel('书籍字数统计', word_card))
        self.word_status = CaptionLabel('', word_card)
        self.word_status.setWordWrap(True)
        self.word_status.setTextColor('#5f6b7a', '#8d95a5')
        word_layout.addWidget(self.word_status)
        self.calibre_label = CaptionLabel('', word_card)
        self.calibre_label.setWordWrap(True)
        word_layout.addWidget(self.calibre_label)
        word_row = QHBoxLayout()
        self.word_button = primary_button(FluentIcon.DOCUMENT, '打开字数统计', word_card)
        self.word_button.clicked.connect(self.open_word_count)
        word_row.addWidget(self.word_button)
        self.calibre_download_button = PushButton(FluentIcon.DOWNLOAD, '安装 Calibre', word_card)
        self.calibre_download_button.clicked.connect(self.open_calibre_install)
        word_row.addWidget(self.calibre_download_button)
        word_row.addStretch(1)
        word_layout.addLayout(word_row)
        layout.addWidget(word_card)

        login_card = CardWidget(self)
        login_card.setBorderRadius(14)
        login_layout = QVBoxLayout(login_card)
        login_layout.setContentsMargins(22, 20, 22, 20)
        login_layout.setSpacing(10)
        login_layout.addWidget(BodyLabel('豆瓣登录', login_card))
        self.login_status = CaptionLabel('', login_card)
        self.login_status.setWordWrap(True)
        self.login_status.setTextColor('#5f6b7a', '#8d95a5')
        login_layout.addWidget(self.login_status)
        login_row = QHBoxLayout()
        self.clear_login_button = PushButton('清除已保存的 Cookie', login_card)
        self.clear_login_button.clicked.connect(self.clear_login)
        login_row.addWidget(self.clear_login_button)
        login_row.addStretch(1)
        login_layout.addLayout(login_row)
        layout.addWidget(login_card)

        state_card = CardWidget(self)
        state_card.setBorderRadius(14)
        state_layout = QVBoxLayout(state_card)
        state_layout.setContentsMargins(22, 20, 22, 20)
        state_layout.setSpacing(10)
        state_layout.addWidget(BodyLabel('本地状态', state_card))
        state_hint = CaptionLabel(
            '清除彻底删除标记与换源忽略记录，清除后豆瓣同步可能重新加回被彻底删除的条目。',
            state_card,)
        state_hint.setWordWrap(False)
        state_hint.setTextColor('#5f6b7a', '#8d95a5')
        state_layout.addWidget(state_hint)
        state_row = QHBoxLayout()
        self.clear_state_button = PushButton('清空本地配置', state_card)
        self.clear_state_button.clicked.connect(self.clear_local_state)
        state_row.addWidget(self.clear_state_button)
        state_row.addStretch(1)
        state_layout.addLayout(state_row)
        layout.addWidget(state_card)

        about_card = CardWidget(self)
        about_card.setBorderRadius(14)
        about_layout = QVBoxLayout(about_card)
        about_layout.setContentsMargins(22, 20, 22, 20)
        about_layout.setSpacing(10)
        about_layout.addWidget(BodyLabel('关于', about_card))
        self.about_label = CaptionLabel(
            f'书影记录 {APP_VERSION_TEXT} · 数据目录 {ROOT}', about_card
        )
        self.about_label.setWordWrap(True)
        self.about_label.setTextColor('#5f6b7a', '#8d95a5')
        about_layout.addWidget(self.about_label)
        about_row = QHBoxLayout()
        open_dir_button = PushButton(FluentIcon.FOLDER, '打开数据目录', about_card)
        open_dir_button.clicked.connect(self.open_data_dir)
        about_row.addWidget(open_dir_button)
        about_row.addStretch(1)
        about_layout.addLayout(about_row)
        layout.addWidget(about_card)

        layout.addStretch(1)
        # 首次显示时再读取设置页数据，避免启动阶段无谓查询。
        self._dirty = True

    def _build_account_card(self, layout: QVBoxLayout) -> None:
        card = CardWidget(self)
        card.setBorderRadius(14)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(22, 20, 22, 20)
        card_layout.setSpacing(10)
        card_layout.addWidget(BodyLabel('用户账号', card))
        hint = CaptionLabel(
            '头像与名称会用于生成的报告和计划分享图。留空时优先使用豆瓣资料，没有豆瓣资料则显示「我」。',
            card,
        )
        hint.setWordWrap(True)
        hint.setTextColor('#5f6b7a', '#8d95a5')
        card_layout.addWidget(hint)

        row = QHBoxLayout()
        row.setSpacing(18)
        self.avatar_view = QLabel(card)
        self.avatar_view.setFixedSize(AVATAR_SIZE, AVATAR_SIZE)
        self.avatar_view.setAlignment(Qt.AlignCenter)
        avatar_col = QVBoxLayout()
        avatar_col.setSpacing(4)
        avatar_col.addWidget(self.avatar_view, 0, Qt.AlignHCenter)
        self.avatar_source_label = CaptionLabel('', card)
        self.avatar_source_label.setAlignment(Qt.AlignCenter)
        self.avatar_source_label.setTextColor('#5f6b7a', '#8d95a5')
        avatar_col.addWidget(self.avatar_source_label, 0, Qt.AlignHCenter)
        row.addLayout(avatar_col, 0)
        row.setAlignment(avatar_col, Qt.AlignTop)

        fields = QVBoxLayout()
        fields.setSpacing(6)
        fields.setAlignment(Qt.AlignLeft)
        fields.addWidget(CaptionLabel('显示名称', card), 0, Qt.AlignLeft)
        self.account_name_edit = LineEdit(card)
        self.account_name_edit.setPlaceholderText('留空则使用豆瓣昵称，没有则显示「我」')
        self.account_name_edit.setClearButtonEnabled(True)
        self.account_name_edit.setMaxLength(10)
        self.account_name_edit.setFixedWidth(240)
        self.account_name_edit.setAlignment(Qt.AlignLeft)
        self.account_name_edit.editingFinished.connect(self._on_name_edit_finished)
        self.name_counter = CaptionLabel('还可输入 10 个字', card)
        self.name_counter.setTextColor('#e5b45f', '#e5b45f')
        counter_font = self.name_counter.font()
        counter_font.setPixelSize(10)
        self.name_counter.setFont(counter_font)
        self.name_counter.hide()
        self._name_counter_timer = QTimer(self)
        self._name_counter_timer.setSingleShot(True)
        self._name_counter_timer.timeout.connect(self._hide_name_counter)
        self.account_name_edit.textChanged.connect(self._update_name_counter)
        name_row = QHBoxLayout()
        name_row.setContentsMargins(0, 0, 0, 0)
        name_row.setSpacing(12)
        name_row.addWidget(self.account_name_edit, 0, Qt.AlignBottom)
        name_row.addWidget(self.name_counter, 0, Qt.AlignBottom)
        name_row.addStretch(1)
        fields.addLayout(name_row)
        self.account_status = CaptionLabel('', card)
        self.account_status.setWordWrap(True)
        self.account_status.setAlignment(Qt.AlignLeft)
        self.account_status.setTextColor('#5f6b7a', '#8d95a5')
        fields.addWidget(self.account_status, 0, Qt.AlignLeft)
        row.addLayout(fields, 1)
        card_layout.addLayout(row)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self.save_account_button = primary_button(FluentIcon.SAVE, '保存名称', card)
        self.save_account_button.clicked.connect(self.save_account_name)
        buttons.addWidget(self.save_account_button)
        self.avatar_button = PushButton(FluentIcon.PHOTO, '选择头像', card)
        self.avatar_button.clicked.connect(self.choose_avatar)
        buttons.addWidget(self.avatar_button)
        self.reset_account_button = PushButton('恢复默认', card)
        self.reset_account_button.clicked.connect(self.reset_account)
        buttons.addWidget(self.reset_account_button)
        buttons.addStretch(1)
        card_layout.addLayout(buttons)
        layout.addWidget(card)

    def refresh_account(self) -> None:
        self.account_name_edit.blockSignals(True)
        self.account_name_edit.setText(effective_name(self.db))
        self.account_name_edit.blockSignals(False)
        self._hide_name_counter()
        avatar_path = effective_avatar(self.db)
        pixmap = _circular_pixmap(avatar_path) if avatar_path else QPixmap()
        if pixmap.isNull():
            pixmap = _initial_pixmap(effective_name(self.db))
        self.avatar_view.setPixmap(pixmap)
        labels = {'custom': '自定义', 'douban': '豆瓣', 'default': '默认'}
        avatar_labels = {'custom': '自定义头像', 'douban': '豆瓣头像', 'default': '默认头像'}
        name_from = labels.get(name_source(self.db), '默认')
        self.avatar_source_label.setText(
            avatar_labels.get(avatar_source(self.db), '默认头像')
        )
        status = f'名称来源：{name_from}'
        self.account_status.setText(status)

    def _update_name_counter(self) -> None:
        remaining = max(0, 10 - len(self.account_name_edit.text()))
        self.name_counter.setText(
            f'还可输入 {remaining} 个字' if remaining else '已达 10 个字上限'
        )
        self.name_counter.show()
        self._name_counter_timer.start(1500)
        name = self.account_name_edit.text().strip()[:10]
        fallback = douban_name(self.db) or DEFAULT_NAME
        if name and name != fallback:
            source = 'custom'
        elif douban_name(self.db):
            source = 'douban'
        else:
            source = 'default'
        labels = {'custom': '自定义', 'douban': '豆瓣', 'default': '默认'}
        self.account_status.setText(f'名称来源：{labels[source]}')

    def _hide_name_counter(self) -> None:
        self._name_counter_timer.stop()
        self.name_counter.hide()

    def _write_account_name(self, name: str) -> None:
        fallback = douban_name(self.db) or DEFAULT_NAME
        if not name or name == fallback:
            self.db.delete_setting('display_name')
            self.db.delete_setting('display_name_custom')
        else:
            self.db.set_setting('display_name', name)
            self.db.set_setting('display_name_custom', '1')

    def _on_name_edit_finished(self) -> None:
        self._hide_name_counter()
        name = self.account_name_edit.text().strip()[:10]
        fallback = douban_name(self.db) or DEFAULT_NAME
        is_custom = bool(name) and name != fallback
        stored_custom = self.db.get_setting('display_name_custom') == '1'
        stored_name = self.db.get_setting('display_name')
        if is_custom:
            changed = not (stored_custom and stored_name == name)
        else:
            changed = stored_custom or bool(stored_name)
        if changed:
            self._write_account_name(name)
            self.refresh_account()

    def save_account_name(self) -> None:
        name = self.account_name_edit.text().strip()[:10]
        self._write_account_name(name)
        self.refresh_account()
        InfoBar.success('名称已保存', '之后生成的报告会使用新的署名。', parent=self,
                        position=InfoBarPosition.TOP, duration=4000)

    def choose_avatar(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, '选择头像', str(Path.home()),
            '图片文件 (*.png *.jpg *.jpeg *.webp *.bmp)',
        )
        if not path:
            return
        try:
            saved = save_custom_avatar(path)
        except OSError as exc:
            InfoBar.error('头像保存失败', str(exc), parent=self,
                          position=InfoBarPosition.TOP, duration=5000)
            return
        self.db.set_setting('display_avatar', saved)
        self.refresh_account()
        InfoBar.success('头像已更新', '之后生成的报告会使用新的头像。', parent=self,
                        position=InfoBarPosition.TOP, duration=4000)

    def reset_account(self) -> None:
        self.db.delete_setting('display_name')
        self.db.delete_setting('display_name_custom')
        self.db.delete_setting('display_avatar')
        clear_custom_avatar()
        self.refresh_account()
        InfoBar.success('已恢复默认', '将优先使用豆瓣资料，没有豆瓣资料则显示「我」。', parent=self,
                        position=InfoBarPosition.TOP, duration=4000)

    def _on_theme_switch(self, checked: bool) -> None:
        setTheme(Theme.LIGHT if checked else Theme.DARK, lazy=True)
        self.db.set_setting('theme', 'light' if checked else 'dark')

    def mark_dirty(self) -> None:
        if self.isVisible():
            self.refresh()
        else:
            self._dirty = True

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._dirty:
            self._dirty = False
            self.refresh()

    def refresh(self) -> None:
        self.refresh_account()
        self.refresh_calibre()
        self.refresh_sync()
        books = self.db.list_all_records('book')
        counts = self.db.word_count_map('book')
        subject_ids = {str(row['subject_id']) for row in books} | set(counts)
        with_words = [subject_id for subject_id in subject_ids
                      if counts.get(subject_id, 0) > 0]
        total_words = sum(counts.get(subject_id, 0) for subject_id in with_words)
        if with_words:
            self.word_status.setText(
                f'已统计 {len(with_words)}/{len(subject_ids)} 本书，'
                f'共 {format_word_count(total_words)}。'
            )
        else:
            self.word_status.setText('还没有统计过书籍字数。选择 Calibre 书库或电子书目录后开始统计。')
        cookie = self.db.get_setting('douban_cookie')
        user_id = self.db.get_setting('douban_user_id')
        if cookie:
            self.login_status.setText(f'已保存登录 Cookie（用户 ID：{user_id or "未知"}），同步时可直接复用。')
        else:
            self.login_status.setText('未保存登录 Cookie。可在同步窗口扫码登录，并勾选「记住登录」。')

    def export_data(self) -> None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        default_path = OUTPUT_DIR / 'exports' / default_export_name()
        default_path.parent.mkdir(parents=True, exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(
            self, '导出 Excel', str(default_path), 'Excel 文件 (*.xlsx)'
        )
        if not path:
            return
        try:
            result = export_excel(self.db, path)
        except Exception as exc:
            InfoBar.error('导出失败', str(exc), parent=self, position=InfoBarPosition.TOP, duration=6000)
            return
        recycle_total = sum(result[key] for key in (
            'recycle_records', 'recycle_doulists', 'recycle_doulist_items',
            'recycle_plan_folders', 'recycle_plan_items',
        ))
        message = (f"记录 {result['records']} 条，书籍字数 {result.get('book_words', 0)} 条，"
                   f"豆列 {result['doulists']} 个，"
                   f"豆列条目 {result['doulist_items']} 条，计划 {result['plans']} 条，"
                   f"回收站 {recycle_total} 项，彻底删除 {result.get('purged_records', 0)} 条，"
                   f"忽略条目 {result['ignored_subjects']} 条。")
        self.excel_status.setText(f'已导出：{result["path"]}')
        InfoBar.success('导出完成', message, parent=self, position=InfoBarPosition.TOP, duration=6000)

    def import_data(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, '选择 Excel 文件', str(OUTPUT_DIR), 'Excel 文件 (*.xlsx *.xlsm)'
        )
        if not path:
            return
        try:
            result = import_excel(self.db, path)
        except ExcelError as exc:
            InfoBar.error('导入失败', str(exc), parent=self, position=InfoBarPosition.TOP, duration=6000)
            return
        except Exception as exc:
            InfoBar.error('导入失败', str(exc), parent=self, position=InfoBarPosition.TOP, duration=6000)
            return
        self.refresh()
        self.records_changed.emit()
        self.backfill_metadata()
        errors = result.get('errors') or []
        recycle_total = sum(result.get(key, 0) for key in (
            'recycle_records', 'recycle_doulists', 'recycle_doulist_items',
            'recycle_plan_folders', 'recycle_plan_items',
        ))
        message = (f"记录新增 {result['records_inserted']}，更新 {result['records_updated']}；"
                   f"书籍字数 {result.get('book_words', 0)} 条；"
                   f"计划文件夹 {result.get('plan_folders_added', 0)} 个，"
                   f"计划条目新增 {result['plans_added']}；豆列 {result['doulists']}，"
                   f"豆列条目 {result['doulist_items']}；回收站 {recycle_total} 项；"
                   f"彻底删除 {result.get('purged_records', 0)} 条；"
                   f"忽略条目 {result.get('ignored_subjects', 0)} 条。")
        if errors:
            message += f" {len(errors)} 条提示：" + '；'.join(errors[:2])
        self.excel_status.setText('导入完成：' + message)
        InfoBar.success('导入完成', message, parent=self, position=InfoBarPosition.TOP, duration=8000)

    def backfill_metadata(self, reason: str = 'import') -> None:
        self._backfill_reason = reason
        self._backfill_worker = BackfillWorker(self.db, self)
        self._backfill_worker.progress.connect(self._on_backfill_progress)
        self._backfill_worker.done.connect(self._on_backfill_done)
        self._backfill_worker.start()

    def _on_backfill_progress(self, message: str, index: int, total: int) -> None:
        text = f'{message}（{index}/{total}）' if total else message
        if getattr(self, '_backfill_reason', 'import') == 'restore':
            self.sync_status.setText(text)
        else:
            self.excel_status.setText(text)

    def _on_backfill_done(self, covers: int, pages: int, failed: int = 0) -> None:
        restore = getattr(self, '_backfill_reason', 'import') == 'restore'
        if not restore:
            self.excel_status.setText('导出文件默认保存在 output/exports。')
        self.refresh()
        self.records_changed.emit()
        parts = []
        if covers:
            parts.append(f'封面 {covers} 张')
        if pages:
            parts.append(f'页数 {pages} 条')
        if parts:
            InfoBar.success('信息已补全', '已从豆瓣补全' + '、'.join(parts) + '。', parent=self,
                            position=InfoBarPosition.TOP, duration=5000)
        if failed:
            hint = '可稍后重试。'
            if not (self.db.get_setting('douban_cookie') or '').strip():
                hint = '可登录豆瓣并同步一次，或稍后重试。'
            InfoBar.warning('部分信息补全失败',
                            f'{failed} 条信息未取到，可能触发了豆瓣风控，{hint}',
                            parent=self, position=InfoBarPosition.TOP, duration=6000)

    def refresh_sync(self) -> None:
        try:
            info = check_sync(self.db)
        except Exception as exc:
            self.sync_status.setText(f'同步状态读取失败：{exc}')
            return
        lines = []
        root = base_dir(self.db)
        if root is None:
            lines.append('未检测到 iCloud 云盘或 OneDrive，请选择同步目录。')
        elif not root.is_dir():
            lines.append(f'同步目录不可用：{root}，请重新选择。')
        else:
            folder = root / SYNC_SUBDIR
            suffix = '' if folder.is_dir() else '（尚未创建）'
            candidates = detected_dirs()
            source = ''
            if not (self.db.get_setting('cloud_sync_dir') or '').strip() and candidates:
                source = f'（自动识别为{candidates[0][0]}）'
            lines.append(f'同步目录：{folder}{suffix}{source}')
        manifest = info.get('manifest')
        if manifest:
            if info.get('missing'):
                lines.append(f'最新快照 {manifest.get("snapshot")} 尚未下载完成。')
            else:
                counts = manifest.get('counts') or {}
                lines.append(
                    f'最近备份：{manifest.get("created_at", "未知时间")}'
                    f' · {manifest.get("device", "未知设备")}'
                    f' · 记录 {counts.get("records", 0)} 条'
                    f' · 计划 {counts.get("plan_items", 0)} 条'
                )
        elif info['folder'] and Path(info['folder']).is_dir():
            lines.append('同步目录里还没有备份，点击「立即备份」创建第一份。')
        if manifest and info.get('dirty'):
            lines.append('本地有尚未备份的改动。')
        self.sync_status.setText('\n'.join(lines))

    def choose_sync_dir(self) -> None:
        start = base_dir(self.db) or Path.home()
        folder = QFileDialog.getExistingDirectory(
            self, '选择同步根目录（会自动创建「书影记录」子目录）', str(start)
        )
        if not folder:
            return
        try:
            Path(folder).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            InfoBar.error('无法使用该目录', str(exc), parent=self,
                          position=InfoBarPosition.TOP, duration=6000)
            return
        selected = Path(folder)
        root = selected.parent if selected.name == SYNC_SUBDIR else selected
        self.db.set_setting('cloud_sync_dir', str(root))
        self.refresh_sync()
        InfoBar.success(
            '同步目录已设置', f'{root / SYNC_SUBDIR}',
            parent=self, position=InfoBarPosition.TOP, duration=5000,
        )

    def backup_to_sync(self) -> None:
        try:
            result = create_snapshot(self.db, force=True)
        except SyncError as exc:
            InfoBar.error('备份失败', str(exc), parent=self,
                          position=InfoBarPosition.TOP, duration=6000)
            return
        except Exception as exc:
            InfoBar.error('备份失败', str(exc), parent=self,
                          position=InfoBarPosition.TOP, duration=6000)
            return
        counts = result.get('counts') or {}
        InfoBar.success(
            '已备份到同步目录',
            f"快照 {result['snapshot']} · 记录 {counts.get('records', 0)} 条"
            f" · 计划 {counts.get('plan_items', 0)} 条",
            parent=self, position=InfoBarPosition.TOP, duration=6000,
        )
        self.refresh_sync()

    def restore_from_sync(self) -> None:
        try:
            info = check_sync(self.db)
        except Exception as exc:
            InfoBar.error('读取同步目录失败', str(exc), parent=self,
                          position=InfoBarPosition.TOP, duration=6000)
            return
        manifest = info.get('manifest')
        if not info['folder'] or not manifest:
            InfoBar.warning(
                '没有可恢复的备份',
                '请确认已设置同步目录，并先在另一台设备上执行「立即备份」。',
                parent=self, position=InfoBarPosition.TOP, duration=7000,
            )
            return
        if info.get('missing'):
            InfoBar.warning(
                '备份尚未就绪', '快照文件还没有完整下载到本机，请稍后再试。',
                parent=self, position=InfoBarPosition.TOP, duration=7000,
            )
            return
        counts = manifest.get('counts') or {}
        details = (
            f"快照时间：{manifest.get('created_at', '未知')}\n"
            f"来源设备：{manifest.get('device', '未知')}\n"
            f"记录 {counts.get('records', 0)} 条 · 豆列 {counts.get('doulists', 0)} 个"
            f" · 计划 {counts.get('plan_items', 0)} 条\n\n"
            '恢复会覆盖本地数据库；恢复前会把当前数据库自动备份到 data/backups。'
        )
        if info.get('dirty'):
            details += '\n\n注意：本地有尚未备份的改动，恢复后这些改动将丢失。'
        app_version = manifest.get('app_version')
        if app_version and app_version != APP_VERSION:
            details += (f'\n\n备份来自 v{app_version}（当前 {APP_VERSION_TEXT}），'
                        '版本不同可能无法完全兼容。')
        box = MessageBox('从同步目录恢复', details, self.window())
        box.yesButton.setText('恢复')
        box.cancelButton.setText('取消')
        if not box.exec():
            return
        try:
            result = restore_snapshot(self.db, manifest)
        except SyncError as exc:
            InfoBar.error('恢复失败', str(exc), parent=self,
                          position=InfoBarPosition.TOP, duration=6000)
            return
        except Exception as exc:
            InfoBar.error('恢复失败', str(exc), parent=self,
                          position=InfoBarPosition.TOP, duration=6000)
            return
        self.refresh()
        self.records_changed.emit()
        restored = result.get('counts') or {}
        InfoBar.success(
            '恢复完成',
            f"已恢复 {restored.get('records', 0)} 条记录；旧数据库备份在 {result['local_backup']}",
            parent=self, position=InfoBarPosition.TOP, duration=8000,
        )
        covers, pages = missing_metadata_counts(self.db)
        if not covers and not pages:
            return
        missing = []
        if covers:
            missing.append(f'{covers} 张封面')
        if pages:
            missing.append(f'{pages} 本书的页数')
        box = MessageBox(
            '补全封面与页数',
            '快照不包含封面图片和书籍页数，检测到缺少' + '、'.join(missing) + '。\n\n'
            '现在从豆瓣公开页面补全吗？补全无需登录；'
            '如果条目无法访问或触发风控，可登录豆瓣后重试。',
            self.window(),
        )
        box.yesButton.setText('开始补全')
        box.cancelButton.setText('稍后')
        if box.exec():
            self.backfill_metadata(reason='restore')

    def open_data_dir(self) -> None:
        ROOT.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(ROOT)))

    def refresh_calibre(self) -> None:
        calibre = find_calibre()
        if calibre:
            self.calibre_label.setText(f'calibre：{calibre}')
            self.calibre_label.setStyleSheet('color:#7fae68;')
            self.calibre_download_button.hide()
        else:
            self.calibre_label.setText(
                'calibre：未检测到，仅能直接统计 EPUB / TXT / DOCX 字数。'
            )
            self.calibre_label.setStyleSheet('color:#d98a4a;')
            self.calibre_download_button.show()

    def open_calibre_install(self) -> None:
        dialog = CalibreInstallDialog(self.window())
        if dialog.exec() and dialog.installed_path:
            self.refresh_calibre()
            InfoBar.success('Calibre 安装完成', '现在可以统计更多电子书格式的字数。',
                            parent=self, position=InfoBarPosition.TOP, duration=6000)

    def open_word_count(self) -> None:
        dialog = WordCountDialog(self.db, self.window())
        dialog.words_updated.connect(self._on_words_updated)
        dialog.exec()

    def _on_words_updated(self, stats: dict) -> None:
        self.refresh()
        self.records_changed.emit()

    def clear_local_state(self) -> None:
        purged = len(self.db.list_purged_records())
        ignored = len(self.db.list_ignored_subjects())
        if not purged and not ignored:
            InfoBar.info('无需清理', '本地没有彻底删除标记或换源忽略记录。', parent=self,
                         position=InfoBarPosition.TOP)
            return
        box = MessageBox(
            '清空本地配置',
            f'将清除彻底删除标记 {purged} 条、换源忽略记录 {ignored} 条。\n'
            '清除后豆瓣同步可能重新加回被彻底删除的条目，确定继续吗？',
            self.window(),
        )
        box.yesButton.setText('清空')
        box.cancelButton.setText('取消')
        if not box.exec():
            return
        result = self.db.clear_sync_state()
        self.refresh()
        self.records_changed.emit()
        InfoBar.success(
            '已清空本地配置',
            f'彻底删除标记 {result["purged"]} 条，换源忽略记录 {result["ignored"]} 条。',
            parent=self, position=InfoBarPosition.TOP, duration=6000,
        )

    def clear_login(self) -> None:
        self.db.delete_setting('douban_cookie')
        self.refresh()
        InfoBar.success('已清除登录', '本地保存的豆瓣 Cookie 已删除。', parent=self,
                        position=InfoBarPosition.TOP)
