"""Gera folego.ico (cada tamanho desenhado à parte para ficar nítido)."""
import math
from PIL import Image, ImageDraw

BG, TRACK, ACCENT = (20, 20, 22, 255), (52, 52, 55, 255), (217, 119, 87, 255)
FILL = 0.72                       # quanto do anel está cheio


def desenhar(n):
    S = n * 8
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, S - 1, S - 1), S * 0.24, fill=BG)
    w = S * (0.15 if n <= 24 else 0.105)          # traço mais grosso nos tamanhos pequenos
    m = S * (0.17 if n <= 24 else 0.22)
    box = (m, m, S - m, S - m)
    d.ellipse(box, outline=TRACK, width=int(w))
    end = -90 + 360 * FILL
    d.arc(box, -90, end, fill=ACCENT, width=int(w))
    c, r = S / 2, (S - 2 * m - w) / 2
    for ang in (-90, end):                         # pontas arredondadas
        a = math.radians(ang)
        x, y = c + r * math.cos(a), c + r * math.sin(a)
        d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=ACCENT)
    return im.resize((n, n), Image.LANCZOS)


sizes = [256, 128, 64, 48, 32, 24, 16]
imgs = [desenhar(n) for n in sizes]
imgs[0].save("folego.ico", sizes=[(n, n) for n in sizes], append_images=imgs[1:])
# prévia lado a lado
prev = Image.new("RGBA", (sum(sizes) + 20 * len(sizes), 276), (235, 235, 235, 255))
x = 10
for im in imgs:
    prev.alpha_composite(im, (x, 10)); x += im.size[0] + 20
prev.save("icone_previa.png")
