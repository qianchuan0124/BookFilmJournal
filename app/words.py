import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unicodedata
import zipfile
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Optional

from app.config import BOOK_WORDS_CACHE

EBOOK_EXTENSIONS = {
    '.epub', '.mobi', '.azw', '.azw3', '.pdf', '.txt', '.fb2', '.docx',
    '.rtf', '.lit', '.djvu', '.chm', '.html', '.htm',
}
FORMAT_PRIORITY = {
    '.epub': 0, '.txt': 1, '.azw3': 2, '.mobi': 3, '.azw': 4,
    '.docx': 5, '.pdf': 6, '.fb2': 7, '.rtf': 8, '.lit': 9,
    '.djvu': 10, '.chm': 11, '.html': 12, '.htm': 13,
}
CJK_RE = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]')
LATIN_WORD_RE = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*")
NUMBER_RE = re.compile(r'\d+(?:[.,]\d+)*')
BRACKET_RE = re.compile(r'\[[^\]]*\]|【[^】]*】|\([^)]*\)|（[^）]*）|\{([^{}]*)\}')


class WordCountError(Exception):
    pass


def find_calibre() -> str:
    candidates = [
        os.environ.get('CALIBRE_EBOOK_CONVERT', ''),
        '/Applications/calibre.app/Contents/MacOS/ebook-convert',
        str(Path.home() / 'Applications' / 'calibre.app' / 'Contents' / 'MacOS' / 'ebook-convert'),
        shutil.which('ebook-convert') or '',
        shutil.which('ebook-convert.exe') or '',
        os.path.expandvars(r'%LOCALAPPDATA%\Programs\calibre\ebook-convert.exe'),
        os.path.expandvars(r'%LOCALAPPDATA%\calibre\ebook-convert.exe'),
        r'C:\Program Files\Calibre2\ebook-convert.exe',
        r'C:\Program Files (x86)\Calibre2\ebook-convert.exe',
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return ''


def format_word_count(count: int) -> str:
    count = int(count or 0)
    if count <= 0:
        return ''
    if count >= 10000:
        value = count / 10000
        return f'{value:.1f} 万字' if value < 100 else f'{value:.0f} 万字'
    return f'{count} 字'


def _normalize_title(text: str) -> str:
    text = unicodedata.normalize('NFKC', str(text or '')).lower()
    text = BRACKET_RE.sub('', text)
    text = re.sub(r'[\s\u3000]+', '', text)
    return ''.join(ch for ch in text if ch.isalnum())


def title_variants(title: str) -> list[str]:
    raw = unicodedata.normalize('NFKC', str(title or '')).strip()
    raw = raw.strip('《》〈〉 ')
    variants: list[str] = []

    def add(value: str) -> None:
        normalized = _normalize_title(value)
        if len(normalized) >= 2 and normalized not in variants:
            variants.append(normalized)

    add(raw)
    add(raw.split(' / ')[0])
    add(BRACKET_RE.sub('', raw))
    add(re.sub(r'[：:].+$', '', raw))
    add(re.sub(r'\s*[-—]\s*.+$', '', raw))
    add(re.sub(r'(第\s*[0-9一二三四五六七八九十]+\s*版|修订版|新版|增订版|精装版|套装|全集|全\s*\d+\s*册)$', '', raw))
    return variants


def _better_path(current: Optional[Path], candidate: Path) -> Path:
    if current is None:
        return candidate
    current_priority = FORMAT_PRIORITY.get(current.suffix.lower(), 99)
    candidate_priority = FORMAT_PRIORITY.get(candidate.suffix.lower(), 99)
    return candidate if candidate_priority < current_priority else current


def _walk_ebooks(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if not name.startswith('.')
                       and name not in ('__MACOSX', '@eaDir')]
        for filename in filenames:
            if filename.startswith('.'):
                continue
            path = Path(dirpath) / filename
            if path.suffix.lower() in EBOOK_EXTENSIONS:
                files.append(path)
    return files


def _calibre_books(root: Path) -> list[tuple[str, Path]]:
    metadata = root / 'metadata.db'
    if not metadata.exists():
        return []
    books: list[tuple[str, Path]] = []
    try:
        conn = sqlite3.connect(f'file:{metadata}?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        formats: dict[int, list[tuple[str, str]]] = defaultdict(list)
        for row in conn.execute('SELECT book, format, name FROM data'):
            formats[int(row['book'])].append((str(row['format'] or '').lower(), str(row['name'] or '')))
        for row in conn.execute('SELECT id, title, path FROM books'):
            book_dir = root / str(row['path'] or '')
            for fmt, name in formats.get(int(row['id']), []):
                if fmt not in {ext.lstrip('.') for ext in EBOOK_EXTENSIONS}:
                    continue
                file_path = book_dir / f'{name}.{fmt}'
                if not file_path.exists():
                    candidate = book_dir / name
                    if candidate.exists():
                        file_path = candidate
                if file_path.exists():
                    books.append((str(row['title'] or file_path.stem), file_path))
        conn.close()
    except (sqlite3.Error, OSError):
        return []
    return books


def build_library_index(root: Path) -> dict[str, Path]:
    root = Path(root)
    if not root.exists() or not root.is_dir():
        raise WordCountError('本地书库目录不存在。')
    index: dict[str, Path] = {}
    entries: list[tuple[str, Path]] = []
    entries.extend(_calibre_books(root))
    entries.extend((path.stem, path) for path in _walk_ebooks(root))
    for title, path in entries:
        for variant in title_variants(title):
            index[variant] = _better_path(index.get(variant), path)
    return index


def match_book(title: str, index: dict[str, Path]) -> Optional[Path]:
    variants = title_variants(title)
    for variant in variants:
        if variant in index:
            return index[variant]
    best_path = None
    best_score = 0.0
    for variant in variants:
        if len(variant) < 2:
            continue
        for key, path in index.items():
            if len(key) < 2:
                continue
            score = 0.0
            if variant == key:
                score = 1.0
            elif variant in key or key in variant:
                shorter, longer = (variant, key) if len(variant) <= len(key) else (key, variant)
                if len(longer) - len(shorter) <= max(4, len(shorter) // 2):
                    score = 0.94 - (len(longer) - len(shorter)) / max(len(longer), 1) * 0.08
            if score < 0.93:
                ratio = SequenceMatcher(None, variant, key).ratio()
                if ratio >= 0.93:
                    score = ratio
            if score > best_score:
                best_score = score
                best_path = path
    return best_path if best_score >= 0.93 else None


def count_words(text: str) -> int:
    if not text:
        return 0
    cjk = len(CJK_RE.findall(text))
    latin = len(LATIN_WORD_RE.findall(text))
    numbers = len(NUMBER_RE.findall(text))
    return cjk + latin + numbers


def _read_text_file(path: Path) -> str:
    for encoding in ('utf-8-sig', 'utf-8', 'gb18030', 'utf-16', 'latin-1'):
        try:
            return path.read_text(encoding=encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return path.read_text(encoding='utf-8', errors='ignore')


def _strip_markup(text: str) -> str:
    text = re.sub(r'(?is)<(script|style)[^>]*>.*?</\1>', ' ', text)
    return re.sub(r'(?s)<[^>]+>', ' ', text)


def _extract_epub_text(path: Path) -> str:
    parts: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if not name.lower().endswith(('.xhtml', '.html', '.htm', '.xml', '.txt')):
                    continue
                try:
                    content = archive.read(name).decode('utf-8', 'ignore')
                except KeyError:
                    continue
                parts.append(_strip_markup(content))
    except (zipfile.BadZipFile, OSError):
        return ''
    return '\n'.join(parts)


def _extract_docx_text(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            content = archive.read('word/document.xml').decode('utf-8', 'ignore')
    except (KeyError, zipfile.BadZipFile, OSError):
        return ''
    content = content.replace('</w:p>', '\n')
    return _strip_markup(content)


def extract_text(path: Path, calibre: str = '') -> str:
    suffix = path.suffix.lower()
    if suffix == '.txt':
        return _read_text_file(path)
    if calibre:
        with tempfile.TemporaryDirectory(prefix='douban_words_') as tmp:
            output = Path(tmp) / 'book.txt'
            try:
                subprocess.run(
                    [calibre, str(path), str(output), '--quiet'],
                    capture_output=True, timeout=240, check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                output = None
            if output and output.exists():
                text = _read_text_file(output)
                if text.strip():
                    return text
    if suffix == '.epub':
        return _extract_epub_text(path)
    if suffix == '.docx':
        return _extract_docx_text(path)
    return ''


def _load_cache() -> dict:
    if not BOOK_WORDS_CACHE.exists():
        return {}
    try:
        return json.loads(BOOK_WORDS_CACHE.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict) -> None:
    BOOK_WORDS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    BOOK_WORDS_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding='utf-8')


def _cache_key(path: Path) -> str:
    try:
        stat = path.stat()
        return f'{path}|{stat.st_size}|{int(stat.st_mtime)}'
    except OSError:
        return str(path)


def run_word_count(db, library_dir: str, force: bool = False,
                   progress: Optional[Callable[[str, int, int], None]] = None) -> dict:
    root = Path(library_dir).expanduser()
    index = build_library_index(root)
    if not index:
        raise WordCountError('目录中没有找到电子书文件。')
    calibre = find_calibre()
    cache = _load_cache()
    existing = db.word_count_map('book')

    candidates: list[dict] = []
    seen: set[str] = set()
    for row in db.list_all_records('book'):
        subject_id = str(row['subject_id'])
        seen.add(subject_id)
        candidates.append({
            'subject_id': subject_id,
            'title': row['title'] or '',
            'record_id': int(row['id']),
            'word_count': int(row['word_count'] or 0),
        })
    for row in db.doulist_book_entries():
        subject_id = str(row['subject_id'])
        if subject_id in seen:
            continue
        seen.add(subject_id)
        candidates.append({
            'subject_id': subject_id,
            'title': row['title'] or '',
            'record_id': 0,
            'word_count': int(existing.get(subject_id, 0) or 0),
        })

    stats = {
        'total': len(candidates),
        'matched': 0,
        'counted': 0,
        'cached': 0,
        'missing': 0,
        'failed': 0,
        'total_words': 0,
        'calibre': calibre,
        'library': str(root),
    }
    for index_number, entry in enumerate(candidates, 1):
        title = entry['title']
        if progress:
            progress(f'匹配《{title}》', index_number, len(candidates))
        if entry['word_count'] > 0 and not force:
            stats['cached'] += 1
            stats['matched'] += 1
            stats['total_words'] += entry['word_count']
            continue
        path = match_book(title, index)
        if not path:
            stats['missing'] += 1
            continue
        stats['matched'] += 1
        key = _cache_key(path)
        word_count = 0 if force else int(cache.get(key, 0) or 0)
        if word_count > 0:
            stats['cached'] += 1
        else:
            text = extract_text(path, calibre)
            word_count = count_words(text)
            if word_count > 0:
                cache[key] = word_count
                stats['counted'] += 1
        if word_count > 0:
            if entry['record_id']:
                db.set_word_count(entry['record_id'], word_count, str(path))
            else:
                db.set_book_word(entry['subject_id'], word_count, str(path))
            stats['total_words'] += word_count
        else:
            stats['failed'] += 1
    _save_cache(cache)
    return stats

