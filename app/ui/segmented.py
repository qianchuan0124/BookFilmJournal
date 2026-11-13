from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QPushButton, QWidget

from qfluentwidgets import isDarkTheme, qconfig


class SegmentedSwitch(QWidget):
    """两段式切换控件（已读/未读、影视/书籍等），跟随主题自动换色"""

    changed = Signal(str)

    def __init__(self, first_text: str, second_text: str, first_value: str,
                 second_value: str, width: int = 210, object_name: str = 'segmentedSwitch',
                 parent=None):
        super().__init__(parent)
        self.first_value = first_value
        self.second_value = second_value
        self.setObjectName(object_name)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedWidth(width)
        self.setFixedHeight(36)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(2)
        self.first_button = QPushButton(first_text, self)
        self.second_button = QPushButton(second_text, self)
        self.movie_button = self.first_button
        self.book_button = self.second_button
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for index, button in enumerate((self.first_button, self.second_button)):
            button.setObjectName('segmentedButton')
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            self.group.addButton(button, index)
            layout.addWidget(button, 1)
        self.first_button.setChecked(True)
        self.group.buttonClicked.connect(self._on_clicked)
        self.apply_theme()
        qconfig.themeChanged.connect(self.apply_theme)

    def apply_theme(self, *_) -> None:
        if isDarkTheme():
            background, text = 'rgba(255,255,255,0.06)', '#b9b5aa'
            hover_background, hover_text = 'rgba(255,255,255,0.07)', '#eae7de'
        else:
            background, text = 'rgba(15,23,42,0.06)', '#5f6b7a'
            hover_background, hover_text = 'rgba(15,23,42,0.09)', '#20242b'
        self.setStyleSheet(f"""
            #{self.objectName()} {{ background-color: {background}; border-radius: 8px; }}
            QPushButton#segmentedButton {{
                background-color: transparent; border: none; border-radius: 6px;
                padding: 5px 12px; color: {text}; font-size: 13px; font-weight: 600;
            }}
            QPushButton#segmentedButton:hover:!checked {{
                background-color: {hover_background}; color: {hover_text};
            }}
            QPushButton#segmentedButton:checked {{ background-color: #e5b45f; color: #171a20; }}
        """)

    def _on_clicked(self, button) -> None:
        self.changed.emit(self.first_value if button is self.first_button else self.second_value)
