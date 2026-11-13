from pathlib import Path
from typing import Iterable, Optional

import math
import zlib

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from app.config import ROOT
from app.douban import cover_file_ok, fetch_cover

TILE_RATIO = 1.5
CELL_GAP = 8
POSTER_MARGIN = 28
TARGET_WIDTH = 2200
MIN_CELL_WIDTH = 150
MAX_CELL_WIDTH = 320
TILE_RADIUS = 9
TILE_BG = '#141a24'
TILE_BORDER = '#2a3242'
POSTER_MAX_WIDTH = 4000
POSTER_MAX_HEIGHT = 8000
POSTER_JPEG_QUALITY = 88
PLACEHOLDER_COLORS = ('#1b2230', '#202a35', '#2a2230', '#1f2a26', '#2a2720')
FONT_CANDIDATES = (
    '/System/Library/Fonts/PingFang.ttc',
    '/System/Library/Fonts/STHeiti Light.ttc',
    'C:/Windows/Fonts/msyh.ttc',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
)


class PosterWallError(Exception):
    pass


def _local_cover(item) -> Optional[Path]:
    cover = getattr(item, 'cover', '') or ''
    if cover:
        path = Path(cover)
        if not path.is_absolute():
            path = ROOT / path
        if cover_file_ok(path):
            return path
    cover_url = getattr(item, 'cover_url', '') or ''
    if not cover_url:
        return None
    media_type = getattr(item, 'media_type', '') or 'image'
    subject_id = getattr(item, 'subject_id', '') or str(abs(hash(cover_url)))
    saved = fetch_cover(cover_url, media_type, subject_id)
    if not saved:
        return None
    path = Path(saved)
    return path if path.is_absolute() else ROOT / path


def _load_image(item) -> Optional[Image.Image]:
    path = _local_cover(item)
    if not path:
        return None
    try:
        with Image.open(path) as image:
            return image.convert('RGB')
    except (OSError, ValueError):
        return None


def _font(size: int):
    for candidate in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(candidate, size=size)
        except (OSError, ValueError):
            continue
    return ImageFont.load_default()


def _placeholder_tile(width: int, height: int, item) -> Image.Image:
    title = str(getattr(item, 'title', '') or '')
    color = PLACEHOLDER_COLORS[zlib.crc32(title.encode('utf-8')) % len(PLACEHOLDER_COLORS)]
    tile = Image.new('RGB', (width, height), color)
    draw = ImageDraw.Draw(tile)
    text = '暂无封面'
    font = _font(max(12, int(width * 0.08)))
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(((width - (box[2] - box[0])) / 2,
               (height - (box[3] - box[1])) / 2),
              text, fill='#8d95a5', font=font)
    return tile


def _rating_key(item) -> int:
    try:
        rating = int(getattr(item, 'rating', 0) or 0)
    except (TypeError, ValueError):
        rating = 0
    return max(0, min(5, rating))


def _columns(count: int) -> int:
    best = None
    for cols in range(4, 16):
        if cols > count:
            break
        rows = _fit_rows(count, cols)
        surplus = len(_slots(cols, rows)) - count
        cell_width = _cell_width(cols)
        cell_height = cell_width * TILE_RATIO
        width = 2 * POSTER_MARGIN + cols * cell_width + (cols - 1) * CELL_GAP
        height = 2 * POSTER_MARGIN + rows * cell_height + (rows - 1) * CELL_GAP
        aspect = width / height
        score = abs(aspect - 0.85) * 2 + surplus * 0.05
        if best is None or score < best[0]:
            best = (score, cols)
    return best[1] if best else count


def _cell_width(cols: int) -> int:
    raw = (TARGET_WIDTH - 2 * POSTER_MARGIN - (cols - 1) * CELL_GAP) / cols
    return int(max(MIN_CELL_WIDTH, min(MAX_CELL_WIDTH, raw)))


def _large_candidates(cols: int, rows: int) -> list[tuple[int, int]]:
    if rows < 3 or cols < 3:
        return []
    return [
        (row, col)
        for row in range(1, rows - 2, 3)
        for col in range(1, cols - 1)
        if (col - row) % 4 == 0
    ]


def _slots(cols: int, rows: int) -> list[tuple[int, int, str]]:
    owners: dict[tuple[int, int], tuple[int, int]] = {}
    slots: list[tuple[int, int, str]] = []
    if rows >= 3 and cols >= 3:
        hero_row = max(0, (rows - 3) // 2)
        hero_col = max(0, (cols - 3) // 2)
        for dr in range(3):
            for dc in range(3):
                owners[(hero_row + dr, hero_col + dc)] = (hero_row, hero_col)
        slots.append((hero_row, hero_col, 'hero'))
    for row, col in _large_candidates(cols, rows):
        cells = [(row + dr, col + dc) for dr in (0, 1) for dc in (0, 1)]
        if any(cell in owners for cell in cells):
            continue
        for cell in cells:
            owners[cell] = (row, col)
        slots.append((row, col, 'large'))
    for row in range(rows):
        for col in range(cols):
            if (row, col) not in owners:
                slots.append((row, col, 'small'))
    return slots


def _fit_rows(count: int, cols: int) -> int:
    rows = max(1, math.ceil(count / cols))
    for _ in range(12):
        if len(_slots(cols, rows)) >= count:
            break
        rows += 1
    while rows > 1 and len(_slots(cols, rows - 1)) >= count:
        rows -= 1
    return rows


def _mosaic_layout(items: list):
    ordered = list(items)
    count = len(ordered)
    if count <= 6:
        return _grid_fallback(ordered)
    cols = _columns(count)
    rows = _fit_rows(count, cols)
    cell_width = _cell_width(cols)
    cell_height = int(cell_width * TILE_RATIO)
    width = POSTER_MARGIN * 2 + cols * cell_width + (cols - 1) * CELL_GAP
    height = POSTER_MARGIN * 2 + rows * cell_height + (rows - 1) * CELL_GAP
    center_x, center_y = width / 2, height / 2
    slots = []
    for row, col, kind in _slots(cols, rows):
        x0 = POSTER_MARGIN + col * (cell_width + CELL_GAP)
        y0 = POSTER_MARGIN + row * (cell_height + CELL_GAP)
        if kind == 'hero':
            w = cell_width * 3 + CELL_GAP * 2
            h = cell_height * 3 + CELL_GAP * 2
        elif kind == 'large':
            w = cell_width * 2 + CELL_GAP
            h = cell_height * 2 + CELL_GAP
        else:
            w, h = cell_width, cell_height
        rect = (x0, y0, x0 + w, y0 + h)
        distance = (x0 + w / 2 - center_x) ** 2 + (y0 + h / 2 - center_y) ** 2
        slots.append((distance, -w * h, rect, kind))
    hero_slots = [slot for slot in slots if slot[3] == 'hero'][:1]
    large_slots = sorted(
        (slot for slot in slots if slot[3] == 'large'),
        key=lambda slot: (slot[0], slot[1]),
    )
    small_slots = sorted(
        (slot for slot in slots if slot[3] == 'small'),
        key=lambda slot: (slot[0], slot[1]),
    )
    slots = [list(slot) for slot in hero_slots + large_slots + small_slots]
    assigned = list(zip(slots[:count], ordered))
    extras = slots[count:]
    usage: dict[int, int] = {}
    for _, item in assigned:
        usage[id(item)] = usage.get(id(item), 0) + 1
    if extras and assigned:
        for slot in extras:
            rect = slot[2]
            cx = (rect[0] + rect[2]) / 2
            cy = (rect[1] + rect[3]) / 2
            nearest_slot, nearest_item = min(
                assigned,
                key=lambda pair: ((pair[0][2][0] + pair[0][2][2]) / 2 - cx) ** 2
                + ((pair[0][2][1] + pair[0][2][3]) / 2 - cy) ** 2,
            )
            rating = _rating_key(nearest_item)
            pool = [
                pair for pair in assigned
                if _rating_key(pair[1]) == rating
                and usage.get(id(pair[1]), 0) < 2
            ]
            if not pool:
                pool = [
                    pair for pair in assigned if usage.get(id(pair[1]), 0) < 2
                ]
            if not pool:
                pool = assigned
            source_slot, source_item = max(
                pool,
                key=lambda pair: ((pair[0][2][0] + pair[0][2][2]) / 2 - cx) ** 2
                + ((pair[0][2][1] + pair[0][2][3]) / 2 - cy) ** 2,
            )
            usage[id(source_item)] = usage.get(id(source_item), 0) + 1
            assigned.append((slot, source_item))
    if slots:
        bottom = max(slot[2][3] for slot in slots)
        last_row = [slot for slot in slots
                    if slot[3] == 'small' and slot[2][3] == bottom]
        if last_row and len(last_row) < cols:
            last_row.sort(key=lambda slot: slot[2][0])
            tile_w = last_row[0][2][2] - last_row[0][2][0]
            left = POSTER_MARGIN
            right = width - POSTER_MARGIN
            if len(last_row) > 1:
                step = (right - left - len(last_row) * tile_w) / (len(last_row) - 1)
                for index, slot in enumerate(last_row):
                    x0 = left + index * (tile_w + step)
                    slot[2] = (x0, slot[2][1], x0 + tile_w, slot[2][3])
            else:
                x0 = (width - tile_w) / 2
                last_row[0][2] = (x0, last_row[0][2][1], x0 + tile_w, last_row[0][2][3])
    min_x = min(slot[2][0] for slot in slots)
    min_y = min(slot[2][1] for slot in slots)
    max_x = max(slot[2][2] for slot in slots)
    max_y = max(slot[2][3] for slot in slots)
    canvas_width = max_x - min_x + 2 * POSTER_MARGIN
    canvas_height = max_y - min_y + 2 * POSTER_MARGIN
    layout = []
    for slot, item in assigned:
        rect = slot[2]
        layout.append((
            (int(round(rect[0] - min_x)) + POSTER_MARGIN,
             int(round(rect[1] - min_y)) + POSTER_MARGIN,
             int(round(rect[2] - min_x)) + POSTER_MARGIN,
             int(round(rect[3] - min_y)) + POSTER_MARGIN),
            item,
        ))
    return layout, (canvas_width, canvas_height)


def _grid_fallback(items: list):
    count = len(items)
    cols = min(count, max(1, math.ceil(math.sqrt(count * 1.5))))
    tile_width = 520 if count <= 4 else 380
    tile_height = int(tile_width * TILE_RATIO)
    rows = math.ceil(count / cols)
    width = POSTER_MARGIN * 2 + cols * tile_width + (cols - 1) * CELL_GAP
    height = POSTER_MARGIN * 2 + rows * tile_height + (rows - 1) * CELL_GAP
    layout = []
    for index, item in enumerate(items):
        row, col = divmod(index, cols)
        row_count = min(cols, count - row * cols)
        row_width = row_count * tile_width + (row_count - 1) * CELL_GAP
        x0 = (width - row_width) // 2 + col * (tile_width + CELL_GAP)
        y0 = POSTER_MARGIN + row * (tile_height + CELL_GAP)
        layout.append(((x0, y0, x0 + tile_width, y0 + tile_height), item))
    return layout, (width, height)


def _rounded_mask(width: int, height: int, radius: int) -> Image.Image:
    mask = Image.new('L', (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, width - 1, height - 1), radius=radius, fill=255
    )
    return mask


def generate_poster_wall(items: Iterable, output_path: Path,
                         theme: str = 'dark') -> dict:
    item_list = list(items)
    if not item_list:
        raise PosterWallError('没有可用于生成海报墙的条目。')
    layout, (width, height) = _mosaic_layout(item_list)
    canvas_color = '#ffffff' if theme == 'light' else '#0f131b'
    canvas = Image.new('RGB', (width, height), canvas_color)
    used = 0
    bleed = CELL_GAP // 2
    for rect, item in layout:
        x0 = rect[0] - bleed
        y0 = rect[1] - bleed
        x1 = rect[2] + bleed
        y1 = rect[3] + bleed
        tile_width = max(1, x1 - x0)
        tile_height = max(1, y1 - y0)
        image = _load_image(item)
        if image is not None:
            used += 1
            tile = ImageOps.fit(
                image, (tile_width, tile_height), method=Image.LANCZOS,
                centering=(0.5, 0.5),
            )
            radius = max(6, min(tile_width, tile_height) // 18)
            tile = tile.filter(ImageFilter.GaussianBlur(radius=radius))
            backdrop = canvas_color if theme == 'light' else '#0f131b'
            strength = 0.75 if theme == 'light' else 0.5
            tile = Image.blend(tile, Image.new('RGB', tile.size, backdrop), strength)
            scaled = ImageOps.contain(
                image, (tile_width, tile_height), method=Image.LANCZOS
            )
            tile.paste(
                scaled,
                ((tile_width - scaled.width) // 2, (tile_height - scaled.height) // 2),
            )
        else:
            tile = _placeholder_tile(tile_width, tile_height, item)
        canvas.paste(
            tile, (x0, y0),
            _rounded_mask(tile_width, tile_height, TILE_RADIUS),
        )
    if used == 0:
        raise PosterWallError('没有可用的封面图片，无法生成海报墙。')
    canvas.thumbnail((POSTER_MAX_WIDTH, POSTER_MAX_HEIGHT), Image.LANCZOS)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format='JPEG', quality=POSTER_JPEG_QUALITY, optimize=True)
    return {
        'path': str(output_path),
        'used': used,
        'total': len(item_list),
        'width': canvas.width,
        'height': canvas.height,
    }
