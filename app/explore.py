"""探索空间：远端下发的资源站点配置、本地站点与连通性检测。

配置由 GitHub（失败回退 Gitee）下发的 sites.json 提供，本地只读；
用户自己添加的站点标记为「本地」，拉取远端配置时不会被覆盖。
"""
from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

import requests

SETTING_KEY = 'explore_sites'
MOVIE = 'movie'
BOOK = 'book'

REMOTE_SOURCES = (
    {
        'id': 'github',
        'name': 'GitHub',
        'url': 'https://raw.githubusercontent.com/catnine/bookfilm-sites/main/sites.json',
    },
    {
        'id': 'gitee',
        'name': 'Gitee',
        'url': 'https://gitee.com/catnine/bookfilm-sites/raw/main/sites.json',
    },
)

USER_AGENT = (
    'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) '
    'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1'
)

DEFAULT_SITES = (
    ('豆瓣电影', 'https://movie.douban.com',
     'https://movie.douban.com/subject_search?search_text={title}', MOVIE, False),
    ('TMDB', 'https://www.themoviedb.org',
     'https://www.themoviedb.org/search?query={title}', MOVIE, False),
    ('爱奇艺', 'https://www.iqiyi.com',
     'https://so.iqiyi.com/so/q_{title}', MOVIE, False),
    ('Letterboxd', 'https://letterboxd.com',
     'https://letterboxd.com/search/{title}/', MOVIE, True),
    ('豆瓣读书', 'https://book.douban.com',
     'https://book.douban.com/subject_search?search_text={title}', BOOK, False),
    ('Goodreads', 'https://www.goodreads.com',
     'https://www.goodreads.com/search?q={title}', BOOK, False),
    ('微信读书', 'https://weread.qq.com',
     'https://weread.qq.com/web/search/books?keyword={title}', BOOK, False),
    ('Open Library', 'https://openlibrary.org',
     'https://openlibrary.org/search?q={title}', BOOK, True),
)


def _new_site(name: str, url: str, search_url: str = '', kind: str = MOVIE,
              local: bool = False) -> dict:
    return {
        'id': uuid.uuid4().hex,
        'name': name.strip(),
        'url': url.strip(),
        'search_url': (search_url or '').strip(),
        'kind': kind if kind in (MOVIE, BOOK) else MOVIE,
        'local': bool(local),
    }


def default_sites() -> list[dict]:
    return [_new_site(name, url, search, kind, local)
            for name, url, search, kind, local in DEFAULT_SITES]


def _normalize_site(raw: dict) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    url = str(raw.get('url') or '').strip()
    name = str(raw.get('name') or '').strip()
    if not url or not name:
        return None
    return {
        'id': str(raw.get('id') or uuid.uuid4().hex),
        'name': name,
        'url': url,
        'search_url': str(raw.get('search_url') or raw.get('searchUrl') or '').strip(),
        'kind': BOOK if raw.get('kind') == BOOK else MOVIE,
        'local': bool(raw.get('local')),
    }


_default_cache: Optional[dict] = None


def _default_state() -> dict:
    """内置站点在首次使用时生成一次，保证同一次运行里各页面拿到的 id 一致。"""
    global _default_cache
    if _default_cache is None:
        _default_cache = {'version': 3, 'last_source': '', 'pulled_at': '',
                          'sites': default_sites()}
    return _default_cache


def load_state(db) -> dict:
    try:
        raw = db.get_setting(SETTING_KEY)
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        data = {}
    sites = [site for site in (_normalize_site(item) for item in data.get('sites') or []) if site]
    if not sites:
        # 首次使用只返回内置站点，不立即写库，避免无意义地产生一次数据变更。
        data = _default_state()
        sites = data['sites']
    data['sites'] = sites
    return data


def save_state(db, state: dict) -> None:
    db.set_setting(SETTING_KEY, json.dumps(state, ensure_ascii=False))


def sites_for(state: dict, kind: str) -> list[dict]:
    return [site for site in state.get('sites') or [] if site.get('kind') == kind]


def add_site(db, state: dict, name: str, url: str, search_url: str, kind: str) -> dict:
    site = _new_site(name, url, search_url, kind, local=True)
    state.setdefault('sites', []).append(site)
    save_state(db, state)
    return site


def update_site(db, state: dict, site_id: str, name: str, url: str,
                search_url: str, kind: str) -> None:
    for site in state.get('sites') or []:
        if site.get('id') == site_id:
            site.update(name=name.strip(), url=url.strip(),
                        search_url=(search_url or '').strip(), kind=kind)
            break
    save_state(db, state)


def move_site(db, state: dict, site_id: str, delta: int) -> bool:
    """在同一类型的站点里上移 / 下移一位，返回是否发生变化。"""
    sites = state.get('sites') or []
    index = next((i for i, site in enumerate(sites) if site.get('id') == site_id), None)
    if index is None:
        return False
    kind = sites[index].get('kind')
    step = -1 if delta < 0 else 1
    target = None
    cursor = index + step
    while 0 <= cursor < len(sites):
        if sites[cursor].get('kind') == kind:
            target = cursor
            break
        cursor += step
    if target is None:
        return False
    sites[index], sites[target] = sites[target], sites[index]
    save_state(db, state)
    return True


def delete_site(db, state: dict, site_id: str) -> None:
    state['sites'] = [site for site in state.get('sites') or []
                      if site.get('id') != site_id]
    save_state(db, state)


def pull_remote(timeout: float = 10) -> tuple[dict, str]:
    """按顺序尝试远端配置源，返回（配置字典, 源名称）。

    配置格式：{"version": 3, "movie": [{"name","url","searchUrl"}], "book": [...]}
    """
    headers = {'User-Agent': USER_AGENT, 'Accept': 'application/json'}
    last_error = None
    for source in REMOTE_SOURCES:
        try:
            response = requests.get(source['url'], headers=headers,
                                    timeout=(4, timeout), allow_redirects=True)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError('配置格式不正确')
            if not data.get('movie') and not data.get('book'):
                raise ValueError('配置里没有站点')
            return data, source['name']
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            continue
    detail = str(last_error or '').split('(')[0][:60]
    raise RuntimeError(f'GitHub / Gitee 均不可用，请检查网络或稍后重试（{detail}）')


def merge_remote(state: dict, config: dict) -> int:
    """用远端配置更新非本地站点，保留用户调整过的顺序。

    已在配置里的站点沿用现有位置与 id（只刷新名称、链接与搜索模板），远端新增的
    站点追加到末尾，远端已删除的站点移除；本地站点原样保留。
    """
    remote_items = []
    version = config.get('version')
    for key, kind in (('movie', MOVIE), ('book', BOOK)):
        for item in config.get(key) or []:
            normalized = _normalize_site({
                'name': item.get('name'),
                'url': item.get('url'),
                'searchUrl': item.get('searchUrl') or item.get('search_url'),
                'kind': kind,
                'local': False,
            })
            if normalized:
                remote_items.append(normalized)
    remote_by_url = {item['url']: item for item in remote_items}

    sites = []
    kept_urls = set()
    for site in state.get('sites') or []:
        if site.get('local'):
            sites.append(site)
            continue
        item = remote_by_url.get(site.get('url') or '')
        if item is None:
            continue
        site['name'] = item['name']
        site['search_url'] = item['search_url']
        site['kind'] = item['kind']
        sites.append(site)
        kept_urls.add(item['url'])
    for item in remote_items:
        if item['url'] not in kept_urls:
            sites.append(item)

    state['sites'] = sites
    if isinstance(version, int):
        state['version'] = version
    return len(sites) - sum(1 for site in sites if site.get('local'))


def to_ios_payload(state: dict) -> str:
    """把桌面站点配置转成 iOS 的 ExploreSite JSON（写入 settings.ios_explore_sites）。"""
    items = []
    for site in state.get('sites') or []:
        raw_id = str(site.get('id') or site.get('url') or '')
        try:
            site_uuid = str(uuid.UUID(raw_id))
        except (ValueError, AttributeError):
            site_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, raw_id))
        item = {
            'id': site_uuid,
            'name': site.get('name', ''),
            'urlString': site.get('url', ''),
            'kind': BOOK if site.get('kind') == BOOK else MOVIE,
            'isLocal': bool(site.get('local')),
        }
        search = (site.get('search_url') or '').strip()
        if search:
            item['searchURLTemplate'] = search
        items.append(item)
    return json.dumps(items, ensure_ascii=False)


def from_ios_payload(raw: str) -> list[dict]:
    """解析 iOS 快照里的 ios_explore_sites，转成桌面站点结构。"""
    try:
        items = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    sites = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get('name') or '').strip()
        url = str(item.get('urlString') or item.get('url') or '').strip()
        if not name or not url:
            continue
        sites.append({
            'id': str(item.get('id') or uuid.uuid4().hex),
            'name': name,
            'url': url,
            'search_url': str(item.get('searchURLTemplate')
                              or item.get('search_url') or '').strip(),
            'kind': BOOK if item.get('kind') == BOOK else MOVIE,
            'local': bool(item.get('isLocal', item.get('local'))),
        })
    return sites


# 连通性检测结果只在当前会话内共享（不写入快照），供记录详情等页面标注状态。
_reachability: dict[str, bool] = {}


def set_reachability(result: dict) -> None:
    _reachability.update({str(key): bool(value) for key, value in (result or {}).items()})


def get_reachability(site_id: str):
    return _reachability.get(str(site_id))


def clear_reachability() -> None:
    _reachability.clear()


def search_link(site: dict, title: str) -> str:
    """按搜索模板生成带标题的链接；没有模板时返回站点首页。"""
    import urllib.parse

    base = (site.get('url') or '').strip()
    template = (site.get('search_url') or '').strip()
    query = (title or '').strip()
    if not template:
        return base
    if not query:
        return base
    encoded = urllib.parse.quote(query, safe='')
    template = template.replace('{query}', '{title}').replace('%s', '{title}')
    if '{title}' in template:
        return template.replace('{title}', encoded)
    if template.endswith(('=', '?', '&')):
        return f'{template}{encoded}'
    parts = urllib.parse.urlsplit(template)
    if not parts.query:
        return urllib.parse.urlunsplit(parts._replace(query=f'q={encoded}'))
    return f'{template}&q={encoded}'


def _looks_like_challenge(response) -> bool:
    """是否是 Cloudflare 之类的浏览器人机校验页。

    这类页面用浏览器能正常打开，但纯 HTTP 请求会拿到 403/503 和「Just a moment...」，
    不能据此判定站点不可访问。
    """
    if (response.headers.get('cf-mitigated') or '').lower() == 'challenge':
        return True
    if response.status_code not in (403, 503):
        return False
    try:
        head = response.text[:2048].lower()
    except (AttributeError, requests.RequestException):
        return False
    return ('just a moment' in head or 'challenges.cloudflare.com' in head
            or 'cf-browser-verification' in head)


def check_site(url: str, timeout: float = 6) -> bool:
    """连通性检测：能拿到 HTTP 响应即视为可达。

    只把 DNS / TLS / 连接失败、超时和 5xx 当作不可达；403 之类的拒绝、以及
    Cloudflare 人机校验都说明服务器在线，浏览器打开没有问题。
    """
    try:
        response = requests.get(
            url,
            headers={'User-Agent': USER_AGENT,
                     'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                     'Accept-Language': 'zh-CN,zh;q=0.9'},
            timeout=(3, timeout),
            allow_redirects=True,
        )
    except requests.RequestException:
        return False
    if response.status_code < 500:
        return True
    return _looks_like_challenge(response)


def check_all(sites: list[dict], timeout: float = 6, workers: int = 4,
              progress: Optional[Callable[[int, int], None]] = None) -> dict[str, bool]:
    """并发检测站点连通性（最多 4 个并发）。"""
    result: dict[str, bool] = {}
    total = len(sites)
    done = 0
    if not sites:
        return result
    with ThreadPoolExecutor(max_workers=max(1, min(4, workers))) as pool:
        futures = {pool.submit(check_site, site['url'], timeout): site for site in sites}
        for future in futures:
            site = futures[future]
            try:
                result[site['id']] = bool(future.result())
            except Exception:
                result[site['id']] = False
            done += 1
            if progress:
                progress(done, total)
    return result
