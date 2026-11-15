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
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.config import APP_VERSION, DB_PATH

SYNC_SUBDIR = '书影记录'
SNAPSHOT_PREFIX = 'douban_trace_'
SNAPSHOT_SUFFIX = '.db'
MANIFEST_NAME = 'latest.json'
SNAPSHOT_KEEP = 8
LOCAL_BACKUP_KEEP = 5

COUNT_TABLES = (
    'records', 'doulists', 'doulist_items', 'plan_folders', 'plan_items',
    'book_words', 'ignored_subjects', 'purged_records',
)

# 只属于当前设备的设置：不写进快照，恢复时保留本机值。
LOCAL_SETTING_KEYS = (
    'douban_cookie',
    'book_library_dir',
    'cloud_sync_dir',
    'cloud_sync_last_state',
    'cloud_sync_last_time',
    'cloud_sync_last_snapshot',
)
PRESERVE_ON_RESTORE = ('douban_cookie', 'book_library_dir', 'cloud_sync_dir')

SETTING_DIR = 'cloud_sync_dir'
SETTING_LAST_STATE = 'cloud_sync_last_state'
SETTING_LAST_TIME = 'cloud_sync_last_time'
SETTING_LAST_SNAPSHOT = 'cloud_sync_last_snapshot'


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


def sync_folder(db, create: bool = False) -> Optional[Path]:
    root = base_dir(db)
    if root is None:
        return None
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


def _prune_snapshots(folder: Path, keep_name: str) -> None:
    snapshots = sorted(
        (path for path in folder.glob(f'{SNAPSHOT_PREFIX}*{SNAPSHOT_SUFFIX}')
         if path.name != keep_name),
        key=lambda path: path.name,
        reverse=True,
    )
    for path in snapshots[SNAPSHOT_KEEP - 1:]:
        try:
            path.unlink()
        except OSError:
            pass


def _device_tag() -> str:
    name = platform.node() or 'device'
    return hashlib.sha1(name.encode('utf-8')).hexdigest()[:4]


def _unique_snapshot_name(folder: Path) -> str:
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    tag = _device_tag()
    name = f'{SNAPSHOT_PREFIX}{stamp}_{tag}{SNAPSHOT_SUFFIX}'
    index = 2
    while (folder / name).exists():
        name = f'{SNAPSHOT_PREFIX}{stamp}_{tag}_{index}{SNAPSHOT_SUFFIX}'
        index += 1
    return name


def create_snapshot(db, force: bool = False) -> dict:
    """把当前数据库写成一致性快照。内容没变且已有快照时直接跳过。"""
    folder = sync_folder(db, create=True)
    if folder is None:
        raise SyncError('没有可用的同步目录，请先在设置中开启 iCloud 云盘或选择目录。')

    current_state = state_id(db.path)
    if not force and db.get_setting(SETTING_LAST_STATE) == current_state:
        latest = load_manifest(folder)
        if latest and (folder / latest['snapshot']).is_file():
            return {
                'skipped': True,
                'folder': str(folder),
                'snapshot': latest.get('snapshot', ''),
                'created_at': latest.get('created_at', ''),
                'counts': latest.get('counts', {}),
            }

    name = _unique_snapshot_name(folder)
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
        target.commit()
        counts = _counts(target)
        last_updated = _records_updated(target)
    finally:
        target.close()
        source.close()

    try:
        sha256 = _sha256_file(temp)
        os.replace(temp, final)
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise SyncError(f'写入快照失败：{exc}') from exc

    manifest = {
        'snapshot': name,
        'created_at': _now_text(),
        'created_ts': int(time.time()),
        'device': platform.node() or '未知设备',
        'app_version': APP_VERSION,
        'sha256': sha256,
        'size': final.stat().st_size,
        'counts': counts,
        'records_updated': last_updated,
    }
    _write_manifest(folder, manifest)
    _prune_snapshots(folder, name)

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
    state = state_id(db.path)
    db.set_setting(SETTING_LAST_STATE, state)
    db.set_setting(SETTING_LAST_TIME, _now_text())
    db.set_setting(SETTING_LAST_SNAPSHOT, manifest['snapshot'])
    return {
        'manifest': manifest,
        'counts': info['counts'],
        'local_backup': str(local_backup),
    }
