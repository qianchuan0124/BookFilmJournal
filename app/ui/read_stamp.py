import math
import random

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from app.config import READ_LABELS


class ReadStamp(QWidget):
    def __init__(self, media_type: str, date_str: str = '', rating: int = 0,
                 size: int = 96, parent=None):
        super().__init__(parent)
        self.media_type = media_type
        self.date_str = date_str or ''
        self.rating = max(0, min(5, int(rating or 0)))
        seed = f'{media_type}|{self.date_str}|{self.rating}'
        rng = random.Random(seed)
        self.seed = seed
        self.angle = -14 + rng.uniform(-3.0, 3.0)
        self.ink_alpha = 190 + rng.randint(0, 18)
        self.ink_color = QColor(172, 175, 180, self.ink_alpha)
        self.star_color = QColor(184, 187, 192, min(220, self.ink_alpha + 12))
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        scale = self.width() / 100
        painter.scale(scale, scale)
        painter.translate(50, 50)
        painter.rotate(self.angle)
        painter.translate(-50, -50)

        rng = random.Random(self.seed)
        painter.setBrush(Qt.NoBrush)
        self._draw_ring(painter, QRectF(3, 3, 94, 94), rng)
        self._draw_ring(painter, QRectF(8, 8, 84, 84), rng)
        painter.setPen(QPen(self.ink_color, 1.5))
        painter.drawRoundedRect(QRectF(12, 31, 76, 26), 5, 5)

        year = self.date_str[:4] if len(self.date_str) >= 4 else ''
        month_day = ''
        if len(self.date_str) >= 10:
            month_day = f'{self.date_str[5:7]}.{self.date_str[8:10]}'
        done_text = READ_LABELS.get(self.media_type, ('已读', '未读'))[0]
        stars = '★' * self.rating + '☆' * (5 - self.rating)

        font = QFont(self.font())
        font.setPixelSize(11)
        painter.setFont(font)
        painter.drawText(QRectF(0, 12, 100, 14), Qt.AlignCenter, year)

        font.setPixelSize(15)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QRectF(0, 33, 100, 22), Qt.AlignCenter, done_text)

        font.setPixelSize(13)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QPen(self.star_color, 1.0))
        painter.drawText(QRectF(0, 58, 100, 14), Qt.AlignCenter, stars)

        font.setPixelSize(11)
        painter.setFont(font)
        painter.setPen(QPen(self.ink_color, 1.5))
        painter.drawText(QRectF(0, 76, 100, 12), Qt.AlignCenter, month_day)

        painter.save()
        painter.setPen(Qt.NoPen)
        for _ in range(14):
            angle = rng.uniform(0, math.tau)
            radius = rng.uniform(10, 45)
            x = 50 + math.cos(angle) * radius
            y = 50 + math.sin(angle) * radius
            dot = rng.uniform(0.35, 1.1)
            painter.setBrush(QColor(31, 35, 42, rng.randint(30, 70)))
            painter.drawEllipse(QRectF(x, y, dot, dot))
        painter.restore()

    def _draw_ring(self, painter: QPainter, rect: QRectF, rng: random.Random) -> None:
        for index in range(12):
            start = index * 30 + rng.uniform(0, 1.2)
            span = 30 - rng.uniform(0.4, 2.2)
            alpha = int(self.ink_alpha * rng.uniform(0.68, 1.0))
            painter.setPen(QPen(QColor(172, 175, 180, alpha), 1.5))
            painter.drawArc(rect, int(start * 16), int(span * 16))
