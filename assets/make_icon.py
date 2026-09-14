"""
把 assets/favicon.png（DSH 页面图标）生成多尺寸 assets/favicon.ico，
供 Nuitka/JadeUI build.py 打包时作为 exe 图标使用。

用法: python assets/make_icon.py
依赖: pip install Pillow
"""
import sys
from pathlib import Path

from PIL import Image

BASE = Path(__file__).resolve().parent
SRC = BASE / "favicon.png"
DST = BASE / "favicon.ico"
# Pillow 的 ICO 写入器上限 128px（256 帧会被静默丢弃），16–128 对 exe 图标已足够
SIZES = (16, 24, 32, 48, 64, 128)


def main() -> int:
    if not SRC.is_file():
        print("缺少 %s" % SRC, file=sys.stderr)
        return 1
    src = Image.open(SRC).convert("RGBA")
    # 居中放到正方形画布上（透明填充），避免非正方形图标变形
    side = max(src.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(src, ((side - src.width) // 2, (side - src.height) // 2), src)
    # Pillow 的 ICO 写入器用 sizes 参数一次生成多尺寸帧
    canvas.save(DST, format="ICO", sizes=[(s, s) for s in SIZES])
    print("已生成 %s (%d 种尺寸)" % (DST, len(SIZES)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
