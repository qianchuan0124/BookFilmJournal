"""生成 DMG 安装窗口背景图（普通 + Retina，合成为多分辨率 TIFF）。

用法: python packaging/dmg_background.py <输出目录>
输出目录中生成 background.png / background@2x.png / background.tiff。
图标位置与窗口尺寸见 packaging/dmg_layout.py 与 build_macos.sh（640×460）。
"""
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

WIDTH, HEIGHT = 640, 460
TOP_COLOR = '#F7F8FA'
BOTTOM_COLOR = '#E8EDF4'
TITLE_COLOR = '#1F2937'
TEXT_COLOR = '#3A4250'
SUBTEXT_COLOR = '#8A93A2'
ACCENT = '#E5B45F'
ACCENT_DEEP = '#D19A34'

APP_CENTER = (170, 200)
LINK_CENTER = (470, 200)
SLOT_SIZE = (156, 176)
SLOT_TOP = APP_CENTER[1] - SLOT_SIZE[1] // 2 + 10

FONT_CANDIDATES = (
    '/System/Library/Fonts/PingFang.ttc',
    '/System/Library/Fonts/Hiragino Sans GB.ttc',
    '/System/Library/Fonts/STHeiti Medium.ttc',
)
TIP_MAIN = '拖动「书影记录」到 Applications 文件夹完成安装'
TIP_SUB = '首次打开若提示无法验证开发者，请右键点击应用选择「打开」'


def load_font(size: int, bold: bool = False):
    for path in FONT_CANDIDATES:
        for index in (1 if bold else 0, 0):
            try:
                return ImageFont.truetype(path, size, index=index)
            except (OSError, Exception):
                continue
    return ImageFont.load_default()


def _rgb(color: str) -> tuple:
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def vertical_gradient(size, top: str, bottom: str) -> Image.Image:
    w, h = size
    top_rgb, bottom_rgb = _rgb(top), _rgb(bottom)
    column = Image.new('RGB', (1, h))
    px = column.load()
    for y in range(h):
        t = y / max(h - 1, 1)
        px[0, y] = tuple(int(top_rgb[i] + (bottom_rgb[i] - top_rgb[i]) * t) for i in range(3))
    return column.resize((w, h), Image.NEAREST)


def _soft_glow(size, center, radius, color, strength, blur) -> Image.Image:
    layer = Image.new('RGBA', size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    cx, cy = center
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                 fill=(*_rgb(color), strength))
    return layer.filter(ImageFilter.GaussianBlur(blur))


def _shadow(size, rect, radius, blur, offset, alpha) -> Image.Image:
    layer = Image.new('RGBA', size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.rounded_rectangle(
        [rect[0] + offset[0], rect[1] + offset[1], rect[2] + offset[0], rect[3] + offset[1]],
        radius=radius, fill=(15, 23, 42, alpha),
    )
    return layer.filter(ImageFilter.GaussianBlur(blur))


def _chevron(draw, x, y, size, fill, outline):
    s = size
    points = [(x, y - s), (x + s * 1.35, y), (x, y + s), (x + s * 0.55, y)]
    draw.polygon([(px + 2, py + 2) for px, py in points], fill=outline)
    draw.polygon(points, fill=fill)


def draw(scale: int) -> Image.Image:
    size = (WIDTH * scale, HEIGHT * scale)
    img = vertical_gradient(size, TOP_COLOR, BOTTOM_COLOR).convert('RGBA')

    # 顶部与图标区的柔和光晕，让背景不至于太平。
    img = Image.alpha_composite(img, _soft_glow(
        size, (APP_CENTER[0] * scale, (APP_CENTER[1] - 20) * scale), 150 * scale,
        '#FFE7B8', 110, 70 * scale))
    img = Image.alpha_composite(img, _soft_glow(
        size, (LINK_CENTER[0] * scale, (LINK_CENTER[1] - 20) * scale), 150 * scale,
        '#DCE9FF', 95, 70 * scale))

    # 图标占位卡片：白色圆角 + 细边 + 落影。
    for cx in (APP_CENTER[0], LINK_CENTER[0]):
        rect = ((cx - SLOT_SIZE[0] // 2) * scale, SLOT_TOP * scale,
                (cx + SLOT_SIZE[0] // 2) * scale, (SLOT_TOP + SLOT_SIZE[1]) * scale)
        img = Image.alpha_composite(img, _shadow(
            size, rect, 24 * scale, 18 * scale, (0, 8 * scale), 26))
        card = Image.new('RGBA', size, (0, 0, 0, 0))
        draw_card = ImageDraw.Draw(card)
        draw_card.rounded_rectangle(rect, radius=24 * scale,
                                    fill=(255, 255, 255, 214),
                                    outline=(15, 23, 42, 18), width=max(1, scale))
        img = Image.alpha_composite(img, card)

    d = ImageDraw.Draw(img)

    # 标题与分隔线。
    title_font = load_font(27 * scale, bold=True)
    d.text((WIDTH * scale / 2, 56 * scale), '书影记录',
           font=title_font, fill=TITLE_COLOR, anchor='mm')
    sub_font = load_font(12 * scale)
    d.text((WIDTH * scale / 2, 84 * scale), '读书 · 观影 · 记录',
           font=sub_font, fill=SUBTEXT_COLOR, anchor='mm')
    d.rounded_rectangle(
        [((WIDTH / 2 - 26) * scale), 103 * scale,
         ((WIDTH / 2 + 26) * scale), 106 * scale],
        radius=2 * scale, fill=(*_rgb(ACCENT), 230))

    # 中间的安装箭头。
    _chevron(d, 320 * scale, APP_CENTER[1] * scale, 15 * scale,
             (*_rgb(ACCENT), 255), (241, 205, 140, 160))

    # 底部提示条。
    tip_rect = (96 * scale, 312 * scale, (WIDTH - 96) * scale, 390 * scale)
    img_tip = Image.new('RGBA', size, (0, 0, 0, 0))
    tip_draw = ImageDraw.Draw(img_tip)
    tip_draw.rounded_rectangle(tip_rect, radius=18 * scale,
                               fill=(255, 255, 255, 190),
                               outline=(15, 23, 42, 16), width=max(1, scale))
    img = Image.alpha_composite(img, img_tip)
    d = ImageDraw.Draw(img)
    tip_font = load_font(14 * scale)
    tip_sub_font = load_font(11 * scale)
    d.text((WIDTH * scale / 2, 337 * scale), TIP_MAIN,
           font=tip_font, fill=TEXT_COLOR, anchor='mm')
    d.text((WIDTH * scale / 2, 363 * scale), TIP_SUB,
           font=tip_sub_font, fill=SUBTEXT_COLOR, anchor='mm')

    return img.convert('RGB')


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
