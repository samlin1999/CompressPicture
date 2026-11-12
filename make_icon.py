"""產生 icon.ico（圖片 + 向內壓縮箭頭）。執行：python make_icon.py"""
from PIL import Image, ImageDraw

S = 1024


def draw() -> Image.Image:
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    # 底：圓角方塊，藍色漸層
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        t = y / S
        c = (int(37 + (14 - 37) * t), int(99 + (165 - 99) * t), int(235 + (233 - 235) * t), 255)
        gd.line([(0, y), (S, y)], fill=c)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([40, 40, S - 40, S - 40], radius=210, fill=255)
    im.paste(grad, (0, 0), mask)

    # 白色相片框
    d.rounded_rectangle([210, 250, 814, 774], radius=60, fill=(255, 255, 255, 255))
    # 山與太陽
    d.polygon([(260, 724), (440, 470), (560, 640), (640, 545), (764, 724)], fill=(37, 99, 235, 255))
    d.ellipse([600, 320, 700, 420], fill=(250, 204, 21, 255))

    # 左右向內箭頭（壓縮）
    w = (255, 255, 255, 255)
    for sx in (1, -1):
        cx = S / 2 - sx * 420
        tip = S / 2 - sx * 330
        d.polygon([(tip, 512), (cx, 430), (cx, 594)], fill=w)
    return im


if __name__ == "__main__":
    img = draw()
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.resize((256, 256), Image.LANCZOS).save("icon.ico", sizes=sizes)
    print("icon.ico written")
