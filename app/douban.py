import html
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from app.config import AVATAR_DIR, CACHE_DIR, COVERS_DIR, MEDIA_LABELS, ROOT
from app.db import Database
from app.media_meta import ensure_subject_meta

_UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
       'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')
_UA_MOBILE = ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
              'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1')
PAGE_SIZE = 15
RATING_RE = re.compile(r'^rating(\d)-t$')


class DoubanError(Exception):
    pass


def _upgrade_cover(url: str) -> str:
    if url.startswith('//'):
        url = 'https:' + url
    if 's_ratio_poster' in url:
        return url.replace('s_ratio_poster', 'm_ratio_poster')
    if '/subject/s/' in url:
        return url.replace('/subject/s/', '/subject/m/')
    return url


def _parse_date(text: str) -> str:
    match = re.search(r'\d{4}-\d{2}-\d{2}', text or '')
    return match.group(0) if match else (text or '').strip()


def extract_user_id_from_cookie(cookie: str) -> str:
    text = cookie or ''
    match = re.search(r'(?:^|;\s*)dbcl2="?(\d+):', text)
    if match:
        return match.group(1)
    match = re.search(r'(?:^|;\s*)dbcl2="?(\d+)', text)
    return match.group(1) if match else ''


def _parse_page(html_text: str, media_type: str) -> list[dict]:
    soup = BeautifulSoup(html_text, 'html.parser')
    nodes = soup.select('li.subject-item') or soup.select('div.item')
    records = []
    for node in nodes:
        book_item = 'subject-item' in (node.get('class') or [])
        if book_item:
            link = node.select_one('h2 a[href]') or node.select_one('div.pic a.nbg[href]')
            intro_node = node.select_one('div.pub')
            comment_node = node.select_one('p.comment')
        else:
            link = node.select_one('li.title a[href]') or node.select_one('div.pic a.nbg[href]')
            intro_node = node.select_one('li.intro')
            comment_node = node.select_one('span.comment')
        if not link:
            continue
        if book_item:
            title = link.get('title') or link.get_text(' ', strip=True)
        else:
            em = link.select_one('em')
            title = em.get_text(' ', strip=True) if em else link.get_text(' ', strip=True)
        rating = 0
        rating_span = node.find('span', class_=RATING_RE)
        if rating_span:
            rating = int(rating_span['class'][0][len('rating')])
        date_node = node.select_one('span.date')
        cover_node = node.select_one('div.pic img')
        match = re.search(r'subject/(\d+)', link.get('href', ''))
        subject_id = match.group(1) if match else ''
        if not subject_id:
            continue
        records.append({
            'media_type': media_type,
            'subject_id': subject_id,
            'title': re.sub(r'\s+', ' ', title).strip(),
            'url': link.get('href', ''),
            'cover_url': _upgrade_cover(cover_node.get('src', '')) if cover_node else '',
            'rating': rating,
            'marked_date': _parse_date(date_node.get_text(' ', strip=True)) if date_node else '',
            'comment': comment_node.get_text(' ', strip=True) if comment_node else '',
            'intro': intro_node.get_text(' ', strip=True) if intro_node else '',
        })
    return records


def _request(session: requests.Session, url: str, headers: dict, timeout: float = 20,
             retries: int = 2) -> requests.Response:
    for attempt in range(retries + 1):
        try:
            response = session.get(url, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            if attempt == retries:
                raise DoubanError(f'网络请求失败：{exc}') from exc
            time.sleep(2 * (attempt + 1))
            continue
        if response.status_code in (403, 418, 429) or response.status_code >= 500:
            if attempt == retries:
                hint = '豆瓣拒绝访问或触发风控，请稍后重试；私密收藏/豆列可在同步窗口填写 Cookie。'
                raise DoubanError(f'HTTP {response.status_code}：{hint}')
            time.sleep(5 * (attempt + 1))
            continue
        if 'accounts.douban.com' in response.url:
            raise DoubanError('请求跳转到登录页，该内容可能仅自己可见，请提供 Cookie。')
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise DoubanError(f'HTTP {response.status_code}') from exc
        return response
    raise DoubanError('请求失败')


def _join_values(value) -> str:
    if isinstance(value, (list, tuple)):
        return ' / '.join(str(item).strip() for item in value if str(item).strip())
    return str(value or '').strip()


def _stars_from_score(score) -> int:
    try:
        value = float(score)
    except (TypeError, ValueError):
        return 0
    return max(0, min(5, int(round(value / 2))))


def _with_subtitle(title: str, subtitle) -> str:
    """把副标题合并进主标题（豆瓣的书籍副标题在 h2.subtitle / API subtitle 字段）"""
    if isinstance(subtitle, (list, tuple)):
        subtitle = ' '.join(str(item) for item in subtitle if item)
    subtitle = re.sub(r'\s+', ' ', str(subtitle or '')).strip()
    subtitle = re.sub(r'^副标题\s*[:：]\s*', '', subtitle)
    if not subtitle or subtitle in title:
        return title
    return f'{title}：{subtitle}' if title else subtitle


def _subtitle_cache_path():
    return CACHE_DIR / 'book_subtitles.json'


def _load_subtitle_cache() -> dict:
    try:
        return json.loads(_subtitle_cache_path().read_text('utf-8'))
    except (OSError, ValueError):
        return {}


def _save_subtitle_cache(cache: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _subtitle_cache_path().write_text(json.dumps(cache, ensure_ascii=False), 'utf-8')
    except OSError:
        pass


def _subtitle_text(value) -> str:
    if isinstance(value, (list, tuple)):
        value = ' '.join(str(item) for item in value if item)
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def _fetch_book_subtitle(session: requests.Session, subject_id: str) -> str:
    url = f'https://m.douban.com/rexxar/api/v2/book/{subject_id}?ck=&for_mobile=1'
    headers = {
        'User-Agent': _UA_MOBILE,
        'Referer': f'https://book.douban.com/subject/{subject_id}/',
    }
    response = _request(session, url, headers, retries=1)
    try:
        data = response.json()
    except ValueError:
        return ''
    return _subtitle_text(data.get('subtitle'))


def _enrich_book_titles(records: list[dict], session: requests.Session,
                        progress: Optional[Callable[[str, int, int], None]] = None) -> None:
    """豆瓣收藏列表只给主标题，这里按 subject 逐本补全副标题（带本地缓存）"""
    cache = _load_subtitle_cache()
    pending = [item for item in records if '：' not in (item.get('title') or '')]
    if not pending:
        return
    total = len(pending)
    handled = 0
    failures = 0
    dirty = False
    for start in range(0, total, 12):
        if failures >= 3:
            break
        chunk = pending[start:start + 12]
        todo = [item for item in chunk if str(item['subject_id']) not in cache]
        fetched: dict[str, str] = {}
        if todo:
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {
                    pool.submit(_fetch_book_subtitle, session, str(item['subject_id'])): item
                    for item in todo
                }
                for future in as_completed(futures):
                    record = futures[future]
                    try:
                        fetched[str(record['subject_id'])] = future.result()
                        failures = 0
                    except Exception:
                        failures += 1
        for record in chunk:
            subject_id = str(record['subject_id'])
            if subject_id in fetched:
                cache[subject_id] = fetched[subject_id]
                dirty = True
            subtitle = cache.get(subject_id, '')
            if subtitle:
                record['title'] = _with_subtitle(record.get('title') or '', subtitle)
        handled += len(chunk)
        if progress:
            progress(f'补全书籍副标题 {handled}/{total}', handled, total)
    if dirty:
        _save_subtitle_cache(cache)


def _detail_from_api(data: dict, subject_id: str, media_type: str) -> dict:
    pic = data.get('pic') or {}
    cover_url = pic.get('large') or pic.get('normal') or data.get('cover_url') or ''
    title = re.sub(r'\s+', ' ', str(data.get('title') or '')).strip()
    rating = data.get('rating')
    score = rating.get('value') if isinstance(rating, dict) else rating
    intro_parts = []
    pages = None
    duration = None
    if media_type == 'movie':
        year = str(data.get('year') or '')
        intro_parts.append(year)
        intro_parts.append(_join_values(data.get('genres')))
        intro_parts.append(_join_values(data.get('durations')))
        intro_parts.append(_join_values(data.get('countries')))
        duration = parse_duration_from_text(_join_values(data.get('durations')))
    else:
        title = _with_subtitle(title, data.get('subtitle'))
        intro_parts.append(_join_values(data.get('author')))
        intro_parts.append(_join_values(data.get('press')))
        intro_parts.append(_join_values(data.get('pubdate')))
        page_values = data.get('pages') or []
        for value in page_values:
            match = re.search(r'\d+', str(value))
            if match:
                pages = int(match.group(0))
                break
        if pages:
            intro_parts.append(f'{pages} 页')
    intro = ' / '.join(part for part in intro_parts if part)
    return {
        'subject_id': subject_id,
        'media_type': media_type,
        'title': title,
        'url': f'https://{("book" if media_type == "book" else "movie")}.douban.com/subject/{subject_id}/',
        'cover_url': cover_url,
        'intro': intro,
        'rating': _stars_from_score(score),
        'pages': pages,
        'duration': duration,
    }


def _info_value(info_text: str, label: str, stop_labels: tuple[str, ...]) -> str:
    if not info_text:
        return ''
    stop = '|'.join(re.escape(item) for item in stop_labels)
    match = re.search(re.escape(label) + r'\s*[:：]\s*(.*?)(?:\s*(?:' + stop + r')\s*[:：]|$)',
                      info_text)
    return re.sub(r'\s+', ' ', match.group(1)).strip() if match else ''


def _detail_from_html(html_text: str, subject_id: str, media_type: str) -> dict:
    soup = BeautifulSoup(html_text, 'html.parser')
    title_node = (soup.select_one('h1 span[property="v:itemreviewed"]')
                  or soup.select_one('h1'))
    title = title_node.get_text(' ', strip=True) if title_node else ''
    title = re.sub(r'\s*\(\s*豆瓣\s*\)\s*$', '', re.sub(r'\s+', ' ', title)).strip()
    cover_node = (soup.select_one('#mainpic img')
                  or soup.select_one('img[rel="v:image"]'))
    cover_url = ''
    if cover_node:
        cover_url = (_upgrade_cover(cover_node.get('src', ''))
                     or cover_node.get('data-src', ''))
    rating_node = soup.select_one('strong.rating_num') or soup.select_one('[property="v:average"]')
    rating = _stars_from_score(rating_node.get_text(strip=True)) if rating_node else 0
    info_node = soup.select_one('#info')
    info_text = info_node.get_text(' ', strip=True) if info_node else ''
    pages = None
    duration = None
    intro_parts = []
    if media_type == 'book':
        tail_labels = ('页数', '定价', '装帧', 'ISBN', '副标题', '出版时间')
        author = _info_value(info_text, '作者',
                             ('出版社', '出版年', '出版时间', '页数', '定价', '装帧', 'ISBN',
                              '副标题', '原作名', '译者'))
        press = _info_value(info_text, '出版社', ('出版年', '出版时间') + tail_labels)
        pubdate = (_info_value(info_text, '出版年', tail_labels)
                   or _info_value(info_text, '出版时间', tail_labels))
        pages_text = _info_value(info_text, '页数', ('定价', '装帧', 'ISBN', '副标题'))
        match = re.search(r'\d+', pages_text)
        pages = int(match.group(0)) if match else None
        subtitle_node = (soup.select_one('h2.subtitle span[property="v:subtitle"]')
                         or soup.select_one('h2.subtitle'))
        subtitle = subtitle_node.get_text(' ', strip=True) if subtitle_node else ''
        if not subtitle:
            subtitle = _info_value(info_text, '副标题',
                                   ('原作名', '译者', '出版社', '出版年', '页数', '定价',
                                    '装帧', 'ISBN'))
        title = _with_subtitle(title, subtitle)
        intro_parts = [author, press, pubdate, f'{pages} 页' if pages else '']
    else:
        year = _info_value(info_text, '上映日期', ('片长', '又名', 'IMDb', '类型', '制片国家'))
        year_match = re.search(r'(19|20)\d{2}', year)
        genres = _info_value(info_text, '类型', ('制片国家', '语言', '上映日期', '片长', '又名', 'IMDb'))
        prod = _info_value(info_text, '制片国家/地区', ('语言', '上映日期', '片长', '又名', 'IMDb'))
        duration_text = _info_value(info_text, '片长', ('又名', 'IMDb', '类型', '制片国家'))
        duration = parse_duration_from_text(duration_text)
        intro_parts = [year_match.group(0) if year_match else '', genres, duration_text, prod]
    intro = ' / '.join(part for part in intro_parts if part)
    return {
        'subject_id': subject_id,
        'media_type': media_type,
        'title': title,
        'url': f'https://{("book" if media_type == "book" else "movie")}.douban.com/subject/{subject_id}/',
        'cover_url': cover_url,
        'intro': intro,
        'rating': rating,
        'pages': pages,
        'duration': duration,
    }


def parse_duration_from_text(text: str) -> Optional[int]:
    match = re.search(r'(\d+)\s*分钟', text or '')
    if match:
        return int(match.group(1))
    match = re.search(r'(\d+)\s*(?:小时|h)', text or '', re.IGNORECASE)
    if match:
        return int(match.group(1)) * 60
    return None


def fetch_subject_detail(subject_id: str, media_type: str, cookie: str = '') -> dict:
    subject_id = str(subject_id or '').strip()
    if not subject_id.isdigit():
        raise DoubanError('豆瓣链接中的条目 ID 无效。')
    host = 'book' if media_type == 'book' else 'movie'
    page_url = f'https://{host}.douban.com/subject/{subject_id}/'
    headers = {'User-Agent': _UA, 'Referer': page_url}
    if cookie.strip():
        headers['Cookie'] = cookie.strip()
    session = requests.Session()
    session.headers.update({'User-Agent': _UA})
    api_url = f'https://m.douban.com/rexxar/api/v2/{host}/{subject_id}?ck=&for_mobile=1'
    try:
        response = _request(session, api_url, {**headers, 'User-Agent': _UA_MOBILE}, retries=1)
        data = response.json()
    except (DoubanError, ValueError):
        data = None
    if isinstance(data, dict) and data.get('title'):
        detail = _detail_from_api(data, subject_id, media_type)
        if detail['title']:
            return detail
    response = _request(session, page_url, headers)
    detail = _detail_from_html(response.text, subject_id, media_type)
    if not detail['title']:
        raise DoubanError('未能解析该豆瓣页面，请检查链接是否正确。')
    return detail


_IMAGE_SIGNATURES = (
    (0, b'\xff\xd8\xff'),        # JPEG
    (0, b'\x89PNG\r\n\x1a\n'),   # PNG
    (0, b'GIF87a'),
    (0, b'GIF89a'),
)

# 同一张封面图在 img1/img2/img3/img9 上互为镜像。豆瓣会对个别域名返回反爬挑战页，
# 换一个镜像域名通常就能拿到真图。
_COVER_MIRRORS = ('img1.doubanio.com', 'img2.doubanio.com',
                  'img3.doubanio.com', 'img9.doubanio.com')


def _image_header_ok(head: bytes) -> bool:
    return (any(head.startswith(sig) for offset, sig in _IMAGE_SIGNATURES if offset == 0)
            or (head[:4] == b'RIFF' and head[8:12] == b'WEBP'))


def _looks_like_image(data: bytes) -> bool:
    """被反爬时豆瓣会返回 200 + 一段约 1 KB 的 JS 挑战页，只能靠文件头判断真伪。"""
    return len(data) > 500 and _image_header_ok(data[:12])


def cover_file_ok(path: Path) -> bool:
    """本地已有的封面文件是否真的是图片（旧版本可能把挑战页存成了 .jpg）。"""
    try:
        if path.stat().st_size <= 500:
            return False
        with path.open('rb') as handle:
            return _image_header_ok(handle.read(12))
    except OSError:
        return False


def _cover_relative(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _cover_mirrors(url: str) -> list[str]:
    parts = urlsplit(url)
    host = (parts.hostname or '').lower()
    if not host.endswith('doubanio.com'):
        return [url]
    candidates = [url]
    for mirror in _COVER_MIRRORS:
        if mirror == host:
            continue
        candidates.append(urlunsplit(
            (parts.scheme or 'https', mirror, parts.path, parts.query, '')
        ))
    return candidates


def _fetch_cover_bytes(session: requests.Session, url: str, referer: str = '') -> bytes:
    """按镜像依次尝试，只返回真正的图片内容。"""
    if not url:
        return b''
    headers = {'User-Agent': _UA, 'Referer': referer or 'https://www.douban.com/'}
    for candidate in _cover_mirrors(_upgrade_cover(url)):
        try:
            response = session.get(candidate, headers=headers, timeout=20)
        except requests.RequestException:
            continue
        if response.status_code == 200 and _looks_like_image(response.content):
            return response.content
    return b''


def _write_cover(path: Path, data: bytes) -> str:
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.part')
    temp.write_bytes(data)
    os.replace(temp, path)
    return _cover_relative(path)


def fetch_cover(cover_url: str, media_type: str, subject_id: str, referer: str = '',
                force: bool = False) -> str:
    url = _upgrade_cover(cover_url or '')
    if not url:
        return ''
    path = COVERS_DIR / f'{media_type}_{subject_id}.jpg'
    if not force and cover_file_ok(path):
        return _cover_relative(path)
    data = _fetch_cover_bytes(requests.Session(), url, referer)
    if not data:
        return ''
    return _write_cover(path, data)


def fetch_collect(media_type: str, user_id: str, cookie: str = '',
                  progress: Optional[Callable[[str, int, int], None]] = None,
                  kind: str = 'collect', allow_empty: bool = False
                  ) -> tuple[list[dict], str, str]:
    profile_url = f'https://{media_type}.douban.com/people/{user_id}/'
    headers = {'User-Agent': _UA, 'Referer': profile_url}
    if cookie.strip():
        headers['Cookie'] = cookie.strip()
    session = requests.Session()
    session.headers.update({'User-Agent': _UA})
    records: list[dict] = []
    profile_name = ''
    profile_avatar = ''
    start = 0
    page = 1
    label = '想看/想读' if kind == 'wish' else MEDIA_LABELS[media_type] + '记录'
    while True:
        url = (f'{profile_url}{kind}?start={start}&sort=time&rating=all'
               f'&filter=all&mode=grid')
        response = _request(session, url, headers)
        if page == 1:
            profile_name = _parse_profile_name(response.text)
            profile_avatar = _parse_profile_avatar(response.text)
        page_records = _parse_page(response.text, media_type)
        if not page_records:
            if page == 1 and not allow_empty:
                raise DoubanError(f'没有读取到{label}，可能是空收藏或未公开。')
            break
        records.extend(page_records)
        if progress:
            progress(f'{label}：第 {page} 页，累计 {len(records)} 条', len(records), 0)
        if len(page_records) < PAGE_SIZE:
            break
        start += PAGE_SIZE
        page += 1
        time.sleep(3)
    return records, profile_name, profile_avatar


def _parse_profile_name(html_text: str) -> str:
    soup = BeautifulSoup(html_text, 'html.parser')
    avatar = soup.select_one('#db-usr-profile .pic img')
    text = (avatar.get('alt') or '').strip() if avatar else ''
    if not text:
        node = soup.select_one('#db-usr-profile .info h1')
        text = node.get_text(' ', strip=True) if node else ''
    if not text and soup.title:
        text = soup.title.get_text(' ', strip=True)
    text = html.unescape(text).strip()
    text = re.sub(r'的(?:读书|观影|书影|音乐|舞台剧|游戏|同城|小组)主页$', '', text)
    text = re.split(r'(?:读过|看过|听过|想看|想读|想听)', text)[0].strip()
    text = re.sub(r'\s*\(\d+\)$', '', text)
    return text


def _parse_profile_avatar(html_text: str) -> str:
    soup = BeautifulSoup(html_text, 'html.parser')
    for selector in ('#db-usr-profile .pic img', '.user-info .pic img',
                     'img.avatar'):
        node = soup.select_one(selector)
        if node and node.get('src'):
            return node['src'].strip()
    return ''


def _download_douban_avatar(url: str, session: requests.Session,
                            referer: str = 'https://book.douban.com/') -> str:
    url = (url or '').strip()
    if not url:
        return ''
    if url.startswith('//'):
        url = 'https:' + url
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    target = AVATAR_DIR / 'douban_avatar.jpg'
    headers = {'User-Agent': _UA}
    if referer:
        headers['Referer'] = referer
    try:
        response = _request(session, url, headers, timeout=15)
        if len(response.content) < 200:
            return ''
        target.write_bytes(response.content)
    except Exception:
        return ''
    try:
        return str(target.relative_to(ROOT))
    except ValueError:
        return str(target)


def _download_cover(session: requests.Session, record: dict, index: int, total: int,
                    progress: Optional[Callable[[str, int, int], None]] = None) -> str:
    url = record.get('cover_url') or ''
    if not url:
        return ''
    filename = f"{record['media_type']}_{record['subject_id']}.jpg"
    path = COVERS_DIR / filename
    if cover_file_ok(path):
        return _cover_relative(path)
    data = _fetch_cover_bytes(session, url, record.get('url', ''))
    if not data:
        return ''
    if progress:
        progress(f'下载封面 {index}/{total}', index, total)
    time.sleep(0.2)
    return _write_cover(path, data)


def _download_covers(records: list[dict], session: requests.Session, label: str,
                     progress: Optional[Callable[[str, int, int], None]]) -> None:
    total = len(records)
    for index, record in enumerate(records, 1):
        cover_path = _download_cover(session, record, index, total, progress)
        if cover_path:
            record['cover_path'] = cover_path
        if progress and index % 10 == 0:
            progress(f'处理{label} {index}/{total}', index, total)


def _parse_doulist_links(html_text: str, relation: str) -> list[dict]:
    soup = BeautifulSoup(html_text, 'html.parser')
    found = []
    for link in soup.select('a[href*="/doulist/"]'):
        match = re.search(r'/doulist/(\d+)/?', link.get('href', ''))
        if not match:
            continue
        douban_id = match.group(1)
        title = re.sub(r'\s+', ' ', link.get_text(' ', strip=True)).strip()
        if not title or title in {'豆列', '片单', '书单'}:
            continue
        found.append({
            'douban_id': douban_id,
            'title': title,
            'url': f'https://www.douban.com/doulist/{douban_id}/',
            'relation': relation,
        })
    return found


def _collect_doulist_page(url: str, relation: str, session: requests.Session,
                          headers: dict, found: dict, max_pages: int = 10) -> None:
    current = url
    for _ in range(max_pages):
        try:
            response = _request(session, current, headers, retries=0)
        except DoubanError:
            return
        for item in _parse_doulist_links(response.text, relation):
            existing = found.get(item['douban_id'])
            if existing is None or (existing.get('relation') == 'followed' and relation == 'created'):
                found[item['douban_id']] = item
        soup = BeautifulSoup(response.text, 'html.parser')
        next_url = None
        for link in soup.select('div.paginator a, .paginator a, a.next'):
            if link.get_text(' ', strip=True) in ('后页', '下一页', '下页', '>'):
                next_url = urljoin(current, link.get('href', ''))
                break
        if not next_url or next_url == current:
            break
        current = next_url
        time.sleep(1)


def _merge_doulist(found: dict[str, dict], item: dict) -> None:
    existing = found.get(item['douban_id'])
    if existing is None or (existing.get('relation') == 'followed'
                            and item.get('relation') == 'created'):
        found[item['douban_id']] = item


def _collection_to_doulist(item: dict, relation: str) -> Optional[dict]:
    douban_id = str(item.get('id') or item.get('douban_id') or '')
    title = re.sub(r'\s+', ' ', str(item.get('title') or item.get('name') or '')).strip()
    if not douban_id or not title:
        return None
    category = str(item.get('category') or item.get('subject_type') or '')
    media_type = category if category in ('movie', 'book') else 'mixed'
    is_collection = str(item.get('type') or '') == 'chart' or not douban_id.isdigit()
    if is_collection:
        url = f'https://m.douban.com/subject_collection/{douban_id}'
    else:
        url = item.get('url') or f'https://www.douban.com/doulist/{douban_id}/'
    count = (item.get('items_count') or item.get('subject_count')
             or item.get('total') or item.get('collect_count') or 0)
    return {
        'douban_id': douban_id,
        'title': title,
        'url': url,
        'relation': relation,
        'media_type': media_type,
        'item_count': int(count or 0),
    }


def _fetch_rexxar_user_doulists(session: requests.Session, headers: dict,
                                user_id: str, found: dict[str, dict]) -> None:
    endpoints = (
        ('owned_doulists', 'created'),
        ('following_doulists', 'followed'),
    )
    for endpoint, relation in endpoints:
        start = 0
        count = 50
        while True:
            url = (f'https://m.douban.com/rexxar/api/v2/user/{user_id}/{endpoint}'
                   f'?start={start}&count={count}&ck=&for_mobile=1')
            try:
                data = _request(session, url, headers, retries=0).json()
            except Exception:
                break
            items = data.get('doulists') or data.get('items') or data.get('subject_collections') or []
            total = int(data.get('total') or 0)
            for item in items:
                doulist = _collection_to_doulist(item, relation)
                if doulist:
                    _merge_doulist(found, doulist)
            start += len(items)
            if not items or start >= total or len(items) < count:
                break
            time.sleep(1)


def fetch_user_doulists(user_id: str, cookie: str) -> list[dict]:
    if not cookie.strip():
        raise DoubanError('豆列页面需要登录 Cookie，请在同步窗口填写 Cookie。')
    session = requests.Session()
    session.headers.update({'User-Agent': _UA})
    headers = {'User-Agent': _UA, 'Cookie': cookie.strip(),
               'Referer': f'https://www.douban.com/people/{user_id}/'}
    base = f'https://www.douban.com/people/{user_id}/doulists'
    found: dict[str, dict] = {}
    last_error = None

    _fetch_rexxar_user_doulists(session, headers, user_id, found)

    try:
        response = _request(session, base, headers)
        soup = BeautifulSoup(response.text, 'html.parser')
        tab_urls: dict[str, str] = {}
        for link in soup.select('a[href]'):
            text = link.get_text(' ', strip=True)
            href = urljoin(base, link.get('href', ''))
            if '/doulists' not in href and '/subject_doulists' not in href:
                continue
            if '关注' in text:
                tab_urls.setdefault('followed', href)
            elif '创建' in text:
                tab_urls.setdefault('created', href)
        for relation, url in tab_urls.items():
            _collect_doulist_page(url, relation, session, headers, found)
        for item in _parse_doulist_links(response.text, 'created'):
            _merge_doulist(found, item)
    except DoubanError as exc:
        last_error = exc

    fallbacks = [
        ('created', f'https://www.douban.com/people/{user_id}/doulists/all'),
        ('created', f'https://www.douban.com/people/{user_id}/subject_doulists/movie'),
        ('created', f'https://www.douban.com/people/{user_id}/subject_doulists/book'),
        ('followed', f'https://www.douban.com/people/{user_id}/doulists?type=follow'),
        ('followed', f'https://www.douban.com/people/{user_id}/doulists?tab=follow'),
        ('followed', f'https://www.douban.com/people/{user_id}/doulists?type=followed'),
        ('followed', f'https://www.douban.com/people/{user_id}/doulists/follow'),
        ('followed', f'https://www.douban.com/people/{user_id}/doulists/followed'),
        ('followed', f'https://m.douban.com/people/{user_id}/doulists?type=followed'),
    ]
    for relation, url in fallbacks:
        _collect_doulist_page(url, relation, session, headers, found)

    if not found:
        if last_error:
            raise DoubanError(f'无法读取豆列列表：{last_error}')
        raise DoubanError('没有读取到豆列，可能是空豆列或未公开。')
    return list(found.values())


def fetch_doulist_items(douban_id: str, cookie: str = '',
                        progress: Optional[Callable[[str, int, int], None]] = None) -> tuple[list[dict], int]:
    session = requests.Session()
    session.headers.update({'User-Agent': _UA_MOBILE})
    is_collection = not str(douban_id).isdigit()
    referer = (f'https://m.douban.com/subject_collection/{douban_id}'
               if is_collection else f'https://www.douban.com/doulist/{douban_id}/')
    headers = {'User-Agent': _UA_MOBILE, 'Referer': referer}
    if cookie.strip():
        headers['Cookie'] = cookie.strip()
    items: list[dict] = []
    start = 0
    total = 0
    while True:
        if is_collection:
            url = (f'https://m.douban.com/rexxar/api/v2/subject_collection/{douban_id}/items'
                   f'?start={start}&count=20&ck=&for_mobile=1')
        else:
            url = (f'https://m.douban.com/rexxar/api/v2/doulist/{douban_id}/items'
                   f'?start={start}&count=20&ck=&for_mobile=1')
        response = _request(session, url, headers)
        data = response.json()
        total = int(data.get('total') or 0)
        page_items = (data.get('subject_collection_items') if is_collection
                      else data.get('items')) or []
        if not page_items:
            break
        for index, item in enumerate(page_items):
            if item.get('type') not in ('movie', 'book'):
                continue
            subject_id = str((item.get('id') if is_collection else item.get('target_id')) or '')
            if not subject_id:
                continue
            rating_data = item.get('rating') or {}
            value = rating_data.get('value') if isinstance(rating_data, dict) else None
            if is_collection:
                intro = item.get('card_subtitle') or item.get('description') or ''
            else:
                intro = item.get('subtitle') or ''
            items.append({
                'media_type': item['type'],
                'subject_id': subject_id,
                'title': item.get('title') or '',
                'url': item.get('url') or '',
                'cover_url': item.get('cover_url') or '',
                'rating': 0,
                'comment': item.get('comment') or '',
                'intro': intro,
                'position': start + index,
                'create_time': (item.get('create_time') or '')[:10],
                'community_rating': value,
            })
        if progress:
            progress(f'豆列 {douban_id}：{min(start + len(page_items), total)}/{total}', min(start + len(page_items), total), total)
        start += len(page_items)
        if start >= total:
            break
        time.sleep(1.5)
    return items, total


def sync_to_db(media_types: list[str], user_id: str, cookie: str, db: Database,
               progress: Optional[Callable[[str, int, int], None]] = None,
               include_wish: bool = False, include_doulists: bool = False,
               sync_mode: str = 'full') -> dict:
    if sync_mode not in ('full', 'incremental_add', 'incremental_sync'):
        raise ValueError(f'unknown sync mode: {sync_mode}')
    session = requests.Session()
    session.headers.update({'User-Agent': _UA})
    summary: dict = {'mode': sync_mode}
    meta_subjects = {'book': {}, 'movie': {}}
    delete_missing = sync_mode in ('full', 'incremental_sync')
    for media_type in media_types:
        records, profile_name, profile_avatar = fetch_collect(
            media_type, user_id, cookie, progress
        )
        if profile_name:
            db.set_setting('douban_display_name', profile_name)
        if profile_avatar:
            saved_avatar = _download_douban_avatar(
                profile_avatar, session, referer=f'https://{media_type}.douban.com/'
            )
            if saved_avatar:
                db.set_setting('douban_avatar', saved_avatar)
        if media_type == 'book':
            _enrich_book_titles(records, session, progress)
        _download_covers(records, session, MEDIA_LABELS[media_type], progress)
        if media_type == 'book':
            for record in records:
                meta_subjects['book'].setdefault(str(record['subject_id']), record)
        result = db.upsert_records([dict(record, status='done') for record in records],
                                   mode=sync_mode)
        deleted = 0
        if delete_missing and records:
            deleted = db.delete_records_not_in(
                media_type, 'done', [record['subject_id'] for record in records]
            )
        summary[media_type] = {
            'fetched': len(records),
            'inserted': result['inserted'],
            'updated': result['updated'],
            'skipped': result.get('skipped', 0),
            'deleted': deleted,
        }

        if include_wish:
            wish_records, _, _ = fetch_collect(media_type, user_id, cookie, progress,
                                               kind='wish', allow_empty=True)
            if media_type == 'book':
                _enrich_book_titles(wish_records, session, progress)
            _download_covers(wish_records, session, f'{MEDIA_LABELS[media_type]}想看/想读', progress)
            if media_type == 'book':
                for record in wish_records:
                    meta_subjects['book'].setdefault(str(record['subject_id']), record)
            wish_result = db.upsert_records([dict(record, status='wish') for record in wish_records],
                                            mode=sync_mode)
            wish_deleted = 0
            if delete_missing and wish_records:
                wish_deleted = db.delete_records_not_in(
                    media_type, 'wish', [record['subject_id'] for record in wish_records]
                )
            summary[media_type].update({
                'wish_fetched': len(wish_records),
                'wish_inserted': wish_result['inserted'],
                'wish_updated': wish_result['updated'],
                'wish_skipped': wish_result.get('skipped', 0),
                'wish_deleted': wish_deleted,
            })

    if include_doulists:
        if not cookie.strip():
            summary['doulists_error'] = '豆列需要登录 Cookie，已跳过。'
        else:
            try:
                doulists = fetch_user_doulists(user_id, cookie)
            except DoubanError as exc:
                summary['doulists_error'] = str(exc)
            else:
                list_count = 0
                item_count = 0
                deleted_items = 0
                failures = []
                for doulist in doulists:
                    if progress:
                        progress(f"同步豆列：{doulist['title']}", 0, 0)
                    try:
                        items, total = fetch_doulist_items(doulist['douban_id'], cookie, progress)
                    except Exception as exc:
                        failures.append(f"{doulist['title']}: {exc}")
                        continue
                    _download_covers(items, session, f"豆列 {doulist['title']}", progress)
                    for item in items:
                        if item['media_type'] in meta_subjects:
                            meta_subjects[item['media_type']].setdefault(
                                str(item['subject_id']),
                                {'subject_id': item['subject_id'], 'title': item['title'],
                                 'url': item.get('url', '')},
                            )
                    media_type = doulist.get('media_type') or 'mixed'
                    types = {item['media_type'] for item in items}
                    if types == {'movie'}:
                        media_type = 'movie'
                    elif types == {'book'}:
                        media_type = 'book'
                    item_count_value = total or doulist.get('item_count') or len(items)
                    doulist_id = db.upsert_doulist(
                        media_type, doulist['douban_id'], doulist['title'],
                        doulist['url'], item_count_value, doulist.get('relation', 'created'),
                    )
                    deleted_items += db.merge_doulist_items(doulist_id, items, mode=sync_mode)
                    list_count += 1
                    item_count += len(items)
                summary['doulists'] = {
                    'lists': list_count,
                    'items': item_count,
                    'deleted_items': deleted_items,
                    'failed': len(failures),
                    'failed_lists': failures[:5],
                    'created': sum(1 for d in doulists if d.get('relation') == 'created'),
                    'followed': sum(1 for d in doulists if d.get('relation') == 'followed'),
                }
    for media_type, subjects in meta_subjects.items():
        if not subjects:
            continue
        try:
            ensure_subject_meta(media_type, subjects.values(), progress)
        except Exception as exc:
            summary['meta_error'] = str(exc)
    return summary
