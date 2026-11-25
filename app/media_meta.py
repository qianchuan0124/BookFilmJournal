from __future__ import annotations

import html
import json
import os
import random
import re
import time
from functools import lru_cache
from typing import Callable, Optional

from app.config import BOOK_META_CACHE, MOVIE_META_CACHE
from app.words import format_word_count

GENRES = {
    '剧情', '喜剧', '动作', '爱情', '科幻', '动画', '悬疑', '惊悚', '恐怖', '犯罪',
    '同性', '音乐', '歌舞', '传记', '历史', '战争', '西部', '奇幻', '冒险', '灾难',
    '武侠', '古装', '运动', '家庭', '儿童', '纪录片', '短片', '情色', '黑色电影',
    '真人秀', '脱口秀',
}
BOOK_CATEGORY_OVERRIDES = {
    '金赛性学报告': '医学健康',
    '1007天的战争': '历史',
    '小狗钱钱2': '经济理财', '小狗钱钱': '经济理财',
    '领袖们': '传记',
    '巴菲特致股东的信 (第4版)': '经济理财',
    '菊与刀': '社会科学',
    '社会契约论': '政治哲学',
    '黑天鹅': '经济理财',
    '三体Ⅲ': '科幻', '三体Ⅱ': '科幻', '三体': '科幻', '三体全集': '科幻',
    '神们自己': '科幻', '全频带阻塞干扰': '科幻', '球状闪电': '科幻',
    '银河帝国：基地七部曲': '科幻', '永恒的终结': '科幻',
    '神秘岛': '科幻', '海底两万里': '科幻',
    '货币金融学（第十三版）': '经济理财',
    '聪明的投资者（第4版注疏点评版）': '经济理财', '聪明的投资者': '经济理财',
    '经济学原理（第5版）': '经济理财', '经济学原理': '经济理财', '经济学原理（上下）': '经济理财',
    '叫魂': '历史', '中国历代政治得失': '历史', '筚路维艰': '历史',
    '翦商': '历史', '万历十五年': '历史', '明朝那些事儿（1-9）': '历史', '人类简史': '历史',
    '倚天屠龙记': '武侠', '射雕英雄传': '武侠', '笑傲江湖（全四册）': '武侠',
    '冰与火之歌': '奇幻', '诡秘之主 1': '奇幻', '道诡异仙': '奇幻',
    '白夜行': '推理悬疑', '恶意': '推理悬疑',
    '被讨厌的勇气': '心理学', '刻意练习': '心理学', '认知觉醒（百万册精装纪念版）': '心理学',
    '亲密关系（第6版）': '心理学', '人性的弱点': '心理学',
    '风光摄影后期基础': '摄影艺术', '摄影笔记': '摄影艺术',
    '娱乐至死': '社会科学', '生育制度': '社会科学',
    '君主论': '政治哲学',
    '富爸爸，穷爸爸': '经济理财', '国富论': '经济理财', '置身事内': '经济理财',
}


def e(text) -> str:
    return html.escape(str(text if text is not None else ''))


def short(text: str, limit: int) -> str:
    text = re.sub(r'\s+', ' ', text or '').strip()
    return text if len(text) <= limit else text[:limit].rstrip() + '…'


def display_title(title: str) -> str:
    return (title or '').split(' / ')[0].strip() or title


def dot_date(date_str: str) -> str:
    return (date_str or '').replace('-', '.')


def parse_duration(intro: str) -> Optional[int]:
    text = intro or ''
    hours = re.search(r'(\d+)\s*(?:小时|时)', text)
    # 兼容「138 分钟」和「3 小时 38 分」两种写法。
    minutes = re.search(r'(\d+)\s*分(?:钟)?', text)
    if not hours and not minutes:
        return None
    return int(hours.group(1) if hours else 0) * 60 + int(minutes.group(1) if minutes else 0)


def parse_year(intro: str) -> Optional[int]:
    match = re.search(r'(?<!\d)(19|20)\d{2}(?!\d)', intro or '')
    return int(match.group(0)) if match else None


def parse_genres(intro: str) -> list[str]:
    found = []
    for token in (intro or '').split('/'):
        token = token.strip()
        if token in GENRES and token not in found:
            found.append(token)
    return found


def parse_pages(values) -> Optional[int]:
    for value in values or []:
        match = re.search(r'\d+', str(value))
        if match:
            return int(match.group(0))
    return None


def book_category(title: str) -> str:
    return BOOK_CATEGORY_OVERRIDES.get(title, '文学小说')


def format_duration(minutes: Optional[int]) -> str:
    if not minutes:
        return '—'
    hours, rest = divmod(minutes, 60)
    return f'{hours} 小时 {rest} 分' if hours else f'{rest} 分钟'


def stars(rating: int) -> str:
    if rating <= 0:
        return '<span class="muted">未评分</span>'
    return f'<span class="gold">{"★" * rating}{"☆" * (5 - rating)}</span>'


@lru_cache(maxsize=1)
def load_book_meta() -> dict:
    if not BOOK_META_CACHE.exists():
        return {}
    with open(BOOK_META_CACHE, encoding='utf-8') as f:
        return json.load(f)


def book_pages(subject_id) -> Optional[int]:
    meta = load_book_meta().get(str(subject_id), {})
    return parse_pages(meta.get('pages'))


def manual_book_pages(subject_id) -> Optional[int]:
    meta = load_book_meta().get(str(subject_id), {})
    if meta.get('pages_manual'):
        return parse_pages(meta.get('pages'))
    return None


def set_book_pages(subject_id, pages: Optional[int], db=None) -> None:
    cache = _read_json(BOOK_META_CACHE)
    entry = cache.setdefault(str(subject_id), {})
    entry['pages'] = [str(int(pages))] if pages else []
    entry['pages_manual'] = True
    _write_json(BOOK_META_CACHE, cache)
    load_book_meta.cache_clear()
    if db is not None:
        db.set_book_pages(str(subject_id), pages, manual=True)


def _cached_page_entries() -> list[dict]:
    entries = []
    for subject_id, entry in _read_json(BOOK_META_CACHE).items():
        pages = parse_pages(entry.get('pages'))
        if not pages and not entry.get('pages_manual'):
            continue
        entries.append({'subject_id': str(subject_id), 'pages': pages or 0,
                        'pages_manual': bool(entry.get('pages_manual'))})
    return entries


def sync_book_pages(db, import_local: bool = False) -> None:
    """数据库是页数的持久来源，本机缓存只作为读取加速。

    import_local=True 时先把本机缓存里的历史页数补进数据库，只在用户显式
    操作（点击「立即备份」）时调用，避免把本地可能过时的数据悄悄写进库；
    默认只把数据库里的页数写回本机缓存，用于恢复快照后补齐本机缓存。
    """
    if import_local:
        db.import_book_pages(_cached_page_entries())
    cache = _read_json(BOOK_META_CACHE)
    changed = False
    for entry in db.book_page_entries():
        item = cache.setdefault(entry['subject_id'], {})
        values = [str(entry['pages'])] if entry['pages'] else []
        if item.get('pages') != values:
            item['pages'] = values
            changed = True
        if bool(item.get('pages_manual')) != entry['pages_manual']:
            item['pages_manual'] = entry['pages_manual']
            changed = True
    if changed:
        _write_json(BOOK_META_CACHE, cache)
    load_book_meta.cache_clear()


@lru_cache(maxsize=1)
def load_movie_meta() -> dict:
    if not MOVIE_META_CACHE.exists():
        return {}
    with open(MOVIE_META_CACHE, encoding='utf-8') as f:
        return json.load(f)


def manual_movie_duration(subject_id) -> Optional[int]:
    meta = load_movie_meta().get(str(subject_id), {})
    minutes = meta.get('duration_manual')
    try:
        value = int(minutes)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def set_movie_duration(subject_id, minutes: Optional[int]) -> None:
    cache = _read_json(MOVIE_META_CACHE)
    entry = cache.setdefault(str(subject_id), {})
    entry['duration_manual'] = int(minutes or 0)
    _write_json(MOVIE_META_CACHE, cache)
    load_movie_meta.cache_clear()


def movie_duration(subject_id) -> Optional[int]:
    manual = manual_movie_duration(subject_id)
    if manual:
        return manual
    meta = load_movie_meta().get(str(subject_id), {})
    for value in meta.get('durations') or []:
        minutes = parse_duration(str(value))
        if minutes:
            return minutes
    return parse_duration(meta.get('intro') or '')


def format_movie_stats(minutes: Optional[int]) -> str:
    return format_duration(minutes) if minutes else '时长未知'


def movie_stats_text(intro: str = '', subject_id='') -> str:
    minutes = manual_movie_duration(subject_id) if subject_id else None
    if not minutes:
        minutes = parse_duration(intro or '')
    if not minutes and subject_id:
        minutes = movie_duration(subject_id)
    return format_movie_stats(minutes)


def _read_json(path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')


def _fetch_movie_mobile(session: requests.Session, subject_id: str) -> Optional[dict]:
    import requests

    try:
        response = session.get(
            f'https://m.douban.com/movie/subject/{subject_id}/',
            headers={'Referer': f'https://movie.douban.com/subject/{subject_id}/'},
            timeout=20,
        )
    except requests.RequestException:
        return None
    if response.status_code != 200:
        return None
    match = re.search(r'(\d+)\s*分钟', response.text)
    if not match:
        return None
    return {'intro': '', 'durations': [f'{match.group(1)}分钟'], 'pubdate': []}


def _meta_is_complete(media_type: str, entry: Optional[dict]) -> bool:
    if not entry:
        return False
    if media_type == 'book':
        if entry.get('pages_checked'):
            return True
        return any(re.search(r'\d+', str(value)) for value in entry.get('pages') or [])
    if entry.get('durations_checked'):
        return True
    return bool(entry.get('durations'))


def ensure_subject_meta(media_type: str, subjects,
                        progress: Optional[Callable[[str, int, int], None]] = None,
                        delay: float = 0.4, db=None) -> dict:
    import requests

    if media_type not in ('book', 'movie'):
        raise ValueError('media_type must be book or movie')
    cache_path = BOOK_META_CACHE if media_type == 'book' else MOVIE_META_CACHE
    cache = _read_json(cache_path)
    unique = {}
    for subject in subjects:
        subject_id = str(subject.get('subject_id') or '')
        if subject_id and subject_id not in unique:
            unique[subject_id] = subject
    missing = [(subject_id, subject) for subject_id, subject in unique.items()
               if not _meta_is_complete(media_type, cache.get(subject_id))]
    if not missing:
        return {'fetched': 0, 'failed': 0, 'skipped': len(unique)}
    if progress:
        label = '书籍' if media_type == 'book' else '影视'
        progress(f'补全{label}信息：剩余 {len(missing)} 条', 0, len(missing))
    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
                                          'AppleWebKit/605.1.15 (KHTML, like Gecko) '
                                          'Version/17.0 Mobile/15E148 Safari/604.1'})
    fetched = 0
    failed = 0
    failure_streak = 0
    # 基准间隔很短，命中风控后自动放慢；每抓一批再停顿一次，兼顾速度与安全。
    base_delay = max(0.05, float(delay))
    current_delay = base_delay
    batch_size = 25
    batch_pause = 3.0
    for index, (subject_id, subject) in enumerate(missing, 1):
        title = subject.get('title') or subject_id
        if progress:
            progress(f'补全信息：{title}', index, len(missing))
        host = 'book' if media_type == 'book' else 'movie'
        url = f'https://m.douban.com/rexxar/api/v2/{host}/{subject_id}?ck=&for_mobile=1'
        data = None
        for attempt in range(2):
            try:
                response = session.get(
                    url,
                    headers={'Referer': f'https://{host}.douban.com/subject/{subject_id}/'},
                    timeout=15,
                )
                if response.status_code in (403, 418, 429):
                    if attempt == 0:
                        current_delay = min(2.0, max(0.5, current_delay * 1.8))
                        time.sleep(5)
                        continue
                    break
                if response.status_code != 200:
                    raise RuntimeError(f'HTTP {response.status_code}')
                data = response.json()
                break
            except Exception:
                if attempt == 0:
                    time.sleep(1.5)
        if media_type == 'movie' and (data is None or not data.get('durations')):
            mobile = _fetch_movie_mobile(session, subject_id)
            if mobile:
                data = mobile
        if data is None:
            failed += 1
            failure_streak += 1
            if failure_streak >= 5:
                break
            continue
        failure_streak = 0
        fetched += 1
        current_delay = max(base_delay, current_delay * 0.95)
        previous = cache.get(subject_id) or {}
        if media_type == 'book':
            entry = {
                'intro': data.get('intro') or '',
                'pages': data.get('pages') or [],
                'press': data.get('press') or [],
                'pubdate': data.get('pubdate') or [],
                'pages_checked': True,
            }
            if previous.get('pages_manual'):
                entry['pages'] = previous.get('pages') or []
                entry['pages_manual'] = True
            cache[subject_id] = entry
            if db is not None:
                # 页数固化进数据库，随快照同步到其他设备，无需再次补全。
                db.set_book_pages(subject_id, parse_pages(entry['pages']),
                                  manual=bool(entry.get('pages_manual')))
        else:
            entry = {
                'intro': data.get('intro') or '',
                'durations': data.get('durations') or [],
                'pubdate': data.get('pubdate') or [],
                'durations_checked': True,
            }
            if previous.get('duration_manual'):
                entry['duration_manual'] = previous['duration_manual']
            cache[subject_id] = entry
        if fetched % 10 == 0:
            _write_json(cache_path, cache)
        if fetched % batch_size == 0:
            time.sleep(batch_pause)
        time.sleep(current_delay + random.uniform(0, 0.2))
    _write_json(cache_path, cache)
    if media_type == 'book':
        load_book_meta.cache_clear()
    else:
        load_movie_meta.cache_clear()
    return {'fetched': fetched, 'failed': failed, 'skipped': len(unique) - len(missing)}


def import_movie_durations(items) -> int:
    """把 iOS 快照 sidecar 里的影片时长写入本机缓存（不覆盖手动值）。"""
    cache = _read_json(MOVIE_META_CACHE)
    imported = 0
    for subject_id, minutes in items:
        try:
            value = int(minutes)
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        entry = cache.setdefault(str(subject_id), {})
        if entry.get('duration_manual') or entry.get('durations'):
            continue
        entry['durations'] = [f'{value}分钟']
        imported += 1
    if imported:
        _write_json(MOVIE_META_CACHE, cache)
        load_movie_meta.cache_clear()
    return imported


def format_book_stats(pages: Optional[int], word_count: int = 0) -> str:
    words = format_word_count(int(word_count or 0))
    if words:
        return words
    return f'{pages:,} 页' if pages else '页数未知'


def book_stats_text(subject_id, word_count: int = 0) -> str:
    return format_book_stats(book_pages(subject_id), word_count)


def format_book_stats_full(pages: Optional[int], word_count: int = 0) -> str:
    """计划等场景同时展示页数与字数，两者都缺省时才显示未知。"""
    parts = []
    if pages:
        parts.append(f'{pages:,} 页')
    words = format_word_count(int(word_count or 0))
    if words:
        parts.append(words)
    return ' · '.join(parts) if parts else '页数/字数未知'


def book_stats_text_full(subject_id, word_count: int = 0) -> str:
    return format_book_stats_full(book_pages(subject_id), word_count)
