import os
import sys
from pathlib import Path


def _app_root() -> Path:
    override = os.environ.get('SHUYINGJILU_HOME', '').strip()
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, 'frozen', False):
        if sys.platform == 'darwin':
            return Path.home() / 'Library' / 'Application Support' / '书影记录'
        if os.name == 'nt':
            base = os.environ.get('APPDATA') or str(Path.home())
            return Path(base) / '书影记录'
        base = os.environ.get('XDG_DATA_HOME') or str(Path.home() / '.local' / 'share')
        return Path(base) / '书影记录'
    return Path(__file__).resolve().parent.parent


ROOT = _app_root()
DATA_DIR = ROOT / 'data'
CACHE_DIR = ROOT / 'cache'
COVERS_DIR = CACHE_DIR / 'covers'
AVATAR_DIR = CACHE_DIR / 'profile'
OUTPUT_DIR = ROOT / 'output'
REPORTS_DIR = OUTPUT_DIR / 'reports'
PLANS_DIR = OUTPUT_DIR / 'plans'
DB_PATH = DATA_DIR / 'douban_trace.db'
BOOK_META_CACHE = CACHE_DIR / 'book_meta.json'
MOVIE_META_CACHE = CACHE_DIR / 'movie_meta.json'
BOOK_WORDS_CACHE = CACHE_DIR / 'book_words.json'

APP_VERSION = '0.1.1'
APP_VERSION_TEXT = f'v{APP_VERSION}'

MEDIA_LABELS = {'movie': '观影', 'book': '读书'}
MEDIA_NOUNS = {'movie': '部', 'book': '本'}
READ_LABELS = {'movie': ('已看', '未看'), 'book': ('已读', '未读')}
WISH_LABELS = {'movie': '想看', 'book': '想读'}


def ensure_dirs() -> None:
    for path in (DATA_DIR, CACHE_DIR, COVERS_DIR, AVATAR_DIR, OUTPUT_DIR, REPORTS_DIR, PLANS_DIR):
        path.mkdir(parents=True, exist_ok=True)
