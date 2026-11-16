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
NAME="ShuyingJilu-mac"
ditto -c -k --norsrc --keepParent "$APP" "dist/${NAME}.zip"

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

ditto "$APP" "$STAGE/书影记录.app"
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
    set the bounds of container window to {200, 100, 840, 540}
    set opts to the icon view options of container window
    set arrangement of opts to not arranged
    set icon size of opts to 96
    set text size of opts to 12
    set background picture of opts to ((POSIX file "$MOUNT_POINT/.background/background.tiff") as alias)
    -- 背景设置后再次指定窗口尺寸，避免 Finder 重置为小尺寸。
    set the bounds of container window to {200, 100, 840, 540}
    delay 2
    -- 窗口图标尚未渲染完成时设置 position 会失败，重试几次。
    repeat 10 times
      try
        set position of item "Applications" of container window to {490, 190}
        exit repeat
      on error
        delay 1
      end try
    end repeat
    set position of item "书影记录.app" of container window to {150, 190}
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
    -o "dist/${NAME}.dmg" -quiet || return 1
}

if build_dmg_with_layout; then
  echo "DMG 安装界面布局完成"
else
  echo "警告：带布局的 DMG 生成失败，回退到简单镜像" >&2
  if [ -n "$MOUNT_POINT" ]; then
    hdiutil detach "$MOUNT_POINT" -quiet -force 2>/dev/null || true
    MOUNT_POINT=""
  fi
  hdiutil makehybrid -quiet -hfs -hfs-volume-name "$VOLNAME" \
    -o "$RAW_DIR/${NAME}" "$STAGE"
  hdiutil convert "$RAW_DIR/${NAME}.dmg" -format UDZO -o "dist/${NAME}.dmg"
fi
hdiutil verify "dist/${NAME}.dmg" >/dev/null

echo "macOS 包已生成："
ls -lh "dist/${NAME}.zip" "dist/${NAME}.dmg"
