"""从正方形原图生成带圆角的 Windows 打包图标。

用法：
    .venv\\Scripts\\python.exe packaging\\make_icons.py

源图：packaging/icon-master.png（1024×1024 正方形原图，手工维护的唯一源）
产物：packaging/icon.png（1024×1024，圆角）
      packaging/icon.ico（多尺寸，圆角，供 exe 与安装包使用）

为什么要圆角：Windows 只按图标自身的 alpha 通道绘制，不会给图标加圆角。
方形原图直接打包，桌面/开始菜单快捷方式就是一块带尖角的深色方块。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

PACKAGING_DIR = Path(__file__).resolve().parent
MASTER = PACKAGING_DIR / "icon-master.png"
PNG_OUT = PACKAGING_DIR / "icon.png"
ICO_OUT = PACKAGING_DIR / "icon.ico"

ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)

# 圆角半径占边长的比例，与 docs/images/icon-rounded.png 保持一致（211/1024）
RADIUS_RATIO = 211 / 1024

# 遮罩超采样倍数：先在放大图上画圆角再缩回去，边缘才有抗锯齿
SUPERSAMPLE = 4


def rounded_icon(src: Image.Image, size: int) -> Image.Image:
    """把正方形原图缩放到 size，并套上圆角遮罩。"""
    art = src.convert("RGBA").resize((size, size), Image.LANCZOS)
    big = size * SUPERSAMPLE
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, big - 1, big - 1),
        radius=round(RADIUS_RATIO * big),
        fill=255,
    )
    art.putalpha(mask.resize((size, size), Image.LANCZOS))
    return art


def main() -> None:
    src = Image.open(MASTER)
    rounded_icon(src, 1024).save(PNG_OUT)

    frames = [rounded_icon(src, size) for size in ICO_SIZES]
    frames[-1].save(
        ICO_OUT,
        format="ICO",
        sizes=[(size, size) for size in ICO_SIZES],
        append_images=frames[:-1],
    )

    print(f"已生成 {PNG_OUT.name}（1024）与 {ICO_OUT.name}（{', '.join(map(str, ICO_SIZES))}）")


if __name__ == "__main__":
    main()
