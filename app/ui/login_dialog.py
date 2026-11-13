from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QDialog, QHBoxLayout, QVBoxLayout

try:
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
    from PySide6.QtWebEngineWidgets import QWebEngineView
    WEBENGINE_AVAILABLE = True
except ImportError:
    QWebEnginePage = QWebEngineProfile = QWebEngineView = None
    WEBENGINE_AVAILABLE = False

from qfluentwidgets import BodyLabel, CaptionLabel, InfoBar, InfoBarPosition, PushButton, SubtitleLabel

from app.douban import extract_user_id_from_cookie
from app.ui.styles import primary_button

_UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
       'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')
LOGIN_URL = 'https://accounts.douban.com/passport/login'


class DoubanLoginDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('扫码登录豆瓣')
        self.resize(980, 760)
        self.setModal(True)
        self.cookie = ''
        self.user_id = ''
        self.cookies: dict[str, str] = {}
        self._logged_in = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        layout.addWidget(SubtitleLabel('扫码登录豆瓣', self))
        hint = CaptionLabel(
            '使用豆瓣 App 扫描下方二维码登录。登录成功后会自动识别 Cookie 和用户 ID；'
            '在同步窗口勾选「记住登录」后，Cookie 会保存到本机数据库。',
            self,
        )
        hint.setWordWrap(True)
        hint.setTextColor('#5f6b7a', '#8d95a5')
        layout.addWidget(hint)

        self.profile = QWebEngineProfile(self)
        self.profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
        self.profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
        self.profile.setHttpUserAgent(_UA)
        self.view = QWebEngineView(self)
        self.page = QWebEnginePage(self.profile, self.view)
        self.view.setPage(self.page)
        self.view.load(QUrl(LOGIN_URL))
        layout.addWidget(self.view, 1)

        status_row = QHBoxLayout()
        self.status_label = BodyLabel('等待扫码…', self)
        self.status_label.setTextColor('#5f6b7a', '#8d95a5')
        status_row.addWidget(self.status_label)
        status_row.addStretch(1)
        self.use_button = primary_button('使用此登录', self)
        self.use_button.setEnabled(False)
        self.use_button.clicked.connect(self._accept_cookie)
        status_row.addWidget(self.use_button)
        self.cancel_button = PushButton('取消', self)
        self.cancel_button.clicked.connect(self.reject)
        status_row.addWidget(self.cancel_button)
        layout.addLayout(status_row)

        self._store = self.profile.cookieStore()
        self._store.cookieAdded.connect(self._on_cookie)
        self._store.loadAllCookies()

    def _on_cookie(self, cookie) -> None:
        domain = cookie.domain() or ''
        if 'douban.com' not in domain:
            return
        name = bytes(cookie.name()).decode('utf-8', 'ignore')
        value = bytes(cookie.value()).decode('utf-8', 'ignore')
        if not name:
            return
        self.cookies[name] = value
        if not self._logged_in and 'dbcl2' in self.cookies and 'ck' in self.cookies:
            self._logged_in = True
            self.user_id = extract_user_id_from_cookie(
                '; '.join(f'{key}={value}' for key, value in self.cookies.items())
            )
            self.use_button.setEnabled(True)
            uid_text = f'，用户 ID：{self.user_id}' if self.user_id else ''
            self.status_label.setText(f'已识别到豆瓣登录 Cookie{uid_text}，可以点击右侧「使用此登录」。')
            self.status_label.setStyleSheet('color:#7fae68;')

    def _accept_cookie(self) -> None:
        if not self.cookies:
            InfoBar.warning('尚未登录', '请先用豆瓣 App 扫码登录。', parent=self, position=InfoBarPosition.TOP)
            return
        self.cookie = '; '.join(f'{name}={value}' for name, value in self.cookies.items())
        self.user_id = self.user_id or extract_user_id_from_cookie(self.cookie)
        self.accept()

    def done(self, result: int) -> None:
        try:
            self._store.deleteAllCookies()
        except Exception:
            pass
        self.cookies.clear()
        super().done(result)
