"""生成 DMG 根目录的 .DS_Store（安装界面布局）。

背景图需要一条指向卷内 `.background/background.tiff` 的别名（alias），而别名必须
基于真实存在的文件创建。因此这个脚本有三种用法：

1. 传 ``--save-alias <文件>``：在只读挂载的镜像上使用，生成指向卷内文件的别名；
2. 传 ``--alias <文件>``：在可写挂载的镜像上使用，复用第 1 步的别名并写 .DS_Store；
3. 只传目录：无法挂载时使用，此时退化为纯色背景（布局、图标位置仍然生效）。

窗口尺寸、图标位置与 packaging/dmg_background.py 保持一致。

布局依赖 ds-store / mac-alias 属于可选增强：缺失时脚本以退出码 2 结束并提示补装，
调用方（build_macos.sh）可以继续走「不含 .DS_Store」的兜底流程，不让打包中断。
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

try:
    from ds_store import DSStore
    from mac_alias import ALIAS_KIND_FILE, Alias, TargetInfo, VolumeInfo
    from mac_alias.utils import UTC
except ImportError as exc:
    DSStore = None
    ALIAS_KIND_FILE = Alias = TargetInfo = VolumeInfo = UTC = None
    MISSING_LAYOUT_DEPS = str(exc)
else:
    MISSING_LAYOUT_DEPS = ''

VOLUME_NAME = '书影记录'
WINDOW_BOUNDS = '{{200, 100}, {640, 460}}'
APP_POSITION = (170, 200)
LINK_POSITION = (470, 200)
BACKGROUND = '/.background/background.tiff'
FALLBACK_COLOR = (0.96, 0.97, 0.98)


def _synthetic_alias() -> bytes:
    """没有可用挂载点时构造一条卷内相对路径别名（背景可能不显示，仅作兜底）。"""
    created = datetime(2020, 1, 1, tzinfo=UTC())
    volume = VolumeInfo(
        VOLUME_NAME, created, b'H+', 5, 0x100, b'\x00\x00',
        posix_path=f'/Volumes/{VOLUME_NAME}',
    )
    target = TargetInfo(
        ALIAS_KIND_FILE, 'background.tiff', 16, 17, created,
        b'\x00\x00\x00\x00', b'\x00\x00\x00\x00',
        folder_name='.background', cnid_path=(16,), posix_path=BACKGROUND,
    )
    return Alias(volume=volume, target=target).to_bytes()


def write_layout(folder: Path, app_name: str, alias_bytes: bytes = None) -> None:
    background = folder / '.background' / 'background.tiff'
    icvp = {
        'viewOptionsVersion': 1,
        'arrangeBy': 'none',
        'iconSize': 96.0,
        'textSize': 12.0,
        'showIconPreview': False,
        'showItemInfo': False,
        'labelOnBottom': True,
        'gridSpacing': 100.0,
        'gridOffsetX': 0.0,
        'gridOffsetY': 0.0,
        'scrollPositionX': 0.0,
        'scrollPositionY': 0.0,
        # Finder 读取背景图时也会用这三个值做兜底，缺了可能导致整块背景不生效。
        'backgroundColorRed': 1.0,
        'backgroundColorGreen': 1.0,
        'backgroundColorBlue': 1.0,
    }
    if alias_bytes:
        # 复用之前只读挂载时生成的真实别名（可写挂载时间窗很短，来不及重新计算）。
        icvp['backgroundType'] = 2
        icvp['backgroundImageAlias'] = alias_bytes
    else:
        real_alias = None
        if background.is_file():
            # 文件已在卷内：别名记录真实的卷信息与相对路径，Finder 挂载后能直接解析。
            try:
                real_alias = Alias.for_file(str(background)).to_bytes()
            except Exception as exc:
                # APFS 的 firmlink 路径上 CNID 会超出 32 位，mac_alias 无法打包；
                # 这类环境退化为纯色背景，布局（窗口尺寸、图标位置）仍然写出。
                print(f'提示：背景图别名生成失败（{exc}），改用纯色背景', file=sys.stderr)
        if real_alias:
            icvp['backgroundType'] = 2
            icvp['backgroundImageAlias'] = real_alias
        else:
            icvp['backgroundType'] = 1
            icvp['backgroundColorRed'], icvp['backgroundColorGreen'], icvp['backgroundColorBlue'] = FALLBACK_COLOR
            icvp['backgroundImageAlias'] = _synthetic_alias()

    with DSStore.open(str(folder / '.DS_Store'), 'w+') as store:
        store['.']['bwsp'] = {
            'WindowBounds': WINDOW_BOUNDS,
            'ShowSidebar': False, 'ShowStatusBar': False, 'ShowPathbar': False,
            'ShowTabView': False, 'ContainerShowSidebar': False,
            'PreviewPaneVisibility': False, 'SidebarWidth': 0, 'ShowToolbar': False,
        }
        store['.']['icvp'] = icvp
        store['.']['vSrn'] = (b'long', 1)
        store[app_name]['Iloc'] = APP_POSITION
        store['Applications']['Iloc'] = LINK_POSITION
        store.flush()


def main() -> None:
    if MISSING_LAYOUT_DEPS:
        print(f'缺少 DMG 布局依赖（ds-store / mac-alias）：{MISSING_LAYOUT_DEPS}',
              file=sys.stderr)
        print('补装：.venv/bin/python -m pip install -r requirements-build.txt；'
              '本次跳过自定义布局，继续打包。', file=sys.stderr)
        raise SystemExit(2)

    parser = argparse.ArgumentParser(description='生成 DMG 安装界面 .DS_Store')
    parser.add_argument('folder')
    parser.add_argument('app_name')
    parser.add_argument('--save-alias', help='只生成背景别名到该文件，不写 .DS_Store')
    parser.add_argument('--alias', help='使用该文件中的背景别名')
    args = parser.parse_args()

    folder = Path(args.folder)
    background = folder / '.background' / 'background.tiff'
    if args.save_alias:
        if not background.is_file():
            raise SystemExit(f'缺少背景图：{background}')
        try:
            alias_bytes = Alias.for_file(str(background)).to_bytes()
        except Exception as exc:
            print(f'生成背景别名失败：{exc}', file=sys.stderr)
            raise SystemExit(1)
        Path(args.save_alias).write_bytes(alias_bytes)
        print(f'已生成背景别名 {args.save_alias}')
        return
    alias_bytes = Path(args.alias).read_bytes() if args.alias else None
    write_layout(folder, args.app_name, alias_bytes)
    print(f'已写入 {folder}/.DS_Store')


if __name__ == '__main__':
    main()
