"""Generate the onboarding guide images used by README.md.

Usage:
    .venv/bin/python tools/make_guide_images.py
    GUIDE_PREVIEW_DIR=/path/to/preview .venv/bin/python tools/make_guide_images.py

Input screenshots come from GUIDE_PREVIEW_DIR (default docs/images in this repo).
Each output is one original app screenshot with a dark mask, cut-out highlights,
small speech bubbles, and the flow title drawn on top of the image itself.
Steps that happen on the same screen are all placed on that single image.
"""

import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PREVIEW = Path(os.environ.get(
    'GUIDE_PREVIEW_DIR', str(REPO / 'docs' / 'images'))).expanduser()
OUTPUT = REPO / 'docs' / 'images'

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_PATH = '/System/Library/Fonts/Hiragino Sans GB.ttc'

ACCENT = (229, 180, 95)
DIM = (8, 10, 14, 132)
BUBBLE_BG = (255, 255, 255)
BUBBLE_TEXT = (28, 33, 44)
BUBBLE_SUB = (92, 100, 114)
TITLE_TEXT = (238, 242, 247)
TITLE_SUB = (158, 168, 184)

Rect = tuple[int, int, int, int]
Callout = tuple[Rect, int, str, str]


def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_PATH, size, index=0)


def wrap(text: str, ft: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines: list[str] = []
    current = ''
    for char in text:
        if ft.getlength(current + char) <= width:
            current += char
        else:
            lines.append(current)
            current = char
    if current:
        lines.append(current)
    if len(lines) > 1 and lines[-1] and lines[-1] in '。，、；：！？）':
        lines[-2] += lines[-1]
        lines.pop()
    while len(lines) > 1 and len(lines[-1]) < 4 and len(lines[-2]) > 6:
        lines[-1] = lines[-2][-1] + lines[-1]
        lines[-2] = lines[-2][:-1]
    return lines


def load_preview(name: str, redactions: tuple[Rect, ...] = ()) -> Image.Image:
    img = Image.open(PREVIEW / name).convert('RGB')
    for rect in redactions:
        img.paste(img.crop(rect).filter(ImageFilter.GaussianBlur(9)), rect)
    return img


def overlaps(a: Rect, b: Rect, margin: int = 0) -> bool:
    return not (a[2] + margin <= b[0] or b[2] + margin <= a[0]
                or a[3] + margin <= b[1] or b[3] + margin <= a[1])


def draw_tail(draw, side: str, bx: int, by: int, bw: int, bh: int,
              cx: int, cy: int) -> None:
    # side 是气泡相对高亮框的位置：below/above 尾巴朝上/朝下，
    # right 尾巴在左边缘朝左，left 尾巴在右边缘朝右。
    size = 11
    if side == 'below':
        tx = min(max(cx, bx + 18), bx + bw - 18)
        draw.polygon([(tx - 9, by), (tx + 9, by), (tx, by - size)], fill=BUBBLE_BG)
    elif side == 'above':
        tx = min(max(cx, bx + 18), bx + bw - 18)
        draw.polygon([(tx - 9, by + bh), (tx + 9, by + bh), (tx, by + bh + size)],
                     fill=BUBBLE_BG)
    elif side == 'right':
        ty = min(max(cy, by + 18), by + bh - 18)
        draw.polygon([(bx, ty - 9), (bx, ty + 9), (bx - size, ty)], fill=BUBBLE_BG)
    else:
        ty = min(max(cy, by + 18), by + bh - 18)
        draw.polygon([(bx + bw, ty - 9), (bx + bw, ty + 9), (bx + bw + size, ty)],
                     fill=BUBBLE_BG)


def place_bubble(width: int, height: int, bw: int, bh: int, hole: Rect,
                 obstacles: list[Rect], placed: list[Rect],
                 gap: int = 20, prefer: str = '') -> tuple[str, int, int]:
    cx = (hole[0] + hole[2]) // 2
    cy = (hole[1] + hole[3]) // 2
    candidates = [
        ('below', cx - bw // 2, hole[3] + gap),
        ('above', cx - bw // 2, hole[1] - gap - bh),
        ('right', hole[2] + gap, cy - bh // 2),
        ('left', hole[0] - gap - bw, cy - bh // 2),
    ]
    if prefer:
        candidates.sort(key=lambda item: item[0] != prefer)
    fallback = None
    for side, raw_x, raw_y in candidates:
        x = min(max(raw_x, 20), width - bw - 20)
        y = min(max(raw_y, 20), height - bh - 20)
        box = (x, y, x + bw, y + bh)
        if fallback is None:
            fallback = (side, x, y)
        if any(overlaps(box, other, 10) for other in obstacles):
            continue
        if any(overlaps(box, other, 12) for other in placed):
            continue
        return side, x, y
    return fallback


TITLE_W, TITLE_H = 340, 84


def title_box(width: int, height: int) -> Rect:
    return width - 20 - TITLE_W, height - 20 - TITLE_H, width - 20, height - 20


def annotate(img: Image.Image, callouts: list[Callout], total: int,
             title: str, subtitle: str) -> Image.Image:
    img = img.copy().convert('RGB')
    width, height = img.size
    pad = 7

    def pad_box(rect):
        return (max(0, rect[0] - pad), max(0, rect[1] - pad),
                min(width - 1, rect[2] + pad), min(height - 1, rect[3] + pad))

    def to_rects(value):
        if isinstance(value, tuple) and len(value) == 4 and isinstance(value[0], int):
            return [value]
        return list(value)

    groups = []
    all_boxes = []
    for callout in callouts:
        raw = to_rects(callout[0])
        boxes = [pad_box(rect) for rect in raw]
        groups.append((raw, boxes, callout))
        all_boxes.extend(boxes)

    dim = Image.new('RGBA', img.size, DIM)
    hole_mask = Image.new('L', img.size, DIM[3])
    hole_draw = ImageDraw.Draw(hole_mask)
    for box in all_boxes:
        hole_draw.rounded_rectangle(box, radius=13, fill=0)
    dim.putalpha(hole_mask)
    base = Image.alpha_composite(img.convert('RGBA'), dim)

    glow = Image.new('RGBA', img.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    for box in all_boxes:
        glow_draw.rounded_rectangle(box, radius=13, outline=ACCENT + (165,), width=8)
    base = Image.alpha_composite(base, glow.filter(ImageFilter.GaussianBlur(10)))
    img = base.convert('RGB')
    draw = ImageDraw.Draw(img)
    for box in all_boxes:
        draw.rounded_rectangle(box, radius=13, outline=ACCENT, width=3)

    panel = title_box(width, height)
    draw.rounded_rectangle(panel, radius=14, fill=(8, 10, 14, 140))
    draw.rectangle((panel[0] + 14, panel[1] + 16, panel[0] + 18, panel[3] - 16), fill=ACCENT)
    draw.text((panel[0] + 30, panel[1] + 14), title, font=font(24), fill=TITLE_TEXT)
    sub_lines = wrap(subtitle, font(13), TITLE_W - 44)[:2]
    for index, line in enumerate(sub_lines):
        draw.text((panel[0] + 30, panel[1] + 50 + index * 17),
                  line, font=font(13), fill=TITLE_SUB)
    obstacles = all_boxes + [panel]

    title_ft, body_ft, step_ft = font(19), font(16), font(11)
    bw = 290
    text_width = bw - 34
    placed: list[Rect] = []

    for raw, boxes, callout in groups:
        step, step_title, body = callout[1:4]
        prefer = callout[4] if len(callout) > 4 else ''
        explicit = callout[5] if len(callout) > 5 else None
        lines = wrap(body, body_ft, text_width)
        bh = 14 + 24 + 6 + len(lines) * 23 + 13
        anchor = boxes[0]
        if explicit:
            side, bx, by = prefer or 'below', explicit[0], explicit[1]
        else:
            side, bx, by = place_bubble(width, height, bw, bh, raw[0], obstacles, placed,
                                        prefer=prefer)
        placed.append((bx, by, bx + bw, by + bh))

        cx = (anchor[0] + anchor[2]) // 2
        cy = (anchor[1] + anchor[3]) // 2

        shadow = Image.new('RGBA', img.size, (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            (bx, by + 5, bx + bw, by + bh + 6), radius=16, fill=(0, 0, 0, 145))
        img = Image.alpha_composite(img.convert('RGBA'),
                                    shadow.filter(ImageFilter.GaussianBlur(12))).convert('RGB')
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle((bx, by, bx + bw, by + bh), radius=16,
                               fill=BUBBLE_BG, outline=(228, 232, 238), width=1)
        draw_tail(draw, side, bx, by, bw, bh, cx, cy)

        badge = f'{step}/{total}'
        badge_w = step_ft.getlength(badge) + 18
        draw.rounded_rectangle((bx + 16, by + 15, bx + 16 + badge_w, by + 39),
                               radius=12, fill=ACCENT)
        draw.text((bx + 16 + badge_w / 2, by + 26), badge, font=step_ft,
                  fill=(33, 27, 16), anchor='mm')
        draw.text((bx + 28 + badge_w, by + 17), step_title, font=title_ft, fill=BUBBLE_TEXT)

        y = by + 48
        for line in lines:
            draw.text((bx + 17, y), line, font=body_ft, fill=BUBBLE_SUB)
            y += 23

    return img


def main() -> None:
    def optional(name: str, redactions: tuple[Rect, ...] = ()):
        return load_preview(name, redactions) if (PREVIEW / name).is_file() else None

    p1 = optional('preview_1.png', ((352, 227, 500, 253),))
    if p1 is not None:
        ImageDraw.Draw(p1).text((360, 231), '12345678', font=font(15), fill=(210, 210, 210))
    p2 = optional('preview_2.png')
    p3 = optional('preview_3.png')
    p6 = optional('preview_6.png', (
        (250, 549, 762, 569),
        (284, 698, 508, 719),
        (330, 844, 762, 864),
    ))
    p8 = optional('preview_8.png')
    p9 = optional('preview_9.png')
    p10 = optional('preview_10.png')
    p11 = optional('preview_11.png')
    p12 = optional('preview_12.png')
    p13 = optional('preview_13.png')

    jobs = [
        ('guide-sync.png', p1, 3, '同步豆瓣收藏',
         '第一次使用，先把豆瓣上的观影和阅读记录同步到本地', [
             ((1143, 90, 1246, 120), 1, '点击「同步豆瓣」',
              '把豆瓣上的观影和阅读记录拉到本地。', 'left'),
             ((338, 192, 942, 360), 2, '填入豆瓣 ID 或扫码登录',
              '手动填写豆瓣 ID，或扫码登录自动获取账号信息；可勾选记住登录。', 'above'),
             ((340, 410, 942, 600), 3, '选择同步方式',
              '支持全量 / 增量同步，可分别同步观影、读书与豆列。', 'below'),
         ]),
        ('guide-records-1.png', p2, 5, '浏览与编辑记录',
         '搜索、切换状态，点开卡片就能编辑或补录', [
             ((862, 89, 1037, 121), 1, '切换看过 / 想看',
              '顶部在两个状态之间切换，未看过的条目也会展示。', 'left'),
             ((233, 148, 533, 183), 2, '搜索标题或短评',
              '输入关键词即时筛选，匹配到的内容会高亮。', 'left'),
             ((234, 242, 444, 648), 3, '点开卡片编辑',
              '日期、评分、短评都能改，本地修改不会被同步覆盖。', 'below'),
         ]),
        ('guide-records-2.png', p3, 5, '浏览与编辑记录',
         '搜索、切换状态，点开卡片就能编辑或补录', [
             ((234, 155, 1044, 470), 4, '手动填写记录',
              '标题、状态、日期、评分与封面都可以直接填写。', 'above'),
             ((235, 560, 1044, 592), 5, '自动解析豆瓣链接',
              '粘贴链接后点「解析豆瓣链接」，自动填好标题、评分和封面。', 'below'),
         ]),
        ('guide-doulist.png', p10, 2, '我的豆列',
         '自己创建和关注的片单书单，按影视与书籍分开管理', [
             ((925, 89, 1131, 121), 1, '切换影视 / 书籍豆列',
              '顶部切换类型，卡片显示已读进度；点开卡片查看条目。', 'left'),
             ((354, 548, 438, 578), 2, '调整顺序或删除',
              '卡片下方可以上移、下移或把整张豆列移入回收站。', 'below'),
         ]),
        ('guide-plan-1.png', p9, 3, '我的计划',
         '把想看的作品收进计划，按自己的节奏完成', [
             ((922, 89, 1254, 121), 1, '切换与新建计划',
              '顶部切换观影 / 读书计划，也可以新建计划文件夹。', 'left'),
             ((848, 166, 1244, 194), 2, '添加内容或导出',
              '从想读 / 想看或豆列添加内容，也可以导出 PNG 分享。', 'below'),
             ((1038, 489, 1166, 521), 3, '调整顺序',
              '每一条都可以上移、下移或删除。', 'below'),
         ]),
        ('guide-record-detail.png', p11, 3, '记录详情',
         '点击已看 / 已读记录，查看信息与完整感想', [
             ((220, 245, 410, 690), 1, '记录信息',
              '封面、评分、日期、页数与字数都集中在左侧。', 'above'),
             ((444, 245, 1052, 700), 2, '完整感想',
              '长评可以滚动阅读；没有感想时会提示「前去添加」。', 'above'),
             ((226, 734, 637, 768), 3, '编辑记录',
              '点这里修改评分、日期与感想，保存后卡片同步更新。', 'below'),
         ]),
        ('guide-report.png', p8, 2, '报告中心',
         '把这一年的观影与阅读整理成可归档的报告', [
             ((246, 178, 1230, 348), 1, '生成报告',
              '支持月报、年报和自定义范围；可选暗色 / 亮色主题与 PNG。', 'below'),
             ((234, 475, 1246, 529), 2, '查看生成结果',
              '生成的 HTML / PNG 会列在下方，可直接打开或删除。', 'below'),
         ]),
        ('guide-explore.png', p12, 3, '探索空间',
         '站点清单从云端下发，也可以添加自己的本地站点', [
             ((232, 157, 594, 194), 1, '拉取配置或新增站点',
              '站点清单从 GitHub 拉取，失败自动回退 Gitee；也可以添加自己的本地站点。',
              'right', (640, 46)),
             ((236, 218, 1238, 284), 2, '配置来源与版本',
              '显示当前站点来自哪里（GitHub / Gitee / iOS 快照）和站点数量。', 'below'),
             ((1056, 342, 1236, 393), 3, '排序、打开与复制',
              '上下箭头调整顺序，点整行复制链接；左侧圆点表示连通性。', 'left'),
         ]),
        ('guide-explore-2.png', p13, 1, '记录详情 · 站点跳转',
         '在探索空间配好站点后，详情页可以直接按标题搜索', [
             ((445, 566, 1056, 680), 1, '资源站点直达',
              '点站点按钮就用当前标题在该站搜索；按钮前的符号表示上次连通性检测：'
              '对勾可访问、叉号不可访问、圆点未检测。', 'above', (720, 396)),
         ]),
        ('guide-settings.png', p6, 3, '应用设置',
         '外观、Excel 备份与本地字数统计都在这里', [
             ((254, 242, 338, 266), 1, '切换暗色 / 亮色',
              '外观选择会保存，下次启动自动恢复。', 'right'),
             ((254, 378, 518, 410), 2, '导入 / 导出 Excel',
              '备份记录、豆列、计划、回收站与换源忽略条目，换机导入即可恢复。', 'below'),
             ((254, 578, 387, 610), 3, '统计书籍字数',
              '选择 Calibre 书库或电子书目录，按书名统计本地字数；统计结果会展示在卡片和阅读报告中。', 'below'),
         ]),
    ]

    OUTPUT.mkdir(parents=True, exist_ok=True)
    # 只清理已经没有对应任务的旧图；预览图缺失被跳过的任务保留原图，避免误删。
    job_names = {name for name, *_ in jobs}
    for stale in OUTPUT.glob('guide-*.png'):
        if stale.name not in job_names:
            stale.unlink()

    for name, img, total, title, subtitle, callouts in jobs:
        if img is None:
            print('skip (missing preview):', name)
            continue
        result = annotate(img, callouts, total, title, subtitle)
        result.save(OUTPUT / name, optimize=True)

    print('guide images written to', OUTPUT)


if __name__ == '__main__':
    main()
