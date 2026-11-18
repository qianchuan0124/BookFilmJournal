#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python3}"

if [ ! -x .venv/bin/python ]; then
  "$PYTHON" -m venv .venv
fi
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-build.txt

rm -rf "dist/书影记录.app" dist/ShuyingJilu
rm -f dist/*.zip dist/*.dmg

.venv/bin/python -m PyInstaller --noconfirm --clean packaging/ShuyingJilu.spec

APP="dist/书影记录.app"
if [ ! -d "$APP" ]; then
  echo "未生成 macOS 应用：$APP" >&2
  exit 1
fi

codesign --force --deep --sign - "$APP" || true
rm -rf dist/ShuyingJilu

# 版本号取自 app/config.py，发布包按「书影记录-<版本>-<架构>」命名，便于内网分发。
VERSION="$(.venv/bin/python -c 'from app.config import APP_VERSION; print(APP_VERSION)')"
ARCH="$(uname -m)"
NAME="书影记录-${VERSION}-${ARCH}"
RELEASE="dist/release"
rm -rf "$RELEASE"
mkdir -p "$RELEASE"
OUT_DMG="$RELEASE/${NAME}.dmg"
ditto -c -k --norsrc --keepParent "$APP" "$RELEASE/${NAME}.zip"

# DMG 安装界面：应用图标 + Applications 拖拽入口 + 背景图 + 固定窗口布局。
# 布局需要挂载可读写卷并用 AppleScript 设置 Finder；若挂载或布局失败，
# 回退到不挂载卷的 makehybrid 流程，保证打包不中断。
STAGE="$(mktemp -d)"
RAW_DIR="$(mktemp -d)"
MOUNT_POINT=""
cleanup() {
  if [ -n "$MOUNT_POINT" ]; then
    hdiutil detach "$MOUNT_POINT" -quiet -force 2>/dev/null || true
  fi
  rm -rf "$STAGE" "$RAW_DIR"
}
trap cleanup EXIT

ditto --norsrc --noextattr "$APP" "$STAGE/书影记录.app"
ln -s /Applications "$STAGE/Applications"
.venv/bin/python packaging/dmg_background.py "$STAGE/.background"

VOLNAME="书影记录"
MOUNT_POINT="/Volumes/$VOLNAME"

build_dmg_with_layout() {
  # 卸载所有同名残留卷（含「书影记录 1」等编号变体），避免 Finder 把布局写到错误的卷上。
  hdiutil info | awk -v v="$VOLNAME" '$0 ~ "Apple_HFS" && $0 ~ v {print $2}' | while read -r dev; do
    hdiutil detach "$dev" -quiet -force 2>/dev/null || true
  done
  hdiutil detach "$MOUNT_POINT" -quiet -force 2>/dev/null || true
  hdiutil create -size 1g -fs HFS+ -volname "$VOLNAME" -type SPARSE -ov -quiet \
    -o "$RAW_DIR/layout" || return 1
  hdiutil attach "$RAW_DIR/layout.sparseimage" -readwrite -noverify -noautoopen \
    -quiet || return 1
  ditto "$STAGE/." "$MOUNT_POINT/" || return 1

  # 卷图标（SetFile 仅在安装了 Xcode CLT 时可用，缺失则跳过）。
  cp packaging/icon.icns "$MOUNT_POINT/.VolumeIcon.icns"
  if command -v SetFile >/dev/null 2>&1; then
    SetFile -a C "$MOUNT_POINT" || true
  fi

  osascript <<EOF || return 1
tell application "Finder"
  tell disk "$VOLNAME"
    open
    set current view of container window to icon view
    set toolbar visible of container window to false
    set statusbar visible of container window to false
    set sidebar width of container window to 0
    set the bounds of container window to {200, 100, 840, 560}
    set opts to the icon view options of container window
    set arrangement of opts to not arranged
    set icon size of opts to 96
    set text size of opts to 12
    set background picture of opts to ((POSIX file "$MOUNT_POINT/.background/background.tiff") as alias)
    -- 背景设置后再次指定窗口尺寸，避免 Finder 重置为小尺寸。
    set the bounds of container window to {200, 100, 840, 560}
    delay 2
    -- 窗口图标尚未渲染完成时设置 position 会失败，重试几次。
    repeat 10 times
      try
        set position of item "Applications" of container window to {470, 200}
        exit repeat
      on error
        delay 1
      end try
    end repeat
    set position of item "书影记录.app" of container window to {170, 200}
    update without registering applications
    delay 1
    close
  end tell
end tell
EOF

  sync
  # 校验 .DS_Store 已写入（布局信息持久化的载体）。
  if [ ! -f "$MOUNT_POINT/.DS_Store" ]; then
    echo "错误：Finder 布局未写入 .DS_Store" >&2
    hdiutil detach "$MOUNT_POINT" -quiet -force 2>/dev/null || true
    return 1
  fi
  hdiutil detach "$MOUNT_POINT" -quiet || return 1
  MOUNT_POINT=""
  hdiutil convert "$RAW_DIR/layout.sparseimage" -format UDZO \
    -o "$OUT_DMG" -quiet || return 1
}

detach_volume() {
  hdiutil info | awk -v v="$VOLNAME" '$0 ~ "Apple_HFS" && $0 ~ v {print $2}' | while read -r dev; do
    hdiutil detach "$dev" -quiet -force 2>/dev/null || true
  done
  hdiutil detach "$MOUNT_POINT" -quiet -force 2>/dev/null || true
}

build_plain_dmg() {
  # 最简兜底：不含 .DS_Store，只保证产出一个可用的 DMG。
  hdiutil makehybrid -quiet -hfs -hfs-volume-name "$VOLNAME" \
    -o "$RAW_DIR/plain" "$STAGE" || return 1
  hdiutil convert "$RAW_DIR/plain.dmg" -format UDZO -o "$OUT_DMG" -quiet
}

build_dmg_with_generated_layout() {
  # 不支持 Finder 排版时的方案：
  # 1) 离线生成只读镜像（不挂载）；
  # 2) 只读挂载后生成真实的背景别名（此时卷内文件已存在，别名记录卷内相对路径）；
  # 3) 转成可写镜像并扩容，在很短的挂载窗口里写入 .DS_Store；
  # 4) 压回最终 DMG。
  # 注意：别名必须用标准挂载点 /Volumes/<卷名> 生成，否则 Finder 解析不到背景图。
  MOUNT_POINT="/Volumes/$VOLNAME"
  detach_volume
  hdiutil makehybrid -quiet -hfs -hfs-volume-name "$VOLNAME" \
    -o "$RAW_DIR/ro" "$STAGE" || return 1
  hdiutil attach "$RAW_DIR/ro.dmg" -readonly -noverify -noautoopen -quiet || return 1
  if ! .venv/bin/python packaging/dmg_layout.py "$MOUNT_POINT" "书影记录.app" \
      --save-alias "$RAW_DIR/alias.bin"; then
    hdiutil detach "$MOUNT_POINT" -quiet -force 2>/dev/null || true
    return 1
  fi
  hdiutil detach "$MOUNT_POINT" -quiet -force 2>/dev/null || true

  hdiutil convert "$RAW_DIR/ro.dmg" -format UDRW -o "$RAW_DIR/rw.dmg" -quiet || return 1
  hdiutil resize -size 1g "$RAW_DIR/rw.dmg" >/dev/null 2>&1 || return 1
  hdiutil attach "$RAW_DIR/rw.dmg" -readwrite -noverify -noautoopen -quiet || return 1
  if ! .venv/bin/python packaging/dmg_layout.py "$MOUNT_POINT" "书影记录.app" \
      --alias "$RAW_DIR/alias.bin"; then
    hdiutil detach "$MOUNT_POINT" -quiet -force 2>/dev/null || true
    return 1
  fi
  hdiutil detach "$MOUNT_POINT" -quiet -force 2>/dev/null || true
  hdiutil convert "$RAW_DIR/rw.dmg" -format UDZO -o "$OUT_DMG" -quiet || return 1
}

# .DS_Store 布局依赖 ds-store / mac-alias，属于可选增强：缺失时跳过自定义布局继续出包。
LAYOUT_DEPS_OK=1
if ! .venv/bin/python -c "import ds_store, mac_alias" >/dev/null 2>&1; then
  LAYOUT_DEPS_OK=0
  echo "提示：未安装 ds-store / mac-alias，本次跳过自定义布局；" \
       "补装：.venv/bin/python -m pip install -r requirements-build.txt" >&2
fi

# 优先离线写入 .DS_Store：不调用 Finder，打包时不会弹出安装界面窗口。
if [ "$LAYOUT_DEPS_OK" = 1 ] && build_dmg_with_generated_layout; then
  echo "DMG 安装界面布局完成（离线写入，不弹 Finder 窗口）"
else
  if [ -n "$MOUNT_POINT" ]; then
    hdiutil detach "$MOUNT_POINT" -quiet -force 2>/dev/null || true
    MOUNT_POINT=""
  fi
  if [ "$LAYOUT_DEPS_OK" = 0 ]; then
    echo "提示：缺少布局依赖，生成不含 .DS_Store 的 DMG" >&2
    build_plain_dmg
  elif build_dmg_with_layout; then
    echo "DMG 安装界面布局完成（Finder 排版）"
  else
    echo "警告：背景别名不可用，退回纯色背景布局" >&2
    .venv/bin/python packaging/dmg_layout.py "$STAGE" "书影记录.app" || true
    build_plain_dmg
  fi
fi
hdiutil verify "$OUT_DMG" >/dev/null

sed "s/__VERSION__/${VERSION}/g" packaging/安装说明.txt > "$RELEASE/书影记录-安装说明.txt"

echo "内网分发包已生成（$RELEASE）："
ls -lh "$RELEASE"
