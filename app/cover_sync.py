"""自定义封面的识别与云端同步辅助。

桌面端封面统一存放在 `cache/covers/`：
- 豆瓣封面：`<media_type>_<subject_id>.jpg`，可从豆瓣重新下载；
- 自定义封面：用户本地上传（`manual_*`）或从其他条目链接获取，文件名不符合上述规则，
  豆瓣拉不回来，需要随快照 sidecar（`.db.ios.json`）以 base64 同步。
"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Optional

from app.config import COVERS_DIR, ROOT

MAX_COVER_BYTES = 4 * 1024 * 1024


def resolve_cover_path(cover_path: str) -> Optional[Path]:
    text = (cover_path or '').strip()
    if not text:
        return None
    path = Path(text)
    if not path.is_absolute():
        path = ROOT / path
    return path if path.is_file() else None


def cover_is_valid(cover_path: str) -> bool:
    """本地封面文件是否可用于展示（存在且不是反爬挑战页那种小文件）。"""
    path = resolve_cover_path(cover_path)
    if path is None:
        return False
    try:
        return path.stat().st_size > 500
    except OSError:
        return False


def is_custom_cover(media_type: str, subject_id, cover_path: str) -> bool:
    """文件名不是豆瓣封面命名规则时，视为需要随云同步的自定义封面。"""
    path = resolve_cover_path(cover_path)
    if path is None:
        return False
    expected = f'{media_type}_{subject_id}.jpg'.lower()
    return path.name.lower() != expected


def read_cover_base64(media_type: str, subject_id, cover_path: str,
                      max_bytes: int = MAX_COVER_BYTES) -> Optional[str]:
    path = resolve_cover_path(cover_path)
    if path is None:
        return None
    try:
        if path.stat().st_size > max_bytes:
            return None
        return base64.b64encode(path.read_bytes()).decode('ascii')
    except OSError:
        return None


def save_cover_base64(media_type: str, subject_id, data: str) -> str:
    """把 sidecar 里的封面写回本地缓存，返回相对路径（失败返回空串）。"""
    try:
        raw = base64.b64decode(data or '', validate=False)
    except (ValueError, TypeError):
        return ''
    if not raw or len(raw) > MAX_COVER_BYTES:
        return ''
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    suffix = '.png' if raw[:8].startswith(b'\x89PNG') else '.jpg'
    target = COVERS_DIR / f'sync_{media_type}_{subject_id}{suffix}'
    try:
        target.write_bytes(raw)
    except OSError:
        return ''
    try:
        return str(target.relative_to(ROOT))
    except ValueError:
        return str(target)
