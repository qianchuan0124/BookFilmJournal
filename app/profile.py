import base64
import shutil
from pathlib import Path
from typing import Optional

from app.config import AVATAR_DIR, ROOT
from app.db import Database

DEFAULT_NAME = '我'

_AVATAR_MIME = {
    '.png': 'image/png',
    '.webp': 'image/webp',
    '.gif': 'image/gif',
    '.bmp': 'image/bmp',
}


def _absolute(path: str) -> Optional[Path]:
    text = (path or '').strip()
    if not text:
        return None
    target = Path(text)
    if not target.is_absolute():
        target = ROOT / target
    return target if target.exists() else None


def custom_name(db: Database) -> str:
    return db.get_setting('display_name').strip()


def douban_name(db: Database) -> str:
    return db.get_setting('douban_display_name').strip()


def has_custom_name(db: Database) -> bool:
    return db.get_setting('display_name_custom') == '1' and bool(custom_name(db))


def migrate_legacy_profile(db: Database) -> None:
    if db.get_setting('douban_display_name'):
        return
    legacy = db.get_setting('display_name').strip()
    if legacy and db.get_setting('display_name_custom') != '1':
        db.set_setting('douban_display_name', legacy)


def effective_name(db: Database) -> str:
    if has_custom_name(db):
        return custom_name(db)
    return douban_name(db) or custom_name(db) or DEFAULT_NAME


def name_source(db: Database) -> str:
    if has_custom_name(db):
        return 'custom'
    if douban_name(db) or custom_name(db):
        return 'douban'
    return 'default'


def custom_avatar(db: Database) -> Optional[Path]:
    return _absolute(db.get_setting('display_avatar'))


def douban_avatar(db: Database) -> Optional[Path]:
    return _absolute(db.get_setting('douban_avatar'))


def effective_avatar(db: Database) -> str:
    path = custom_avatar(db) or douban_avatar(db)
    return str(path) if path else ''


def avatar_source(db: Database) -> str:
    if custom_avatar(db):
        return 'custom'
    if douban_avatar(db):
        return 'douban'
    return 'default'


def relative(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def save_custom_avatar(source: str) -> str:
    src = Path(source)
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    for old in AVATAR_DIR.glob('custom_avatar.*'):
        old.unlink(missing_ok=True)
    suffix = src.suffix.lower() or '.jpg'
    target = AVATAR_DIR / f'custom_avatar{suffix}'
    shutil.copyfile(src, target)
    return relative(target)


def clear_custom_avatar() -> None:
    for old in AVATAR_DIR.glob('custom_avatar.*'):
        old.unlink(missing_ok=True)


def avatar_data_uri(path: str) -> str:
    target = _absolute(path)
    if not target:
        return ''
    suffix = target.suffix.lower()
    mime = _AVATAR_MIME.get(suffix, 'image/jpeg')
    encoded = base64.b64encode(target.read_bytes()).decode('ascii')
    return f'data:{mime};base64,{encoded}'
