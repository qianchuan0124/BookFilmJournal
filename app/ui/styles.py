from qfluentwidgets import isDarkTheme, setCustomStyleSheet


def surface_style(name: str, radius: int) -> str:
    """卡片/行容器样式：暗色用极淡白色，亮色用白底 + 细边框区分层次"""
    if isDarkTheme():
        return (
            f'#{name} {{ background-color: rgba(255,255,255,0.035); border-radius: {radius}px; }}'
            f'#{name}:hover {{ background-color: rgba(255,255,255,0.06); }}'
        )
    return (
        f'#{name} {{ background-color: #ffffff; border: 1px solid rgba(15,23,42,0.10);'
        f' border-radius: {radius}px; }}'
        f'#{name}:hover {{ background-color: #ffffff; border: 1px solid rgba(15,23,42,0.22); }}'
    )


def progress_style() -> str:
    """进度条样式：亮色下用浅灰轨道，避免深色轨道过于抢眼"""
    track = '#4a5563' if isDarkTheme() else '#dfe5ec'
    return (
        f'QProgressBar{{border:none;border-radius:3px;background:{track};}}'
        'QProgressBar::chunk{border-radius:3px;background:#55b7a5;}'
    )


def flatten_button_bottom(button, selector: str) -> None:
    """去掉按钮底部的深色描边（亮色下看起来像黑边）"""
    qss = f'{selector}{{border-bottom: 1px solid transparent;}}'
    setCustomStyleSheet(button, qss, qss)


PRIMARY_LIGHT_QSS = (
    'PrimaryPushButton{border:1px solid rgba(176,124,32,0.35);'
    ' border-bottom:1px solid rgba(176,124,32,0.35);}'
)
PRIMARY_DARK_QSS = (
    'PrimaryPushButton{border:1px solid transparent; border-bottom:1px solid transparent;}'
)


def style_primary_button(button) -> None:
    """黄色主要按钮：亮色下加一圈略深的描边，避免和浅色背景糊在一起"""
    setCustomStyleSheet(button, PRIMARY_LIGHT_QSS, PRIMARY_DARK_QSS)


def search_edit_style(edit) -> None:
    """搜索框：亮色下白底 + 均匀细边框，聚焦时描边金色"""
    setCustomStyleSheet(
        edit,
        'LineEdit{background:#ffffff; border:1px solid rgba(15,23,42,0.14);'
        ' border-radius:8px; padding:5px 10px;}'
        'LineEdit:hover{border:1px solid rgba(15,23,42,0.24);}'
        'LineEdit:focus{border:1px solid #e5b45f;}',
        'LineEdit{background:rgba(255,255,255,0.06); border:1px solid rgba(255,255,255,0.14);'
        ' border-radius:8px; padding:5px 10px;}'
        'LineEdit:hover{border:1px solid rgba(255,255,255,0.24);}'
        'LineEdit:focus{border:1px solid #e5b45f;}',
    )


def primary_button(*args, **kwargs):
    """主要按钮：去掉底部深色描边"""
    from qfluentwidgets import PrimaryPushButton
    button = PrimaryPushButton(*args, **kwargs)
    style_primary_button(button)
    return button


def install_flat_buttons() -> None:
    """对话框底部按钮同样去掉深色描边（亮色皮肤下像一道黑边）"""
    from qfluentwidgets.components.dialog_box.message_box_base import MessageBoxBase

    if getattr(MessageBoxBase, '_shuying_flat', False):
        return

    original = MessageBoxBase.__init__

    def patched(self, *args, **kwargs):
        original(self, *args, **kwargs)
        flatten_button_bottom(self.cancelButton, '#cancelButton')
        style_primary_button(self.yesButton)

    MessageBoxBase.__init__ = patched
    MessageBoxBase._shuying_flat = True

    original_exec = MessageBoxBase.exec

    def exec_patched(self, *args, **kwargs):
        style_dialog_inputs(self)
        return original_exec(self, *args, **kwargs)

    MessageBoxBase.exec = exec_patched


def input_style(widget, selector: str = 'LineEdit') -> None:
    """输入框：统一白底/深色底的细边框与内边距"""
    setCustomStyleSheet(
        widget,
        f'{selector}{{background:#ffffff; border:1px solid rgba(15,23,42,0.14);'
        ' border-radius:8px; padding:6px 10px;}'
        f'{selector}:hover{{border:1px solid rgba(15,23,42,0.24);}}'
        f'{selector}:focus{{border:1px solid #e5b45f;}}',
        f'{selector}{{background:rgba(255,255,255,0.06); border:1px solid rgba(255,255,255,0.14);'
        ' border-radius:8px; padding:6px 10px;}'
        f'{selector}:hover{{border:1px solid rgba(255,255,255,0.24);}}'
        f'{selector}:focus{{border:1px solid #e5b45f;}}',
    )


def divider_style() -> str:
    color = 'rgba(255,255,255,0.07)' if isDarkTheme() else 'rgba(15,23,42,0.10)'
    return f'background: {color}; border: none;'


def cover_placeholder_style() -> str:
    if isDarkTheme():
        return ('background:#0f131b; border:1px dashed rgba(255,255,255,0.18);'
                ' border-radius:8px; color:#5d6474; font-size:11px;')
    return ('background:#f4f7fb; border:1px dashed rgba(15,23,42,0.22);'
            ' border-radius:8px; color:#5f6b7a; font-size:11px;')


def style_dialog_inputs(root) -> None:
    """给对话框内所有输入框套用统一风格（同步/统计等对话框内容较简单，需要单独适配）"""
    from qfluentwidgets import LineEdit, TextEdit

    for edit in root.findChildren(LineEdit):
        input_style(edit, 'LineEdit')
    for edit in root.findChildren(TextEdit):
        input_style(edit, 'TextEdit')
