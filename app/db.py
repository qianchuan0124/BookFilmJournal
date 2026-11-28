import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from app.config import DB_PATH, ensure_dirs
from app.cover_sync import cover_is_valid, is_custom_cover


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


SCHEMA = '''
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    cover_url TEXT NOT NULL DEFAULT '',
    cover_path TEXT NOT NULL DEFAULT '',
    rating INTEGER NOT NULL DEFAULT 0,
    marked_date TEXT NOT NULL DEFAULT '',
    effective_date TEXT NOT NULL DEFAULT '',
    date_overridden INTEGER NOT NULL DEFAULT 0,
    rating_overridden INTEGER NOT NULL DEFAULT 0,
    comment_overridden INTEGER NOT NULL DEFAULT 0,
    comment TEXT NOT NULL DEFAULT '',
    intro TEXT NOT NULL DEFAULT '',
    word_count INTEGER NOT NULL DEFAULT 0,
    book_file TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'douban',
    status TEXT NOT NULL DEFAULT 'done',
    synced_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    deleted_at TEXT NOT NULL DEFAULT '',
    deleted_by TEXT NOT NULL DEFAULT '',
    UNIQUE(media_type, subject_id, status)
);
CREATE TABLE IF NOT EXISTS doulists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_type TEXT NOT NULL DEFAULT 'movie',
    douban_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    item_count INTEGER NOT NULL DEFAULT 0,
    relation TEXT NOT NULL DEFAULT 'created',
    position INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL DEFAULT '',
    deleted_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS doulist_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doulist_id INTEGER NOT NULL REFERENCES doulists(id) ON DELETE CASCADE,
    media_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    cover_url TEXT NOT NULL DEFAULT '',
    cover_path TEXT NOT NULL DEFAULT '',
    rating INTEGER NOT NULL DEFAULT 0,
    comment TEXT NOT NULL DEFAULT '',
    intro TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0,
    create_time TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'done',
    duration_minutes INTEGER NOT NULL DEFAULT 0,
    pages INTEGER NOT NULL DEFAULT 0,
    word_count INTEGER NOT NULL DEFAULT 0,
    year TEXT NOT NULL DEFAULT '',
    release_date TEXT NOT NULL DEFAULT '',
    UNIQUE(doulist_id, media_type, subject_id)
);
CREATE TABLE IF NOT EXISTS plan_folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_type TEXT NOT NULL,
    title TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT '',
    deleted_at TEXT NOT NULL DEFAULT '',
    UNIQUE(media_type, title)
);
CREATE TABLE IF NOT EXISTS plan_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_id INTEGER NOT NULL DEFAULT 0,
    media_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    cover_url TEXT NOT NULL DEFAULT '',
    cover_path TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT '',
    deleted_at TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'done',
    release_date TEXT NOT NULL DEFAULT '',
    UNIQUE(folder_id, media_type, subject_id)
);
CREATE TABLE IF NOT EXISTS book_words (
    subject_id TEXT PRIMARY KEY,
    word_count INTEGER NOT NULL DEFAULT 0,
    book_file TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS book_meta (
    subject_id TEXT PRIMARY KEY,
    pages INTEGER NOT NULL DEFAULT 0,
    pages_manual INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS movie_meta (
    subject_id TEXT PRIMARY KEY,
    duration_minutes INTEGER NOT NULL DEFAULT 0,
    duration_manual INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ignored_subjects (
    media_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(media_type, subject_id)
);
CREATE TABLE IF NOT EXISTS purged_records (
    media_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'done',
    title TEXT NOT NULL DEFAULT '',
    purged_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(media_type, subject_id, status)
);
'''

INDEXES = '''
CREATE INDEX IF NOT EXISTS idx_records_type_date
    ON records(media_type, effective_date DESC);
CREATE INDEX IF NOT EXISTS idx_records_type_status_date
    ON records(media_type, status, effective_date DESC);
CREATE INDEX IF NOT EXISTS idx_doulist_items_list
    ON doulist_items(doulist_id, position);
CREATE INDEX IF NOT EXISTS idx_plan_items_type_position
    ON plan_items(media_type, position);
CREATE INDEX IF NOT EXISTS idx_plan_items_folder_position
    ON plan_items(folder_id, position);
CREATE INDEX IF NOT EXISTS idx_plan_folders_type_position
    ON plan_folders(media_type, position);
'''

PLAN_MIGRATE = '''
ALTER TABLE plan_items RENAME TO plan_items_old;
CREATE TABLE plan_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_id INTEGER NOT NULL DEFAULT 0,
    media_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    cover_url TEXT NOT NULL DEFAULT '',
    cover_path TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT '',
    UNIQUE(folder_id, media_type, subject_id)
);
INSERT INTO plan_folders (media_type, title, note, position, created_at)
SELECT DISTINCT o.media_type, '默认计划', '', 0, ''
FROM plan_items_old o
WHERE NOT EXISTS (
    SELECT 1 FROM plan_folders f
    WHERE f.media_type = o.media_type AND f.title = '默认计划'
);
INSERT INTO plan_items (
    folder_id, media_type, subject_id, title, url, cover_url, cover_path,
    source, source_id, note, position, created_at
)
SELECT
    f.id, o.media_type, o.subject_id, o.title, o.url, o.cover_url, o.cover_path,
    o.source, o.source_id, o.note, o.position, o.created_at
FROM plan_items_old o
JOIN plan_folders f ON f.media_type = o.media_type AND f.title = '默认计划';
DROP TABLE plan_items_old;
'''

MIGRATE = '''
ALTER TABLE records RENAME TO records_old;
CREATE TABLE records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    cover_url TEXT NOT NULL DEFAULT '',
    cover_path TEXT NOT NULL DEFAULT '',
    rating INTEGER NOT NULL DEFAULT 0,
    marked_date TEXT NOT NULL DEFAULT '',
    effective_date TEXT NOT NULL DEFAULT '',
    date_overridden INTEGER NOT NULL DEFAULT 0,
    rating_overridden INTEGER NOT NULL DEFAULT 0,
    comment_overridden INTEGER NOT NULL DEFAULT 0,
    comment TEXT NOT NULL DEFAULT '',
    intro TEXT NOT NULL DEFAULT '',
    word_count INTEGER NOT NULL DEFAULT 0,
    book_file TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'douban',
    status TEXT NOT NULL DEFAULT 'done',
    synced_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    UNIQUE(media_type, subject_id, status)
);
INSERT INTO records (
    id, media_type, subject_id, title, url, cover_url, cover_path,
    rating, marked_date, effective_date, date_overridden,
    comment, intro, status, synced_at, created_at, updated_at
)
SELECT
    id, media_type, subject_id, title, url, cover_url, cover_path,
    rating, marked_date, effective_date, date_overridden,
    comment, intro, 'done', synced_at, created_at, updated_at
FROM records_old;
DROP TABLE records_old;
'''


class Database:
    def __init__(self, path: Path = DB_PATH):
        self.path = Path(path)
        ensure_dirs()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            columns = [row[1] for row in conn.execute('PRAGMA table_info(records)')]
            if 'status' not in columns:
                conn.executescript(MIGRATE)
                columns = [row[1] for row in conn.execute('PRAGMA table_info(records)')]
            for column, definition in (
                ('word_count', "INTEGER NOT NULL DEFAULT 0"),
                ('book_file', "TEXT NOT NULL DEFAULT ''"),
                ('source', "TEXT NOT NULL DEFAULT 'douban'"),
                ('deleted_at', "TEXT NOT NULL DEFAULT ''"),
                ('deleted_by', "TEXT NOT NULL DEFAULT ''"),
                ('rating_overridden', 'INTEGER NOT NULL DEFAULT 0'),
                ('comment_overridden', 'INTEGER NOT NULL DEFAULT 0'),
            ):
                if column not in columns:
                    conn.execute(f'ALTER TABLE records ADD COLUMN {column} {definition}')
            doulist_columns = [row[1] for row in conn.execute('PRAGMA table_info(doulists)')]
            if 'relation' not in doulist_columns:
                conn.execute("ALTER TABLE doulists ADD COLUMN relation TEXT NOT NULL DEFAULT 'created'")
            if 'position' not in doulist_columns:
                conn.execute('ALTER TABLE doulists ADD COLUMN position INTEGER NOT NULL DEFAULT 0')
            if 'deleted_at' not in doulist_columns:
                conn.execute("ALTER TABLE doulists ADD COLUMN deleted_at TEXT NOT NULL DEFAULT ''")
            # iOS 侧条目带状态/时长/页数/字数/年份/上映日期，补齐列以便 Excel 双向无损。
            for table, extra in (
                ('doulist_items', (
                    ('status', "TEXT NOT NULL DEFAULT 'done'"),
                    ('duration_minutes', 'INTEGER NOT NULL DEFAULT 0'),
                    ('pages', 'INTEGER NOT NULL DEFAULT 0'),
                    ('word_count', 'INTEGER NOT NULL DEFAULT 0'),
                    ('year', "TEXT NOT NULL DEFAULT ''"),
                    ('release_date', "TEXT NOT NULL DEFAULT ''"),
                )),
                ('plan_items', (
                    ('status', "TEXT NOT NULL DEFAULT 'done'"),
                    ('release_date', "TEXT NOT NULL DEFAULT ''"),
                )),
            ):
                present = [row[1] for row in conn.execute(f'PRAGMA table_info({table})')]
                for column, definition in extra:
                    if column not in present:
                        conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
            order_marker = conn.execute(
                "SELECT value FROM settings WHERE key='doulist_order_global'"
            ).fetchone()
            if not order_marker:
                rows = conn.execute('''
                    SELECT id FROM doulists
                    ORDER BY position, relation, synced_at DESC, id
                ''').fetchall()
                for index, row in enumerate(rows):
                    conn.execute('UPDATE doulists SET position=? WHERE id=?',
                                 (index, row['id']))
                conn.execute('''
                    INSERT INTO settings(key, value) VALUES('doulist_order_global', '1')
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                ''')
            plan_columns = [row[1] for row in conn.execute('PRAGMA table_info(plan_items)')]
            if 'folder_id' not in plan_columns:
                conn.executescript(PLAN_MIGRATE)
            plan_columns = [row[1] for row in conn.execute('PRAGMA table_info(plan_items)')]
            if 'deleted_at' not in plan_columns:
                conn.execute("ALTER TABLE plan_items ADD COLUMN deleted_at TEXT NOT NULL DEFAULT ''")
            folder_columns = [row[1] for row in conn.execute('PRAGMA table_info(plan_folders)')]
            if 'deleted_at' not in folder_columns:
                conn.execute("ALTER TABLE plan_folders ADD COLUMN deleted_at TEXT NOT NULL DEFAULT ''")
            orphans = conn.execute('''
                SELECT DISTINCT media_type FROM plan_items WHERE folder_id=0
            ''').fetchall()
            for row in orphans:
                folder = conn.execute(
                    'SELECT id FROM plan_folders WHERE media_type=? AND title=?',
                    (row['media_type'], '默认计划'),
                ).fetchone()
                if folder:
                    folder_id = int(folder['id'])
                else:
                    cursor = conn.execute('''
                        INSERT INTO plan_folders (media_type, title, note, position, created_at)
                        VALUES (?, '默认计划', '', 0, ?)
                    ''', (row['media_type'], _now()))
                    folder_id = int(cursor.lastrowid or 0)
                conn.execute('UPDATE plan_items SET folder_id=? WHERE media_type=? AND folder_id=0',
                             (folder_id, row['media_type']))
            conn.executescript(INDEXES)

    def upsert_records(self, records: Iterable[dict], mode: str = 'full') -> dict:
        if mode not in ('full', 'incremental_add', 'incremental_sync'):
            raise ValueError(f'unknown sync mode: {mode}')
        now = _now()
        inserted = 0
        updated = 0
        skipped = 0
        ignored = self.ignored_subjects()
        purged = self.purged_subjects()
        with self._connect() as conn:
            for record in records:
                media_type = record['media_type']
                subject_id = str(record['subject_id'])
                status = record.get('status') or 'done'
                if (media_type, subject_id) in ignored:
                    skipped += 1
                    continue
                if (media_type, subject_id, status) in purged:
                    skipped += 1
                    continue
                existing = conn.execute(
                    'SELECT id, date_overridden, rating_overridden, comment_overridden,'
                    ' deleted_at, deleted_by, title, cover_url, cover_path FROM records'
                    ' WHERE media_type=? AND subject_id=? AND status=?',
                    (media_type, subject_id, status),
                ).fetchone()
                # 用户自选的封面（本地上传 / 其他条目链接）同步时不能被豆瓣封面顶掉。
                keep_cover = bool(existing) and is_custom_cover(
                    media_type, subject_id, existing['cover_path'] or '')
                restored = False
                if existing and existing['deleted_at']:
                    if existing['deleted_by'] != 'sync':
                        # 用户自己删除的记录，同步不再恢复
                        skipped += 1
                        continue
                    conn.execute(
                        "UPDATE records SET deleted_at='', deleted_by='', updated_at=? WHERE id=?",
                        (now, existing['id']),
                    )
                    restored = True
                if existing:
                    if mode != 'full' and not restored:
                        skipped += 1
                        # 增量同步不更新已存在的记录，但本地缺封面时补上本次下载到的封面，
                        # 否则封面文件下好了、库里路径还是空，界面永远显示不出来。
                        incoming_cover = record.get('cover_path') or ''
                        if (incoming_cover and not cover_is_valid(existing['cover_path'] or '')
                                and not is_custom_cover(media_type, subject_id,
                                                        existing['cover_path'] or '')):
                            conn.execute(
                                'UPDATE records SET cover_path=? WHERE id=?',
                                (incoming_cover, existing['id']),
                            )
                            if not existing['cover_url'] and record.get('cover_url'):
                                conn.execute(
                                    'UPDATE records SET cover_url=? WHERE id=?',
                                    (record.get('cover_url') or '', existing['id']),
                                )
                        continue
                    new_title = record.get('title', '')
                    old_title = existing['title'] or ''
                    if old_title and new_title and old_title.startswith(new_title):
                        # 保留本地更完整的标题（例如带副标题的版本）
                        new_title = old_title
                    conn.execute('''
                        UPDATE records SET
                            title=?, url=?, cover_url=?,
                            cover_path=CASE WHEN ?=1 THEN cover_path
                                            WHEN ?<>'' THEN ? ELSE cover_path END,
                            rating=CASE WHEN rating_overridden=1 THEN rating ELSE ? END,
                            marked_date=?,
                            effective_date=CASE WHEN date_overridden=1 THEN effective_date ELSE ? END,
                            comment=CASE WHEN comment_overridden=1 THEN comment ELSE ? END,
                            intro=?,
                            source=CASE WHEN source='manual' THEN source ELSE 'douban' END,
                            synced_at=?, updated_at=?
                        WHERE id=?
                    ''', (
                        new_title, record.get('url', ''), record.get('cover_url', ''),
                        int(keep_cover), record.get('cover_path', ''), record.get('cover_path', ''),
                        int(record.get('rating') or 0), record.get('marked_date', ''),
                        record.get('marked_date', ''),
                        record.get('comment', ''), record.get('intro', ''), now, now,
                        existing['id'],
                    ))
                    updated += 1
                else:
                    conn.execute('''
                        INSERT INTO records (
                            media_type, subject_id, title, url, cover_url, cover_path,
                            rating, marked_date, effective_date, date_overridden,
                            comment, intro, word_count, book_file, source, status,
                            synced_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        media_type, subject_id, record.get('title', ''), record.get('url', ''),
                        record.get('cover_url', ''), record.get('cover_path', ''),
                        int(record.get('rating') or 0), record.get('marked_date', ''),
                        record.get('marked_date', ''), record.get('comment', ''),
                        record.get('intro', ''), int(record.get('word_count') or 0),
                        record.get('book_file', ''), record.get('source', 'douban'),
                        status, now, now, now,
                    ))
                    inserted += 1
        return {'inserted': inserted, 'updated': updated, 'skipped': skipped}

    def delete_records_not_in(self, media_type: str, status: str,
                              subject_ids: Iterable[str]) -> int:
        ids = [str(subject_id) for subject_id in subject_ids]
        if not ids:
            return 0
        with self._connect() as conn:
            conn.execute('CREATE TEMP TABLE IF NOT EXISTS _sync_keep (subject_id TEXT PRIMARY KEY)')
            conn.execute('DELETE FROM _sync_keep')
            conn.executemany('INSERT OR IGNORE INTO _sync_keep(subject_id) VALUES (?)',
                             [(subject_id,) for subject_id in ids])
            now = _now()
            cursor = conn.execute('''
                UPDATE records SET deleted_at=?, deleted_by='sync', updated_at=?
                WHERE media_type=? AND status=? AND source<>'manual'
                  AND deleted_at=''
                  AND subject_id NOT IN (SELECT subject_id FROM _sync_keep)
            ''', (now, now, media_type, status))
            deleted = int(cursor.rowcount or 0)
            conn.execute('DELETE FROM _sync_keep')
        return deleted

    def add_manual_record(self, record: dict) -> int:
        now = _now()
        with self._connect() as conn:
            cursor = conn.execute('''
                INSERT INTO records (
                    media_type, subject_id, title, url, cover_url, cover_path,
                    rating, marked_date, effective_date, date_overridden,
                    comment, intro, word_count, book_file, source, status,
                    synced_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                record['media_type'], str(record['subject_id']), record.get('title', ''),
                record.get('url', ''), record.get('cover_url', ''), record.get('cover_path', ''),
                int(record.get('rating') or 0), record.get('marked_date', ''),
                record.get('effective_date') or record.get('marked_date', ''),
                int(record.get('date_overridden') or 0), record.get('comment', ''),
                record.get('intro', ''), int(record.get('word_count') or 0),
                record.get('book_file', ''), record.get('source', 'manual'),
                record.get('status') or 'done', now, now, now,
            ))
            conn.execute(
                'DELETE FROM ignored_subjects WHERE media_type=? AND subject_id=?',
                (record['media_type'], str(record['subject_id'])),
            )
            conn.execute(
                'DELETE FROM purged_records WHERE media_type=? AND subject_id=? AND status=?',
                (record['media_type'], str(record['subject_id']),
                 record.get('status') or 'done'),
            )
            return int(cursor.lastrowid or 0)

    def import_records(self, records: Iterable[dict]) -> dict:
        now = _now()
        inserted = 0
        updated = 0
        skipped = 0
        ignored = self.ignored_subjects()
        purged = self.purged_subjects()
        with self._connect() as conn:
            for record in records:
                media_type = record['media_type']
                subject_id = str(record['subject_id'])
                status = record.get('status') or 'done'
                # 换源忽略的旧条目、彻底删除的墓碑都不再从 Excel 导入回来。
                if (media_type, subject_id) in ignored:
                    skipped += 1
                    continue
                if (media_type, subject_id, status) in purged:
                    skipped += 1
                    continue
                existing = conn.execute(
                    'SELECT id, rating_overridden, comment_overridden FROM records'
                    ' WHERE media_type=? AND subject_id=? AND status=?',
                    (media_type, subject_id, status),
                ).fetchone()
                rating_overridden = record.get('rating_overridden')
                if rating_overridden is None:
                    rating_overridden = existing['rating_overridden'] if existing else 0
                comment_overridden = record.get('comment_overridden')
                if comment_overridden is None:
                    comment_overridden = existing['comment_overridden'] if existing else 0
                values = (
                    record.get('title', ''), record.get('url', ''),
                    record.get('cover_url', ''), record.get('cover_path', ''),
                    int(record.get('rating') or 0), record.get('marked_date', ''),
                    record.get('effective_date') or record.get('marked_date', ''),
                    int(record.get('date_overridden') or 0),
                    int(rating_overridden or 0),
                    int(comment_overridden or 0),
                    record.get('comment', ''),
                    record.get('intro', ''), int(record.get('word_count') or 0),
                    record.get('book_file', ''), record.get('source', 'excel'),
                )
                if existing:
                    conn.execute('''
                        UPDATE records SET
                            title=?, url=?, cover_url=?, cover_path=?, rating=?,
                            marked_date=?, effective_date=?, date_overridden=?,
                            rating_overridden=?, comment_overridden=?,
                            comment=?, intro=?, word_count=?, book_file=?, source=?,
                            deleted_at='', synced_at=?, updated_at=?
                        WHERE id=?
                    ''', (*values, now, now, existing['id']))
                    updated += 1
                else:
                    conn.execute('''
                        INSERT INTO records (
                            media_type, subject_id, title, url, cover_url, cover_path,
                            rating, marked_date, effective_date, date_overridden,
                            rating_overridden, comment_overridden,
                            comment, intro, word_count, book_file, source, status,
                            synced_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        media_type, subject_id, *values, status, now, now, now,
                    ))
                    inserted += 1
        return {'inserted': inserted, 'updated': updated, 'skipped': skipped}

    def import_deleted_records(self, records: Iterable[dict]) -> dict:
        now = _now()
        inserted = 0
        updated = 0
        skipped = 0
        with self._connect() as conn:
            for record in records:
                media_type = record['media_type']
                subject_id = str(record['subject_id'])
                status = record.get('status') or 'done'
                existing = conn.execute(
                    'SELECT id, deleted_at, rating_overridden, comment_overridden FROM records'
                    ' WHERE media_type=? AND subject_id=? AND status=?',
                    (media_type, subject_id, status),
                ).fetchone()
                if existing and not existing['deleted_at']:
                    skipped += 1
                    continue
                rating_overridden = record.get('rating_overridden')
                if rating_overridden is None:
                    rating_overridden = existing['rating_overridden'] if existing else 0
                comment_overridden = record.get('comment_overridden')
                if comment_overridden is None:
                    comment_overridden = existing['comment_overridden'] if existing else 0
                deleted_at = record.get('deleted_at') or now
                deleted_by = record.get('deleted_by') or 'user'
                values = (
                    record.get('title', ''), record.get('url', ''),
                    record.get('cover_url', ''), record.get('cover_path', ''),
                    int(record.get('rating') or 0), record.get('marked_date', ''),
                    record.get('effective_date') or record.get('marked_date', ''),
                    int(record.get('date_overridden') or 0),
                    int(rating_overridden or 0),
                    int(comment_overridden or 0),
                    record.get('comment', ''),
                    record.get('intro', ''), int(record.get('word_count') or 0),
                    record.get('book_file', ''), record.get('source', 'excel'),
                )
                if existing:
                    conn.execute('''
                        UPDATE records SET
                            title=?, url=?, cover_url=?, cover_path=?, rating=?,
                            marked_date=?, effective_date=?, date_overridden=?,
                            rating_overridden=?, comment_overridden=?,
                            comment=?, intro=?, word_count=?, book_file=?, source=?,
                            deleted_at=?, deleted_by=?, synced_at=?, updated_at=?
                        WHERE id=?
                    ''', (*values, deleted_at, deleted_by, now, now, existing['id']))
                    updated += 1
                else:
                    conn.execute('''
                        INSERT INTO records (
                            media_type, subject_id, title, url, cover_url, cover_path,
                            rating, marked_date, effective_date, date_overridden,
                            rating_overridden, comment_overridden,
                            comment, intro, word_count, book_file, source, status,
                            deleted_at, deleted_by, synced_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        media_type, subject_id, *values, status, deleted_at, deleted_by,
                        now, now, now,
                    ))
                    inserted += 1
        return {'inserted': inserted, 'updated': updated, 'skipped': skipped}

    def records_with_cover_url(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute('''
                SELECT id, media_type, subject_id, title, url, cover_url, cover_path
                FROM records
                WHERE deleted_at='' AND (cover_url<>'' OR cover_path='')
            ''').fetchall()

    def set_record_cover(self, record_id: int, cover_path: str,
                         cover_url: Optional[str] = None) -> None:
        with self._connect() as conn:
            if cover_url:
                conn.execute(
                    "UPDATE records SET cover_path=?, cover_url=CASE WHEN cover_url=''"
                    ' THEN ? ELSE cover_url END, updated_at=? WHERE id=?',
                    (cover_path, cover_url, _now(), record_id),
                )
                return
            conn.execute(
                'UPDATE records SET cover_path=?, updated_at=? WHERE id=?',
                (cover_path, _now(), record_id),
            )

    def doulist_items_with_cover_url(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute('''
                SELECT i.id, i.media_type, i.subject_id, i.title, i.url,
                       i.cover_url, i.cover_path
                FROM doulist_items i
                JOIN doulists d ON d.id=i.doulist_id AND d.deleted_at=''
                WHERE i.cover_url<>'' OR i.cover_path=''
            ''').fetchall()

    def plan_items_with_cover_url(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute('''
                SELECT id, media_type, subject_id, title, url, cover_url, cover_path
                FROM plan_items
                WHERE deleted_at='' AND (cover_url<>'' OR cover_path='')
            ''').fetchall()

    def fill_item_covers_from_records(self) -> int:
        """豆列/计划条目缺封面时，从同豆瓣条目的记录里借一份（恢复 iOS 快照后常见）。

        iOS 快照的计划条目不带封面地址；如果本机有对应记录且已有封面文件，直接复用，
        不必再走一次豆瓣补全。
        """
        updated = 0
        with self._connect() as conn:
            covers = {}
            for row in conn.execute(
                "SELECT media_type, subject_id, cover_url, cover_path FROM records"
                " WHERE deleted_at='' AND (cover_url<>'' OR cover_path<>'')"
            ):
                covers.setdefault((row['media_type'], str(row['subject_id'])),
                                  (row['cover_url'] or '', row['cover_path'] or ''))
            if not covers:
                return 0
            for table in ('doulist_items', 'plan_items'):
                rows = conn.execute(
                    f'SELECT id, media_type, subject_id, cover_url, cover_path FROM {table}'
                ).fetchall()
                for row in rows:
                    url, path = covers.get((row['media_type'], str(row['subject_id'])), ('', ''))
                    if not url and not path:
                        continue
                    new_url = row['cover_url'] or url
                    new_path = row['cover_path'] or path
                    if new_url == (row['cover_url'] or '') and new_path == (row['cover_path'] or ''):
                        continue
                    conn.execute(
                        f'UPDATE {table} SET cover_url=?, cover_path=? WHERE id=?',
                        (new_url, new_path, row['id']),
                    )
                    updated += 1
        return updated

    def set_doulist_item_cover(self, item_id: int, cover_path: str,
                               cover_url: Optional[str] = None) -> None:
        with self._connect() as conn:
            if cover_url:
                conn.execute(
                    "UPDATE doulist_items SET cover_path=?, cover_url=CASE WHEN cover_url=''"
                    ' THEN ? ELSE cover_url END WHERE id=?',
                    (cover_path, cover_url, int(item_id)),
                )
                return
            conn.execute('UPDATE doulist_items SET cover_path=? WHERE id=?',
                         (cover_path, int(item_id)))

    def set_plan_item_cover(self, item_id: int, cover_path: str,
                            cover_url: Optional[str] = None) -> None:
        with self._connect() as conn:
            if cover_url:
                conn.execute(
                    "UPDATE plan_items SET cover_path=?, cover_url=CASE WHEN cover_url=''"
                    ' THEN ? ELSE cover_url END WHERE id=?',
                    (cover_path, cover_url, int(item_id)),
                )
                return
            conn.execute('UPDATE plan_items SET cover_path=? WHERE id=?',
                         (cover_path, int(item_id)))

    def book_subjects_for_meta(self) -> list[dict]:
        """记录、豆列、计划中出现过的全部书籍（去重），用于补全页数等信息。"""
        with self._connect() as conn:
            rows = conn.execute('''
                SELECT subject_id, MAX(title) AS title FROM (
                    SELECT subject_id, title FROM records
                    WHERE media_type='book' AND deleted_at=''
                    UNION ALL
                    SELECT i.subject_id, i.title FROM doulist_items i
                    JOIN doulists d ON d.id=i.doulist_id AND d.deleted_at=''
                    WHERE i.media_type='book'
                    UNION ALL
                    SELECT subject_id, title FROM plan_items
                    WHERE media_type='book' AND deleted_at=''
                )
                GROUP BY subject_id
                ORDER BY title
            ''').fetchall()
        return [{'subject_id': str(row['subject_id']), 'title': row['title'] or ''}
                for row in rows]

    def movie_subjects_for_meta(self) -> list[dict]:
        """记录、豆列、计划中出现过的全部影视（去重），用于补全时长。"""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT subject_id, MAX(title) AS title, MAX(intro) AS intro FROM (
                    SELECT subject_id, title, intro FROM records
                    WHERE media_type='movie' AND deleted_at=''
                    UNION ALL
                    SELECT i.subject_id, i.title, i.intro FROM doulist_items i
                    JOIN doulists d ON d.id=i.doulist_id AND d.deleted_at=''
                    WHERE i.media_type='movie'
                    UNION ALL
                    SELECT subject_id, title, '' AS intro FROM plan_items
                    WHERE media_type='movie' AND deleted_at=''
                )
                GROUP BY subject_id
                ORDER BY title
            """).fetchall()
        return [{'subject_id': str(row['subject_id']), 'title': row['title'] or '',
                 'intro': row['intro'] or ''} for row in rows]

    def set_word_count(self, record_id: int, word_count: int, book_file: str = '') -> None:
        with self._connect() as conn:
            conn.execute('''
                UPDATE records SET word_count=?, book_file=?, updated_at=? WHERE id=?
            ''', (int(word_count or 0), book_file, _now(), record_id))

    def update_record_fields(self, record_id: int, rating: Optional[int] = None,
                             comment: Optional[str] = None,
                             word_count: Optional[int] = None) -> None:
        updates = ['updated_at=?']
        params: list = [_now()]
        if rating is not None:
            value = max(0, min(5, int(rating)))
            updates.append('rating=?')
            updates.append('rating_overridden=CASE WHEN rating<>? THEN 1 ELSE rating_overridden END')
            params.extend((value, value))
        if comment is not None:
            updates.append('comment=?')
            updates.append('comment_overridden=CASE WHEN comment<>? THEN 1 ELSE comment_overridden END')
            params.extend((comment, comment))
        if word_count is not None:
            updates.append('word_count=?')
            params.append(max(0, int(word_count)))
        params.append(record_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE records SET {', '.join(updates)} WHERE id=?", params)

    def list_records(self, media_type: str, month: Optional[str] = None,
                     status: str = 'done', start_date: str = '',
                     end_date: str = '') -> list[sqlite3.Row]:
        query = "SELECT * FROM records WHERE media_type=? AND status=? AND deleted_at=''"
        params: list = [media_type, status]
        if month:
            query += " AND substr(effective_date, 1, 7)=?"
            params.append(month)
        if start_date:
            query += ' AND effective_date>=?'
            params.append(start_date)
        if end_date:
            query += ' AND effective_date<=?'
            params.append(end_date)
        query += ' ORDER BY effective_date DESC, rating DESC, title'
        with self._connect() as conn:
            return conn.execute(query, params).fetchall()

    def list_all_records(self, media_type: Optional[str] = None,
                         status: Optional[str] = None) -> list[sqlite3.Row]:
        query = 'SELECT * FROM records'
        conditions = ["deleted_at=''"]
        params = []
        if media_type:
            conditions.append('media_type=?')
            params.append(media_type)
        if status:
            conditions.append('status=?')
            params.append(status)
        if conditions:
            query += ' WHERE ' + ' AND '.join(conditions)
        query += ' ORDER BY media_type, status, effective_date DESC, title'
        with self._connect() as conn:
            return conn.execute(query, params).fetchall()

    def date_bounds(self, media_type: str, status: str = 'done') -> tuple[str, str]:
        with self._connect() as conn:
            row = conn.execute('''
                SELECT MIN(effective_date) AS first, MAX(effective_date) AS last
                FROM records
                WHERE media_type=? AND status=? AND effective_date<>'' AND deleted_at=''
            ''', (media_type, status)).fetchone()
        return (row['first'] or '', row['last'] or '') if row else ('', '')

    def grouped_records(self, media_type: str, status: str = 'done') -> list[tuple[str, list[sqlite3.Row]]]:
        rows = self.list_records(media_type, status=status)
        groups: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            groups.setdefault(row['effective_date'] or '未设置', []).append(row)
        return list(groups.items())

    def get_record(self, record_id: int) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute('SELECT * FROM records WHERE id=?', (record_id,)).fetchone()

    def find_record(self, media_type: str, subject_id: str,
                    status: str = 'done') -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute('''
                SELECT * FROM records
                WHERE media_type=? AND subject_id=? AND status=? AND deleted_at=''
            ''', (media_type, str(subject_id), status)).fetchone()

    def find_record_any(self, media_type: str, subject_id: str) -> Optional[sqlite3.Row]:
        """按条目找记录，不限定状态；已看/已读优先于想看/想读。"""
        with self._connect() as conn:
            return conn.execute('''
                SELECT * FROM records
                WHERE media_type=? AND subject_id=? AND deleted_at=''
                ORDER BY CASE status WHEN 'done' THEN 0 ELSE 1 END, id
                LIMIT 1
            ''', (media_type, str(subject_id))).fetchone()

    def find_deleted_record(self, media_type: str, subject_id: str,
                            status: str = 'done') -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute('''
                SELECT * FROM records
                WHERE media_type=? AND subject_id=? AND status=? AND deleted_at<>''
            ''', (media_type, str(subject_id), status)).fetchone()

    def set_effective_date(self, record_id: int, date_str: str) -> None:
        with self._connect() as conn:
            conn.execute(
                'UPDATE records SET effective_date=?, date_overridden=1, updated_at=? WHERE id=?',
                (date_str, _now(), record_id),
            )

    def reset_effective_date(self, record_id: int) -> None:
        with self._connect() as conn:
            conn.execute('''
                UPDATE records SET effective_date=marked_date, date_overridden=0, updated_at=?
                WHERE id=?
            ''', (_now(), record_id))

    def change_record_status(self, record_id: int, status: str) -> bool:
        if status not in ('done', 'wish'):
            return False
        with self._connect() as conn:
            row = conn.execute(
                'SELECT media_type, subject_id, status FROM records WHERE id=?',
                (record_id,),
            ).fetchone()
            if not row:
                return False
            if row['status'] == status:
                return True
            duplicate = conn.execute('''
                SELECT id FROM records
                WHERE media_type=? AND subject_id=? AND status=? AND id<>?
            ''', (row['media_type'], row['subject_id'], status, record_id)).fetchone()
            if duplicate:
                return False
            conn.execute('UPDATE records SET status=?, updated_at=? WHERE id=?',
                         (status, _now(), record_id))
            return True

    def soft_delete_record(self, record_id: int) -> None:
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE records SET deleted_at=?, deleted_by='user', updated_at=? WHERE id=?",
                (now, now, record_id),
            )

    def ignored_subjects(self, media_type: Optional[str] = None) -> set[tuple[str, str]]:
        query = 'SELECT media_type, subject_id FROM ignored_subjects'
        params: list = []
        if media_type:
            query += ' WHERE media_type=?'
            params.append(media_type)
        with self._connect() as conn:
            return {(row['media_type'], row['subject_id'])
                    for row in conn.execute(query, params)}

    def list_ignored_subjects(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                'SELECT media_type, subject_id, created_at FROM ignored_subjects'
                ' ORDER BY media_type, subject_id'
            ).fetchall()

    def ignore_subject(self, media_type: str, subject_id: str,
                       created_at: str = '') -> None:
        with self._connect() as conn:
            conn.execute(
                'INSERT OR IGNORE INTO ignored_subjects(media_type, subject_id, created_at)'
                ' VALUES (?, ?, ?)',
                (media_type, str(subject_id), created_at or _now()),
            )

    def unignore_subject(self, media_type: str, subject_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                'DELETE FROM ignored_subjects WHERE media_type=? AND subject_id=?',
                (media_type, str(subject_id)),
            )

    def change_record_source(self, record_id: int, detail: dict) -> bool:
        """把记录换成另一个豆瓣条目（保留本地日期、短评与本地评分）"""
        subject_id = str(detail['subject_id'])
        now = _now()
        with self._connect() as conn:
            row = conn.execute(
                'SELECT media_type, status, rating_overridden FROM records WHERE id=?',
                (record_id,),
            ).fetchone()
            if not row:
                return False
            conflict = conn.execute(
                'SELECT id FROM records'
                ' WHERE media_type=? AND subject_id=? AND status=? AND id<>?',
                (row['media_type'], subject_id, row['status'], record_id),
            ).fetchone()
            if conflict:
                return False
            cover_path = detail.get('cover_path') or ''
            old_subject_id = conn.execute(
                'SELECT subject_id FROM records WHERE id=?', (record_id,)
            ).fetchone()['subject_id']
            conn.execute('''
                UPDATE records SET
                    subject_id=?, title=?, url=?, cover_url=?,
                    cover_path=CASE WHEN ?<>'' THEN ? ELSE cover_path END,
                    intro=?,
                    rating=CASE WHEN ?=1 THEN rating ELSE ? END,
                    source='manual',
                    synced_at=?, updated_at=?
                WHERE id=?
            ''', (
                subject_id, detail.get('title', ''), detail.get('url', ''),
                detail.get('cover_url', ''), cover_path, cover_path,
                detail.get('intro', ''), int(row['rating_overridden'] or 0),
                int(detail.get('rating') or 0), now, now, record_id,
            ))
            # 换源后旧条目不再参与同步，避免同步时又冒出一条；新条目解除忽略
            if old_subject_id and str(old_subject_id) != subject_id:
                conn.execute(
                    'INSERT OR IGNORE INTO ignored_subjects(media_type, subject_id, created_at)'
                    ' VALUES (?, ?, ?)',
                    (row['media_type'], str(old_subject_id), now),
                )
            conn.execute(
                'DELETE FROM ignored_subjects WHERE media_type=? AND subject_id=?',
                (row['media_type'], subject_id),
            )
        return True

    def clear_field_overrides(self, record_id: int) -> None:
        """允许后续同步覆盖本地评分与短评"""
        with self._connect() as conn:
            conn.execute(
                'UPDATE records SET rating_overridden=0, comment_overridden=0, updated_at=?'
                ' WHERE id=?',
                (_now(), record_id),
            )

    def restore_record(self, record_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE records SET deleted_at='', deleted_by='', updated_at=? WHERE id=?",
                (_now(), record_id),
            )

    def hard_delete_record(self, record_id: int) -> None:
        now = _now()
        with self._connect() as conn:
            row = conn.execute(
                'SELECT media_type, subject_id, status, title FROM records WHERE id=?',
                (record_id,),
            ).fetchone()
            if row is not None:
                conn.execute('''
                    INSERT OR REPLACE INTO purged_records
                        (media_type, subject_id, status, title, purged_at)
                    VALUES (?, ?, ?, ?, ?)
                ''', (row['media_type'], row['subject_id'], row['status'],
                      row['title'], now))
            conn.execute('DELETE FROM records WHERE id=?', (record_id,))

    def purged_subjects(self) -> set[tuple[str, str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                'SELECT media_type, subject_id, status FROM purged_records'
            ).fetchall()
        return {(row['media_type'], row['subject_id'], row['status']) for row in rows}

    def list_purged_records(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                'SELECT * FROM purged_records ORDER BY purged_at DESC, title'
            ).fetchall()

    def clear_sync_state(self) -> dict:
        with self._connect() as conn:
            purged = conn.execute('SELECT COUNT(*) FROM purged_records').fetchone()[0]
            ignored = conn.execute('SELECT COUNT(*) FROM ignored_subjects').fetchone()[0]
            conn.execute('DELETE FROM purged_records')
            conn.execute('DELETE FROM ignored_subjects')
        return {'purged': int(purged or 0), 'ignored': int(ignored or 0)}

    def import_purged_records(self, records: Iterable[dict]) -> dict:
        now = _now()
        saved = 0
        deleted = 0
        with self._connect() as conn:
            for record in records:
                media_type = record['media_type']
                subject_id = str(record['subject_id'])
                status = record.get('status') or 'done'
                conn.execute('''
                    INSERT OR REPLACE INTO purged_records
                        (media_type, subject_id, status, title, purged_at)
                    VALUES (?, ?, ?, ?, ?)
                ''', (media_type, subject_id, status,
                      record.get('title', ''), record.get('purged_at') or now))
                cursor = conn.execute(
                    'DELETE FROM records WHERE media_type=? AND subject_id=? AND status=?',
                    (media_type, subject_id, status),
                )
                deleted += int(cursor.rowcount or 0)
                saved += 1
        return {'saved': saved, 'deleted': deleted}

    def purge_all_recycle(self) -> dict:
        now = _now()
        counts = {'records': 0, 'doulists': 0, 'plan_folders': 0, 'plan_items': 0}
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, media_type, subject_id, status, title FROM records"
                " WHERE deleted_at<>''"
            ).fetchall()
            for row in rows:
                conn.execute('''
                    INSERT OR REPLACE INTO purged_records
                        (media_type, subject_id, status, title, purged_at)
                    VALUES (?, ?, ?, ?, ?)
                ''', (row['media_type'], row['subject_id'], row['status'],
                      row['title'], now))
                conn.execute('DELETE FROM records WHERE id=?', (row['id'],))
            counts['records'] = len(rows)

            doulists = conn.execute(
                "SELECT id FROM doulists WHERE deleted_at<>''"
            ).fetchall()
            for row in doulists:
                conn.execute('DELETE FROM doulist_items WHERE doulist_id=?', (row['id'],))
                conn.execute('DELETE FROM doulists WHERE id=?', (row['id'],))
            counts['doulists'] = len(doulists)

            folders = conn.execute(
                "SELECT id FROM plan_folders WHERE deleted_at<>''"
            ).fetchall()
            for row in folders:
                conn.execute('DELETE FROM plan_items WHERE folder_id=?', (row['id'],))
                conn.execute('DELETE FROM plan_folders WHERE id=?', (row['id'],))
            counts['plan_folders'] = len(folders)

            cursor = conn.execute("DELETE FROM plan_items WHERE deleted_at<>''")
            counts['plan_items'] = int(cursor.rowcount or 0)
        return counts

    def list_deleted_records(self, media_type: str,
                             status: Optional[str] = None) -> list[sqlite3.Row]:
        query = "SELECT * FROM records WHERE media_type=? AND deleted_at<>''"
        params: list = [media_type]
        if status:
            query += ' AND status=?'
            params.append(status)
        query += ' ORDER BY deleted_at DESC, id DESC'
        with self._connect() as conn:
            return conn.execute(query, params).fetchall()

    def list_deleted_doulists(self, media_type: Optional[str] = None) -> list[sqlite3.Row]:
        query = '''
            SELECT d.*,
                (SELECT COUNT(*) FROM doulist_items i
                 WHERE i.doulist_id=d.id) AS local_count,
                (SELECT i.cover_path FROM doulist_items i
                 WHERE i.doulist_id=d.id AND i.cover_path<>''
                 ORDER BY i.position, i.id LIMIT 1) AS cover_path
            FROM doulists d
            WHERE d.deleted_at<>''
        '''
        params: list = []
        if media_type:
            query += ' AND d.media_type=?'
            params.append(media_type)
        query += ' ORDER BY d.deleted_at DESC, d.id DESC'
        with self._connect() as conn:
            return conn.execute(query, params).fetchall()

    def soft_delete_doulist(self, doulist_id: int) -> None:
        with self._connect() as conn:
            conn.execute('UPDATE doulists SET deleted_at=? WHERE id=?', (_now(), doulist_id))

    def restore_doulist(self, doulist_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE doulists SET deleted_at='', position=-1 WHERE id=?",
                         (doulist_id,))
            rows = conn.execute('''
                SELECT id FROM doulists WHERE deleted_at='' ORDER BY position, id
            ''').fetchall()
            for position, row in enumerate(rows):
                conn.execute('UPDATE doulists SET position=? WHERE id=?',
                             (position, row['id']))

    def hard_delete_doulist(self, doulist_id: int) -> None:
        with self._connect() as conn:
            conn.execute('DELETE FROM doulist_items WHERE doulist_id=?', (doulist_id,))
            conn.execute('DELETE FROM doulists WHERE id=?', (doulist_id,))

    def months(self, media_type: str, status: str = 'done') -> list[str]:
        with self._connect() as conn:
            rows = conn.execute('''
                SELECT DISTINCT substr(effective_date, 1, 7) AS month
                FROM records
                WHERE media_type=? AND status=? AND effective_date<>'' AND deleted_at=''
                ORDER BY month DESC
            ''', (media_type, status)).fetchall()
        return [row['month'] for row in rows]

    def years(self, media_type: str, status: str = 'done') -> list[str]:
        with self._connect() as conn:
            rows = conn.execute('''
                SELECT DISTINCT substr(effective_date, 1, 4) AS year
                FROM records
                WHERE media_type=? AND status=? AND effective_date<>'' AND deleted_at=''
                ORDER BY year DESC
            ''', (media_type, status)).fetchall()
        return [row['year'] for row in rows]

    def done_subject_ids(self, media_type: str) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute('''
                SELECT subject_id FROM records
                WHERE media_type=? AND status='done' AND deleted_at=''
            ''', (media_type,)).fetchall()
        return {str(row['subject_id']) for row in rows}

    def intro_map(self, media_type: str) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute('''
                SELECT subject_id, MAX(intro) AS intro
                FROM records WHERE media_type=? AND deleted_at=''
                GROUP BY subject_id
            ''', (media_type,)).fetchall()
        return {str(row['subject_id']): row['intro'] or '' for row in rows}

    def word_count_map(self, media_type: str = 'book') -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute('''
                SELECT subject_id, MAX(word_count) AS word_count
                FROM records WHERE media_type=? AND deleted_at=''
                GROUP BY subject_id
            ''', (media_type,)).fetchall()
            result = {str(row['subject_id']): int(row['word_count'] or 0) for row in rows}
            if media_type == 'book':
                for row in conn.execute('SELECT subject_id, word_count FROM book_words'):
                    subject_id = str(row['subject_id'])
                    result[subject_id] = max(result.get(subject_id, 0),
                                             int(row['word_count'] or 0))
        return result

    def set_book_word(self, subject_id: str, word_count: int, book_file: str = '') -> None:
        with self._connect() as conn:
            conn.execute('''
                INSERT INTO book_words(subject_id, word_count, book_file, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(subject_id) DO UPDATE SET
                    word_count=excluded.word_count,
                    book_file=excluded.book_file,
                    updated_at=excluded.updated_at
            ''', (str(subject_id), int(word_count or 0), book_file, _now()))

    def book_word_entries(self) -> list[dict]:
        """所有书籍的字数（记录表 + 豆列/计划统计结果），按 subject_id 合并。"""
        with self._connect() as conn:
            rows = conn.execute('''
                SELECT subject_id, MAX(word_count) AS word_count,
                       MAX(book_file) AS book_file
                FROM (
                    SELECT subject_id, word_count, book_file FROM records
                    WHERE media_type='book' AND deleted_at='' AND word_count>0
                    UNION ALL
                    SELECT subject_id, word_count, book_file FROM book_words
                    WHERE word_count>0
                )
                GROUP BY subject_id
                ORDER BY subject_id
            ''').fetchall()
        return [{'subject_id': str(row['subject_id']),
                 'word_count': int(row['word_count'] or 0),
                 'book_file': row['book_file'] or ''} for row in rows]

    def set_word_count_for_subject(self, subject_id: str, word_count: int,
                                   book_file: str = '') -> None:
        now = _now()
        with self._connect() as conn:
            conn.execute('''
                UPDATE records SET word_count=?, updated_at=?
                WHERE media_type='book' AND subject_id=? AND deleted_at=''
            ''', (int(word_count or 0), now, str(subject_id)))
        self.set_book_word(subject_id, word_count, book_file)

    def book_page_entries(self) -> list[dict]:
        """已固化的书籍页数，随数据库快照一起同步。"""
        with self._connect() as conn:
            rows = conn.execute('''
                SELECT subject_id, pages, pages_manual FROM book_meta
                WHERE pages>0 OR pages_manual=1
                ORDER BY subject_id
            ''').fetchall()
        return [{'subject_id': str(row['subject_id']),
                 'pages': int(row['pages'] or 0),
                 'pages_manual': bool(row['pages_manual'])} for row in rows]

    def set_book_pages(self, subject_id: str, pages: Optional[int],
                       manual: bool = False) -> None:
        with self._connect() as conn:
            conn.execute('''
                INSERT INTO book_meta(subject_id, pages, pages_manual, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(subject_id) DO UPDATE SET
                    pages=excluded.pages,
                    pages_manual=excluded.pages_manual,
                    updated_at=excluded.updated_at
            ''', (str(subject_id), max(0, int(pages or 0)), 1 if manual else 0, _now()))

    def doulist_item_meta_entries(self) -> list[dict]:
        """豆列条目自带页数/时长时，可作为书籍页数、影片时长的来源。"""
        with self._connect() as conn:
            rows = conn.execute('''
                SELECT i.media_type, i.subject_id,
                       MAX(i.duration_minutes) AS duration_minutes, MAX(i.pages) AS pages
                FROM doulist_items i
                JOIN doulists d ON d.id=i.doulist_id AND d.deleted_at=''
                WHERE i.duration_minutes>0 OR i.pages>0
                GROUP BY i.media_type, i.subject_id
            ''').fetchall()
        return [{'media_type': str(row['media_type']),
                 'subject_id': str(row['subject_id']),
                 'duration_minutes': int(row['duration_minutes'] or 0),
                 'pages': int(row['pages'] or 0)} for row in rows]

    def import_book_pages(self, entries: Iterable[dict]) -> int:
        """把本机缓存里的历史页数首次写进数据库；已有记录不覆盖。"""
        now = _now()
        saved = 0
        with self._connect() as conn:
            for entry in entries:
                pages = max(0, int(entry.get('pages') or 0))
                if pages <= 0 and not entry.get('pages_manual'):
                    continue
                cursor = conn.execute('''
                    INSERT INTO book_meta(subject_id, pages, pages_manual, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(subject_id) DO NOTHING
                ''', (str(entry['subject_id']), pages,
                      1 if entry.get('pages_manual') else 0, now))
                saved += max(0, cursor.rowcount)
        return saved

    def movie_duration_entries(self) -> list[dict]:
        """已固化的影片时长，随数据库快照一起同步（与书籍页数同一套思路）。"""
        with self._connect() as conn:
            rows = conn.execute('''
                SELECT subject_id, duration_minutes, duration_manual FROM movie_meta
                WHERE duration_minutes>0 OR duration_manual=1
                ORDER BY subject_id
            ''').fetchall()
        return [{'subject_id': str(row['subject_id']),
                 'duration_minutes': int(row['duration_minutes'] or 0),
                 'duration_manual': bool(row['duration_manual'])} for row in rows]

    def set_movie_duration(self, subject_id: str, minutes: Optional[int],
                           manual: bool = False) -> None:
        minutes = max(0, int(minutes or 0))
        with self._connect() as conn:
            existing = conn.execute(
                'SELECT duration_minutes, duration_manual FROM movie_meta WHERE subject_id=?',
                (str(subject_id),),
            ).fetchone()
            # 值没变化就不写：避免每次备份/恢复都把状态指纹改掉。
            if existing and int(existing[0]) == minutes and bool(existing[1]) == bool(manual):
                return
            if manual:
                conn.execute('''
                    INSERT INTO movie_meta(subject_id, duration_minutes, duration_manual, updated_at)
                    VALUES (?, ?, 1, ?)
                    ON CONFLICT(subject_id) DO UPDATE SET
                        duration_minutes=excluded.duration_minutes,
                        duration_manual=1,
                        updated_at=excluded.updated_at
                ''', (str(subject_id), minutes, _now()))
                return
            # 抓取到的时长不覆盖手动值。
            conn.execute('''
                INSERT INTO movie_meta(subject_id, duration_minutes, duration_manual, updated_at)
                VALUES (?, ?, 0, ?)
                ON CONFLICT(subject_id) DO UPDATE SET
                    duration_minutes=CASE WHEN movie_meta.duration_manual=1
                        THEN movie_meta.duration_minutes ELSE excluded.duration_minutes END,
                    updated_at=excluded.updated_at
            ''', (str(subject_id), minutes, _now()))

    def doulist_book_entries(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute('''
                SELECT i.subject_id, MAX(i.title) AS title
                FROM doulist_items i
                JOIN doulists d ON d.id=i.doulist_id AND d.deleted_at=''
                WHERE i.media_type='book'
                GROUP BY i.subject_id
                ORDER BY title
            ''').fetchall()

    def upsert_doulist(self, media_type: str, douban_id: str, title: str, url: str,
                       item_count: int, relation: str = 'created',
                       position: Optional[int] = None, synced_at: str = '') -> int:
        now = synced_at or _now()
        with self._connect() as conn:
            row = conn.execute('SELECT id FROM doulists WHERE douban_id=?', (douban_id,)).fetchone()
            if row:
                conn.execute('''
                    UPDATE doulists SET media_type=?, title=?, url=?, item_count=?, relation=?, synced_at=?
                    WHERE id=?
                ''', (media_type, title, url, item_count, relation, now, row['id']))
                return int(row['id'])
            if position is not None:
                next_row = {'next': int(position)}
            else:
                next_row = conn.execute(
                    'SELECT COALESCE(MAX(position), -1) + 1 AS next FROM doulists'
                    ' WHERE media_type=?',
                    (media_type,),
                ).fetchone()
            cursor = conn.execute('''
                INSERT INTO doulists (
                    media_type, douban_id, title, url, item_count, relation, position, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (media_type, douban_id, title, url, item_count, relation,
                  int(next_row['next']), now))
            return int(cursor.lastrowid or 0)

    def import_deleted_doulist(self, media_type: str, douban_id: str, title: str,
                               url: str, item_count: int, relation: str = 'created',
                               position: Optional[int] = None, synced_at: str = '',
                               deleted_at: str = '') -> bool:
        now = _now()
        deleted = deleted_at or now
        synced = synced_at or now
        with self._connect() as conn:
            row = conn.execute(
                'SELECT id, deleted_at FROM doulists WHERE douban_id=?', (str(douban_id),)
            ).fetchone()
            if row and not row['deleted_at']:
                return False
            if row:
                conn.execute('''
                    UPDATE doulists SET
                        media_type=?, title=?, url=?, item_count=?, relation=?,
                        position=COALESCE(?, position), synced_at=?, deleted_at=?
                    WHERE id=?
                ''', (media_type, title, url, item_count, relation,
                      position, synced, deleted, row['id']))
                return True
            if position is None:
                next_position = int(conn.execute(
                    'SELECT COALESCE(MAX(position), -1) + 1 AS next FROM doulists'
                    ' WHERE media_type=?',
                    (media_type,),
                ).fetchone()['next'])
            else:
                next_position = int(position)
            conn.execute('''
                INSERT INTO doulists (
                    media_type, douban_id, title, url, item_count, relation,
                    position, synced_at, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (media_type, str(douban_id), title, url, item_count, relation,
                  next_position, synced, deleted))
            return True

    def _insert_doulist_items(self, conn: sqlite3.Connection, doulist_id: int,
                              items: list[dict], replace: bool = True) -> None:
        verb = 'INSERT OR REPLACE' if replace else 'INSERT OR IGNORE'
        conn.executemany(f'''
            {verb} INTO doulist_items (
                doulist_id, media_type, subject_id, title, url, cover_url, cover_path,
                rating, comment, intro, position, create_time,
                status, duration_minutes, pages, word_count, year, release_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', [(
            doulist_id, item['media_type'], item['subject_id'], item['title'],
            item.get('url', ''), item.get('cover_url', ''), item.get('cover_path', ''),
            int(item.get('rating') or 0), item.get('comment', ''), item.get('intro', ''),
            item.get('position', 0), item.get('create_time', ''),
            item.get('status') or 'done', int(item.get('duration_minutes') or 0),
            int(item.get('pages') or 0), int(item.get('word_count') or 0),
            item.get('year') or '', item.get('release_date') or '',
        ) for item in items])

    def replace_doulist_items(self, doulist_id: int, items: list[dict]) -> None:
        with self._connect() as conn:
            conn.execute('DELETE FROM doulist_items WHERE doulist_id=?', (doulist_id,))
            self._insert_doulist_items(conn, doulist_id, items)

    def merge_doulist_items(self, doulist_id: int, items: list[dict], mode: str = 'full') -> int:
        if mode == 'full':
            self.replace_doulist_items(doulist_id, items)
            return 0
        with self._connect() as conn:
            self._insert_doulist_items(conn, doulist_id, items, replace=False)
            deleted = 0
            if mode == 'incremental_sync':
                keep = {str(item['subject_id']) for item in items}
                if keep:
                    conn.execute('CREATE TEMP TABLE IF NOT EXISTS _doulist_keep (subject_id TEXT PRIMARY KEY)')
                    conn.execute('DELETE FROM _doulist_keep')
                    conn.executemany('INSERT OR IGNORE INTO _doulist_keep(subject_id) VALUES (?)',
                                     [(subject_id,) for subject_id in keep])
                    cursor = conn.execute('''
                        DELETE FROM doulist_items
                        WHERE doulist_id=? AND subject_id NOT IN (SELECT subject_id FROM _doulist_keep)
                    ''', (doulist_id,))
                    deleted = int(cursor.rowcount or 0)
                    conn.execute('DELETE FROM _doulist_keep')
            return deleted

    def get_doulist_by_douban_id(self, douban_id: str) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                'SELECT * FROM doulists WHERE douban_id=?', (str(douban_id),)
            ).fetchone()

    def list_doulists(self, media_type: Optional[str] = None,
                      relation: Optional[str] = None) -> list[sqlite3.Row]:
        query = '''
            SELECT d.*,
                (SELECT COUNT(*) FROM doulist_items i
                 WHERE i.doulist_id=d.id) AS local_count,
                (SELECT COUNT(*) FROM doulist_items i
                 JOIN records r ON r.media_type=i.media_type
                     AND r.subject_id=i.subject_id AND r.status='done'
                     AND r.deleted_at=''
                 WHERE i.doulist_id=d.id) AS read_count
            FROM doulists d
        '''
        conditions = ["d.deleted_at=''"]
        params = []
        if media_type:
            conditions.append('d.media_type=?')
            params.append(media_type)
        if relation:
            conditions.append('d.relation=?')
            params.append(relation)
        if conditions:
            query += ' WHERE ' + ' AND '.join(conditions)
        query += ' ORDER BY d.position, d.id'
        with self._connect() as conn:
            return conn.execute(query, params).fetchall()

    def move_doulist(self, doulist_id: int, offset: int,
                     visible_types: Optional[Iterable[str]] = None) -> bool:
        with self._connect() as conn:
            rows = conn.execute('''
                SELECT id, media_type FROM doulists WHERE deleted_at=''
                ORDER BY position, id
            ''').fetchall()
            all_ids = [int(item['id']) for item in rows]
            if doulist_id not in all_ids:
                return False
            visible_set = {str(value) for value in visible_types} if visible_types else None
            ids = [int(item['id']) for item in rows
                   if visible_set is None or item['media_type'] in visible_set]
            index = ids.index(doulist_id)
            target = index + offset
            if target < 0 or target >= len(ids):
                return False
            ids[index], ids[target] = ids[target], ids[index]
            visible_ids = set(ids)
            ordered = iter(ids)
            new_order = [next(ordered) if doulist_id_value in visible_ids else doulist_id_value
                         for doulist_id_value in all_ids]
            for position, doulist_id_value in enumerate(new_order):
                conn.execute('UPDATE doulists SET position=? WHERE id=?',
                             (position, doulist_id_value))
            return True

    def doulist_items(self, doulist_id: int) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute('''
                SELECT i.*,
                    CASE WHEN r.id IS NULL THEN 0 ELSE 1 END AS is_read,
                    COALESCE(NULLIF(r.effective_date, ''), r.marked_date) AS read_date,
                    r.rating AS read_rating
                FROM doulist_items i
                LEFT JOIN records r ON r.media_type=i.media_type
                    AND r.subject_id=i.subject_id AND r.status='done'
                    AND r.deleted_at=''
                WHERE i.doulist_id=?
                ORDER BY i.position, i.id
            ''', (doulist_id,)).fetchall()

    def move_doulist_item(self, item_id: int, offset: int) -> bool:
        with self._connect() as conn:
            row = conn.execute('SELECT doulist_id FROM doulist_items WHERE id=?',
                               (item_id,)).fetchone()
            if not row:
                return False
            doulist_id = int(row['doulist_id'])
            rows = conn.execute('''
                SELECT id FROM doulist_items WHERE doulist_id=? ORDER BY position, id
            ''', (doulist_id,)).fetchall()
            ids = [int(item['id']) for item in rows]
            if item_id not in ids:
                return False
            index = ids.index(item_id)
            target = index + offset
            if target < 0 or target >= len(ids):
                return False
            ids[index], ids[target] = ids[target], ids[index]
            for position, item_id_value in enumerate(ids):
                conn.execute('UPDATE doulist_items SET position=? WHERE id=?',
                             (position, item_id_value))
            return True

    def add_plan_folder(self, media_type: str, title: str, note: str = '',
                        position: Optional[int] = None) -> Optional[int]:
        title = (title or '').strip()
        if not title:
            return None
        now = _now()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM plan_folders WHERE media_type=? AND title=? AND deleted_at=''",
                (media_type, title),
            ).fetchone()
            if existing:
                return None
            if position is not None:
                row = {'next': int(position)}
            else:
                row = conn.execute(
                    "SELECT COALESCE(MAX(position), -1) + 1 AS next"
                    " FROM plan_folders WHERE media_type=? AND deleted_at=''",
                    (media_type,),
                ).fetchone()
            cursor = conn.execute('''
                INSERT INTO plan_folders (media_type, title, note, position, created_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (media_type, title, note or '', int(row['next']), now))
            return int(cursor.lastrowid or 0)

    def import_deleted_plan_folder(self, media_type: str, title: str, note: str = '',
                                   position: Optional[int] = None,
                                   deleted_at: str = '') -> Optional[int]:
        title = (title or '').strip()
        if not title:
            return None
        now = _now()
        deleted = deleted_at or now
        with self._connect() as conn:
            existing = conn.execute(
                'SELECT id, deleted_at FROM plan_folders WHERE media_type=? AND title=?',
                (media_type, title),
            ).fetchone()
            if existing:
                if existing['deleted_at']:
                    conn.execute('''
                        UPDATE plan_folders SET
                            note=?, position=COALESCE(?, position), deleted_at=?
                        WHERE id=?
                    ''', (note or '', position, deleted, existing['id']))
                return int(existing['id'])
            if position is None:
                next_position = int(conn.execute(
                    "SELECT COALESCE(MAX(position), -1) + 1 AS next"
                    " FROM plan_folders WHERE media_type=? AND deleted_at=''",
                    (media_type,),
                ).fetchone()['next'])
            else:
                next_position = int(position)
            cursor = conn.execute('''
                INSERT INTO plan_folders (
                    media_type, title, note, position, created_at, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?)
            ''', (media_type, title, note or '', next_position, now, deleted))
            return int(cursor.lastrowid or 0)

    def import_deleted_plan_item(self, item: dict, folder_id: int,
                                 deleted_at: Optional[str] = None) -> Optional[int]:
        now = _now()
        deleted = now if deleted_at is None else deleted_at
        subject_id = str(item['subject_id'])
        with self._connect() as conn:
            folder = conn.execute('SELECT media_type FROM plan_folders WHERE id=?',
                                  (folder_id,)).fetchone()
            if not folder:
                return None
            media_type = folder['media_type']
            existing = conn.execute('''
                SELECT id, deleted_at FROM plan_items
                WHERE folder_id=? AND media_type=? AND subject_id=?
            ''', (folder_id, media_type, subject_id)).fetchone()
            if existing and not existing['deleted_at']:
                return None
            position = item.get('position')
            if position is None:
                position = int(conn.execute('''
                    SELECT COALESCE(MAX(position), -1) + 1 AS next
                    FROM plan_items WHERE folder_id=? AND deleted_at=''
                ''', (folder_id,)).fetchone()['next'])
            if existing:
                conn.execute('''
                    UPDATE plan_items SET
                        title=?, url=?, cover_url=?, cover_path=?, source=?, source_id=?,
                        note=?, position=?, deleted_at=?, status=?, release_date=?
                    WHERE id=?
                ''', (
                    item.get('title', ''), item.get('url', ''), item.get('cover_url', ''),
                    item.get('cover_path', ''), item.get('source', ''),
                    str(item.get('source_id', '')), item.get('note', ''), int(position),
                    deleted, item.get('status') or 'done', item.get('release_date') or '',
                    existing['id'],
                ))
                return int(existing['id'])
            cursor = conn.execute('''
                INSERT INTO plan_items (
                    folder_id, media_type, subject_id, title, url, cover_url, cover_path,
                    source, source_id, note, position, created_at, deleted_at,
                    status, release_date
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                folder_id, media_type, subject_id, item.get('title', ''),
                item.get('url', ''), item.get('cover_url', ''), item.get('cover_path', ''),
                item.get('source', ''), str(item.get('source_id', '')), item.get('note', ''),
                int(position), now, deleted, item.get('status') or 'done',
                item.get('release_date') or '',
            ))
            return int(cursor.lastrowid or 0)

    def list_recycle_plan_items(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute('''
                SELECT i.*, f.title AS folder_title
                FROM plan_items i
                LEFT JOIN plan_folders f ON f.id=i.folder_id
                WHERE i.deleted_at<>''
                   OR (f.id IS NOT NULL AND f.deleted_at<>'')
                ORDER BY i.deleted_at DESC, i.id DESC
            ''').fetchall()

    def list_plan_folders(self, media_type: str) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute('''
                SELECT f.*,
                    (SELECT COUNT(*) FROM plan_items i
                     WHERE i.folder_id=f.id AND i.deleted_at='') AS item_count,
                    (SELECT COUNT(*) FROM plan_items i
                     JOIN records r ON r.media_type=i.media_type
                         AND r.subject_id=i.subject_id AND r.status='done'
                         AND r.deleted_at=''
                     WHERE i.folder_id=f.id AND i.deleted_at='') AS read_count,
                    (SELECT i.cover_path FROM plan_items i
                     WHERE i.folder_id=f.id AND i.cover_path<>'' AND i.deleted_at=''
                     ORDER BY i.position, i.id LIMIT 1) AS cover_path
                FROM plan_folders f
                WHERE f.media_type=? AND f.deleted_at=''
                ORDER BY f.position, f.id
            ''', (media_type,)).fetchall()

    def get_plan_folder_by_title(self, media_type: str, title: str) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute('''
                SELECT * FROM plan_folders WHERE media_type=? AND title=?
            ''', (media_type, (title or '').strip())).fetchone()

    def update_plan_folder(self, folder_id: int, title: str, note: str = '') -> bool:
        title = (title or '').strip()
        if not title:
            return False
        with self._connect() as conn:
            folder = conn.execute('SELECT media_type FROM plan_folders WHERE id=?',
                                  (folder_id,)).fetchone()
            if not folder:
                return False
            duplicate = conn.execute('''
                SELECT id FROM plan_folders
                WHERE media_type=? AND title=? AND id<>? AND deleted_at=''
            ''', (folder['media_type'], title, folder_id)).fetchone()
            if duplicate:
                return False
            conn.execute('UPDATE plan_folders SET title=?, note=? WHERE id=?',
                         (title, note or '', folder_id))
            return True

    def delete_plan_folder(self, folder_id: int) -> None:
        with self._connect() as conn:
            row = conn.execute('SELECT media_type FROM plan_folders WHERE id=?',
                               (folder_id,)).fetchone()
            if not row:
                return
            conn.execute('UPDATE plan_folders SET deleted_at=? WHERE id=?',
                         (_now(), folder_id))
            self._renumber_plan_folders(conn, row['media_type'])

    def restore_plan_folder(self, folder_id: int) -> None:
        with self._connect() as conn:
            row = conn.execute('SELECT media_type FROM plan_folders WHERE id=?',
                               (folder_id,)).fetchone()
            if not row:
                return
            conn.execute("UPDATE plan_folders SET deleted_at='', position=-1 WHERE id=?",
                         (folder_id,))
            self._renumber_plan_folders(conn, row['media_type'])

    def hard_delete_plan_folder(self, folder_id: int) -> None:
        with self._connect() as conn:
            conn.execute('DELETE FROM plan_items WHERE folder_id=?', (folder_id,))
            conn.execute('DELETE FROM plan_folders WHERE id=?', (folder_id,))

    def find_deleted_plan_folder(self, media_type: str, title: str) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM plan_folders"
                " WHERE media_type=? AND title=? AND deleted_at<>''",
                (media_type, (title or '').strip()),
            ).fetchone()

    def list_deleted_plan_folders(self, media_type: Optional[str] = None) -> list[sqlite3.Row]:
        query = '''
            SELECT f.*,
                (SELECT COUNT(*) FROM plan_items i
                 WHERE i.folder_id=f.id AND i.deleted_at='') AS item_count,
                (SELECT i.cover_path FROM plan_items i
                 WHERE i.folder_id=f.id AND i.cover_path<>'' AND i.deleted_at=''
                 ORDER BY i.position, i.id LIMIT 1) AS cover_path
            FROM plan_folders f
            WHERE f.deleted_at<>''
        '''
        params: list = []
        if media_type:
            query += ' AND f.media_type=?'
            params.append(media_type)
        query += ' ORDER BY f.deleted_at DESC, f.id DESC'
        with self._connect() as conn:
            return conn.execute(query, params).fetchall()

    def list_deleted_plan_items(self, media_type: Optional[str] = None) -> list[sqlite3.Row]:
        query = '''
            SELECT i.*, f.title AS folder_title
            FROM plan_items i
            LEFT JOIN plan_folders f ON f.id=i.folder_id
            WHERE i.deleted_at<>''
        '''
        params: list = []
        if media_type:
            query += ' AND i.media_type=?'
            params.append(media_type)
        query += ' ORDER BY i.deleted_at DESC, i.id DESC'
        with self._connect() as conn:
            return conn.execute(query, params).fetchall()

    def move_plan_folder(self, folder_id: int, offset: int) -> bool:
        with self._connect() as conn:
            row = conn.execute('SELECT media_type FROM plan_folders WHERE id=?',
                               (folder_id,)).fetchone()
            if not row:
                return False
            media_type = row['media_type']
            rows = conn.execute('''
                SELECT id FROM plan_folders WHERE media_type=? AND deleted_at=''
                ORDER BY position, id
            ''', (media_type,)).fetchall()
            ids = [int(item['id']) for item in rows]
            if folder_id not in ids:
                return False
            index = ids.index(folder_id)
            target = index + offset
            if target < 0 or target >= len(ids):
                return False
            ids[index], ids[target] = ids[target], ids[index]
            for position, folder_id_value in enumerate(ids):
                conn.execute('UPDATE plan_folders SET position=? WHERE id=?',
                             (position, folder_id_value))
            return True

    def _renumber_plan_folders(self, conn: sqlite3.Connection, media_type: str) -> None:
        rows = conn.execute('''
            SELECT id FROM plan_folders WHERE media_type=? AND deleted_at=''
            ORDER BY position, id
        ''', (media_type,)).fetchall()
        for position, row in enumerate(rows):
            conn.execute('UPDATE plan_folders SET position=? WHERE id=?', (position, row['id']))

    def _default_plan_folder(self, conn: sqlite3.Connection, media_type: str) -> int:
        folder = conn.execute(
            'SELECT id, deleted_at FROM plan_folders WHERE media_type=? AND title=?',
            (media_type, '默认计划'),
        ).fetchone()
        if folder:
            if folder['deleted_at']:
                conn.execute("UPDATE plan_folders SET deleted_at='' WHERE id=?",
                             (folder['id'],))
            return int(folder['id'])
        row = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 AS next"
            " FROM plan_folders WHERE media_type=? AND deleted_at=''",
            (media_type,),
        ).fetchone()
        cursor = conn.execute('''
            INSERT INTO plan_folders (media_type, title, note, position, created_at)
            VALUES (?, '默认计划', '', ?, ?)
        ''', (media_type, int(row['next']), _now()))
        return int(cursor.lastrowid or 0)

    def add_plan_item(self, item: dict, folder_id: Optional[int] = None,
                      position: Optional[int] = None) -> Optional[int]:
        subject_id = str(item['subject_id'])
        now = _now()
        with self._connect() as conn:
            if folder_id is None:
                folder_id = self._default_plan_folder(conn, item['media_type'])
            folder = conn.execute('SELECT media_type FROM plan_folders WHERE id=?',
                                  (folder_id,)).fetchone()
            if not folder:
                return None
            media_type = folder['media_type']
            existing = conn.execute('''
                SELECT id, deleted_at FROM plan_items
                WHERE folder_id=? AND media_type=? AND subject_id=?
            ''', (folder_id, media_type, subject_id)).fetchone()
            if existing and not existing['deleted_at']:
                return None
            if position is None:
                row = conn.execute('''
                    SELECT COALESCE(MAX(position), -1) + 1 AS next
                    FROM plan_items WHERE folder_id=? AND deleted_at=''
                ''', (folder_id,)).fetchone()
                position = int(row['next'])
            if existing:
                conn.execute('''
                    UPDATE plan_items SET
                        title=?, url=?, cover_url=?, cover_path=?, source=?, source_id=?,
                        note=?, position=?, created_at=?, deleted_at='', status=?, release_date=?
                    WHERE id=?
                ''', (
                    item.get('title', ''), item.get('url', ''), item.get('cover_url', ''),
                    item.get('cover_path', ''), item.get('source', ''),
                    str(item.get('source_id', '')), item.get('note', ''), int(position),
                    now, item.get('status') or 'done', item.get('release_date') or '',
                    existing['id'],
                ))
                return int(existing['id'])
            cursor = conn.execute('''
                INSERT INTO plan_items (
                    folder_id, media_type, subject_id, title, url, cover_url, cover_path,
                    source, source_id, note, position, created_at, status, release_date
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                folder_id, media_type, subject_id, item.get('title', ''), item.get('url', ''),
                item.get('cover_url', ''), item.get('cover_path', ''),
                item.get('source', ''), str(item.get('source_id', '')), item.get('note', ''),
                int(position), now, item.get('status') or 'done',
                item.get('release_date') or '',
            ))
            return int(cursor.lastrowid or 0)

    def list_plan_items(self, media_type: str,
                        folder_id: Optional[int] = None) -> list[sqlite3.Row]:
        base = '''
            SELECT i.*,
                CASE WHEN r.id IS NULL THEN 0 ELSE 1 END AS is_read,
                COALESCE(NULLIF(r.effective_date, ''), r.marked_date) AS read_date,
                r.rating AS read_rating
            FROM plan_items i
            JOIN plan_folders f ON f.id=i.folder_id AND f.deleted_at=''
            LEFT JOIN records r ON r.media_type=i.media_type
                AND r.subject_id=i.subject_id AND r.status='done'
                AND r.deleted_at=''
        '''
        with self._connect() as conn:
            if folder_id is None:
                return conn.execute(
                    base + " WHERE i.media_type=? AND i.deleted_at=''"
                    ' ORDER BY i.position, i.id',
                    (media_type,),
                ).fetchall()
            return conn.execute(
                base + " WHERE i.folder_id=? AND i.media_type=? AND i.deleted_at=''"
                ' ORDER BY i.position, i.id',
                (folder_id, media_type),
            ).fetchall()

    def plan_subject_ids(self, media_type: str,
                         folder_id: Optional[int] = None) -> set[str]:
        with self._connect() as conn:
            if folder_id is None:
                rows = conn.execute('''
                    SELECT i.subject_id FROM plan_items i
                    JOIN plan_folders f ON f.id=i.folder_id AND f.deleted_at=''
                    WHERE i.media_type=? AND i.deleted_at=''
                ''', (media_type,)).fetchall()
            else:
                rows = conn.execute('''
                    SELECT i.subject_id FROM plan_items i
                    JOIN plan_folders f ON f.id=i.folder_id AND f.deleted_at=''
                    WHERE i.folder_id=? AND i.media_type=? AND i.deleted_at=''
                ''', (folder_id, media_type)).fetchall()
        return {str(row['subject_id']) for row in rows}

    def plan_subject_folders(self, media_type: str) -> dict[str, list[str]]:
        with self._connect() as conn:
            rows = conn.execute('''
                SELECT i.subject_id, f.title FROM plan_items i
                JOIN plan_folders f ON f.id=i.folder_id AND f.deleted_at=''
                WHERE i.media_type=? AND i.deleted_at=''
                ORDER BY f.position, f.id
            ''', (media_type,)).fetchall()
        result: dict[str, list[str]] = {}
        for row in rows:
            result.setdefault(str(row['subject_id']), []).append(row['title'] or '未命名计划')
        return result

    def remove_plan_item(self, item_id: int) -> None:
        with self._connect() as conn:
            row = conn.execute('SELECT folder_id FROM plan_items WHERE id=?',
                               (item_id,)).fetchone()
            if not row:
                return
            conn.execute('UPDATE plan_items SET deleted_at=? WHERE id=?',
                         (_now(), item_id))
            self._renumber_plan_items(conn, int(row['folder_id']))

    def restore_plan_item(self, item_id: int) -> None:
        with self._connect() as conn:
            row = conn.execute('SELECT folder_id, media_type FROM plan_items WHERE id=?',
                               (item_id,)).fetchone()
            if not row:
                return
            folder_id = int(row['folder_id'])
            folder = conn.execute('SELECT deleted_at FROM plan_folders WHERE id=?',
                                  (folder_id,)).fetchone()
            if folder and folder['deleted_at']:
                self.restore_plan_folder(folder_id)
            conn.execute("UPDATE plan_items SET deleted_at='', position=-1 WHERE id=?",
                         (item_id,))
            self._renumber_plan_items(conn, folder_id)

    def hard_delete_plan_item(self, item_id: int) -> None:
        with self._connect() as conn:
            conn.execute('DELETE FROM plan_items WHERE id=?', (item_id,))

    def move_plan_item(self, item_id: int, offset: int) -> bool:
        with self._connect() as conn:
            row = conn.execute('SELECT folder_id FROM plan_items WHERE id=?',
                               (item_id,)).fetchone()
            if not row:
                return False
            folder_id = int(row['folder_id'])
            rows = conn.execute('''
                SELECT id FROM plan_items WHERE folder_id=? AND deleted_at=''
                ORDER BY position, id
            ''', (folder_id,)).fetchall()
            ids = [int(item['id']) for item in rows]
            if item_id not in ids:
                return False
            index = ids.index(item_id)
            target = index + offset
            if target < 0 or target >= len(ids):
                return False
            ids[index], ids[target] = ids[target], ids[index]
            for position, item_id_value in enumerate(ids):
                conn.execute('UPDATE plan_items SET position=? WHERE id=?',
                             (position, item_id_value))
            return True

    def _renumber_plan_items(self, conn: sqlite3.Connection, folder_id: int) -> None:
        rows = conn.execute('''
            SELECT id FROM plan_items WHERE folder_id=? AND deleted_at=''
            ORDER BY position, id
        ''', (folder_id,)).fetchall()
        for position, row in enumerate(rows):
            conn.execute('UPDATE plan_items SET position=? WHERE id=?', (position, row['id']))

    def delete_setting(self, key: str) -> None:
        with self._connect() as conn:
            conn.execute('DELETE FROM settings WHERE key=?', (key,))

    def get_setting(self, key: str, default: str = '') -> str:
        with self._connect() as conn:
            row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row['value'] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute('''
                INSERT INTO settings(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
            ''', (key, value))
