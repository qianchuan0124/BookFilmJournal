import hashlib
import re
from copy import copy
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from app.db import Database
from app.media_meta import book_pages, movie_stats_text

RECORD_HEADERS = [
    '类型', '状态', '豆瓣ID', '标题', '链接', '评分', '豆瓣标记日期', '本地生效日期',
    '本地已调整', '评分已调整', '短评已调整', '短评', '简介', '封面URL', '封面路径',
    '时长', '页数', '字数', '电子书文件', '来源',
]
DOULIST_HEADERS = ['类型', '豆列ID', '标题', '链接', '条目数', '关系', '顺序', '同步时间']
DOULIST_ITEM_HEADERS = [
    '豆列ID', '类型', '条目ID', '标题', '链接', '封面URL', '封面路径',
    '评分', '短评', '简介', '位置', '创建时间',
]
PLAN_FOLDER_HEADERS = ['类型', '计划名称', '备注', '顺序']
PLAN_HEADERS = ['计划名称', '顺序', '类型', '条目ID', '标题', '链接', '封面URL',
                '封面路径', '来源', '来源ID', '备注']
RECYCLE_RECORD_HEADERS = RECORD_HEADERS + ['删除时间', '删除来源']
RECYCLE_DOULIST_HEADERS = DOULIST_HEADERS + ['删除时间']
RECYCLE_PLAN_FOLDER_HEADERS = PLAN_FOLDER_HEADERS + ['删除时间']
RECYCLE_PLAN_HEADERS = PLAN_HEADERS + ['删除时间']
IGNORED_HEADERS = ['类型', '豆瓣ID', '记录时间']
PURGED_HEADERS = ['类型', '状态', '豆瓣ID', '标题', '彻底删除时间']


class ExcelError(Exception):
    pass


def _openpyxl():
    try:
        import openpyxl
    except ImportError as exc:
        raise ExcelError('未安装 openpyxl，请执行 pip install openpyxl。') from exc
    return openpyxl


def _sheet_title(media_type: str, suffix: str) -> str:
    return f"{'观影' if media_type == 'movie' else '读书'}{suffix}"


def _status_text(media_type: str, status: str) -> str:
    if status == 'wish':
        return '想看' if media_type == 'movie' else '想读'
    return '看过' if media_type == 'movie' else '读过'


def _record_row(row) -> list:
    media_type = row['media_type']
    return [
        row['media_type'], _status_text(media_type, row['status']), row['subject_id'],
        row['title'], row['url'], int(row['rating'] or 0), row['marked_date'],
        row['effective_date'], '是' if row['date_overridden'] else '否',
        '是' if row['rating_overridden'] else '否',
        '是' if row['comment_overridden'] else '否',
        row['comment'], row['intro'], row['cover_url'], row['cover_path'],
        movie_stats_text(row['intro'], row['subject_id']) if media_type == 'movie' else '',
        book_pages(row['subject_id']) or '' if media_type == 'book' else '',
        int(row['word_count'] or 0), row['book_file'], row['source'],
    ]


def _doulist_item_row(douban_id: str, item) -> list:
    return [
        douban_id, item['media_type'], item['subject_id'], item['title'],
        item['url'], item['cover_url'], item['cover_path'], item['rating'],
        item['comment'], item['intro'], item['position'], item['create_time'],
    ]


def _deleted_by_text(value: str) -> str:
    return {'sync': '豆瓣同步', 'user': '手动删除'}.get(value or '', '手动删除')


def _parse_deleted_by(value) -> str:
    return 'sync' if _text(value) == '豆瓣同步' else 'user'


def _write_sheet(ws, headers: list[str], rows: list[list]) -> None:
    ws.append(headers)
    for cell in ws[1]:
        font = copy(cell.font)
        font.bold = True
        cell.font = font
    for row in rows:
        ws.append(row)
    ws.freeze_panes = 'A2'
    for index, header in enumerate(headers, 1):
        width = max([len(str(header))] + [len(str(row[index - 1])) for row in rows if len(row) >= index])
        ws.column_dimensions[ws.cell(row=1, column=index).column_letter].width = min(max(width + 2, 10), 48)


def export_excel(db: Database, path: str) -> dict:
    openpyxl = _openpyxl()
    workbook = openpyxl.Workbook()
    default_sheet = workbook.active
    workbook.remove(default_sheet)

    records = db.list_all_records()
    for media_type in ('movie', 'book'):
        ws = workbook.create_sheet(_sheet_title(media_type, '记录'))
        rows = [_record_row(row) for row in records if row['media_type'] == media_type]
        _write_sheet(ws, RECORD_HEADERS, rows)

    ws = workbook.create_sheet('豆列')
    doulists = db.list_doulists()
    _write_sheet(ws, DOULIST_HEADERS, [[
        row['media_type'], row['douban_id'], row['title'], row['url'],
        row['item_count'], row['relation'], position, row['synced_at'],
    ] for position, row in enumerate(doulists)])

    ws = workbook.create_sheet('豆列条目')
    item_rows = []
    for doulist in doulists:
        for item in db.doulist_items(int(doulist['id'])):
            item_rows.append(_doulist_item_row(doulist['douban_id'], item))
    _write_sheet(ws, DOULIST_ITEM_HEADERS, item_rows)

    ws = workbook.create_sheet('计划文件夹')
    folder_rows = []
    for media_type in ('movie', 'book'):
        for position, folder in enumerate(db.list_plan_folders(media_type)):
            folder_rows.append([media_type, folder['title'], folder['note'], position])
    _write_sheet(ws, PLAN_FOLDER_HEADERS, folder_rows)

    for media_type in ('movie', 'book'):
        ws = workbook.create_sheet(_sheet_title(media_type, '计划'))
        rows = []
        for folder in db.list_plan_folders(media_type):
            for position, item in enumerate(db.list_plan_items(media_type, int(folder['id'])), 1):
                rows.append([
                    folder['title'], position, media_type, item['subject_id'], item['title'],
                    item['url'], item['cover_url'], item['cover_path'], item['source'],
                    item['source_id'], item['note'],
                ])
        _write_sheet(ws, PLAN_HEADERS, rows)

    ws = workbook.create_sheet('回收站记录')
    recycle_rows = []
    for media_type in ('movie', 'book'):
        for row in db.list_deleted_records(media_type):
            recycle_rows.append(_record_row(row) + [
                row['deleted_at'], _deleted_by_text(row['deleted_by']),
            ])
    _write_sheet(ws, RECYCLE_RECORD_HEADERS, recycle_rows)

    ws = workbook.create_sheet('回收站豆列')
    deleted_doulists = db.list_deleted_doulists()
    _write_sheet(ws, RECYCLE_DOULIST_HEADERS, [[
        row['media_type'], row['douban_id'], row['title'], row['url'], row['item_count'],
        row['relation'], row['position'], row['synced_at'], row['deleted_at'],
    ] for row in deleted_doulists])

    ws = workbook.create_sheet('回收站豆列条目')
    recycle_item_rows = []
    for doulist in deleted_doulists:
        for item in db.doulist_items(int(doulist['id'])):
            recycle_item_rows.append(_doulist_item_row(doulist['douban_id'], item))
    _write_sheet(ws, DOULIST_ITEM_HEADERS, recycle_item_rows)

    ws = workbook.create_sheet('回收站计划文件夹')
    recycle_folder_rows = []
    for folder in db.list_deleted_plan_folders():
        recycle_folder_rows.append([
            folder['media_type'], folder['title'], folder['note'],
            folder['position'], folder['deleted_at'],
        ])
    _write_sheet(ws, RECYCLE_PLAN_FOLDER_HEADERS, recycle_folder_rows)

    ws = workbook.create_sheet('回收站计划条目')
    recycle_plan_rows = []
    for item in db.list_recycle_plan_items():
        recycle_plan_rows.append([
            item['folder_title'] or '默认计划', item['position'], item['media_type'],
            item['subject_id'], item['title'], item['url'], item['cover_url'],
            item['cover_path'], item['source'], item['source_id'], item['note'],
            item['deleted_at'],
        ])
    _write_sheet(ws, RECYCLE_PLAN_HEADERS, recycle_plan_rows)

    ws = workbook.create_sheet('彻底删除记录')
    purged_rows = db.list_purged_records()
    _write_sheet(ws, PURGED_HEADERS, [[
        row['media_type'], _status_text(row['media_type'], row['status']),
        row['subject_id'], row['title'], row['purged_at'],
    ] for row in purged_rows])

    ws = workbook.create_sheet('忽略条目')
    ignored_rows = db.list_ignored_subjects()
    _write_sheet(ws, IGNORED_HEADERS, [[
        row['media_type'], row['subject_id'], row['created_at'],
    ] for row in ignored_rows])

    output_path = Path(path).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output_path)
    return {
        'path': str(output_path),
        'records': len(records),
        'doulists': len(doulists),
        'doulist_items': len(item_rows),
        'plan_folders': len(folder_rows),
        'plans': sum(len(db.list_plan_items(media_type)) for media_type in ('movie', 'book')),
        'recycle_records': len(recycle_rows),
        'recycle_doulists': len(deleted_doulists),
        'recycle_doulist_items': len(recycle_item_rows),
        'recycle_plan_folders': len(recycle_folder_rows),
        'recycle_plan_items': len(recycle_plan_rows),
        'ignored_subjects': len(ignored_rows),
        'purged_records': len(purged_rows),
    }


def _rows_as_dicts(ws) -> list[dict]:
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    headers = [str(value or '').strip() for value in rows[0]]
    result = []
    for values in rows[1:]:
        if not any(value not in (None, '') for value in values):
            continue
        result.append({headers[index]: values[index] for index in range(min(len(headers), len(values)))})
    return result


def _text(value) -> str:
    return '' if value is None else str(value).strip()


def _date_text(value) -> str:
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d')
    if isinstance(value, date):
        return value.strftime('%Y-%m-%d')
    text = _text(value)
    match = re.search(r'(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})', text)
    if match:
        return f'{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}'
    return text


def _int(value, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _bool(value) -> int:
    return 1 if _text(value).lower() in ('1', 'true', 'yes', 'y', '是', '本地') else 0


def _parse_status(value) -> str:
    text = _text(value).lower()
    if text in ('wish', '想看', '想读', '想听'):
        return 'wish'
    return 'done'


def _parse_media_type(value, fallback: str) -> str:
    text = _text(value).lower()
    if text in ('movie', '观影', '影视', '电影'):
        return 'movie'
    if text in ('book', '读书', '书籍', '图书'):
        return 'book'
    return fallback


def _subject_id(row: dict, media_type: str, status: str) -> str:
    subject_id = _text(row.get('豆瓣ID'))
    if subject_id:
        return subject_id
    url = _text(row.get('链接'))
    match = re.search(r'/subject/(\d+)', url)
    if match:
        return match.group(1)
    seed = f"{media_type}|{status}|{_text(row.get('标题'))}|{_text(row.get('豆瓣标记日期'))}"
    return 'local-' + hashlib.sha1(seed.encode('utf-8')).hexdigest()[:16]


def _record_from_row(row: dict, fallback_type: str) -> Optional[dict]:
    title = _text(row.get('标题'))
    if not title:
        return None
    media_type = _parse_media_type(row.get('类型'), fallback_type)
    status = _parse_status(row.get('状态'))
    marked_date = _date_text(row.get('豆瓣标记日期'))
    effective_date = _date_text(row.get('本地生效日期')) or marked_date
    return {
        'media_type': media_type,
        'status': status,
        'subject_id': _subject_id(row, media_type, status),
        'title': title,
        'url': _text(row.get('链接')),
        'rating': _int(row.get('评分')),
        'marked_date': marked_date,
        'effective_date': effective_date,
        'date_overridden': _bool(row.get('本地已调整')),
        'rating_overridden': _bool(row.get('评分已调整')) if '评分已调整' in row else None,
        'comment_overridden': _bool(row.get('短评已调整')) if '短评已调整' in row else None,
        'comment': _text(row.get('短评')),
        'intro': _text(row.get('简介')),
        'cover_url': _text(row.get('封面URL')),
        'cover_path': _text(row.get('封面路径')),
        'word_count': _int(row.get('字数')),
        'book_file': _text(row.get('电子书文件')),
        'source': _text(row.get('来源')) or 'excel',
    }


def _doulist_item_from_row(row: dict) -> Optional[dict]:
    subject_id = _text(row.get('条目ID'))
    title = _text(row.get('标题'))
    if not subject_id or not title:
        return None
    return {
        'media_type': _parse_media_type(row.get('类型'), 'movie'),
        'subject_id': subject_id,
        'title': title,
        'url': _text(row.get('链接')),
        'cover_url': _text(row.get('封面URL')),
        'cover_path': _text(row.get('封面路径')),
        'rating': _int(row.get('评分')),
        'comment': _text(row.get('短评')),
        'intro': _text(row.get('简介')),
        'position': _int(row.get('位置')),
        'create_time': _text(row.get('创建时间')),
    }


def import_excel(db: Database, path: str) -> dict:
    openpyxl = _openpyxl()
    workbook = openpyxl.load_workbook(Path(path).expanduser(), read_only=True, data_only=True)
    stats = {
        'records_inserted': 0,
        'records_updated': 0,
        'plans_added': 0,
        'plan_folders_added': 0,
        'doulists': 0,
        'doulist_items': 0,
        'recycle_records': 0,
        'recycle_doulists': 0,
        'recycle_doulist_items': 0,
        'recycle_plan_folders': 0,
        'recycle_plan_items': 0,
        'ignored_subjects': 0,
        'purged_records': 0,
        'errors': [],
    }
    try:
        record_rows = []
        for media_type in ('movie', 'book'):
            sheet_name = _sheet_title(media_type, '记录')
            if sheet_name not in workbook.sheetnames:
                continue
            for row in _rows_as_dicts(workbook[sheet_name]):
                record = _record_from_row(row, media_type)
                if record:
                    record_rows.append(record)
        if record_rows:
            result = db.import_records(record_rows)
            stats['records_inserted'] += result['inserted']
            stats['records_updated'] += result['updated']
        else:
            for sheet_name in ('观影记录', '读书记录'):
                if sheet_name not in workbook.sheetnames:
                    continue
                fallback = 'movie' if sheet_name == '观影记录' else 'book'
                for row in _rows_as_dicts(workbook[sheet_name]):
                    record = _record_from_row(row, fallback)
                    if record:
                        record_rows.append(record)
            if record_rows:
                result = db.import_records(record_rows)
                stats['records_inserted'] += result['inserted']
                stats['records_updated'] += result['updated']

        if '豆列' in workbook.sheetnames:
            for row in _rows_as_dicts(workbook['豆列']):
                douban_id = _text(row.get('豆列ID'))
                title = _text(row.get('标题'))
                if not douban_id or not title:
                    continue
                media_type = _parse_media_type(row.get('类型'), 'mixed')
                position = _int(row.get('顺序'), -1)
                db.upsert_doulist(
                    media_type, douban_id, title, _text(row.get('链接')),
                    _int(row.get('条目数')), _text(row.get('关系')) or 'created',
                    position=position if position >= 0 else None,
                    synced_at=_text(row.get('同步时间')),
                )
                stats['doulists'] += 1

        if '豆列条目' in workbook.sheetnames:
            grouped: dict[str, list[dict]] = {}
            for row in _rows_as_dicts(workbook['豆列条目']):
                douban_id = _text(row.get('豆列ID'))
                item = _doulist_item_from_row(row)
                if not douban_id or not item:
                    continue
                grouped.setdefault(douban_id, []).append(item)
            for douban_id, items in grouped.items():
                doulist = db.get_doulist_by_douban_id(douban_id)
                if not doulist:
                    stats['errors'].append(f'豆列条目找不到对应豆列：{douban_id}')
                    continue
                db.replace_doulist_items(int(doulist['id']), items)
                stats['doulist_items'] += len(items)

        if '计划文件夹' in workbook.sheetnames:
            for row in _rows_as_dicts(workbook['计划文件夹']):
                media_type = _parse_media_type(row.get('类型'), 'movie')
                title = _text(row.get('计划名称')) or '默认计划'
                if db.get_plan_folder_by_title(media_type, title):
                    continue
                position = _int(row.get('顺序'), -1)
                added = db.add_plan_folder(
                    media_type, title, _text(row.get('备注')),
                    position=position if position >= 0 else None,
                )
                if added:
                    stats['plan_folders_added'] += 1

        for media_type in ('movie', 'book'):
            sheet_name = _sheet_title(media_type, '计划')
            if sheet_name not in workbook.sheetnames:
                continue
            for row in _rows_as_dicts(workbook[sheet_name]):
                folder_title = _text(row.get('计划名称')) or '默认计划'
                folder = db.get_plan_folder_by_title(media_type, folder_title)
                folder_id = int(folder['id']) if folder else db.add_plan_folder(
                    media_type, folder_title
                )
                if not folder_id:
                    stats['errors'].append(f'无法创建计划文件夹：{folder_title}')
                    continue
                subject_id = _text(row.get('条目ID'))
                title = _text(row.get('标题'))
                if not subject_id or not title:
                    continue
                position = _int(row.get('顺序'), -1)
                added = db.add_plan_item({
                    'media_type': media_type,
                    'subject_id': subject_id,
                    'title': title,
                    'url': _text(row.get('链接')),
                    'cover_url': _text(row.get('封面URL')),
                    'cover_path': _text(row.get('封面路径')),
                    'source': _text(row.get('来源')),
                    'source_id': _text(row.get('来源ID')),
                    'note': _text(row.get('备注')),
                }, folder_id=folder_id, position=position if position >= 0 else None)
                if added:
                    stats['plans_added'] += 1

        if '回收站记录' in workbook.sheetnames:
            deleted_records = []
            for row in _rows_as_dicts(workbook['回收站记录']):
                record = _record_from_row(row, 'movie')
                if not record:
                    continue
                record['deleted_at'] = _text(row.get('删除时间'))
                record['deleted_by'] = _parse_deleted_by(row.get('删除来源'))
                deleted_records.append(record)
            if deleted_records:
                result = db.import_deleted_records(deleted_records)
                stats['recycle_records'] += result['inserted'] + result['updated']

        if '回收站豆列' in workbook.sheetnames:
            for row in _rows_as_dicts(workbook['回收站豆列']):
                douban_id = _text(row.get('豆列ID'))
                title = _text(row.get('标题'))
                if not douban_id or not title:
                    continue
                position = _int(row.get('顺序'), -1)
                added = db.import_deleted_doulist(
                    _parse_media_type(row.get('类型'), 'mixed'), douban_id, title,
                    _text(row.get('链接')), _int(row.get('条目数')),
                    _text(row.get('关系')) or 'created',
                    position=position if position >= 0 else None,
                    synced_at=_text(row.get('同步时间')),
                    deleted_at=_text(row.get('删除时间')),
                )
                if added:
                    stats['recycle_doulists'] += 1

        if '回收站豆列条目' in workbook.sheetnames:
            grouped = {}
            for row in _rows_as_dicts(workbook['回收站豆列条目']):
                douban_id = _text(row.get('豆列ID'))
                item = _doulist_item_from_row(row)
                if not douban_id or not item:
                    continue
                grouped.setdefault(douban_id, []).append(item)
            for douban_id, items in grouped.items():
                doulist = db.get_doulist_by_douban_id(douban_id)
                if not doulist or not doulist['deleted_at']:
                    continue
                db.replace_doulist_items(int(doulist['id']), items)
                stats['recycle_doulist_items'] += len(items)

        if '回收站计划文件夹' in workbook.sheetnames:
            for row in _rows_as_dicts(workbook['回收站计划文件夹']):
                media_type = _parse_media_type(row.get('类型'), 'movie')
                title = _text(row.get('计划名称')) or '默认计划'
                position = _int(row.get('顺序'), -1)
                folder_id = db.import_deleted_plan_folder(
                    media_type, title, _text(row.get('备注')),
                    position=position if position >= 0 else None,
                    deleted_at=_text(row.get('删除时间')),
                )
                if folder_id:
                    stats['recycle_plan_folders'] += 1

        if '回收站计划条目' in workbook.sheetnames:
            for row in _rows_as_dicts(workbook['回收站计划条目']):
                media_type = _parse_media_type(row.get('类型'), 'movie')
                folder_title = _text(row.get('计划名称')) or '默认计划'
                subject_id = _text(row.get('条目ID'))
                title = _text(row.get('标题'))
                if not subject_id or not title:
                    continue
                deleted_at = _text(row.get('删除时间'))
                folder = db.get_plan_folder_by_title(media_type, folder_title)
                if folder and not folder['deleted_at'] and not deleted_at:
                    continue
                if folder:
                    folder_id = int(folder['id'])
                else:
                    folder_id = db.import_deleted_plan_folder(
                        media_type, folder_title, deleted_at=deleted_at,
                    )
                if not folder_id:
                    stats['errors'].append(f'无法创建计划文件夹：{folder_title}')
                    continue
                position = _int(row.get('顺序'), -1)
                added = db.import_deleted_plan_item({
                    'media_type': media_type,
                    'subject_id': subject_id,
                    'title': title,
                    'url': _text(row.get('链接')),
                    'cover_url': _text(row.get('封面URL')),
                    'cover_path': _text(row.get('封面路径')),
                    'source': _text(row.get('来源')),
                    'source_id': _text(row.get('来源ID')),
                    'note': _text(row.get('备注')),
                    'position': position if position >= 0 else None,
                }, folder_id, deleted_at=deleted_at)
                if added:
                    stats['recycle_plan_items'] += 1

        if '彻底删除记录' in workbook.sheetnames:
            purged = []
            for row in _rows_as_dicts(workbook['彻底删除记录']):
                subject_id = _text(row.get('豆瓣ID'))
                if not subject_id:
                    continue
                purged.append({
                    'media_type': _parse_media_type(row.get('类型'), 'movie'),
                    'status': _parse_status(row.get('状态')),
                    'subject_id': subject_id,
                    'title': _text(row.get('标题')),
                    'purged_at': _text(row.get('彻底删除时间')),
                })
            if purged:
                result = db.import_purged_records(purged)
                stats['purged_records'] = result['saved']

        if '忽略条目' in workbook.sheetnames:
            for row in _rows_as_dicts(workbook['忽略条目']):
                subject_id = _text(row.get('豆瓣ID'))
                if not subject_id:
                    continue
                db.ignore_subject(
                    _parse_media_type(row.get('类型'), 'movie'), subject_id,
                    created_at=_text(row.get('记录时间')),
                )
                stats['ignored_subjects'] += 1
    finally:
        workbook.close()
    return stats


def default_export_name() -> str:
    return f"douban_trace_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
