"""数据库快照同步到网盘目录（iCloud 云盘 / OneDrive / 自定义目录）。

不依赖任何云服务 SDK，也不需要在应用内登录：系统登录并开启 iCloud 云盘
（Windows 为 iCloud for Windows）后，网盘目录就是普通文件夹，应用把
SQLite 一致性快照写进去，另一端读取最新快照恢复即可。

正在使用的数据库不会直接放到同步目录里，避免多设备并发写、iCloud 异步上传
和占位文件导致数据库损坏。
"""
import hashlib
import json
import os
import platform
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from app import explore
from app.config import APP_VERSION, DB_PATH
from app.cover_sync import is_custom_cover, read_cover_base64, save_cover_base64
from app.media_meta import import_movie_durations, movie_duration, sync_book_pages

SYNC_SUBDIR = '书影记录'
DEVICES_NAME = 'devices.json'
SNAPSHOT_PREFIX = 'douban_trace_'
SNAPSHOT_SUFFIX = '.db'
ICLOUD_STUB_SUFFIX = f'{SNAPSHOT_SUFFIX}.icloud'
MANIFEST_NAME = 'latest.json'
# 每台设备分别保留：自动备份最近 3 份、手动备份最近 2 份，互不影响。
SNAPSHOT_KEEP_AUTO = 3
SNAPSHOT_KEEP_MANUAL = 2
KIND_AUTO = 'auto'
KIND_MANUAL = 'manual'
SNAPSHOT_NAME_RE = re.compile(
    rf'^{re.escape(SNAPSHOT_PREFIX)}(\d{{8}}_\d{{6}})_([0-9a-f]{{4}})'
    rf'(?:_({KIND_AUTO}|{KIND_MANUAL}))?(?:_(\d+))?{re.escape(SNAPSHOT_SUFFIX)}$'
)
LOCAL_BACKUP_KEEP = 5

COUNT_TABLES = (
    'records', 'doulists', 'doulist_items', 'plan_folders', 'plan_items',
    'book_words', 'book_meta', 'ignored_subjects', 'purged_records',
)

# 只属于当前设备的设置：不写进快照，恢复时保留本机值。
LOCAL_SETTING_KEYS = (
    'douban_cookie',
    'book_library_dir',
    'cloud_sync_dir',
    'cloud_sync_last_state',
    'cloud_sync_last_time',
    'cloud_sync_last_snapshot',
    'cloud_sync_auto_backup',
    'cloud_sync_auto_sync',
)
PRESERVE_ON_RESTORE = ('douban_cookie', 'book_library_dir', 'cloud_sync_dir')

SETTING_DIR = 'cloud_sync_dir'
SETTING_LAST_STATE = 'cloud_sync_last_state'
SETTING_LAST_TIME = 'cloud_sync_last_time'
SETTING_LAST_SNAPSHOT = 'cloud_sync_last_snapshot'
SETTING_AUTO_BACKUP = 'cloud_sync_auto_backup'
SETTING_AUTO_SYNC = 'cloud_sync_auto_sync'
# 快照里声明「ignored_subjects / purged_records 由写入方维护」的标记；
# 老快照没有这个键，恢复时按 iOS 旧版本保守处理。
SETTING_MARKS_VERSION = 'snapshot_marks_version'
MARKS_VERSION = '1'


class SyncError(Exception):
    """同步失败，消息可直接展示给用户。"""


def _now_text() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def detected_dirs() -> list[tuple[str, Path]]:
    """按优先级返回可用的同步根目录（iCloud > OneDrive > Dropbox）。"""
    found: list[tuple[str, Path]] = []
    override = os.environ.get('SHUYINGJILU_SYNC_DIR', '').strip()
    if override:
        found.append(('自定义目录', Path(override).expanduser()))

    home = Path.home()
    icloud_root = None
    if sys.platform == 'darwin':
        icloud_root = home / 'Library' / 'Mobile Documents' / 'com~apple~CloudDocs'
    elif os.name == 'nt':
        for name in ('iCloudDrive', 'iCloud Drive'):
            candidate = home / name
            if candidate.is_dir():
                icloud_root = candidate
                break
    if icloud_root and icloud_root.is_dir():
        found.append(('iCloud 云盘', icloud_root))

    for key in ('OneDrive', 'OneDriveConsumer'):
        value = os.environ.get(key, '').strip()
        if value:
            path = Path(value)
            if path.is_dir() and all(path != item for _, item in found):
                found.append(('OneDrive', path))
    for name, label in (('OneDrive', 'OneDrive'), ('Dropbox', 'Dropbox')):
        path = home / name
        if path.is_dir() and all(path != item for _, item in found):
            found.append((label, path))
    return found


def base_dir(db) -> Optional[Path]:
    manual = (db.get_setting(SETTING_DIR) or '').strip()
    if manual:
        return Path(manual).expanduser()
    candidates = detected_dirs()
    return candidates[0][1] if candidates else None


def _has_snapshot_data(folder: Path) -> bool:
    """目录里是否已经有快照、清单或设备登记。"""
    if not folder.is_dir():
        return False
    try:
        for path in folder.iterdir():
            name = path.name
            if name in (MANIFEST_NAME, DEVICES_NAME) or name.startswith(SNAPSHOT_PREFIX):
                return True
    except OSError:
        return False
    return False


def sync_folder(db, create: bool = False) -> Optional[Path]:
    """定位实际读写快照的目录。

    与 iOS 一致：用户选的就是「书影记录」文件夹时直接用它；否则在所选根目录下
    创建 `书影记录/`。早前版本会在「书影记录」里再套一层，已有数据时继续用那一层。
    """
    root = base_dir(db)
    if root is None:
        return None
    folder = root
    if root.name != SYNC_SUBDIR:
        folder = root / SYNC_SUBDIR
    elif not _has_snapshot_data(root) and _has_snapshot_data(root / SYNC_SUBDIR):
        folder = root / SYNC_SUBDIR
    if create:
        folder.mkdir(parents=True, exist_ok=True)
    return folder


def state_id(db_path: Optional[Path] = None, conn: Optional[sqlite3.Connection] = None) -> str:
    """数据库逻辑内容指纹：用于判断本地是否有未备份的改动。

    本机专属设置（Cookie、书库路径、同步簿记）不参与计算，否则每次写完
    同步状态都会把自己算成一次新改动。
    """
    own = conn is None
    if conn is None:
        conn = sqlite3.connect(str(db_path or DB_PATH))
    try:
        digest = hashlib.sha256()
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        placeholders = ','.join('?' for _ in LOCAL_SETTING_KEYS)
        for table in tables:
            digest.update(table.encode('utf-8'))
            if table == 'settings':
                rows = conn.execute(
                    f'SELECT key, value FROM settings'
                    f' WHERE key NOT IN ({placeholders}) ORDER BY key',
                    LOCAL_SETTING_KEYS,
                ).fetchall()
            else:
                rows = conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
            for row in rows:
                digest.update(repr(tuple(row)).encode('utf-8'))
        return digest.hexdigest()[:16]
    finally:
        if own:
            conn.close()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    result: dict[str, int] = {}
    for table in COUNT_TABLES:
        try:
            result[table] = int(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0])
        except sqlite3.Error:
            result[table] = 0
    return result


def _media_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """记录按影视/书籍拆分，便于对比快照与本机的数据构成。"""
    result = {'movie': 0, 'book': 0}
    try:
        for media_type, value in conn.execute(
                "SELECT media_type, COUNT(*) FROM records GROUP BY media_type"):
            if media_type in result:
                result[media_type] = int(value or 0)
    except sqlite3.Error:
        pass
    return result


def _records_updated(conn: sqlite3.Connection) -> str:
    try:
        return str(conn.execute(
            "SELECT COALESCE(MAX(updated_at), '') FROM records"
        ).fetchone()[0] or '')
    except sqlite3.Error:
        return ''


def load_manifest(folder: Path) -> Optional[dict]:
    path = folder / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get('snapshot'):
        return None
    return data


def _write_manifest(folder: Path, manifest: dict) -> None:
    temp = folder / f'.{MANIFEST_NAME}.part'
    temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, folder / MANIFEST_NAME)


def _snapshot_info(name: str) -> Optional[dict]:
    """从快照文件名解析时间与设备标记，格式不符时返回 None。"""
    match = SNAPSHOT_NAME_RE.match(name)
    if not match:
        return None
    stamp, tag = match.group(1), match.group(2)
    kind = match.group(3) or KIND_MANUAL  # 旧版快照没有类型标记，按手动备份处理
    try:
        created = datetime.strptime(stamp, '%Y%m%d_%H%M%S')
    except ValueError:
        return None
    # 同一秒内基础名可能被复用，用序号区分先后（基础名为 1）。
    seq = int(match.group(4) or 1)
    return {'snapshot': name, 'tag': tag, 'kind': kind,
            'created': created, 'seq': seq}


def _iter_snapshot_files(folder: Path):
    """遍历快照文件，同时识别 iCloud 尚未下载的占位文件，产出（真实文件名, 路径, 是否占位）。

    下载过程中可能同时存在真实文件与 `.icloud` 占位文件；同一份快照只产出一次，
    真实文件优先，避免列表出现重复项或清理时误删。
    """
    real: dict[str, Path] = {}
    stubs: dict[str, Path] = {}
    for path in folder.iterdir():
        name = path.name
        if name.startswith('.') and name.endswith(ICLOUD_STUB_SUFFIX):
            real_name = name[1:-len(ICLOUD_STUB_SUFFIX)] + SNAPSHOT_SUFFIX
            stubs[real_name] = path
        elif name.startswith(SNAPSHOT_PREFIX) and name.endswith(SNAPSHOT_SUFFIX):
            real[name] = path
    for name, path in real.items():
        yield name, path, False
    for name, path in stubs.items():
        if name not in real:
            yield name, path, True


def _prune_snapshots(folder: Path) -> None:
    """每台设备的自动/手动快照分别只保留最近几份，避免同步目录无限增长。"""
    groups: dict[tuple, list[tuple]] = {}
    for name, _path, _stub in _iter_snapshot_files(folder):
        info = _snapshot_info(name)
        if info:
            groups.setdefault((info['tag'], info['kind']), []).append(
                (info['created'], info['seq'], name))
    for (tag, kind), entries in groups.items():
        keep = SNAPSHOT_KEEP_AUTO if kind == KIND_AUTO else SNAPSHOT_KEEP_MANUAL
        # 按「时间 + 同秒序号」排序，避免基础名在清理后被复用导致误删最新的快照。
        entries.sort(reverse=True)
        for _created, _seq, name in entries[keep:]:
            # 快照本体、iOS 侧车文件（.db.ios.json）以及未下载占位一起清理。
            for path in (folder / name, folder / f'{name}.ios.json'):
                try:
                    path.unlink()
                except OSError:
                    pass
            try:
                (folder / f'.{name}.icloud').unlink()
            except OSError:
                pass


def _device_tag() -> str:
    name = platform.node() or 'device'
    return hashlib.sha1(name.encode('utf-8')).hexdigest()[:4]


def device_profile() -> dict:
    """当前设备的可读信息：名称、系统、架构。"""
    system = platform.system()
    if system == 'Darwin':
        label = 'macOS'
    elif system == 'Windows':
        label = 'Windows'
    elif system == 'Linux':
        label = 'Linux'
    else:
        label = system or '未知系统'
    return {
        'tag': _device_tag(),
        'name': platform.node() or '未知设备',
        'platform': label,
        'arch': platform.machine() or '',
    }


def _load_devices(folder: Optional[Path]) -> dict:
    if folder is None:
        return {}
    path = folder / DEVICES_NAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _remember_device(folder: Path) -> None:
    """把本机名称/系统登记到 devices.json，供历史快照显示可读设备名。"""
    profile = device_profile()
    devices = _load_devices(folder)
    devices[profile['tag']] = {
        'name': profile['name'],
        'platform': profile['platform'],
        'arch': profile['arch'],
        'last_seen': _now_text(),
    }
    temp = folder / f'.{DEVICES_NAME}.part'
    try:
        temp.write_text(json.dumps(devices, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temp, folder / DEVICES_NAME)
    except OSError:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def list_snapshots(folder: Optional[Path]) -> list[dict]:
    """列出同步目录里的全部快照，按时间从新到旧，供用户选择恢复哪一份。"""
    if folder is None or not folder.is_dir():
        return []
    manifest = load_manifest(folder)
    latest_name = (manifest or {}).get('snapshot', '')
    own_tag = _device_tag()
    devices = _load_devices(folder)
    items = []
    for name, path, stub in _iter_snapshot_files(folder):
        info = _snapshot_info(name)
        if not info:
            continue
        created_at = info['created'].strftime('%Y-%m-%d %H:%M:%S')
        device = '本机' if info['tag'] == own_tag else f'其他设备（{info["tag"]}）'
        known = devices.get(info['tag']) or {}
        if info['tag'] != own_tag and known.get('name'):
            device = str(known['name'])
        if manifest and name == latest_name:
            created_at = manifest.get('created_at') or created_at
            device = manifest.get('device') or device
        platform_label = ((manifest or {}).get('platform') if name == latest_name else '') \
            or known.get('platform') or ''
        if platform_label:
            device = f'{device} · {platform_label}'
        size = 0
        counts: Optional[dict] = None
        records_updated = ''
        if not stub:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            # 尝试读取快照内的数据统计，供恢复选择界面展示。
            try:
                conn = _open_snapshot(path)
                try:
                    counts = _counts(conn)
                    media = _media_counts(conn)
                    counts['records_movie'] = media['movie']
                    counts['records_book'] = media['book']
                    records_updated = _records_updated(conn)
                finally:
                    conn.close()
            except sqlite3.Error:
                counts = None
        items.append({
            'snapshot': name,
            'tag': info['tag'],
            'kind': info['kind'],
            'created_at': created_at,
            'created_ts': int(info['created'].timestamp()),
            'seq': info['seq'],
            'device': device,
            'is_mine': info['tag'] == own_tag,
            'is_latest': name == latest_name,
            'downloaded': not stub,
            'size': size,
            'counts': counts,
            'records_updated': records_updated,
            'app_version': (manifest or {}).get('app_version', '')
            if name == latest_name else '',
            'sha256': (manifest or {}).get('sha256', '') if name == latest_name else '',
        })
    items.sort(key=lambda item: (item['created_ts'], item['seq']), reverse=True)
    return items


def _snapshot_present(folder: Path, name: str) -> bool:
    """快照是否已经存在（本机文件或 iCloud 占位文件都算）。"""
    if not name:
        return False
    return ((folder / name).is_file()
            or (folder / f'.{name}.icloud').is_file())


def _unique_snapshot_name(folder: Path, kind: str = KIND_MANUAL) -> str:
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    tag = _device_tag()
    prefix = f'{SNAPSHOT_PREFIX}{stamp}_{tag}_{kind}'
    # 同一秒内不复用基础名：已有同秒快照时顺延序号，否则清理后重建会用回
    # `_auto.db`，按文件名排序时会被误认为最早的一份。
    max_seq = 0
    for name, _path, _stub in _iter_snapshot_files(folder):
        info = _snapshot_info(name)
        if not info or info['tag'] != tag or info['kind'] != kind:
            continue
        if info['snapshot'].startswith(prefix):
            max_seq = max(max_seq, info['seq'])
    index = max_seq + 1
    if index <= 1:
        return f'{prefix}{SNAPSHOT_SUFFIX}'
    return f'{prefix}_{index}{SNAPSHOT_SUFFIX}'


def _embed_ios_explore_sites(conn: sqlite3.Connection) -> None:
    """把桌面探索空间站点同时写成 iOS 可识别的 ios_explore_sites。"""
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key='explore_sites'"
        ).fetchone()
        if not row or not row[0]:
            return
        payload = explore.to_ios_payload(json.loads(row[0]))
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('ios_explore_sites', ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (payload,),
        )
    except (sqlite3.Error, ValueError):
        pass


def _mark_marks_authoritative(conn: sqlite3.Connection) -> None:
    """标记这份快照完整维护换源忽略与彻底删除墓碑。

    桌面端与 iOS v1.3+ 都会把两张表写进快照；标记用于区分「表为空是用户
    真的清空了」和「老 iOS 快照根本不写这两张表」，避免恢复时误保留旧标记。
    """
    try:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (SETTING_MARKS_VERSION, MARKS_VERSION),
        )
    except sqlite3.Error:
        pass


def _strip_snapshot_meta(db) -> None:
    """清除只属于快照的元数据，避免落到本机设置里。"""
    try:
        db.delete_setting(SETTING_MARKS_VERSION)
    except sqlite3.Error:
        pass


def _restore_ios_compatibility(db, snapshot_path: Path) -> None:
    """兼容 iOS 快照：回填探索空间站点，并从 sidecar 导入影片时长。"""
    try:
        conn = sqlite3.connect(str(db.path))
        try:
            if not conn.execute(
                    "SELECT 1 FROM settings WHERE key='explore_sites'").fetchone():
                row = conn.execute(
                    "SELECT value FROM settings WHERE key='ios_explore_sites'"
                ).fetchone()
                sites = explore.from_ios_payload(row[0]) if row and row[0] else []
                if sites:
                    state = {'version': 3, 'last_source': 'iOS 快照',
                             'pulled_at': '', 'sites': sites}
                    conn.execute(
                        "INSERT INTO settings(key, value) VALUES('explore_sites', ?)"
                        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (json.dumps(state, ensure_ascii=False),),
                    )
                    conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass

    sidecar = snapshot_path.with_name(snapshot_path.name + '.ios.json')
    if not sidecar.is_file():
        return
    try:
        data = json.loads(sidecar.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return
    durations = []
    for record in data.get('records') or []:
        if not isinstance(record, dict) or record.get('mediaType') != 'movie':
            continue
        subject_id = record.get('subjectID')
        minutes = record.get('durationMinutes')
        if subject_id and minutes:
            durations.append((subject_id, minutes))
    if durations:
        import_movie_durations(durations)


def _collect_sidecar(conn: sqlite3.Connection) -> dict:
    """收集 iOS 侧车需要的数据：影视时长、书籍页数、自定义封面。"""
    pages = {str(row[0]): int(row[1] or 0)
             for row in conn.execute('SELECT subject_id, pages FROM book_meta')}
    entries = []
    try:
        rows = conn.execute(
            "SELECT media_type, subject_id, status, cover_path, cover_url"
            " FROM records WHERE deleted_at='' ORDER BY id"
        ).fetchall()
    except sqlite3.Error:
        rows = []
    for record in rows:
        media_type, subject_id, status, cover_path, cover_url = record
        media_type = str(media_type)
        subject_id = str(subject_id)
        cover_path = cover_path or ''
        cover_url = cover_url or ''
        entry = {
            'mediaType': media_type,
            'subjectID': subject_id,
            'status': status or 'done',
        }
        if media_type == 'movie':
            minutes = movie_duration(subject_id)
            if minutes:
                entry['durationMinutes'] = int(minutes)
        elif pages.get(subject_id):
            entry['pages'] = pages[subject_id]
        if cover_url:
            entry['coverURL'] = cover_url
        if is_custom_cover(media_type, subject_id, cover_path):
            data = read_cover_base64(media_type, subject_id, cover_path)
            if data:
                entry['coverSource'] = 'link'
                entry['coverData'] = data
        elif cover_url:
            entry['coverSource'] = 'douban'
        entries.append(entry)
    return {'version': 1, 'records': entries,
            'doulistItems': _collect_sidecar_doulist_items(conn)}


# iOS 侧车用「2001-01-01 UTC 起的秒数」表示时间（Swift Date 默认编码）。
SIDECAR_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


UUID_RE = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}'
                      r'-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')


def _is_local_doulist_id(douban_id: str) -> bool:
    """iOS 把 `ios-local-<UUID>`、裸 UUID 和空值都当成「本地豆列」。"""
    value = (douban_id or '').strip()
    return (not value) or value.startswith('ios-local-') or bool(UUID_RE.match(value))


def _sidecar_list_key(media_type: str, douban_id: str, title: str) -> str:
    """与 iOS `sidecarListKey` 保持一致：真实豆瓣 ID 用 `douban:`，本地豆列按类型+标题。"""
    if not _is_local_doulist_id(douban_id):
        return f'douban:{(douban_id or "").strip()}'
    normalized = ' '.join((title or '').split())
    return f'local:{media_type}:{normalized}'


def _to_sidecar_date(text: str) -> Optional[float]:
    try:
        moment = datetime.strptime((text or '').strip(), '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None
    return (moment.astimezone() - SIDECAR_EPOCH).total_seconds()


def _from_sidecar_date(value) -> str:
    """iOS 当前用 Swift Date 默认编码（2001 起秒数）；也兼容文档里的 ISO 字符串。"""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ''
        try:
            moment = datetime.fromisoformat(text.replace('Z', '+00:00'))
        except ValueError:
            return ''
        return moment.astimezone().strftime('%Y-%m-%d %H:%M:%S')
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return ''
    return (SIDECAR_EPOCH + timedelta(seconds=seconds)).astimezone().strftime('%Y-%m-%d %H:%M:%S')


def _collect_sidecar_doulist_items(conn: sqlite3.Connection) -> list[dict]:
    """豆列条目的展示字段不在桌面 schema 里时也能跨端保留（与 iOS 侧车同名）。"""
    keys = {}
    for row in conn.execute('SELECT id, media_type, douban_id, title FROM doulists'):
        keys[int(row[0])] = _sidecar_list_key(str(row[1]), str(row[2] or ''), str(row[3] or ''))
    if not keys:
        return []
    items = []
    for row in conn.execute(
        'SELECT doulist_id, media_type, subject_id, duration_minutes, pages,'
        ' word_count, year, release_date, rating, comment, intro, create_time'
        ' FROM doulist_items ORDER BY doulist_id, position, id'
    ):
        list_key = keys.get(int(row[0]))
        if not list_key:
            continue
        entry = {'listKey': list_key, 'mediaType': str(row[1]),
                 'subjectID': str(row[2])}
        for index, name in ((3, 'durationMinutes'), (4, 'pages'), (5, 'wordCount')):
            value = int(row[index] or 0)
            if value:
                entry[name] = value
        for index, name in ((6, 'year'), (7, 'releaseDate')):
            value = str(row[index] or '').strip()
            if value:
                entry[name] = value
        rating = int(row[8] or 0)
        if rating:
            entry['rating'] = rating
        for index, name in ((9, 'comment'), (10, 'intro')):
            value = str(row[index] or '')
            if value:
                entry[name] = value
        created = _to_sidecar_date(str(row[11] or ''))
        if created:
            entry['createdAt'] = created
        items.append(entry)
    return items


def _write_sidecar(snapshot_path: Path, payload: dict) -> None:
    sidecar = snapshot_path.with_name(snapshot_path.name + '.ios.json')
    temp = sidecar.with_name(f'.{sidecar.name}.part')
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        os.replace(temp, sidecar)
    except OSError:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def _apply_sidecar_covers(db, snapshot_path: Path) -> int:
    """用 sidecar 里的自定义封面覆盖本地封面文件与记录路径。"""
    sidecar = snapshot_path.with_name(snapshot_path.name + '.ios.json')
    if not sidecar.is_file():
        return 0
    try:
        data = json.loads(sidecar.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return 0
    covers = {}
    for record in data.get('records') or []:
        if not isinstance(record, dict) or record.get('coverSource') != 'link':
            continue
        media_type = record.get('mediaType')
        subject_id = record.get('subjectID')
        payload = record.get('coverData')
        if media_type not in ('movie', 'book') or not subject_id or not payload:
            continue
        path = save_cover_base64(media_type, subject_id, payload)
        if path:
            covers[(media_type, str(subject_id))] = path
    if not covers:
        return 0
    try:
        conn = sqlite3.connect(str(db.path))
        try:
            for (media_type, subject_id), path in covers.items():
                conn.execute(
                    "UPDATE records SET cover_path=? WHERE media_type=? AND subject_id=?",
                    (path, media_type, subject_id))
                conn.execute(
                    "UPDATE doulist_items SET cover_path=? WHERE media_type=? AND subject_id=?",
                    (path, media_type, subject_id))
                conn.execute(
                    "UPDATE plan_items SET cover_path=? WHERE media_type=? AND subject_id=?",
                    (path, media_type, subject_id))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        return 0
    return len(covers)


def _snapshot_marks_authoritative(conn: sqlite3.Connection) -> bool:
    """快照是否声明自己完整维护 ignored_subjects / purged_records。"""
    try:
        row = conn.execute(
            'SELECT value FROM settings WHERE key=?', (SETTING_MARKS_VERSION,)
        ).fetchone()
    except sqlite3.Error:
        return False
    return bool(row and str(row[0]).strip() == MARKS_VERSION)


SIDECAR_ITEM_FIELDS = (
    ('durationMinutes', 'duration_minutes', int),
    ('pages', 'pages', int),
    ('wordCount', 'word_count', int),
    ('year', 'year', str),
    ('releaseDate', 'release_date', str),
    ('rating', 'rating', int),
    ('comment', 'comment', str),
    ('intro', 'intro', str),
)


def _apply_sidecar_doulist_items(db, snapshot_path: Path) -> int:
    """把 iOS 侧车里的豆列条目展示字段写回本机（桌面 schema 不全都有这些列）。"""
    sidecar = snapshot_path.with_name(snapshot_path.name + '.ios.json')
    if not sidecar.is_file():
        return 0
    try:
        data = json.loads(sidecar.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return 0
    items = data.get('doulistItems')
    if not isinstance(items, list) or not items:
        return 0
    updated = 0
    try:
        conn = sqlite3.connect(str(db.path))
        try:
            keys = {}
            for row in conn.execute('SELECT id, media_type, douban_id, title FROM doulists'):
                raw_id = str(row[2] or '')
                canonical = _sidecar_list_key(str(row[1]), raw_id, str(row[3] or ''))
                keys[canonical] = int(row[0])
                # 兼容早期桌面端写出的 `douban:ios-local-…` / `douban:<UUID>` 写法。
                legacy = f'douban:{raw_id.strip()}'
                if raw_id.strip() and legacy != canonical:
                    keys.setdefault(legacy, int(row[0]))
            for item in items:
                if not isinstance(item, dict):
                    continue
                doulist_id = keys.get(str(item.get('listKey') or ''))
                media_type = str(item.get('mediaType') or '')
                subject_id = str(item.get('subjectID') or '')
                if not doulist_id or not media_type or not subject_id:
                    continue
                assignments, values = [], []
                for source, column, cast in SIDECAR_ITEM_FIELDS:
                    if item.get(source) is None:
                        continue
                    try:
                        values.append(cast(item[source]))
                    except (TypeError, ValueError):
                        continue
                    assignments.append(f'{column}=?')
                if item.get('createdAt') is not None:
                    created = _from_sidecar_date(item.get('createdAt'))
                    if created:
                        assignments.append('create_time=?')
                        values.append(created)
                if not assignments:
                    continue
                cursor = conn.execute(
                    f'UPDATE doulist_items SET {", ".join(assignments)}'
                    ' WHERE doulist_id=? AND media_type=? AND subject_id=?',
                    (*values, doulist_id, media_type, subject_id),
                )
                updated += int(cursor.rowcount or 0)
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        return 0
    return updated


def _preserve_ios_markers(db, snapshot_path: Path, local_backup: Path) -> None:
    """老 iOS 快照不维护 ignored_subjects / purged_records。

    这类快照里两张表为空，并不代表用户主动清空了标记；恢复时保留本机原有的
    换源忽略与彻底删除墓碑，避免豆瓣同步把旧条目/已删条目重新加回来。

    带 `snapshot_marks_version` 标记的快照（桌面端与 iOS v1.3+）写全了两张表，
    空表就是真的清空，按快照覆盖即可。
    """
    try:
        conn = sqlite3.connect(str(db.path))
        try:
            if _snapshot_marks_authoritative(conn):
                return
            ios_like = snapshot_path.with_name(snapshot_path.name + '.ios.json').is_file()
            if not ios_like:
                row = conn.execute(
                    "SELECT 1 FROM settings WHERE key='ios_explore_sites'").fetchone()
                ios_like = bool(row)
            if not ios_like:
                return
            counts = [conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                      for table in ('ignored_subjects', 'purged_records')]
            if any(counts):
                return
            conn.execute('ATTACH DATABASE ? AS olddb', (str(local_backup),))
            try:
                conn.execute(
                    'INSERT OR IGNORE INTO ignored_subjects'
                    ' SELECT media_type, subject_id, created_at FROM olddb.ignored_subjects')
                conn.execute(
                    'INSERT OR IGNORE INTO purged_records'
                    ' SELECT media_type, subject_id, status, title, purged_at'
                    ' FROM olddb.purged_records')
                conn.commit()
            finally:
                try:
                    conn.execute('DETACH DATABASE olddb')
                except sqlite3.Error:
                    pass
        finally:
            conn.close()
    except sqlite3.Error:
        pass


def create_snapshot(db, force: bool = False, kind: str = KIND_MANUAL) -> dict:
    """把当前数据库写成一致性快照。内容没变且已有快照时直接跳过。

    kind 区分「自动备份」（auto）与用户点击生成的手动备份（manual），
    两者各自计数、各自保留，互不覆盖。
    """
    kind = kind if kind in (KIND_AUTO, KIND_MANUAL) else KIND_MANUAL
    folder = sync_folder(db, create=True)
    if folder is None:
        raise SyncError('没有可用的同步目录，请先在设置中开启 iCloud 云盘或选择目录。')

    current_state = state_id(db.path)
    if not force and db.get_setting(SETTING_LAST_STATE) == current_state:
        latest = load_manifest(folder)
        # 最新快照被 iCloud 驱逐、只剩占位文件时也算已备份：这里若重做一份会把
        # 清单指向本机旧数据，其他设备下次同步就会把这份旧的当成「最新」。
        if latest and _snapshot_present(folder, latest.get('snapshot') or ''):
            return {
                'skipped': True,
                'folder': str(folder),
                'snapshot': latest.get('snapshot', ''),
                'created_at': latest.get('created_at', ''),
                'counts': latest.get('counts', {}),
            }

    name = _unique_snapshot_name(folder, kind)
    final = folder / name
    temp = folder / f'.{name}.part'
    source = sqlite3.connect(str(db.path))
    target = sqlite3.connect(str(temp))
    try:
        source.backup(target)
        placeholders = ','.join('?' for _ in LOCAL_SETTING_KEYS)
        target.execute(
            f'DELETE FROM settings WHERE key IN ({placeholders})', LOCAL_SETTING_KEYS
        )
        _embed_ios_explore_sites(target)
        _mark_marks_authoritative(target)
        target.commit()
        counts = _counts(target)
        media = _media_counts(target)
        counts['records_movie'] = media['movie']
        counts['records_book'] = media['book']
        last_updated = _records_updated(target)
        sidecar = _collect_sidecar(target)
    finally:
        target.close()
        source.close()

    try:
        sha256 = _sha256_file(temp)
        os.replace(temp, final)
        _write_sidecar(final, sidecar)
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise SyncError(f'写入快照失败：{exc}') from exc

    profile = device_profile()
    manifest = {
        'snapshot': name,
        'created_at': _now_text(),
        'tag': profile['tag'],
        'platform': profile['platform'],
        'arch': profile['arch'],
        'created_ts': int(time.time()),
        'device': platform.node() or '未知设备',
        'app_version': APP_VERSION,
        'sha256': sha256,
        'size': final.stat().st_size,
        'counts': counts,
        'records_updated': last_updated,
        'kind': kind,
    }
    _write_manifest(folder, manifest)
    _remember_device(folder)
    _prune_snapshots(folder)

    db.set_setting(SETTING_LAST_STATE, current_state)
    db.set_setting(SETTING_LAST_TIME, manifest['created_at'])
    db.set_setting(SETTING_LAST_SNAPSHOT, name)
    return {'skipped': False, 'folder': str(folder), **manifest}


def check_sync(db) -> dict:
    """检查同步目录与本地状态，供设置页判断是否可备份/恢复。"""
    folder = sync_folder(db)
    local_state = state_id(db.path)
    last_state = db.get_setting(SETTING_LAST_STATE)
    result = {
        'folder': str(folder) if folder else '',
        'configured': bool((db.get_setting(SETTING_DIR) or '').strip()),
        'manifest': None,
        'local_state': local_state,
        'dirty': local_state != (last_state or ''),
        'new': False,
        'missing': False,
    }
    if folder is None or not folder.is_dir():
        return result
    manifest = load_manifest(folder)
    result['manifest'] = manifest
    if not manifest:
        return result
    result['missing'] = not (folder / manifest['snapshot']).is_file()
    result['new'] = manifest.get('snapshot') != db.get_setting(SETTING_LAST_SNAPSHOT)
    return result


def _open_snapshot(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f'file:{path}?mode=ro', uri=True)


def validate_snapshot(path: Path) -> dict:
    if not path.is_file():
        if (path.parent / f'.{path.name}.icloud').is_file():
            raise SyncError('这份快照还没有下载到本机，请稍候或先在同步目录中打开一次再恢复。')
        raise SyncError('快照文件不存在，可能还在同步中，请稍后再试。')
    try:
        conn = _open_snapshot(path)
    except sqlite3.Error as exc:
        raise SyncError(f'无法读取快照：{exc}') from exc
    try:
        try:
            status = conn.execute('PRAGMA integrity_check').fetchone()[0]
        except sqlite3.Error as exc:
            raise SyncError(f'快照校验失败：{exc}') from exc
        if status != 'ok':
            raise SyncError('快照文件已损坏，无法恢复。')
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if not {'records', 'settings', 'doulists', 'plan_folders'} <= tables:
            raise SyncError('不是书影记录数据库快照。')
        return {'counts': _counts(conn), 'records_updated': _records_updated(conn)}
    finally:
        conn.close()


def _backup_local_before_restore(db) -> Path:
    backups = Path(db.path).parent / 'backups'
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    target = backups / f'douban_trace_before_restore_{stamp}.db'
    index = 2
    while target.exists():
        target = backups / f'douban_trace_before_restore_{stamp}_{index}.db'
        index += 1
    source = sqlite3.connect(str(db.path))
    dest = sqlite3.connect(str(target))
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()
    old = sorted(backups.glob('douban_trace_before_restore_*.db'),
                 key=lambda path: path.name, reverse=True)
    for path in old[LOCAL_BACKUP_KEEP:]:
        try:
            path.unlink()
        except OSError:
            pass
    return target


def restore_snapshot(db, manifest: Optional[dict] = None) -> dict:
    """用同步目录中的快照覆盖本地数据库；覆盖前自动备份本地库。"""
    folder = sync_folder(db)
    if folder is None or not folder.is_dir():
        raise SyncError('没有可用的同步目录。')
    manifest = manifest or load_manifest(folder)
    if not manifest:
        raise SyncError('同步目录里还没有备份。')
    path = folder / manifest['snapshot']
    info = validate_snapshot(path)
    expected = manifest.get('sha256')
    if expected and _sha256_file(path) != expected:
        raise SyncError('快照校验失败，可能还在同步中，请稍后再试。')

    preserved = {key: db.get_setting(key) for key in PRESERVE_ON_RESTORE}
    local_backup = _backup_local_before_restore(db)

    source = sqlite3.connect(str(path))
    dest = sqlite3.connect(str(db.path))
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()

    db._init_schema()
    for key, value in preserved.items():
        if value:
            db.set_setting(key, value)
    sync_book_pages(db)
    _restore_ios_compatibility(db, path)
    _apply_sidecar_covers(db, path)
    _apply_sidecar_doulist_items(db, path)
    _preserve_ios_markers(db, path, local_backup)
    _strip_snapshot_meta(db)
    state = state_id(db.path)
    db.set_setting(SETTING_LAST_STATE, state)
    db.set_setting(SETTING_LAST_TIME, _now_text())
    db.set_setting(SETTING_LAST_SNAPSHOT, manifest['snapshot'])
    return {
        'manifest': manifest,
        'counts': info['counts'],
        'local_backup': str(local_backup),
    }


def db_overview(db) -> dict:
    """本机当前库的概览，用于和快照做对比展示。"""
    try:
        conn = sqlite3.connect(str(db.path))
        try:
            counts = _counts(conn)
            media = _media_counts(conn)
            counts['records_movie'] = media['movie']
            counts['records_book'] = media['book']
            return {'counts': counts, 'records_updated': _records_updated(conn)}
        finally:
            conn.close()
    except sqlite3.Error:
        return {'counts': {}, 'records_updated': ''}


def _db_empty(db) -> bool:
    """本地库是否还没有任何业务数据（新设备首次启动）。"""
    try:
        conn = sqlite3.connect(str(db.path))
        try:
            for table in ('records', 'doulists', 'plan_items'):
                if conn.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone():
                    return False
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return True


def auto_backup(db) -> Optional[dict]:
    """勾选「自动备份」时生成自动快照；未开启、内容没变或目录不可用时静默跳过。"""
    if db.get_setting(SETTING_AUTO_BACKUP) != '1':
        return None
    try:
        return create_snapshot(db, kind=KIND_AUTO)
    except SyncError:
        return None


def auto_restore(db) -> Optional[dict]:
    """勾选「自动同步」时从最新快照恢复。

    只有本机没有未备份的改动（或本地还是空库）时才自动恢复，避免覆盖本地编辑。
    """
    if db.get_setting(SETTING_AUTO_SYNC) != '1':
        return None
    folder = sync_folder(db)
    if folder is None or not folder.is_dir():
        return None
    manifest = load_manifest(folder)
    if not manifest:
        return None
    if not (folder / manifest['snapshot']).is_file():
        return None
    if manifest.get('snapshot') == db.get_setting(SETTING_LAST_SNAPSHOT):
        return None
    local_state = state_id(db.path)
    if local_state != (db.get_setting(SETTING_LAST_STATE) or '') and not _db_empty(db):
        return None
    try:
        return restore_snapshot(db, manifest)
    except SyncError:
        return None
