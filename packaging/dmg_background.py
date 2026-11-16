"""生成 DMG 安装窗口背景图（普通 + Retina，合成为多分辨率 TIFF）。

用法: python packaging/dmg_background.py <输出目录>
输出目录中生成 background.png / background@2x.png / background.tiff。
窗口布局见 build_macos.sh 中的 AppleScript（640x400，图标 96）。
"""
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 640, 440
TOP_COLOR = '#EDEEF1'      # 顶部渐变起点
BOTTOM_COLOR = '#F8F9FB'   # 底部渐变终点
ARROW_COLOR = '#3A3A3C'    # 箭头：深灰，在浅背景上清晰
ARROW_SHADOW = '#C7C7CC'   # 箭头阴影
TEXT_COLOR = '#3A3A3C'     # 正文文字
SUBTEXT_COLOR = '#86868B'  # 辅助文字
FONT_CANDIDATES = (
    '/System/Library/Fonts/PingFang.ttc',
    '/System/Library/Fonts/Hiragino Sans GB.ttc',
    '/System/Library/Fonts/STHeiti Medium.ttc',
)
TIP = '将「书影记录」拖入 Applications 完成安装'


def load_font(size: int, bold: bool = False):
    # PingFang.ttc 含多个字重，尝试取第 1 个（Semibold）做粗体。
    for path in FONT_CANDIDATES:
        for index in (1 if bold else 0, 0):
            try:
                return ImageFont.truetype(path, size, index=index)
            except (OSError, Exception):
                continue
    return ImageFont.load_default()


def vertical_gradient(size, top, bottom) -> Image.Image:
    w, h = size
    top = tuple(int(top[i:i + 2], 16) for i in (1, 3, 5))
    bottom = tuple(int(bottom[i:i + 2], 16) for i in (1, 3, 5))
    img = Image.new('RGB', (1, h))
    px = img.load()
    for y in range(h):
        t = y / max(h - 1, 1)
        px[0, y] = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    return img.resize((w, h), Image.NEAREST)


def draw_chevron(draw, x, y, size, color, shadow, scale):
    """在 (x, y) 处画一个指向右的实心燕尾箭头。"""
    s = size
    points = [
        (x, y - s),
        (x + s * 1.4, y),
        (x, y + s),
        (x + s * 0.5, y),
    ]
    # 阴影（向右下偏移）
    offset = 3 * scale
    shadow_pts = [(px + offset, py + offset) for px, py in points]
    draw.polygon(shadow_pts, fill=shadow)
    draw.polygon(points, fill=color)


def draw(scale: int) -> Image.Image:
    img = vertical_gradient((WIDTH * scale, HEIGHT * scale), TOP_COLOR, BOTTOM_COLOR)
    d = ImageDraw.Draw(img)

    # 两个图标下方的柔和落影（增加层次）
    for cx in (150 * scale, 490 * scale):
        cy = 250 * scale
        rx, ry = 56 * scale, 14 * scale
        d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill='#E5E6EA')

    # 箭头（位于两个图标中心之间，y=190）
    draw_chevron(d, 300 * scale, 190 * scale, 16 * scale, ARROW_COLOR, ARROW_SHADOW, scale)

    # 提示文字
    font = load_font(15 * scale)
    d.text((WIDTH * scale / 2, 300 * scale), TIP,
           font=font, fill=TEXT_COLOR, anchor='mm')

    # 辅助小字
    subfont = load_font(12 * scale)
    d.text((WIDTH * scale / 2, 328 * scale), '首次打开若被系统拦截，请在「系统设置 · 隐私与安全性」中允许',
           font=subfont, fill=SUBTEXT_COLOR, anchor='mm')

    return img


def main() -> None:
    out_dir = Path(sys.argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)
    normal = out_dir / 'background.png'
    retina = out_dir / 'background@2x.png'
    draw(1).save(normal)
    draw(2).save(retina)
    # 合成多分辨率 TIFF，Finder 在 Retina 屏上会自动选用 @2x。
    subprocess.run(
        ['tiffutil', '-cathidpicheck', str(normal), str(retina),
         '-out', str(out_dir / 'background.tiff')],
        check=True,
    )


if __name__ == '__main__':
    main()
