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

# hdiutil create --srcfolder 需要挂载临时卷，在残留映像、卷名冲突或文件系统抖动时
# 容易报 I/O 错误。这里改为先把 app 复制到 staging 目录，用 makehybrid 生成镜像，
# 再 convert 成压缩格式，整个过程不挂载卷。
STAGE="$(mktemp -d)"
RAW_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$STAGE" "$RAW_DIR"
}
trap cleanup EXIT

ditto "$APP" "$STAGE/书影记录.app"
hdiutil makehybrid -quiet -hfs -hfs-volume-name "书影记录" \
  -o "$RAW_DIR/${NAME}" "$STAGE"
hdiutil convert "$RAW_DIR/${NAME}.dmg" -format UDZO -o "dist/${NAME}.dmg"
hdiutil verify "dist/${NAME}.dmg" >/dev/null

echo "macOS 包已生成："
ls -lh "dist/${NAME}.zip" "dist/${NAME}.dmg"
