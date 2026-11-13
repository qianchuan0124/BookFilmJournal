import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional

import requests

from app.words import find_calibre

RELEASE_API = 'https://api.github.com/repos/kovidgoyal/calibre/releases/latest'
DOWNLOAD_PAGE = {
    'darwin': 'https://calibre-ebook.com/download_osx',
    'win32': 'https://calibre-ebook.com/download_windows64',
}
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
      'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')
Progress = Callable[[str, int, int], None]


class CalibreInstallError(Exception):
    pass


def _asset_name(platform: str, version: str) -> str:
    return f'calibre-{version}.dmg' if platform == 'darwin' else f'calibre-64bit-{version}.msi'


def _release_url(platform: str) -> str:
    try:
        response = requests.get(RELEASE_API, timeout=20, headers={'User-Agent': UA})
        response.raise_for_status()
        release = response.json()
        tag = str(release.get('tag_name') or '').lstrip('v')
        assets = release.get('assets') or []
        expected = _asset_name(platform, tag)
        for asset in assets:
            if asset.get('name') == expected:
                return str(asset.get('browser_download_url') or '')
        suffix = '.dmg' if platform == 'darwin' else '.msi'
        for asset in assets:
            name = str(asset.get('name') or '')
            if name.endswith(suffix):
                return str(asset.get('browser_download_url') or '')
    except (requests.RequestException, ValueError, KeyError):
        pass
    page = DOWNLOAD_PAGE.get(platform, '')
    if not page:
        raise CalibreInstallError('当前平台暂不支持自动安装 Calibre。')
    try:
        response = requests.get(page, timeout=20, headers={'User-Agent': UA})
        response.raise_for_status()
        suffix = r'\.dmg' if platform == 'darwin' else r'\.msi'
        import re
        match = re.search(r'https://github\.com/kovidgoyal/calibre/releases/download/[^"\' ]+' + suffix,
                          response.text)
        if match:
            return match.group(0)
    except requests.RequestException:
        pass
    raise CalibreInstallError('无法获取 Calibre 下载地址，请检查网络或稍后重试。')


def _download(url: str, target: Path, progress: Optional[Progress]) -> None:
    try:
        response = requests.get(
            url, stream=True, timeout=60,
            headers={'User-Agent': UA, 'Accept': 'application/octet-stream'},
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise CalibreInstallError(f'下载 Calibre 失败：{exc}') from exc
    total = int(response.headers.get('content-length') or 0)
    done = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('wb') as file:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            file.write(chunk)
            done += len(chunk)
            if progress:
                progress(f'下载 Calibre… {done / 1024 / 1024:.1f} MB',
                         done, total)
    if target.stat().st_size < 1024 * 1024:
        raise CalibreInstallError('Calibre 安装包下载不完整。')


def _run(command: list, timeout: int = 600) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CalibreInstallError(f'执行安装命令失败：{exc}') from exc


def _verify_macos_app(app: Path) -> None:
    result = _run(['codesign', '--verify', '--deep', '--strict', '--verbose=2', str(app)])
    if result.returncode != 0:
        raise CalibreInstallError('Calibre 代码签名校验失败，已停止安装。')
    result = _run(['spctl', '--assess', '--type', 'execute', '-vv', str(app)])
    if result.returncode != 0:
        raise CalibreInstallError('Calibre 未通过 macOS Gatekeeper 校验，已停止安装。')


def _install_macos(progress: Optional[Progress]) -> str:
    url = _release_url('darwin')
    with tempfile.TemporaryDirectory(prefix='calibre_install_') as tmp:
        dmg = Path(tmp) / 'calibre.dmg'
        _download(url, dmg, progress)
        mount = Path(tmp) / 'mount'
        mount.mkdir()
        attached = _run([
            'hdiutil', 'attach', str(dmg), '-nobrowse', '-readonly',
            '-mountpoint', str(mount),
        ])
        if attached.returncode != 0:
            raise CalibreInstallError(f'挂载 Calibre 安装包失败：{attached.stderr.strip()}')
        try:
            app = mount / 'calibre.app'
            if not app.is_dir():
                raise CalibreInstallError('安装包中未找到 calibre.app。')
            _verify_macos_app(app)
            target = Path.home() / 'Applications' / 'calibre.app'
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise CalibreInstallError(
                    f'用户目录已存在 Calibre：{target}。请先手动处理旧版本。'
                )
            if progress:
                progress('正在安装 Calibre 到用户目录…', 0, 0)
            copied = _run(['ditto', str(app), str(target)], timeout=900)
            if copied.returncode != 0:
                raise CalibreInstallError(f'复制 Calibre 失败：{copied.stderr.strip()}')
        finally:
            _run(['hdiutil', 'detach', str(mount), '-force'])
    path = find_calibre()
    if not path:
        raise CalibreInstallError('Calibre 安装完成，但未找到 ebook-convert。')
    return path


def _verify_windows_installer(path: Path) -> None:
    script = (
        "$s = Get-AuthenticodeSignature -LiteralPath "
        f"'{str(path).replace(chr(39), chr(39) * 2)}'; "
        "if ($s.Status -ne 'Valid') { Write-Output $s.Status; exit 1 }"
    )
    result = _run(['powershell', '-NoProfile', '-Command', script], timeout=60)
    if result.returncode != 0:
        raise CalibreInstallError('Calibre 安装包签名校验失败，已停止安装。')


def _install_windows(progress: Optional[Progress]) -> str:
    winget = shutil.which('winget')
    if winget:
        if progress:
            progress('正在通过 winget 安装 Calibre…', 0, 0)
        result = _run([
            winget, 'install', '-e', '--id', 'calibre.calibre',
            '--silent', '--accept-package-agreements', '--accept-source-agreements',
        ], timeout=900)
        path = find_calibre()
        if path:
            return path
        if result.returncode == 0:
            raise CalibreInstallError('winget 已完成，但未找到 ebook-convert。')

    url = _release_url('win32')
    with tempfile.TemporaryDirectory(prefix='calibre_install_') as tmp:
        installer = Path(tmp) / 'calibre.msi'
        _download(url, installer, progress)
        _verify_windows_installer(installer)
        if progress:
            progress('正在安装 Calibre…', 0, 0)
        result = _run([
            'msiexec', '/i', str(installer), '/qn', '/norestart',
        ], timeout=900)
        if result.returncode not in (0, 3010):
            raise CalibreInstallError(f'Calibre 安装失败：{result.stderr.strip()}')
    path = find_calibre()
    if not path:
        raise CalibreInstallError('Calibre 安装完成，但未找到 ebook-convert。')
    return path


def install_calibre(progress: Optional[Progress] = None) -> str:
    if sys.platform == 'darwin':
        return _install_macos(progress)
    if os.name == 'nt':
        return _install_windows(progress)
    raise CalibreInstallError('当前平台暂不支持自动安装 Calibre。')
