import zlib
from datetime import datetime
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont, ImageOps

from app.config import MEDIA_NOUNS, READ_LABELS, ROOT
from app.db import Database
from app.media_meta import book_stats_text_full, movie_stats_text

WIDTH = 1240
PADDING = 64
ROW_GAP = 14
COVER_W = 96
COVER_H = 136
RIGHT_COL = 220

BG = '#0f131b'
PANEL = '#151b27'
PANEL_LINE = '#293241'
TEXT = '#eae7de'
MUTED = '#8d95a5'
DIM = '#5d6474'
GOLD = '#e5b45f'
TEAL = '#55b7a5'
GREEN = '#7fae68'
ORANGE = '#d98a4a'
PLACEHOLDER_COLORS = ('#1b2230', '#202a35', '#2a2230', '#1f2a26', '#2a2720')
SOURCE_LABELS = {'wish': '想看/想读', 'doulist': '豆列', 'manual': '手动'}
FONT_CANDIDATES = (
    '/System/Library/Fonts/PingFang.ttc',
    '/System/Library/Fonts/STHeiti Light.ttc',
    'C:/Windows/Fonts/msyh.ttc',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
)


def _font(size: int):
    for candidate in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(candidate, size=size)
        except (OSError, ValueError):
            continue
    return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int,
          max_lines: Optional[int] = None) -> list[str]:
    text = ' '.join(str(text or '').split())
    if not text:
        return []
    lines: list[tuple[int, str]] = []
    line = ''
    start = 0
    for index, char in enumerate(text):
        if not line:
            start = index
        candidate = line + char
        if draw.textlength(candidate, font=font) <= max_width:
            line = candidate
        else:
            lines.append((start, line))
            line = char
            start = index
    if line:
        lines.append((start, line))
    if max_lines and len(lines) > max_lines:
        start = lines[max_lines - 1][0]
        remaining = text[start:]
        while remaining and draw.textlength(remaining + '…', font=font) > max_width:
            remaining = remaining[:-1]
        lines = lines[:max_lines - 1] + [(start, remaining + '…')]
    return [line for _, line in lines]


def _load_cover(path: str, width: int, height: int) -> Optional[Image.Image]:
    if not path:
        return None
    full = Path(path)
    if not full.is_absolute():
        full = ROOT / full
    if not full.exists():
        return None
    try:
        with Image.open(full) as image:
            return ImageOps.fit(image.convert('RGB'), (width, height), method=Image.LANCZOS)
    except (OSError, ValueError):
        return None


def _placeholder(width: int, height: int, title: str):
    color = PLACEHOLDER_COLORS[zlib.crc32((title or '').encode('utf-8')) % len(PLACEHOLDER_COLORS)]
    tile = Image.new('RGB', (width, height), color)
    draw = ImageDraw.Draw(tile)
    font = _font(max(12, width // 7))
    text = '暂无封面'
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(((width - (box[2] - box[0])) / 2, (height - (box[3] - box[1])) / 2),
              text, fill=MUTED, font=font)
    return tile


def _stars(rating: int) -> str:
    rating = max(0, min(5, int(rating or 0)))
    return ('★' * rating + '☆' * (5 - rating)) if rating else '未评分'


def export_plan_png(db: Database, folder, media_type: str, output_path: Path,
                    name: str = '') -> dict:
    items = db.list_plan_items(media_type, int(folder['id']))
    word_counts = db.word_count_map('book')
    movie_intros = db.intro_map('movie')
    noun = MEDIA_NOUNS.get(media_type, '条')
    done_text, todo_text = READ_LABELS.get(media_type, ('已读', '未读'))

    title_font = _font(46)
    meta_font = _font(22)
    note_font = _font(20)
    index_font = _font(26)
    item_title_font = _font(26)
    item_meta_font = _font(18)
    item_note_font = _font(17)
    stamp_font = _font(19)
    footer_font = _font(15)
    empty_font = _font(22)

    measure = ImageDraw.Draw(Image.new('RGB', (10, 10)))
    content_width = WIDTH - PADDING * 2
    text_width = content_width - 22 - 60 - COVER_W - 22 - RIGHT_COL

    title_lines = _wrap(measure, folder['title'] or '', title_font, content_width, 3)
    note_text = folder['note'] or ''
    note_lines = _wrap(measure, note_text, note_font, content_width, 2)

    total = len(items)
    read = sum(1 for item in items if item['is_read'])
    header_height = 20 + len(title_lines) * 58 + 40 + (len(note_lines) * 30 + 6 if note_lines else 0) + 54

    rows = []
    for position, item in enumerate(items, 1):
        if item['media_type'] == 'book':
            stats = book_stats_text_full(
                item['subject_id'], word_counts.get(str(item['subject_id']), 0))
        elif item['media_type'] == 'movie':
            stats = movie_stats_text(
                movie_intros.get(str(item['subject_id']), '') or item['intro'] or '',
                item['subject_id'],
            )
        else:
            stats = ''
        item_title_lines = _wrap(measure, item['title'] or '', item_title_font, text_width, 2)
        item_note_lines = _wrap(measure, item['note'] or '', item_note_font, text_width, 2)
        content_height = (len(item_title_lines) * 36 + 30
                          + (len(item_note_lines) * 26 + 6 if item_note_lines else 0))
        row_height = max(COVER_H + 28, content_height + 28)
        rows.append((item, position, stats, item_title_lines, item_note_lines, row_height))

    body_height = sum(row[5] + ROW_GAP for row in rows)
    if not rows:
        body_height = 120
    footer_height = 70
    height = PADDING + header_height + body_height + footer_height + 10

    canvas = Image.new('RGB', (WIDTH, height), BG)
    draw = ImageDraw.Draw(canvas)

    y = PADDING
    for line in title_lines:
        draw.text((PADDING, y), line, fill=TEXT, font=title_font)
        y += 58

    meta = (f'{done_text} {read}/{total} {noun} · {todo_text} {max(0, total - read)} · '
            f'{"观影计划" if media_type == "movie" else "读书计划"}')
    y += 10
    draw.text((PADDING, y), meta, fill=MUTED, font=meta_font)
    y += 40

    if note_lines:
        for line in note_lines:
            draw.text((PADDING, y), line, fill=MUTED, font=note_font)
            y += 30
        y += 6

    bar_width = min(460, content_width)
    bar_y = y
    draw.rounded_rectangle((PADDING, bar_y, PADDING + bar_width, bar_y + 10), radius=5,
                           fill='#2a3340')
    if total:
        filled = int(bar_width * read / total)
        if filled > 0:
            draw.rounded_rectangle((PADDING, bar_y, PADDING + filled, bar_y + 10),
                                   radius=5, fill=TEAL)
    y = PADDING + header_height

    if not rows:
        draw.text((PADDING + 30, y + 40), '这个计划还没有条目。', fill=DIM, font=empty_font)
        y += 120

    for item, position, stats, item_title_lines, item_note_lines, row_height in rows:
        draw.rounded_rectangle(
            (PADDING, y, WIDTH - PADDING, y + row_height), radius=16,
            fill=PANEL, outline=PANEL_LINE, width=1,
        )
        draw.text((PADDING + 24, y + 20), f'{position:02d}', fill=GOLD, font=index_font)

        cover_x = PADDING + 78
        cover_y = y + 14
        cover = _load_cover(item['cover_path'] or '', COVER_W, COVER_H)
        if cover is None:
            cover = _placeholder(COVER_W, COVER_H, item['title'] or '')
        canvas.paste(cover, (cover_x, cover_y))

        text_x = cover_x + COVER_W + 22
        text_y = y + 16
        for line in item_title_lines:
            draw.text((text_x, text_y), line, fill=TEXT, font=item_title_font)
            text_y += 36
        source = SOURCE_LABELS.get(item['source'], item['source'] or '计划')
        meta_line = ' · '.join(part for part in (stats, source) if part)
        if meta_line:
            draw.text((text_x, text_y), meta_line, fill=MUTED, font=item_meta_font)
            text_y += 30
        for line in item_note_lines:
            draw.text((text_x, text_y), line, fill='#c9c5bb', font=item_note_font)
            text_y += 26

        right_x = WIDTH - PADDING - RIGHT_COL + 20
        if item['is_read']:
            draw.text((right_x, y + 22), done_text, fill=GREEN, font=stamp_font)
            date_text = item['read_date'] or ''
            if date_text:
                draw.text((right_x, y + 52), date_text.replace('-', '.'), fill=MUTED,
                          font=item_meta_font)
            draw.text((right_x, y + 80), _stars(item['read_rating'] or 0), fill=GOLD,
                      font=item_meta_font)
        else:
            draw.text((right_x, y + 22), todo_text, fill=ORANGE, font=stamp_font)

        y += row_height + ROW_GAP

    footer_y = height - footer_height + 6
    draw.line((PADDING, footer_y - 10, WIDTH - PADDING, footer_y - 10), fill=PANEL_LINE, width=1)
    draw.text((PADDING, footer_y + 4), '书影记录 · 计划导出', fill=DIM, font=footer_font)
    stamp = f'{name or ""} · {datetime.now().strftime("%Y-%m-%d %H:%M")}'
    box = draw.textbbox((0, 0), stamp, font=footer_font)
    draw.text((WIDTH - PADDING - (box[2] - box[0]), footer_y + 4), stamp, fill=DIM,
              font=footer_font)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format='PNG')
    return {'path': str(output_path), 'items': total, 'width': canvas.width,
            'height': canvas.height}
