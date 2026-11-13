import calendar as calmod
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from string import Template
from typing import Optional

from app.config import MEDIA_LABELS, REPORTS_DIR, ROOT
from app.db import Database
from app.media_meta import (
    book_category, display_title, dot_date, e, format_book_stats, format_movie_stats,
    load_book_meta, manual_movie_duration, movie_duration, parse_duration,
    parse_genres, parse_pages, parse_year, short, stars,
)
from app.poster_wall import generate_poster_wall
from app.profile import avatar_data_uri, effective_avatar, effective_name

POSTER_MIN_ITEMS = 5

WEEKDAYS = ('周一', '周二', '周三', '周四', '周五', '周六', '周日')
GENRE_COLORS = ('g0', 'g1', 'g2', 'g3', 'g4', 'g5', 'g6', 'g7')
DECADE_COLORS = {
    2020: 'd2020', 2010: 'd2010', 2000: 'd2000',
    1990: 'd1990', 1980: 'd1980', 1970: 'd1970',
}


@dataclass
class ReportItem:
    record_id: int
    subject_id: str
    title: str
    url: str
    rating: int
    date: str
    comment: str
    cover: str
    cover_url: str
    tags: list[str] = field(default_factory=list)
    year: Optional[int] = None
    duration: Optional[int] = None
    pages: Optional[int] = None
    word_count: int = 0


def _cover_src(item: ReportItem, report_dir: Path) -> str:
    if item.cover:
        full = Path(item.cover)
        if not full.is_absolute():
            full = ROOT / full
        if full.exists():
            try:
                return Path(os.path.relpath(full, report_dir)).as_posix()
            except ValueError:
                return str(full)
    return item.cover_url


def _load_items(db: Database, media_type: str, month: Optional[str] = None,
                start_date: str = '', end_date: str = '') -> list[ReportItem]:
    rows = db.list_records(media_type, month, start_date=start_date, end_date=end_date)
    meta_cache = load_book_meta() if media_type == 'book' else {}
    items = []
    for row in rows:
        intro = row['intro'] or ''
        if media_type == 'movie':
            tags = parse_genres(intro)
            year = parse_year(intro)
            duration = (manual_movie_duration(row['subject_id'])
                        or parse_duration(intro) or movie_duration(row['subject_id']))
            pages = None
            word_count = 0
        else:
            meta = meta_cache.get(row['subject_id'], {})
            tags = [book_category(row['title'])]
            year = parse_year(' '.join(meta.get('pubdate') or [])) or parse_year(intro)
            duration = None
            pages = parse_pages(meta.get('pages'))
            word_count = int(row['word_count'] or 0)
        items.append(ReportItem(
            record_id=row['id'],
            subject_id=row['subject_id'],
            title=row['title'],
            url=row['url'],
            rating=int(row['rating'] or 0),
            date=row['effective_date'] or row['marked_date'],
            comment=row['comment'] or '',
            cover=row['cover_path'] or '',
            cover_url=row['cover_url'] or '',
            tags=tags,
            year=year,
            duration=duration,
            pages=pages,
            word_count=word_count,
        ))
    return items


def _render_kpis(items: list[ReportItem], media_type: str, scope: str) -> str:
    noun = '部' if media_type == 'movie' else '本'
    rated = [item.rating for item in items if item.rating > 0]
    five = sum(1 for item in items if item.rating == 5)
    comments = sum(1 for item in items if item.comment)
    average = f'{sum(rated) / len(rated):.1f}' if rated else '—'
    active_days = len({item.date for item in items})
    active_months = len({item.date[:7] for item in items})
    tag_count = len({tag for item in items for tag in item.tags})
    durations = [item.duration for item in items if item.duration]
    pages = [item.pages for item in items if item.pages]
    words = [item.word_count for item in items if item.word_count > 0]
    total_duration = sum(durations)
    total_pages = sum(pages)
    total_words = sum(words)
    words_partial = 0 < len(words) < len(items)
    words_sub = ''

    def word_kpi_value(count: int) -> str:
        prefix = '&gt; ' if words_partial else ''
        if count >= 10000:
            return f'{prefix}{count / 10000:.1f} <small>万字</small>'
        return f'{prefix}{count:,} <small>字</small>'

    def kpi(label, value, sub=''):
        sub_html = f'<span class="kpi-sub">{sub}</span>' if sub else ''
        return (f'<div class="kpi"><span class="kpi-label">{label}</span>'
                f'<span class="kpi-value">{value}</span>{sub_html}</div>')

    type_label = '类型'
    period_label = {'month': '本期', 'year': '本年', 'range': '本范围'}.get(scope, '全部')
    kpis = [
        kpi('记录', f'{len(items)} <small>{noun}</small>', f'{period_label}记录'),
        kpi('有记录' + ('天数' if scope == 'month' else '月份'),
            f'{active_days if scope == "month" else active_months} <small>{"天" if scope == "month" else "个月"}</small>',
            f'覆盖 {active_months} 个月' if scope != 'month' else ''),
        kpi('五星', f'{five} <small>{noun}</small>',
            f'占已评分 {five / len(rated) * 100:.0f}%' if rated else ''),
        kpi('平均评分', f'{average} <small>★</small>', f'{len(rated)} 本已评分'.replace('本', noun)),
        kpi('短评', f'{comments} <small>条</small>', f'占记录 {comments / len(items) * 100:.0f}%'),
    ]
    if media_type == 'movie':
        kpis.append(kpi(
            '总片长',
            f'{total_duration // 60} <small>小时</small> {total_duration % 60} <small>分</small>'
            if durations else '—',
        ))
        kpis.append(kpi(
            '平均片长',
            f'{round(total_duration / len(durations))} <small>分钟</small>'
            if durations else '—',
        ))
    else:
        if words_partial:
            words_sub = f'已统计 {len(words)}/{len(items)} 本'
        kpis.append(kpi(
            '总页数', f'{total_pages:,} <small>页</small>' if pages else '—'
        ))
        kpis.append(kpi(
            '总字数', word_kpi_value(total_words) if total_words else '—', words_sub
        ))
    kpis.append(kpi(type_label, f'{tag_count} <small>类</small>', ''))
    return ''.join(kpis)


def _render_top5(items: list[ReportItem], media_type: str, report_dir: Path) -> str:
    rows = []
    for rank, item in enumerate(items[:5], 1):
        meta_parts = [dot_date(item.date)]
        if media_type == 'book':
            meta_parts.append(format_book_stats(item.pages, item.word_count))
        elif media_type == 'movie':
            meta_parts.append(format_movie_stats(item.duration))
        if item.comment:
            meta_parts.append(short(item.comment, 42))
        meta = ' · '.join(meta_parts)
        cover = _cover_src(item, report_dir)
        rows.append(f'''        <div class="top-row">
          <span class="rank">{rank:02d}</span>
          <img class="top-thumb" src="{e(cover)}" alt="{e(item.title)}" loading="lazy">
          <div class="top-main">
            <a class="top-title" href="{e(item.url)}" target="_blank" rel="noopener" title="{e(item.title)}">{e(display_title(item.title))}</a>
            <span class="top-meta">{e(meta)}</span>
          </div>
          <span class="top-stars">{stars(item.rating)}</span>
        </div>''')
    return '<div class="top-list">\n' + '\n'.join(rows) + '\n    </div>'


def _render_featured(item: ReportItem, media_type: str, report_dir: Path) -> str:
    cover = _cover_src(item, report_dir)
    tag = '5 STAR' if item.rating == 5 else (f'{item.rating} STAR' if item.rating else 'UNRATED')
    text = item.comment or ''
    comment = f'<p class="featured-comment">{e(short(text, 140))}</p>' if text else ''
    return f'''      <div class="featured-card">
        <span class="featured-tag">{tag} · 本期最佳</span>
        <a href="{e(item.url)}" target="_blank" rel="noopener">
          <img class="featured-poster" src="{e(cover)}" alt="{e(item.title)}">
        </a>
        <h3 class="featured-title" title="{e(item.title)}">{e(display_title(item.title))}</h3>
        <div class="featured-meta">{stars(item.rating)} <span>{dot_date(item.date)}</span>{f' <span>{e(format_book_stats(item.pages, item.word_count))}</span>' if media_type == 'book' else (f' <span>{e(format_movie_stats(item.duration))}</span>' if media_type == 'movie' else '')}</div>
        {comment}
      </div>'''


def _render_highlights(items: list[ReportItem], scope: str, media_type: str, report_dir: Path) -> str:
    featured = items[0]

    def distinct(candidates: list[ReportItem], used: set[int]) -> ReportItem:
        for candidate in candidates:
            if candidate.record_id not in used:
                return candidate
        return candidates[0]

    first = distinct(sorted(items, key=lambda x: (x.date, -x.rating)), {featured.record_id})
    last = distinct(sorted(items, key=lambda x: (x.date, x.rating), reverse=True), {featured.record_id})
    comments = [item for item in sorted(items, key=lambda x: len(x.comment), reverse=True) if item.comment]
    longest = distinct(comments, {featured.record_id}) if comments else featured
    if scope == 'month':
        labels = ('本期首看', '本期收官', '最长短评')
    elif scope == 'year':
        labels = ('年度首看', '年度收官', '最长短评')
    else:
        labels = ('首次记录', '最近记录', '最长短评')
    cards = []
    for label, item in zip(labels, (first, last, longest)):
        cover = _cover_src(item, report_dir)
        text = item.comment or ''
        meta_parts = [dot_date(item.date)]
        if media_type == 'book':
            meta_parts.append(format_book_stats(item.pages, item.word_count))
        elif media_type == 'movie':
            meta_parts.append(format_movie_stats(item.duration))
        meta_parts.append(stars(item.rating))
        meta = ' · '.join(meta_parts)
        cards.append(f'''      <article class="highlight">
        <img src="{e(cover)}" alt="{e(item.title)}" loading="lazy">
        <div class="highlight-main">
          <span class="highlight-kind">{label}</span>
          <h4 title="{e(item.title)}">{e(display_title(item.title))}</h4>
          <span class="highlight-meta">{meta}</span>
          <p>{e(short(text, 100))}</p>
        </div>
      </article>''')
    return '\n'.join(cards)


def _render_calendar_month(items: list[ReportItem], month: str, report_dir: Path) -> str:
    year, mon = map(int, month.split('-'))
    first_weekday, days_in_month = calmod.monthrange(year, mon)
    by_day: dict[int, list[ReportItem]] = defaultdict(list)
    for item in items:
        by_day[int(item.date[8:10])].append(item)
    for day_items in by_day.values():
        day_items.sort(key=lambda x: (-x.rating, x.date))
    busiest_day = max(by_day, key=lambda d: len(by_day[d])) if by_day else 0
    busiest_count = len(by_day[busiest_day]) if by_day else 0
    weekend_count = sum(len(by_day[d]) for d in by_day if (first_weekday + d - 1) % 7 >= 5)
    cells = ['      <div class="cal-cell is-blank"></div>' for _ in range(first_weekday)]
    for day in range(1, days_in_month + 1):
        day_items = by_day.get(day, [])
        weekend = (first_weekday + day - 1) % 7 >= 5
        classes = ['cal-cell', 'has-items' if day_items else 'is-quiet']
        if weekend:
            classes.append('is-weekend')
        title_attr = f' title="{e(" / ".join(item.title for item in day_items))}"' if day_items else ''
        if day_items:
            images = ''.join(
                f'<img src="{e(_cover_src(item, report_dir))}" alt="{e(item.title)}" loading="lazy">'
                for item in day_items[:6]
            )
            inner = (f'        <div class="cal-strip" style="--n:{min(len(day_items), 6)}">{images}</div>\n'
                     f'        <span class="cal-count">{len(day_items)} 条</span>')
        else:
            inner = ''
        cells.append(f'''      <div class="{' '.join(classes)}"{title_attr}>
        <span class="cal-day">{day}</span>
{inner}
      </div>''')
    while len(cells) % 7:
        cells.append('      <div class="cal-cell is-blank"></div>')
    week_head = ''.join(f'<span>{label}</span>' for label in WEEKDAYS)
    return f'''    <div class="calendar-card">
      <div class="cal-summary">
        <span><b>{len(by_day)}</b> 天有记录</span>
        <span><b>{busiest_count}</b> 条单日最多 · {mon} 月 {busiest_day} 日</span>
        <span class="accent"><b>{round(weekend_count / len(items) * 100) if items else 0}%</b> 在周末</span>
      </div>
      <div class="cal-weekhead">{week_head}</div>
      <div class="cal-grid">
{chr(10).join(cells)}
      </div>
    </div>'''


def _render_tags(items: list[ReportItem], media_type: str) -> str:
    counts = Counter(tag for item in items for tag in item.tags)
    if not counts:
        return '<p class="panel-empty">条目信息中没有可提取的类型。</p>'
    tags = ''.join(
        f'<span class="genre-tag {GENRE_COLORS[index % len(GENRE_COLORS)]}">{e(tag)}<sup>{count}</sup></span>'
        for index, (tag, count) in enumerate(counts.most_common(8))
    )
    note = ('一部影片可属于多个类型' if media_type == 'movie'
            else '每本书取一个主要类型')
    return f'<div class="genre-tags">{tags}</div><p class="prefs-note">{note}</p>'


def _render_decades(items: list[ReportItem], media_type: str) -> str:
    years = [item.year for item in items if item.year]
    if not years:
        return '<p class="panel-empty">条目信息中没有可提取的年代。</p>'
    counts = Counter(year // 10 * 10 for year in years)
    total = len(years)
    decades = sorted(counts)
    segments = ''
    for decade in decades:
        share = counts[decade] / total * 100
        label = f'<em>{counts[decade]}</em>' if share >= 12 else ''
        segments += (f'<span class="decade-seg {DECADE_COLORS.get(decade, "dother")}" '
                     f'style="width:{share:.2f}%">{label}</span>')
    legend = ''.join(
        f'<span class="decade-item"><i class="decade-dot {DECADE_COLORS.get(decade, "dother")}"></i>'
        f'{decade}<b>{counts[decade]}</b></span>'
        for decade in decades
    )
    average = round(sum(years) / len(years))
    note = (f'{min(years)} — {max(years)} · 平均 {average} 年 · '
            f'{"按影片上映年份" if media_type == "movie" else "按书籍出版年份"}统计')
    return f'<div class="decade-bar">{segments}</div><div class="decade-legend">{legend}</div><p class="prefs-note">{note}</p>'


def _render_quotes(items: list[ReportItem]) -> str:
    quotes = sorted([item for item in items if item.comment], key=lambda x: len(x.comment), reverse=True)[:6]
    if not quotes:
        return '<p class="panel-empty">还没有短评。</p>'
    cards = ''.join(
        f'''      <article class="quote-card">
        <span class="quote-mark">“</span>
        <p class="quote-text" title="{e(item.comment)}">{e(item.comment)}</p>
        <div class="quote-foot"><span class="quote-source" title="{e(item.title)}">{e(display_title(item.title))}</span><span class="quote-date">{dot_date(item.date)}</span><span class="quote-stars">{stars(item.rating)}</span></div>
      </article>'''
        for item in quotes
    )
    return cards


def _render_calendar_year(items: list[ReportItem], year: int, report_dir: Path) -> str:
    month_items: dict[int, list[ReportItem]] = defaultdict(list)
    for item in items:
        if item.date[:4] != str(year):
            continue
        try:
            month_items[int(item.date[5:7])].append(item)
        except (TypeError, ValueError):
            continue
    total = sum(len(values) for values in month_items.values())
    month_blocks = []
    for month in range(1, 13):
        day_items: dict[int, list[ReportItem]] = defaultdict(list)
        for item in month_items.get(month, []):
            try:
                day_items[int(item.date[8:10])].append(item)
            except (TypeError, ValueError):
                continue
        for values in day_items.values():
            values.sort(key=lambda x: (-x.rating, x.date))
        first_weekday, days_in_month = calmod.monthrange(year, month)
        cells = ['<div class="mini-day is-blank"></div>' for _ in range(first_weekday)]
        for day in range(1, days_in_month + 1):
            values = day_items.get(day, [])
            weekend = (first_weekday + day - 1) % 7 >= 5
            classes = ['mini-day']
            if weekend:
                classes.append('is-weekend')
            if values:
                classes.append('has-items')
                cover = _cover_src(values[0], report_dir)
                title_attr = e(' / '.join(item.title for item in values))
                image = f'<img src="{e(cover)}" alt="{e(values[0].title)}" loading="lazy">' if cover else ''
                inner = (f'{image}<span class="mini-num">{day}</span>'
                         f'<span class="mini-count">{len(values)}</span>')
                cells.append(f'<div class="{" ".join(classes)}" title="{title_attr}">{inner}</div>')
            else:
                cells.append(f'<div class="{" ".join(classes)} is-quiet"><span class="mini-num">{day}</span></div>')
        while len(cells) % 7:
            cells.append('<div class="mini-day is-blank"></div>')
        month_blocks.append(f'''      <div class="mini-month">
        <div class="mini-month-head"><b>{month} 月</b><span>{len(month_items.get(month, []))} 条</span></div>
        <div class="mini-weekhead">{''.join(f'<span>{label}</span>' for label in '一二三四五六日')}</div>
        <div class="mini-grid">{''.join(cells)}</div>
      </div>''')
    return f'''    <div class="year-calendar-block">
      <div class="year-calendar-head"><b>{year}</b> 年<span>{total} 条记录</span></div>
      <div class="year-months">
{chr(10).join(month_blocks)}
      </div>
    </div>'''


def _render_calendar_years(items: list[ReportItem], noun: str, report_dir: Path) -> str:
    years = sorted({item.date[:4] for item in items if item.date})
    header = ''.join(f'<span>{month}月</span>' for month in range(1, 13))
    rows = []
    for year in years:
        year_items = [item for item in items if item.date[:4] == year]
        by_month: dict[int, list[ReportItem]] = defaultdict(list)
        for item in year_items:
            try:
                by_month[int(item.date[5:7])].append(item)
            except (TypeError, ValueError):
                continue
        cells = []
        for month in range(1, 13):
            values = sorted(by_month.get(month, []), key=lambda x: (-x.rating, x.date))
            classes = ['year-month']
            title_attr = ''
            if not values:
                classes.append('is-quiet')
                inner = f'<span class="ym-month">{month}月</span><span class="ym-count">—</span>'
            else:
                titles = e(' / '.join(item.title for item in values))
                title_attr = f' title="{titles}"'
                images = ''.join(
                    f'<img src="{e(_cover_src(item, report_dir))}" alt="{e(item.title)}" loading="lazy">'
                    for item in values[:4] if _cover_src(item, report_dir)
                )
                inner = (f'<span class="ym-month">{month}月</span>'
                         f'<span class="ym-count">{len(values)}</span>'
                         f'<div class="ym-thumbs">{images}</div>')
                classes.append('has-items')
            cells.append(f'<div class="{" ".join(classes)}"{title_attr}>{inner}</div>')
        rows.append(f'''      <div class="year-row">
        <div class="year-label"><b>{year}</b><span>{len(year_items)} {noun}</span></div>
        {''.join(cells)}
      </div>''')
    return f'''    <div class="years-calendar">
      <div class="years-head"><span>年份</span>{header}</div>
{chr(10).join(rows)}
    </div>'''


def _calendar_mode(scope: str, items: list[ReportItem]) -> str:
    if scope == 'month':
        return 'month'
    if scope == 'year':
        return 'year'
    years = sorted({item.date[:4] for item in items if item.date})
    if not years:
        return 'year'
    try:
        span = int(years[-1]) - int(years[0]) + 1
    except ValueError:
        span = len(years)
    return 'years' if span > 2 else 'year'


_LIGHT_THEME_CSS = '''
.theme-light {
  --bg:#f4f1e8; --panel:#ffffff; --panel2:#f7f4ec; --line:rgba(24,28,38,.12);
  --line-soft:rgba(24,28,38,.07); --text:#242832; --muted:#6d7482; --dim:#9aa1ad;
  --gold:#b5822c; --red:#c15a4c; --teal:#2f8c7c; --blue:#4a6fb0; --purple:#7a5cae;
}
.theme-light body {
  background:
    radial-gradient(900px 420px at 8% -6%, rgba(181,130,44,.12) 0%, transparent 55%),
    radial-gradient(760px 380px at 92% -4%, rgba(193,90,76,.10) 0%, transparent 55%),
    radial-gradient(1000px 600px at 50% 120%, rgba(47,140,124,.08) 0%, transparent 60%),
    var(--bg);
}
.theme-light .avatar {
  background:linear-gradient(145deg, rgba(181,130,44,.20), rgba(193,90,76,.14));
  border-color:rgba(181,130,44,.45);
}
.theme-light .featured-card {
  background:linear-gradient(180deg, rgba(181,130,44,.10), rgba(255,255,255,.94) 46%);
  border-color:rgba(181,130,44,.35);
}
.theme-light .featured-tag { background:rgba(255,255,255,.86); border-color:rgba(181,130,44,.55); }
.theme-light .poster { background:rgba(24,28,38,.05); }
.theme-light .poster-rating {
  background:rgba(255,255,255,.86); color:var(--gold); border-color:rgba(181,130,44,.35);
}
.theme-light .poster-caption {
  background:linear-gradient(transparent, rgba(255,255,255,.96)); color:#242832;
}
.theme-light .poster-caption small { color:var(--muted); }
.theme-light .top-thumb, .theme-light .highlight img { background:rgba(24,28,38,.05); }
.theme-light .featured-poster { background:rgba(24,28,38,.04); }
.theme-light .cal-cell { background:rgba(24,28,38,.02); }
.theme-light .cal-cell.is-weekend { background:rgba(193,90,76,.05); border-color:rgba(193,90,76,.16); }
.theme-light .cal-day { color:#242832; text-shadow:0 1px 4px rgba(255,255,255,.9); }
.theme-light .cal-cell.has-items .cal-day { background:rgba(255,255,255,.74); }
.theme-light .cal-cell.has-items::after {
  background:linear-gradient(180deg, rgba(255,255,255,.72), transparent 42%, transparent 68%, rgba(255,255,255,.32));
}
.theme-light .cal-count { background:rgba(255,255,255,.88); color:#3b4048; }
.theme-light .mini-day { background:rgba(24,28,38,.03); }
.theme-light .mini-day.is-weekend { background:rgba(193,90,76,.07); }
.theme-light .mini-num { color:#242832; text-shadow:0 1px 3px rgba(255,255,255,.9); }
.theme-light .mini-count { background:rgba(255,255,255,.9); color:var(--gold); }
.theme-light .quote-text { color:#454b56; }
.theme-light .decade-seg em { color:rgba(255,255,255,.95); }
.theme-light .year-month { background:rgba(24,28,38,.02); }
.theme-light .panel-empty { color:var(--dim); }
'''


_HTML_TEMPLATE = Template('''<!DOCTYPE html>
<html lang="zh-CN" class="theme-$theme">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$doc_title</title>
<style>
:root {
  --bg:#080b11; --panel:#121722; --panel2:#151b27; --line:rgba(255,255,255,.08);
  --line-soft:rgba(255,255,255,.04); --text:#eae7de; --muted:#8d95a5; --dim:#5d6474;
  --gold:#e5b45f; --red:#df6053; --teal:#55b7a5; --blue:#5f92d8; --purple:#9d7ed8;
}
* { box-sizing:border-box; }
body {
  margin:0; color:var(--text); line-height:1.55;
  background:
    radial-gradient(900px 420px at 8% -6%, rgba(229,180,95,.10) 0%, transparent 55%),
    radial-gradient(760px 380px at 92% -4%, rgba(223,96,83,.09) 0%, transparent 55%),
    radial-gradient(1000px 600px at 50% 120%, rgba(85,183,165,.06) 0%, transparent 60%),
    var(--bg);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
}
a { color:inherit; text-decoration:none; }
img { display:block; }
.wrap { max-width:1180px; margin:0 auto; padding:0 24px 64px; }
.hero { display:flex; justify-content:space-between; align-items:flex-end; gap:24px; padding:44px 0 26px; border-bottom:1px solid var(--line); }
.brand { display:flex; align-items:center; gap:14px; }
.avatar { width:48px; height:48px; border-radius:50%; display:flex; align-items:center; justify-content:center; overflow:hidden; font-family:"Songti SC",Georgia,serif; font-size:22px; color:var(--gold); background:linear-gradient(145deg, rgba(229,180,95,.22), rgba(223,96,83,.16)); border:1px solid rgba(229,180,95,.45); }
.avatar img { width:100%; height:100%; object-fit:cover; }
.brand-name { font-size:16px; font-weight:600; }
.brand-line { font-size:10px; letter-spacing:.22em; color:var(--gold); text-transform:uppercase; }
.period-block { text-align:right; }
.big-period { line-height:.8; letter-spacing:-.045em; font-weight:800; font-size:clamp(64px,8vw,104px); color:var(--gold); font-variant-numeric:tabular-nums; }
.period-sub { margin:10px 0 0; color:var(--muted); font-size:12px; }
.kpis { display:grid; grid-template-columns:repeat(4,1fr); border-bottom:1px solid var(--line); }
.kpi { padding:18px 22px 20px; border-left:1px solid var(--line); }
.kpi:nth-child(4n+1) { border-left:none; }
.kpi:nth-child(n+5) { border-top:1px solid var(--line); }
.kpi-label { display:block; font-size:10px; letter-spacing:.16em; color:var(--dim); text-transform:uppercase; }
.kpi-value { display:block; margin-top:6px; font-size:23px; font-weight:700; font-variant-numeric:tabular-nums; }
.kpi-value small { font-size:11px; color:var(--muted); font-weight:400; margin-left:3px; }
.kpi-sub { display:block; margin-top:3px; font-size:11px; color:var(--dim); }
.feature { display:grid; grid-template-columns:300px 1fr; gap:20px; margin-top:28px; align-items:stretch; }
.top-panel { display:flex; flex-direction:column; background:var(--panel); border:1px solid var(--line); border-radius:16px; padding:18px 20px; }
.top-list { flex:1; display:flex; flex-direction:column; justify-content:space-evenly; min-height:0; }
.panel-head { display:flex; align-items:baseline; gap:10px; margin-bottom:6px; }
.panel-head h3 { margin:0; font-size:15px; }
.panel-head .en { font-size:10px; letter-spacing:.2em; color:var(--dim); text-transform:uppercase; }
.featured-card { position:relative; display:flex; flex-direction:column; gap:9px; padding:15px; border:1px solid rgba(229,180,95,.32); border-radius:16px; background:linear-gradient(180deg, rgba(229,180,95,.08), rgba(18,23,34,.9) 46%); }
.featured-tag { position:absolute; top:25px; left:25px; z-index:2; font-size:10px; letter-spacing:.14em; color:var(--gold); background:rgba(8,11,17,.82); border:1px solid rgba(229,180,95,.6); border-radius:4px; padding:3px 7px; text-transform:uppercase; }
.featured-poster { width:100%; aspect-ratio:2/3; object-fit:contain; background:rgba(255,255,255,.035); border-radius:10px; }
.featured-title { margin:2px 0 0; font-size:16px; font-weight:650; line-height:1.4; }
.featured-meta { display:flex; gap:8px; flex-wrap:wrap; align-items:center; font-size:11px; color:var(--muted); }
.featured-comment { margin:3px 0 0; font-size:11.5px; color:var(--muted); line-height:1.65; display:-webkit-box; -webkit-line-clamp:4; -webkit-box-orient:vertical; overflow:hidden; }
.top-row { display:grid; grid-template-columns:34px 38px minmax(0,1fr) auto; gap:12px; align-items:center; padding:11px 0; border-top:1px solid var(--line-soft); }
.top-row:first-child { border-top:none; }
.rank { font-size:19px; font-weight:800; color:var(--gold); font-variant-numeric:tabular-nums; }
.top-thumb { width:38px; height:54px; object-fit:contain; background:rgba(255,255,255,.035); border-radius:5px; }
.top-main { min-width:0; display:flex; flex-direction:column; gap:3px; }
.top-title { font-size:13px; font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.top-title:hover { color:var(--gold); }
.top-meta { font-size:11px; color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.top-stars { font-size:12px; white-space:nowrap; }
.highlights { display:grid; grid-template-columns:repeat(3,1fr); gap:14px; margin-top:16px; }
.highlight { display:grid; grid-template-columns:74px minmax(0,1fr); gap:12px; padding:14px; background:var(--panel); border:1px solid var(--line); border-radius:14px; }
.highlight img { width:74px; height:106px; object-fit:contain; background:rgba(255,255,255,.035); border-radius:7px; }
.highlight-main { min-width:0; display:flex; flex-direction:column; gap:4px; }
.highlight-kind { font-size:9.5px; letter-spacing:.15em; color:var(--gold); text-transform:uppercase; }
.highlight h4 { margin:1px 0; font-size:13px; line-height:1.35; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.highlight-meta { font-size:10.5px; color:var(--muted); }
.highlight p { margin:3px 0 0; font-size:11px; line-height:1.55; color:var(--muted); display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden; }
.section { margin-top:34px; }
.section-head { display:flex; align-items:baseline; gap:10px; margin-bottom:14px; }
.section-head::before { content:""; width:7px; height:7px; border-radius:2px; background:var(--gold); display:inline-block; }
.section-head h3 { margin:0; font-size:19px; font-weight:700; }
.section-head .en { font-size:10px; letter-spacing:.2em; color:var(--dim); text-transform:uppercase; }
.section-head .count { margin-left:auto; font-size:11px; color:var(--muted); }
.poster-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(78px,1fr)); gap:10px; }
.poster-grid.is-small { grid-template-columns:repeat(auto-fit,minmax(120px,150px)); justify-content:center; gap:14px; }
.poster-grid.is-large { grid-template-columns:repeat(auto-fill,minmax(78px,1fr)); }
.poster-wall-image-wrap { width:100%; overflow:hidden; background:var(--panel); border:1px solid var(--line); border-radius:16px; }
.poster-wall-image { display:block; width:100%; height:auto; }
.poster { position:relative; border-radius:8px; overflow:hidden; border:1px solid var(--line); background:rgba(255,255,255,.035); }
.poster img { width:100%; aspect-ratio:2/3; object-fit:contain; transition:transform .3s ease; }
.poster:hover img { transform:scale(1.04); }
.poster-rating { position:absolute; top:5px; right:5px; font-size:9px; padding:1px 5px; border-radius:5px; background:rgba(8,11,17,.78); color:var(--gold); border:1px solid rgba(229,180,95,.3); }
.poster-caption { position:absolute; inset:auto 0 0; padding:22px 6px 6px; font-size:9.5px; line-height:1.35; background:linear-gradient(transparent, rgba(5,7,11,.94)); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.poster-caption small { display:block; color:var(--muted); font-size:8.5px; }
.calendar-card { background:var(--panel); border:1px solid var(--line); border-radius:16px; padding:18px; }
.cal-summary { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px; font-size:10.5px; color:var(--muted); }
.cal-summary span { display:inline-flex; align-items:baseline; gap:4px; padding:4px 10px; border:1px solid var(--line); border-radius:999px; background:var(--panel2); }
.cal-summary b { color:var(--gold); font-weight:600; } .cal-summary span.accent { border-color:rgba(223,96,83,.35); color:#e2a09a; } .cal-summary span.accent b { color:var(--red); }
.cal-weekhead { display:grid; grid-template-columns:repeat(7,1fr); gap:8px; margin-bottom:8px; font-size:10px; color:var(--dim); text-align:center; }
.cal-grid { display:grid; grid-template-columns:repeat(7,1fr); gap:8px; }
.cal-cell { position:relative; min-height:68px; overflow:hidden; border:1px solid var(--line); border-radius:9px; background:rgba(255,255,255,.015); }
.cal-cell.is-weekend { background:rgba(223,96,83,.045); border-color:rgba(223,96,83,.14); }
.cal-cell.is-blank { border-color:transparent; background:transparent; }
.cal-cell.is-quiet { padding:6px; }
.cal-cell.is-quiet .cal-day { position:static; color:var(--dim); text-shadow:none; }
.cal-day { position:absolute; top:5px; left:7px; z-index:3; font-size:10px; color:#e4e1d8; text-shadow:0 1px 4px rgba(0,0,0,.85); }
.cal-cell.has-items .cal-day { padding:1px 5px; border-radius:4px; background:rgba(5,7,11,.62); }
.cal-strip { position:absolute; inset:0; display:grid; grid-template-columns:repeat(var(--n),1fr); gap:1px; }
.cal-strip img { width:100%; height:100%; object-fit:cover; opacity:.92; }
.cal-cell.has-items::after { content:""; position:absolute; inset:0; z-index:1; pointer-events:none; background:linear-gradient(180deg, rgba(5,7,11,.6), transparent 42%, transparent 68%, rgba(5,7,11,.3)); }
.cal-count { position:absolute; right:5px; top:5px; z-index:3; font-size:9px; padding:1px 5px; border-radius:4px; background:rgba(5,7,11,.7); color:var(--muted); }
.year-calendar-block { background:var(--panel); border:1px solid var(--line); border-radius:16px; padding:18px; }
.year-calendar-block + .year-calendar-block { margin-top:16px; }
.year-calendar-head { display:flex; align-items:baseline; gap:8px; margin-bottom:14px; font-size:12px; color:var(--muted); }
.year-calendar-head b { font-size:20px; color:var(--gold); }
.year-months { display:grid; grid-template-columns:repeat(3,1fr); gap:14px; }
.mini-month { border:1px solid var(--line); border-radius:10px; padding:10px; background:rgba(255,255,255,.015); }
.mini-month-head { display:flex; justify-content:space-between; align-items:baseline; margin-bottom:6px; font-size:10.5px; color:var(--muted); }
.mini-month-head b { font-size:13px; color:var(--text); }
.mini-weekhead { display:grid; grid-template-columns:repeat(7,1fr); gap:2px; margin-bottom:3px; font-size:8.5px; color:var(--dim); text-align:center; }
.mini-grid { display:grid; grid-template-columns:repeat(7,1fr); gap:2px; }
.mini-day { position:relative; overflow:hidden; aspect-ratio:1; border-radius:3px; background:rgba(255,255,255,.02); }
.mini-day.is-blank { background:transparent; }
.mini-day.is-weekend { background:rgba(223,96,83,.06); }
.mini-day img { position:absolute; inset:0; width:100%; height:100%; object-fit:cover; opacity:.82; }
.mini-num { position:absolute; top:1px; left:3px; z-index:2; font-size:8px; color:#e4e1d8; text-shadow:0 1px 3px #000; }
.mini-day.is-quiet .mini-num { color:var(--dim); text-shadow:none; }
.mini-count { position:absolute; right:2px; bottom:2px; z-index:2; padding:0 2px; border-radius:2px; font-size:7px; color:var(--gold); background:rgba(5,7,11,.75); }
.years-calendar { background:var(--panel); border:1px solid var(--line); border-radius:16px; padding:14px 18px; }
.years-head, .year-row { display:grid; grid-template-columns:88px repeat(12,minmax(0,1fr)); gap:6px; }
.years-head { padding:4px 0 8px; border-bottom:1px solid var(--line-soft); font-size:9.5px; color:var(--dim); text-align:center; }
.years-head span:first-child { text-align:left; }
.year-row { padding:8px 0; border-top:1px solid var(--line-soft); }
.year-row:nth-child(2) { border-top:none; }
.year-label { display:flex; flex-direction:column; justify-content:center; }
.year-label b { font-size:15px; color:var(--gold); }
.year-label span { font-size:9.5px; color:var(--muted); }
.year-month { position:relative; min-height:76px; overflow:hidden; padding:5px; border:1px solid var(--line-soft); border-radius:7px; background:rgba(255,255,255,.015); }
.year-month.is-quiet { color:var(--dim); }
.ym-month { font-size:9.5px; color:var(--muted); }
.ym-count { position:absolute; top:5px; right:6px; font-size:9px; color:var(--gold); }
.ym-thumbs { position:absolute; left:5px; right:5px; bottom:5px; display:flex; gap:2px; }
.ym-thumbs img { width:20px; height:28px; object-fit:cover; border-radius:2px; opacity:.9; }
.prefs { display:grid; grid-template-columns:minmax(0,1.05fr) minmax(0,1fr); gap:48px; margin-top:34px; padding:22px 0 24px; border-top:1px solid var(--line); border-bottom:1px solid var(--line); }
.prefs-block h3 { margin:0 0 12px; font-size:15px; font-weight:600; }
.prefs-block h3 .en { margin-left:8px; font-size:9.5px; letter-spacing:.18em; color:var(--dim); text-transform:uppercase; }
.genre-tags { display:flex; flex-wrap:wrap; gap:9px 18px; align-items:baseline; }
.genre-tag { font-size:16px; font-weight:600; }
.genre-tag sup { margin-left:2px; font-size:10px; opacity:.75; vertical-align:super; }
.genre-tag.g0 { color:var(--gold); } .genre-tag.g1 { color:var(--red); } .genre-tag.g2 { color:var(--teal); } .genre-tag.g3 { color:var(--blue); } .genre-tag.g4 { color:var(--purple); } .genre-tag.g5 { color:#d98a4a; } .genre-tag.g6 { color:#7fae68; } .genre-tag.g7 { color:#c76c9a; }
.prefs-note { margin:10px 0 0; font-size:10.5px; color:var(--dim); line-height:1.6; }
.decade-bar { display:flex; height:24px; border-radius:6px; overflow:hidden; background:rgba(255,255,255,.05); margin-bottom:10px; }
.decade-seg { display:flex; align-items:center; justify-content:center; height:100%; min-width:0; }
.decade-seg em { font-style:normal; font-size:10px; font-weight:600; color:rgba(8,11,17,.82); }
.decade-legend { display:flex; flex-wrap:wrap; gap:6px 14px; }
.decade-item { display:flex; align-items:center; gap:5px; font-size:11px; color:var(--muted); }
.decade-item b { color:var(--text); font-weight:600; }
.decade-dot { width:8px; height:8px; border-radius:2px; display:inline-block; flex:none; }
.d2020 { background:var(--gold); } .d2010 { background:#d98a4a; } .d2000 { background:var(--purple); } .d1990 { background:var(--blue); } .d1980 { background:var(--teal); } .d1970 { background:#6b7a99; } .dother { background:#4b5262; }
.quotes-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:14px; }
.quote-card { display:flex; flex-direction:column; gap:9px; padding:16px; background:var(--panel); border:1px solid var(--line); border-radius:14px; }
.quote-mark { font-family:Georgia,serif; font-size:34px; line-height:.6; color:var(--gold); opacity:.55; }
.quote-text { margin:0; font-size:11.5px; line-height:1.7; color:#c9c5bb; display:-webkit-box; -webkit-line-clamp:6; -webkit-box-orient:vertical; overflow:hidden; }
.quote-foot { display:grid; grid-template-columns:minmax(0,1fr) auto auto; align-items:center; gap:6px; margin-top:auto; padding-top:8px; border-top:1px solid var(--line-soft); font-size:10px; color:var(--muted); }
.quote-source { min-width:0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.quote-date, .quote-stars { white-space:nowrap; }
.gold { color:var(--gold); } .muted { color:var(--muted); }
.panel-empty { margin:0; font-size:12px; color:var(--dim); }
footer { margin-top:44px; padding-top:16px; border-top:1px solid var(--line); font-size:10px; color:var(--dim); display:flex; justify-content:space-between; gap:16px; flex-wrap:wrap; }
@media (max-width:960px) {
  .kpis { grid-template-columns:repeat(2,1fr); } .kpi:nth-child(2n+1){border-left:none;} .kpi:nth-child(n+3){border-top:1px solid var(--line);}
  .feature { grid-template-columns:260px 1fr; } .prefs { grid-template-columns:1fr; gap:22px; }
  .highlights, .quotes-grid { grid-template-columns:1fr 1fr; }
  .year-months { grid-template-columns:repeat(2,1fr); }
}
@media (max-width:720px) {
  .wrap { padding:0 14px 48px; } .hero { flex-direction:column; align-items:flex-start; padding-top:30px; } .period-block { text-align:left; }
  .feature { grid-template-columns:1fr; } .featured-card { max-width:340px; }
  .highlights, .quotes-grid { grid-template-columns:1fr; } .poster-grid { grid-template-columns:repeat(4,1fr); gap:7px; }
  .year-months { grid-template-columns:1fr; }
  .years-head, .year-row { grid-template-columns:64px repeat(12,minmax(0,1fr)); gap:3px; }
  .year-month { min-height:54px; padding:3px; }
  .ym-thumbs img { width:14px; height:20px; }
}
$theme_css
</style>
</head>
<body>
<div class="wrap">
  <header class="hero">
    <div class="brand">
      <div class="avatar">$avatar</div>
      <div><div class="brand-name">$name</div><div class="brand-line">书影记录 · Report</div></div>
    </div>
    <div class="period-block">
      <div class="big-period">$big_period</div>
      <p class="period-sub">$period_sub</p>
    </div>
  </header>
  <section class="kpis">$kpis</section>
  <section class="feature">
$featured
    <div class="top-panel">
      <div class="panel-head"><h3>TOP 5</h3><span class="en">Top Rated</span></div>
$top5
    </div>
  </section>
  <section class="highlights">$highlights</section>
$poster_section
  <section class="section">
    <div class="section-head"><h3>$calendar_title</h3><span class="en">$calendar_en</span></div>
$calendar
  </section>
  <section class="prefs">
    <div class="prefs-block"><h3>类型偏好<span class="en">Categories</span></h3>$tags</div>
    <div class="prefs-block"><h3>年代构成<span class="en">Decades</span></h3>$decades</div>
  </section>
  <section class="section">
    <div class="section-head"><h3>短评摘录</h3><span class="en">Notes</span><span class="count">$comment_count 条短评</span></div>
    <div class="quotes-grid">$quotes</div>
  </section>
  <footer><span>数据来自本地 SQLite 记录 · 标记日期为本地生效时间 · 报告由书影记录生成</span><span>$exported_at</span></footer>
</div>
</body>
</html>
''')


def generate_report(db: Database, media_type: str, scope: str, month: Optional[str],
                    name: str = '', export_png: bool = False,
                    start_date: str = '', end_date: str = '', year_value: str = '',
                    theme: str = 'dark') -> dict:
    if scope not in ('month', 'year', 'all', 'range'):
        raise ValueError('scope must be month, year, range or all')
    theme = theme if theme in ('dark', 'light') else 'dark'
    if scope == 'month' and not month:
        raise ValueError('month is required for monthly reports')
    if scope == 'year' and not year_value:
        raise ValueError('year is required for yearly reports')
    if scope == 'range' and (not start_date or not end_date):
        raise ValueError('start_date and end_date are required for range reports')
    month_value = month or ''
    year_value = year_value or ''
    name = (name or '').strip() or effective_name(db)
    avatar_html = e(name[0])
    try:
        avatar_uri = avatar_data_uri(effective_avatar(db))
        if avatar_uri:
            avatar_html = f'<img src="{avatar_uri}" alt="{e(name)}">'
    except OSError:
        pass
    items = _load_items(
        db, media_type,
        month_value if scope == 'month' else None,
        start_date=f'{year_value}-01-01' if scope == 'year' else (start_date if scope == 'range' else ''),
        end_date=f'{year_value}-12-31' if scope == 'year' else (end_date if scope == 'range' else ''),
    )
    if not items:
        raise ValueError('没有符合条件的记录。')
    items.sort(key=lambda x: (x.date, x.rating), reverse=True)

    report_dir = REPORTS_DIR
    report_dir.mkdir(parents=True, exist_ok=True)
    theme_suffix = '_light' if theme == 'light' else ''
    if scope == 'month':
        filename = f'{media_type}_month_{month_value}{theme_suffix}.html'
    elif scope == 'year':
        filename = f'{media_type}_year_{year_value}{theme_suffix}.html'
    elif scope == 'range':
        filename = f'{media_type}_range_{start_date}_{end_date}{theme_suffix}.html'
    else:
        filename = f'{media_type}_all{theme_suffix}.html'
    html_path = report_dir / filename

    noun = '部' if media_type == 'movie' else '本'
    type_label = MEDIA_LABELS[media_type]
    mode = _calendar_mode(scope, items)
    if scope == 'month':
        month_year, mon = month_value.split('-')
        big_period = f'{month_year}.{int(mon):02d}'
        period_sub = f'{month_year} 年 {int(mon)} 月 · {len(items)} {noun}{type_label}记录'
        doc_title = f'{month_year} 年 {int(mon)} 月{type_label}报告 · {name}'
    elif scope == 'year':
        big_period = year_value
        period_sub = f'{year_value} 年 · {len(items)} {noun}{type_label}记录'
        doc_title = f'{year_value} 年{type_label}报告 · {name}'
    elif scope == 'range':
        big_period = f'{start_date[:7].replace("-", ".")}–{end_date[:7].replace("-", ".")}'
        period_sub = f'{start_date} — {end_date} · 自定义范围 · {len(items)} {noun}'
        doc_title = f'{start_date} 至 {end_date} {type_label}报告 · {name}'
    else:
        years = sorted({item.date[:4] for item in items if item.date})
        big_period = years[-1] if len(years) == 1 else 'ALL'
        span = f'{years[0]} — {years[-1]}' if len(years) > 1 else years[-1] if years else ''
        period_sub = f'{span} · 全部记录 · {len(items)} {noun}'
        doc_title = f'全部{type_label}报告 · {name}'

    if mode == 'month':
        calendar_title = '观影日历' if media_type == 'movie' else '阅读日历'
        calendar_en = 'Monthly'
    elif mode == 'year':
        calendar_title = '观影月历' if media_type == 'movie' else '阅读月历'
        calendar_en = 'Year Calendar'
    else:
        calendar_title = '观影年历' if media_type == 'movie' else '阅读年历'
        calendar_en = 'Years'

    ranked = sorted(items, key=lambda x: (x.rating, len(x.comment), x.date), reverse=True)
    featured = ranked[0]
    if mode == 'month':
        calendar = _render_calendar_month(items, month_value, report_dir)
    elif mode == 'year':
        item_years = sorted({item.date[:4] for item in items if item.date})
        calendar = ''.join(
            _render_calendar_year([item for item in items if item.date[:4] == item_year],
                                  int(item_year), report_dir)
            for item_year in item_years
        )
    else:
        calendar = _render_calendar_years(items, noun, report_dir)
    comment_count = sum(1 for item in items if item.comment)
    result_wall = ''
    poster_section = ''
    if len(items) >= POSTER_MIN_ITEMS:
        try:
            wall_path = report_dir / f'{html_path.stem}_wall.jpg'
            wall_info = generate_poster_wall(ranked, wall_path, theme=theme)
            rel_path = os.path.relpath(wall_path, report_dir).replace(os.sep, '/')
            poster_wall = (
                f'<div class="poster-wall-image-wrap"><a href="{e(rel_path)}" target="_blank" rel="noopener">'
                f'<img class="poster-wall-image" src="{e(rel_path)}" alt="整合海报"'
                f' width="{wall_info["width"]}" height="{wall_info["height"]}"></a></div>'
                f'<p class="prefs-note">共 {len(items)} 条，评分最高的居中且最大，其余按评分分配大小格。</p>'
            )
            result_wall = str(wall_path)
        except Exception as exc:
            poster_wall = f'<p class="prefs-note">整合海报生成失败：{e(str(exc))}</p>'
        poster_section = (
            '  <section class="section">\n'
            f'    <div class="section-head"><h3>整合海报</h3><span class="en">Poster</span>'
            f'<span class="count">{len(items)} {noun}</span></div>\n'
            f'    {poster_wall}\n'
            '  </section>\n'
        )
    content = _HTML_TEMPLATE.substitute(
        theme=theme,
        theme_css=_LIGHT_THEME_CSS if theme == 'light' else '',
        doc_title=doc_title,
        name=e(name),
        avatar=avatar_html,
        big_period=e(big_period),
        period_sub=e(period_sub),
        kpis=_render_kpis(items, media_type, scope),
        featured=_render_featured(featured, media_type, report_dir),
        top5=_render_top5(ranked, media_type, report_dir),
        highlights=_render_highlights(ranked, scope, media_type, report_dir),
        count=len(items),
        noun=noun,
        poster_section=poster_section,
        calendar_title=calendar_title,
        calendar_en=calendar_en,
        calendar=calendar,
        tags=_render_tags(items, media_type),
        decades=_render_decades(items, media_type),
        comment_count=comment_count,
        quotes=_render_quotes(items),
        exported_at=datetime.now().astimezone().isoformat(timespec='seconds'),
    )
    html_path.write_text(content, encoding='utf-8')
    result = {'html': str(html_path), 'png': '', 'png_error': '',
              'wall': result_wall, 'theme': theme}
    if export_png:
        png_path = html_path.with_suffix('.png')
        try:
            render_png(html_path, png_path)
            result['png'] = str(png_path)
        except Exception as exc:
            result['png_error'] = str(exc)
    return result


def _browser_path() -> Optional[str]:
    local_app_data = os.environ.get('LOCALAPPDATA', '')
    program_files = os.environ.get('PROGRAMFILES') or os.environ.get('ProgramFiles', '')
    program_files_x86 = (os.environ.get('PROGRAMFILES(X86)')
                         or os.environ.get('ProgramFiles(x86)', ''))
    candidates = [
        os.environ.get('CHROME_PATH', ''),
        '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
        str(Path.home() / 'Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
        shutil.which('google-chrome'),
        shutil.which('google-chrome-stable'),
        shutil.which('chrome'),
        shutil.which('chromium'),
        shutil.which('chrome.exe'),
        str(Path(program_files) / 'Google/Chrome/Application/chrome.exe') if program_files else '',
        str(Path(program_files_x86) / 'Google/Chrome/Application/chrome.exe') if program_files_x86 else '',
        str(Path(local_app_data) / 'Google/Chrome/Application/chrome.exe') if local_app_data else '',
        str(Path(program_files) / 'Microsoft/Edge/Application/msedge.exe') if program_files else '',
        str(Path(program_files_x86) / 'Microsoft/Edge/Application/msedge.exe') if program_files_x86 else '',
        str(Path(local_app_data) / 'Microsoft/Edge/Application/msedge.exe') if local_app_data else '',
        shutil.which('msedge'),
        shutil.which('msedge.exe'),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def render_png(html_path: Path, png_path: Path) -> None:
    from PySide6.QtCore import QCoreApplication, QThread
    app = QCoreApplication.instance()
    if app is not None and QThread.currentThread() is not app.thread():
        raise RuntimeError('PNG 渲染需要在主线程执行')
    qt_error = ''
    try:
        _render_png_qt(html_path, png_path)
        return
    except Exception as exc:
        qt_error = str(exc)
    _render_png_browser(html_path, png_path, qt_error)


def _render_png_qt(html_path: Path, png_path: Path) -> None:
    from PySide6.QtCore import QEvent, QEventLoop, QMarginsF, QSize, QSizeF, QTimer, QUrl
    from PySide6.QtGui import QImage, QPageLayout, QPageSize, QPainter
    from PySide6.QtPdf import QPdfDocument
    from PySide6.QtWebEngineCore import QWebEnginePage
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        raise RuntimeError('图形环境不可用')
    page = QWebEnginePage()
    load_loop = QEventLoop()
    loaded = {'ok': False}
    def on_load(ok: bool) -> None:
        loaded['ok'] = bool(ok)
        load_loop.quit()
    page.loadFinished.connect(on_load)
    page.load(QUrl.fromLocalFile(str(Path(html_path).resolve())))
    QTimer.singleShot(30000, load_loop.quit)
    load_loop.exec()
    if not loaded['ok']:
        raise RuntimeError('页面加载失败')
    settle = QEventLoop()
    QTimer.singleShot(600, settle.quit)
    settle.exec()

    measured = {}
    measure_loop = QEventLoop()
    def on_measure(value) -> None:
        measured['height'] = int(value or 0)
        measure_loop.quit()
    js = ("(() => { const f=document.querySelector('footer');"
          " return f ? Math.ceil(f.getBoundingClientRect().bottom + window.scrollY) + 28"
          " : Math.max(document.body.scrollHeight, document.documentElement.scrollHeight); })()")
    page.runJavaScript(js, 0, on_measure)
    QTimer.singleShot(15000, measure_loop.quit)
    measure_loop.exec()
    height = int(measured.get('height') or 0)
    if height <= 0:
        raise RuntimeError('无法测量页面高度')

    width = 1220
    layout = QPageLayout(
        QPageSize(QSizeF(width * 0.75, height * 0.75), QPageSize.Unit.Point, 'report'),
        QPageLayout.Portrait, QMarginsF(0, 0, 0, 0), QPageLayout.Unit.Point,
    )
    pdf_data = {}
    pdf_loop = QEventLoop()
    def on_pdf(data) -> None:
        pdf_data['bytes'] = bytes(data)
        pdf_loop.quit()
    page.printToPdf(on_pdf, layout)
    QTimer.singleShot(60000, pdf_loop.quit)
    pdf_loop.exec()
    if not pdf_data.get('bytes'):
        raise RuntimeError('PDF 渲染失败')
    pdf_path = Path(tempfile.gettempdir()) / f'douban_trace_{uuid.uuid4().hex}.pdf'
    pdf_path.write_bytes(pdf_data['bytes'])
    document = None
    try:
        document = QPdfDocument()
        document.load(str(pdf_path))
        if document.pageCount() <= 0:
            raise RuntimeError('PDF 页面为空')
        scale = 2440 / document.pagePointSize(0).width()
        images = []
        for index in range(document.pageCount()):
            size = document.pagePointSize(index)
            images.append(document.render(
                index, QSize(int(size.width() * scale), int(size.height() * scale))
            ))
        total_height = sum(image.height() for image in images)
        canvas = QImage(images[0].width(), total_height, QImage.Format_RGB32)
        canvas.fill(0xFFFFFFFF)
        painter = QPainter(canvas)
        offset = 0
        for image in images:
            painter.drawImage(0, offset, image)
            offset += image.height()
        painter.end()
        canvas = _trim_report_image(canvas)
        if not canvas.save(str(png_path), 'PNG'):
            raise RuntimeError('PNG 写入失败')
    finally:
        # Windows 上 QPdfDocument 会一直占着这个文件，必须先关掉再删；
        # 删不掉也不该让已经渲染好的 PNG 白做，所以这里静默忽略。
        if document is not None:
            try:
                document.close()
            except Exception:
                pass
        try:
            pdf_path.unlink(missing_ok=True)
        except OSError:
            pass
    page.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _trim_report_image(canvas):
    width, height = canvas.width(), canvas.height()
    if width <= 0 or height <= 0:
        return canvas
    background = canvas.pixelColor(width - 4, height - 4)
    step_x = max(1, width // 12)
    last = 0
    for y in range(height - 1, -1, -1):
        for x in range(0, width, step_x):
            color = canvas.pixelColor(x, y)
            if (abs(color.red() - background.red())
                    + abs(color.green() - background.green())
                    + abs(color.blue() - background.blue())) > 18:
                last = y
                break
        else:
            continue
        break
    if last and last < height - 1:
        return canvas.copy(0, 0, width, min(height, last + 28))
    return canvas


def _render_png_browser(html_path: Path, png_path: Path, qt_error: str = '') -> None:
    browser = _browser_path()
    if not browser:
        detail = f'（内置渲染失败：{qt_error}）' if qt_error else ''
        raise RuntimeError(f'PNG 内置渲染失败，且未找到 Chrome / Edge 作为回退。{detail}')
    measure = html_path.parent / f'.measure_{uuid.uuid4().hex}.html'
    html_text = Path(html_path).read_text(encoding='utf-8')
    stamp = str(int(datetime.now().timestamp() * 1000))
    html_text = re.sub(r'(src="[^"]+_wall\.jpg)"', rf'\1?v={stamp}"', html_text)
    measure_script = '''<script>
window.addEventListener('load', function () {
  requestAnimationFrame(function () {
    document.title = String(Math.max(
      document.body.scrollHeight,
      document.documentElement.scrollHeight
    ));
  });
});
</script>'''
    measure.write_text(
        html_text.replace('</body>', measure_script + '</body>'),
        encoding='utf-8',
    )
    match = None
    try:
        for _ in range(3):
            profile_dir = tempfile.mkdtemp(prefix='douban_trace_measure_')
            try:
                result = subprocess.run([
                    browser, '--headless=new', '--disable-gpu', '--hide-scrollbars',
                    f'--user-data-dir={profile_dir}', '--no-first-run',
                    '--no-default-browser-check',
                    '--window-size=1220,1000', '--virtual-time-budget=20000',
                    '--dump-dom', measure.resolve().as_uri(),
                ], capture_output=True, text=True, encoding='utf-8',
                    errors='replace', timeout=120)
            finally:
                shutil.rmtree(profile_dir, ignore_errors=True)
            match = re.search(r'<title>\s*(\d+)\s*</title>', result.stdout)
            if match:
                break
    finally:
        measure.unlink(missing_ok=True)
    if not match:
        raise RuntimeError('无法测量报告页面高度。')
    height = int(match.group(1)) + 60
    profile_dir = tempfile.mkdtemp(prefix='douban_trace_chrome_')
    try:
        subprocess.run([
            browser, '--headless=new', '--disable-gpu', '--hide-scrollbars',
            f'--user-data-dir={profile_dir}',
            '--run-all-compositor-stages-before-draw',
            '--force-device-scale-factor=2', '--virtual-time-budget=20000',
            f'--window-size=1220,{height}', f'--screenshot={png_path}',
            Path(html_path).resolve().as_uri(),
        ], capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=240)
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)
    if not Path(png_path).exists():
        raise RuntimeError('浏览器未能生成 PNG。')
