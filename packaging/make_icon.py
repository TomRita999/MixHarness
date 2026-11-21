"""生成 MixHarness 应用图标：深色圆角底板上两个交叠的圆，代表两个模型共用一个会话。"""
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
SCALE = 4
SIZE = 1024 * SCALE


def s(value):
    return int(value * SCALE)


def main():
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    # macOS 图标网格：824px 内容区，四周留白 100px。
    box = (s(100), s(100), s(924), s(924))
    radius = s(186)
    shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((box[0], box[1] + s(14), box[2], box[3] + s(14)), radius, fill=(0, 0, 0, 110))
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(s(18))))

    top, bottom = (52, 52, 56), (14, 14, 16)
    gradient = Image.new("RGBA", (1, SIZE))
    for y in range(SIZE):
        t = y / (SIZE - 1)
        gradient.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,))
    gradient = gradient.resize((SIZE, SIZE))
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius, fill=255)
    canvas.paste(gradient, (0, 0), mask)

    rim = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(rim).rounded_rectangle(box, radius, outline=(255, 255, 255, 34), width=s(3))
    canvas.alpha_composite(rim)

    r = s(178)
    cy = s(512)
    left_c, right_c = s(408), s(616)
    left = Image.new("L", (SIZE, SIZE), 0)
    right = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(left).ellipse((left_c - r, cy - r, left_c + r, cy + r), fill=255)
    ImageDraw.Draw(right).ellipse((right_c - r, cy - r, right_c + r, cy + r), fill=255)
    stroke = s(30)
    right_inner = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(right_inner).ellipse((right_c - r + stroke, cy - r + stroke, right_c + r - stroke, cy + r - stroke), fill=255)
    ring = ImageChops.subtract(right, right_inner)
    overlap = ImageChops.multiply(left, right)

    def layer(alpha_mask, color):
        piece = Image.new("RGBA", (SIZE, SIZE), color)
        piece.putalpha(alpha_mask)
        canvas.alpha_composite(piece)

    layer(ImageChops.subtract(left, right), (236, 236, 238, 255))   # 左圆：实心
    layer(ImageChops.subtract(ring, left), (236, 236, 238, 255))    # 右圆：描边
    layer(overlap, (255, 255, 255, 255))                             # 交叠：共享会话

    gap = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(gap).ellipse((right_c - r - s(10), cy - r - s(10), right_c + r + s(10), cy + r + s(10)), outline=255, width=s(10))
    gap = ImageChops.multiply(gap, left)
    knock = Image.new("RGBA", (SIZE, SIZE), (24, 24, 27, 255))
    knock.putalpha(gap)
    canvas.alpha_composite(knock)

    icon = canvas.resize((1024, 1024), Image.LANCZOS)
    icon.save(ROOT / "mixharness/resources/app.png")


if __name__ == "__main__":
    main()
