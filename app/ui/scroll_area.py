from PySide6.QtCore import QEvent

from qfluentwidgets import SmoothScrollArea as _FluentSmoothScrollArea


class SmoothScrollArea(_FluentSmoothScrollArea):
    """修复 macOS 触摸板经常滚不动的 SmoothScrollArea。

    qfluentwidgets 的 SmoothScrollDelegate 在 viewport 上用 eventFilter 拦截滚轮
    事件，只读取 angleDelta：macOS 触摸板的精密滚动事件 angleDelta 常为 0、只有
    pixelDelta 有值，会被当成「水平滚动 0」直接吞掉，导致无法滚动。

    这里在 viewport 上再装一个事件过滤器（后安装的先执行）：pixelDelta 非空的
    触摸板事件直接按像素滚动滚动条（系统自带惯性），其余事件交回原过滤器，
    鼠标滚轮仍走原来的平滑动画。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.viewport().installEventFilter(self)

    def eventFilter(self, obj, e):
        if obj is self.viewport() and e.type() == QEvent.Type.Wheel:
            pixel = e.pixelDelta()
            if not pixel.isNull():
                if self._scroll_by_pixel(pixel):
                    e.accept()
                    return True
                # 已到滚动边界，交还原过滤器，保留嵌套滚动的传递行为。
                return False
        return super().eventFilter(obj, e)

    def _scroll_by_pixel(self, pixel) -> bool:
        moved = False
        if pixel.y():
            bar = self.verticalScrollBar()
            value = bar.value()
            at_end = ((pixel.y() < 0 and value >= bar.maximum())
                      or (pixel.y() > 0 and value <= bar.minimum()))
            if not at_end:
                bar.setValue(value - pixel.y())
                moved = True
        if pixel.x():
            bar = self.horizontalScrollBar()
            value = bar.value()
            at_end = ((pixel.x() < 0 and value >= bar.maximum())
                      or (pixel.x() > 0 and value <= bar.minimum()))
            if not at_end:
                bar.setValue(value - pixel.x())
                moved = True
        return moved
