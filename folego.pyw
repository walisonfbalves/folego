"""Fôlego — widget flutuante com o uso do plano do Claude (sessão 5h e semanal).

Os números vêm do próprio Claude Code: da linha de status (quando você usa o terminal) e de
uma consulta mínima feita pelo Claude Code em segundo plano a cada 10 min (modo automático).
O app nunca toca no login nem em tokens.

Clique: expande/contrai.  Arraste: muda de lugar.  Botão direito: menu (minimizar
para a bandeja, atualizar, iniciar com o Windows).  Ícone na bandeja: clique mostra/esconde
o widget; botão direito > Sair fecha o app.
"""
import ctypes
import ctypes.wintypes as wt
import datetime as dt
import json
import math
import os
import queue
import socket
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

try:
    import pystray
except ImportError:          # sem a biblioteca, o app funciona só com o widget
    pystray = None

sys.path.insert(0, str(Path(__file__).resolve().parent))
import uso_core  # noqa: E402

# ---------------------------------------------------------------- config

APP_DIR = uso_core.APP_DIR
CONFIG = APP_DIR / "config.json"
PORT = 47613                     # trava de instância única
SS = 3                           # supersampling dos desenhos

BG = (20, 20, 22)
BG_ALPHA = 250
BORDER = (255, 255, 255, 22)
TEXT = (240, 238, 234)
MUTED = (150, 148, 144)
FAINT = (95, 94, 92)
TRACK = (255, 255, 255, 24)
ACCENT = (217, 119, 87)          # argila do Claude
WARN = (232, 168, 64)
DANGER = (229, 72, 77)

AUTO_INTERVALO = 600   # segundos entre consultas automáticas
AUTO_AO_ABRIR = 120    # ao abrir o cartão, consulta se o dado tiver mais que isso

DIAS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]

# ---------------------------------------------------------------- win32

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
ULW_ALPHA = 2


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


user32.GetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
user32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE),
                                       wt.HDC, ctypes.POINTER(wt.POINT), wt.DWORD,
                                       ctypes.POINTER(BLENDFUNCTION), wt.DWORD]
user32.GetDC.restype = wt.HDC
user32.GetDC.argtypes = [wt.HWND]
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
user32.GetParent.restype = wt.HWND
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   wt.HANDLE, wt.DWORD]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wt.HDC]


def blit(hwnd, img, x, y):
    """Desenha uma imagem RGBA na janela com transparência por pixel."""
    w, h = img.size
    r, g, b, a = img.split()
    r, g, b = (ImageChops.multiply(c, a) for c in (r, g, b))   # alfa pré-multiplicado
    data = Image.merge("RGBA", (b, g, r, a)).tobytes()

    screen = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(screen)
    bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    bits = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    ctypes.memmove(bits, data, len(data))
    old = gdi32.SelectObject(mem, hbmp)
    blend = BLENDFUNCTION(0, 0, 255, 1)
    user32.UpdateLayeredWindow(hwnd, screen, ctypes.byref(wt.POINT(x, y)), ctypes.byref(wt.SIZE(w, h)),
                               mem, ctypes.byref(wt.POINT(0, 0)), 0, ctypes.byref(blend), ULW_ALPHA)
    gdi32.SelectObject(mem, old)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(None, screen)


def work_area():
    rect = wt.RECT()
    user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)   # SPI_GETWORKAREA
    return rect.left, rect.top, rect.right, rect.bottom


def dpi_scale():
    try:
        return user32.GetDpiForSystem() / 96
    except Exception:
        return 1.0

# ---------------------------------------------------------------- fontes


def _font_path(*names):
    folder = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for n in names:
        if (folder / n).exists():
            return str(folder / n)
    return None


FONT_NUM = _font_path("bahnschrift.ttf", "seguisb.ttf", "segoeui.ttf")
FONT_UI = _font_path("segoeui.ttf")
FONT_UI_SB = _font_path("seguisb.ttf", "segoeui.ttf")
_font_cache = {}


def font(path, size):
    key = (path, size)
    if key not in _font_cache:
        f = ImageFont.truetype(path, size) if path else ImageFont.load_default()
        if path and path.endswith("bahnschrift.ttf"):
            try:
                f.set_variation_by_name("SemiBold")
            except Exception:
                pass
        _font_cache[key] = f
    return _font_cache[key]

# ---------------------------------------------------------------- formatação


def parse_iso(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def falta(reset):
    if not reset:
        return "sem uso no momento"
    secs = int((reset - dt.datetime.now().astimezone()).total_seconds())
    if secs <= 0:
        return "resetando…"
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"reseta em {d}d {h}h"
    if h:
        return f"reseta em {h}h {m:02d}min"
    return f"reseta em {max(m, 1)}min"


def quando(reset):
    if not reset:
        return ""
    hoje = dt.datetime.now().astimezone().date()
    hora = reset.strftime("%H:%M")
    if reset.date() == hoje:
        return f"hoje às {hora}"
    if reset.date() == hoje + dt.timedelta(days=1):
        return f"amanhã às {hora}"
    return f"{DIAS[reset.weekday()]} {reset.strftime('%d/%m')} às {hora}"


def cor(pct):
    if pct >= 90:
        return DANGER
    if pct >= 75:
        return WARN
    return ACCENT

# ---------------------------------------------------------------- desenho


class Painter:
    PILL_H = 34
    CARD_W = 304
    PAD = 10        # margem transparente para a sombra

    def __init__(self, scale):
        self.s = scale

    def px(self, v):
        return int(round(v * self.s))

    # -- primitivas ------------------------------------------------------

    def panel(self, w, h, radius, alpha=1.0):
        """Fundo arredondado com sombra suave e borda fina (w,h incluem a margem)."""
        P = self.px(self.PAD)
        big = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
        d = ImageDraw.Draw(big)
        box = (P * SS, P * SS, (w - P) * SS - 1, (h - P) * SS - 1)
        r = radius * SS
        # sombra: alguns contornos concêntricos bem fracos
        for i in range(P, 0, -1):
            a = int(18 * (1 - i / P) ** 2 * alpha)
            off = i * SS
            d.rounded_rectangle((box[0] - off, box[1] - off + SS * 2, box[2] + off, box[3] + off + SS * 2),
                                r + off, fill=(0, 0, 0, a))
        d.rounded_rectangle(box, r, fill=BG + (int(BG_ALPHA * alpha),))
        bw = max(1, self.px(1)) * SS
        d.rounded_rectangle(box, r, outline=BORDER[:3] + (int(BORDER[3] * alpha),), width=bw)
        return big.resize((w, h), Image.LANCZOS)

    def ring(self, size, pct, width, color):
        big = Image.new("RGBA", (size * SS, size * SS), (0, 0, 0, 0))
        d = ImageDraw.Draw(big)
        w = width * SS
        box = (0, 0, size * SS - 1, size * SS - 1)          # no PIL o traço cresce para dentro
        d.ellipse(box, outline=TRACK, width=w)
        if pct > 0:
            end = -90 + 360 * min(pct, 100) / 100
            d.arc(box, -90, end, fill=color + (255,), width=w)
            # pontas arredondadas
            c = size * SS / 2
            rr = (size * SS - w) / 2
            for ang in (-90, end):
                a = math.radians(ang)
                cx, cy = c + rr * math.cos(a), c + rr * math.sin(a)
                d.ellipse((cx - w / 2, cy - w / 2, cx + w / 2, cy + w / 2), fill=color + (255,))
        return big.resize((size, size), Image.LANCZOS)

    def bar(self, w, h, pct, color):
        big = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
        d = ImageDraw.Draw(big)
        d.rounded_rectangle((0, 0, w * SS - 1, h * SS - 1), h * SS / 2, fill=TRACK)
        fw = int(w * SS * min(pct, 100) / 100)
        if fw > 0:
            d.rounded_rectangle((0, 0, max(fw, h * SS) - 1, h * SS - 1), h * SS / 2, fill=color + (255,))
        return big.resize((w, h), Image.LANCZOS)

    @staticmethod
    def text_w(draw, text, f):
        return draw.textlength(text, font=f)

    # -- pílula (contraída) ---------------------------------------------

    def pill(self, st):
        px = self.px
        P = px(self.PAD)
        h = px(self.PILL_H)
        f = font(FONT_NUM, px(13))
        ring = px(15)
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))

        if st.data:
            items = [(st.session_pct, "5h"), (st.week_pct, "7d")]
            labels = [f"{p:.0f}%" for p, _ in items]
            tags = [t for _, t in items]
            ft = font(FONT_UI, px(10))
            inner = px(14)
            for lab, tag in zip(labels, tags):
                inner += ring + px(6) + self.text_w(probe, lab, f) + px(4) + self.text_w(probe, tag, ft)
            inner += px(14) * (len(items) - 1) + px(14)
        else:
            msg = st.short_status()
            ft = font(FONT_UI, px(12))
            inner = px(14) + px(8) + px(8) + self.text_w(probe, msg, ft) + px(14)
        w = int(inner) + 2 * P
        img = Image.new("RGBA", (w, h + 2 * P), (0, 0, 0, 0))
        img.alpha_composite(self.panel(w, h + 2 * P, h // 2))
        d = ImageDraw.Draw(img)
        cy = P + h // 2
        x = P + px(14)
        if st.data:
            for i, ((pct, _), lab, tag) in enumerate(zip(items, labels, tags)):
                img.alpha_composite(self.ring(ring, pct, max(2, px(2.6)), cor(pct)), (int(x), cy - ring // 2))
                x += ring + px(6)
                d.text((x, cy), lab, font=f, fill=TEXT, anchor="lm")
                x += self.text_w(d, lab, f) + px(4)
                d.text((x, cy + px(0.5)), tag, font=ft, fill=MUTED, anchor="lm")
                x += self.text_w(d, tag, ft)
                if i < len(items) - 1:
                    x += px(7)
                    d.line((x, cy - px(6), x, cy + px(6)), fill=(255, 255, 255, 30), width=max(1, px(1)))
                    x += px(7)
        else:
            dot = px(8)
            d.ellipse((x, cy - dot // 2, x + dot, cy + dot // 2), fill=st.status_color())
            d.text((x + dot + px(8), cy), msg, font=ft, fill=MUTED, anchor="lm")
        return img

    # -- cartão (expandido) ---------------------------------------------

    def card(self, st):
        px = self.px
        P = px(self.PAD)
        W = px(self.CARD_W)
        pad = px(18)
        f_title = font(FONT_UI_SB, px(13))
        f_small = font(FONT_UI, px(11))
        f_label = font(FONT_UI_SB, px(11))
        f_big = font(FONT_NUM, px(26))
        f_pct = font(FONT_NUM, px(13))
        f_body = font(FONT_UI, px(12))

        extras = st.extra_limits() if st.data else []
        breakdown = st.breakdown() if st.data else []
        H = px(18 + 22 + 16)                                  # topo + título + respiro
        if st.data:
            H += px(104 + 14 + 18 + 18 + 16)                  # anéis + textos
            H += px(30) * len(extras)
            if breakdown:
                H += px(16 + 16 + 8 + 16)
        else:
            H += px(70)
        H += px(1 + 12 + 14 + 16)                             # linha + rodapé

        img = Image.new("RGBA", (W + 2 * P, H + px(200)), (0, 0, 0, 0))   # camada de conteúdo
        d = ImageDraw.Draw(img)
        L, R = P + pad, P + W - pad
        y = P + px(18)

        # cabeçalho
        d.ellipse((L, y + px(4), L + px(8), y + px(12)), fill=st.status_color())
        d.text((L + px(15), y + px(8)), "Fôlego", font=f_title, fill=TEXT, anchor="lm")
        if st.plan:
            tw = self.text_w(d, st.plan, f_small)
            d.rounded_rectangle((R - tw - px(14), y, R, y + px(17)), px(8.5), outline=(255, 255, 255, 40),
                                width=max(1, px(1)))
            d.text((R - px(7), y + px(8.5)), st.plan, font=f_small, fill=MUTED, anchor="rm")
        y += px(22 + 16)

        if not st.data:
            d.text(((L + R) / 2, y + px(22)), st.long_status(), font=f_body, fill=TEXT, anchor="mm")
            d.text(((L + R) / 2, y + px(44)), st.hint(), font=f_small, fill=MUTED, anchor="mm")
            y += px(70)
        else:
            cols = [(st.session_pct, "SESSÃO · 5H", st.session_reset),
                    (st.week_pct, "SEMANA · 7D", st.week_reset)]
            colw = (R - L) / 2
            rs = px(104)
            for i, (pct, label, reset) in enumerate(cols):
                cx = L + colw * i + colw / 2
                img.alpha_composite(self.ring(rs, pct, px(7), cor(pct)), (int(cx - rs / 2), y))
                d.text((cx, y + rs / 2 - px(3)), f"{pct:.0f}", font=f_big, fill=TEXT, anchor="mm")
                d.text((cx, y + rs / 2 + px(17)), "%", font=f_small, fill=MUTED, anchor="mm")
                ty = y + rs + px(14)
                d.text((cx, ty), label, font=f_label, fill=MUTED, anchor="mm")
                d.text((cx, ty + px(18)), falta(reset), font=f_body, fill=TEXT, anchor="mm")
                d.text((cx, ty + px(36)), quando(reset), font=f_small, fill=FAINT, anchor="mm")
            y += px(104 + 14 + 18 + 18 + 16)

            for name, pct in extras:
                d.text((L, y + px(6)), name, font=f_small, fill=MUTED, anchor="lm")
                d.text((R, y + px(6)), f"{pct:.0f}%", font=f_pct, fill=TEXT, anchor="rm")
                bw = R - L
                img.alpha_composite(self.bar(int(bw), px(4), pct, cor(pct)), (int(L), int(y + px(16))))
                y += px(30)

            if breakdown:
                y += px(16)
                d.text((L, y), "Uso da semana por app", font=f_small, fill=MUTED, anchor="lm")
                y += px(16)
                bw, bh = int(R - L), px(6)
                big = Image.new("RGBA", (bw * SS, bh * SS), (0, 0, 0, 0))
                bd = ImageDraw.Draw(big)
                bd.rounded_rectangle((0, 0, bw * SS - 1, bh * SS - 1), bh * SS / 2, fill=TRACK)
                shades = [ACCENT, (236, 190, 168), (120, 118, 114), (80, 79, 76)]
                xx = 0
                mask = Image.new("L", big.size, 0)
                ImageDraw.Draw(mask).rounded_rectangle((0, 0, bw * SS - 1, bh * SS - 1), bh * SS / 2, fill=255)
                seg = Image.new("RGBA", big.size, (0, 0, 0, 0))
                sd = ImageDraw.Draw(seg)
                for j, (_, p) in enumerate(breakdown):
                    ww = bw * SS * p / 100
                    sd.rectangle((xx, 0, xx + ww, bh * SS), fill=shades[j % len(shades)] + (255,))
                    xx += ww + SS * 2
                big.paste(seg, (0, 0), ImageChops.multiply(mask, seg.split()[3]))
                img.alpha_composite(big.resize((bw, bh), Image.LANCZOS), (int(L), int(y)))
                y += px(8 + 8)
                xx = L
                for j, (n, p) in enumerate(breakdown):
                    c = shades[j % len(shades)]
                    d.ellipse((xx, y + px(2), xx + px(6), y + px(8)), fill=c)
                    t = f"{n} {p}%"
                    d.text((xx + px(10), y + px(5)), t, font=f_small, fill=MUTED, anchor="lm")
                    xx += px(10) + self.text_w(d, t, f_small) + px(14)
                y += px(16)

        y += px(12)
        d.line((L, y, R, y), fill=(255, 255, 255, 18), width=max(1, px(1)))
        y += px(14)
        d.text((L, y + px(6)), st.footer(), font=f_small, fill=FAINT, anchor="lm")
        d.text((R, y + px(6)), "botão direito: menu", font=f_small, fill=FAINT, anchor="rm")
        H = int(y + px(6 + 8 + 16)) - P
        out = self.panel(W + 2 * P, H + 2 * P, px(16))
        out.alpha_composite(img.crop((0, 0, W + 2 * P, H + 2 * P)))
        return out

# ---------------------------------------------------------------- estado


class State:
    def __init__(self):
        self.data = None           # {"five_hour": {"pct", "reset"}, "seven_day": {...}}
        self.plan = ""
        self.updated = None
        self.error = None          # ("setup"|"wait"|"noclaude", mensagem)
        self.loading = True
        self.auto = False

    def _lim(self, key):
        return (self.data or {}).get(key) or {}

    @property
    def session_pct(self):
        return float(self._lim("five_hour").get("pct") or 0)

    @property
    def week_pct(self):
        return float(self._lim("seven_day").get("pct") or 0)

    @property
    def session_reset(self):
        return self._lim("five_hour").get("reset")

    @property
    def week_reset(self):
        return self._lim("seven_day").get("reset")

    def extra_limits(self):
        return []

    def breakdown(self):
        return []

    def status_color(self):
        if self.error:
            return WARN
        if self.loading and not self.data:
            return MUTED
        if self.updated and time.time() - self.updated > 30 * 60:
            return MUTED
        return (88, 196, 132)

    def short_status(self):
        if self.error and self.error[0] == "noclaude":
            return "claude code não encontrado"
        if self.error and self.auto:
            return "consultando o claude…"
        if self.error and self.error[0] == "setup":
            return "conecte ao claude code"
        if self.error:
            return "abra o claude code no terminal"
        return "carregando…"

    def long_status(self):
        if self.error and self.error[0] == "noclaude":
            return "Claude Code não encontrado"
        if self.error and self.auto:
            return "Consultando o Claude Code…"
        if self.error and self.error[0] == "setup":
            return "Fôlego não está conectado"
        if self.error:
            return "Esperando o Claude Code"
        return "Carregando…"

    def hint(self):
        if self.error and self.error[0] == "noclaude":
            return "instale o Claude Code e faça login"
        if self.error and self.auto:
            return "leva alguns segundos na primeira vez"
        if self.error and self.error[0] == "setup":
            return "botão direito → Conectar ao Claude Code"
        if self.error:
            return "rode  claude  no terminal e mande uma mensagem"
        return ""

    def footer(self):
        if not self.updated:
            return ""
        mins = int(time.time() - self.updated) // 60
        if mins < 1:
            return "atualizado agora"
        if mins < 60:
            return f"atualizado há {mins} min"
        h = mins // 60
        return f"último dado há {h} h" if h < 48 else f"último dado há {h // 24} dias"

# ---------------------------------------------------------------- app


class App:
    def __init__(self):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self.cfg = self._load_cfg()
        self.st = State()
        self.expanded = False
        self.anim = None
        self.fetching = False
        self.last_fetch = 0
        self.hidden = False
        self.calls = queue.Queue()      # pedidos vindos de outras threads (bandeja, 2ª instância)

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("Fôlego")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.geometry("10x10+0+0")
        self.root.deiconify()
        self.root.update_idletasks()
        self.hwnd = user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
        ex = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)

        self.paint = Painter(dpi_scale())
        self.cur_img = None
        self.cur_pos = (0, 0)

        self.root.bind("<ButtonPress-1>", self.on_press)
        self.root.bind("<B1-Motion>", self.on_drag)
        self.root.bind("<ButtonRelease-1>", self.on_release)
        self.root.bind("<Button-3>", self.on_menu)
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Atualizar agora", command=lambda: self.consulta_auto(force=True))
        self.autostart = tk.BooleanVar(value=self._autostart_enabled())
        self.auto_on = self.autostart.get()
        self.menu.add_checkbutton(label="Iniciar com o Windows", variable=self.autostart,
                                  command=self.toggle_autostart)
        self.menu.add_command(label="Voltar para o canto", command=self.reset_position)
        self.conectado = tk.BooleanVar(value=uso_core.conectado())
        self.menu.add_checkbutton(label="Conectado ao Claude Code", variable=self.conectado,
                                  command=self.toggle_conexao)
        self.auto_var = tk.BooleanVar(value=self.cfg.get("auto", True))
        self.menu.add_checkbutton(label="Atualizar automaticamente (a cada 10 min)", variable=self.auto_var,
                                  command=self.toggle_auto)
        self.st.auto = self.auto_var.get()
        self.consultando = False
        self.last_auto = 0
        self.menu.add_separator()

        self.tray = self._start_tray()
        if self.tray:
            self.menu.add_command(label="Minimizar para a bandeja", command=self.hide)
        else:
            self.menu.add_command(label="Fechar", command=self.quit)

        self._preparar_conexao()
        self._start_ipc()
        self.render()
        self.fetch()
        self.root.after(1000, self.tick)
        self.root.after(100, self._pump)
        self.root.after(800, self._auto_tick)

    # -- config / posição ------------------------------------------------

    def _load_cfg(self):
        try:
            return json.loads(CONFIG.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_cfg(self):
        try:
            CONFIG.write_text(json.dumps(self.cfg), encoding="utf-8")
        except Exception:
            pass

    def anchor(self):
        """Canto superior direito da pílula, em pixels de tela."""
        l, t, r, b = work_area()
        if "right" in self.cfg and "top" in self.cfg:
            return self.cfg["right"], self.cfg["top"]
        # padrão: canto superior direito, à esquerda dos botões de fechar/minimizar
        return r - self.paint.px(150), t + self.paint.px(4)

    def reset_position(self):
        self.cfg.pop("right", None)
        self.cfg.pop("top", None)
        self._save_cfg()
        self.render()

    def place(self, img):
        """Posição da imagem: alinhada pela direita/topo do âncora, sem sair da tela."""
        P = self.paint.px(Painter.PAD)
        right, top = self.anchor()
        w, h = img.size
        x, y = right + P - w, top - P
        l, t, r, b = work_area()
        x = max(l - P, min(x, r + P - w))
        y = max(t - P, min(y, b + P - h))
        return x, y

    # -- render ----------------------------------------------------------

    def show(self, img, pos):
        if self.hidden:
            return
        self.cur_img, self.cur_pos = img, pos
        w, h = img.size
        self.root.geometry(f"{w}x{h}+{pos[0]}+{pos[1]}")
        blit(self.hwnd, img, *pos)

    def render(self):
        if self.anim:
            return
        img = self.paint.card(self.st) if self.expanded else self.paint.pill(self.st)
        self.show(img, self.place(img))

    def animate(self, expand):
        """Transição curta entre pílula e cartão (cresce a partir do canto)."""
        pill, card = self.paint.pill(self.st), self.paint.card(self.st)
        cx, cy = self.place(card)
        px_, py_ = self.place(pill)
        W, H = card.size
        frames = 9
        self.anim = True

        def step(i):
            t = i / frames
            e = 1 - (1 - t) ** 3                       # ease-out
            k = e if expand else 1 - e
            frame = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            # recorte do cartão crescendo a partir do canto superior direito
            vw = int(pill.size[0] + (W - pill.size[0]) * k)
            vh = int(pill.size[1] + (H - pill.size[1]) * k)
            crop = card.crop((W - vw, 0, W, vh))
            a = crop.split()[3].point(lambda v: int(v * min(1, k * 1.4)))
            crop.putalpha(a)
            frame.alpha_composite(crop, (W - vw + (px_ + pill.size[0] - cx - W), py_ - cy))
            pa = pill.split()[3].point(lambda v: int(v * max(0, 1 - k * 2.2)))
            p2 = pill.copy()
            p2.putalpha(pa)
            frame.alpha_composite(p2, (px_ - cx, py_ - cy))
            self.show(frame, (cx, cy))
            if i < frames:
                self.root.after(14, step, i + 1)
            else:
                self.anim = None
                self.render()

        step(1)

    def toggle(self):
        self.expanded = not self.expanded
        if self.expanded and (not self.st.updated or time.time() - self.st.updated > AUTO_AO_ABRIR):
            self.consulta_auto()
        if self.expanded and time.time() - self.last_fetch > 30:
            self.fetch()
        self.animate(self.expanded)

    # -- mouse -----------------------------------------------------------

    def on_press(self, e):
        self.drag_from = (e.x_root, e.y_root)
        self.drag_win = self.cur_pos
        self.dragging = False

    def on_drag(self, e):
        dx, dy = e.x_root - self.drag_from[0], e.y_root - self.drag_from[1]
        if not self.dragging and abs(dx) + abs(dy) < self.paint.px(5):
            return
        self.dragging = True
        pos = (self.drag_win[0] + dx, self.drag_win[1] + dy)
        self.show(self.cur_img, pos)

    def on_release(self, e):
        if self.dragging:
            P = self.paint.px(Painter.PAD)
            w = self.cur_img.size[0]
            self.cfg["right"] = self.cur_pos[0] + w - P
            self.cfg["top"] = self.cur_pos[1] + P
            self._save_cfg()
            self.dragging = False
            self.render()
        elif not self.anim:
            self.toggle()

    def on_menu(self, e):
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    # -- dados -----------------------------------------------------------

    def fetch(self, force=False):
        """Lê o arquivo que o ajudante da linha de status grava (rápido, sem rede)."""
        self.last_fetch = time.time()
        try:
            self.last_mtime = uso_core.USO.stat().st_mtime
        except OSError:
            self.last_mtime = None
        try:
            self.st.data, self.st.updated = uso_core.ler_uso()
            self.st.error = None
        except uso_core.SemDados:
            if not (self.st.error and self.st.error[0] == "noclaude"):
                self.st.error = ("wait" if uso_core.conectado() else "setup", "")
        self.st.loading = False
        self.render()
        self._update_tray()

    def tick(self):
        try:
            mtime = uso_core.USO.stat().st_mtime
        except OSError:
            mtime = None
        if mtime != getattr(self, "last_mtime", None) or time.time() - self.last_fetch >= 30:
            self.fetch()
            self.root.attributes("-topmost", True)
        self.root.after(3000, self.tick)

    # -- consulta automática --------------------------------------------

    def consulta_auto(self, force=False):
        """Pede ao Claude Code (em segundo plano) os números atuais; o resultado cai em uso.json."""
        if self.consultando or not (force or self.auto_var.get()):
            return
        self.consultando = True
        self.last_auto = time.time()

        def work():
            try:
                uso_core.consultar_claude()
            except uso_core.SemClaude:
                self.calls.put(self._sem_claude)
            except Exception:
                pass
            finally:
                self.consultando = False
                self.calls.put(self.fetch)

        threading.Thread(target=work, daemon=True).start()

    def _sem_claude(self):
        if not self.st.data:
            self.st.error = ("noclaude", "")
            self.render()

    def _auto_tick(self):
        if self.auto_var.get() and time.time() - self.last_auto >= AUTO_INTERVALO:
            self.consulta_auto()
        self.root.after(30000, self._auto_tick)

    def toggle_auto(self):
        self.cfg["auto"] = self.auto_var.get()
        self.st.auto = self.auto_var.get()
        self._save_cfg()
        if self.auto_var.get():
            self.consulta_auto()
        self.render()

    # -- conexão com o Claude Code ---------------------------------------

    def _preparar_conexao(self):
        """Na primeira vez, conecta sozinho; depois só mantém o caminho do comando atualizado."""
        try:
            if uso_core.conectado():
                uso_core.atualizar_comando()
            elif self.cfg.get("auto_conectar", True):
                uso_core.conectar()
            self.conectado.set(uso_core.conectado())
        except Exception:
            pass

    def toggle_conexao(self):
        try:
            if self.conectado.get():
                uso_core.conectar()
                self.cfg["auto_conectar"] = True
            else:
                uso_core.desconectar()
                self.cfg["auto_conectar"] = False
            self._save_cfg()
        except Exception:
            pass
        self.conectado.set(uso_core.conectado())
        self.fetch()

    # -- iniciar com o Windows ------------------------------------------

    RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

    def _autostart_cmd(self):
        if getattr(sys, "frozen", False):          # rodando como Folego.exe
            return f'"{sys.executable}"'
        pyw = Path(sys.executable).with_name("pythonw.exe")
        return f'"{pyw}" "{Path(__file__).resolve()}"'

    def _autostart_enabled(self):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.RUN_KEY) as k:
                winreg.QueryValueEx(k, "Folego")
                return True
        except OSError:
            return False

    def set_autostart(self, on):
        self.autostart.set(on)
        self.toggle_autostart()

    def toggle_autostart(self):
        import winreg
        self.auto_on = self.autostart.get()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if self.autostart.get():
                winreg.SetValueEx(k, "Folego", 0, winreg.REG_SZ, self._autostart_cmd())
            else:
                try:
                    winreg.DeleteValue(k, "Folego")
                except OSError:
                    pass

    # -- instância única -------------------------------------------------

    def _start_ipc(self):
        def serve():
            while True:
                try:
                    conn, _ = self.sock.accept()
                    conn.close()
                    self.calls.put(self.reveal)
                except OSError:
                    return
        threading.Thread(target=serve, daemon=True).start()

    def _pump(self):
        while True:
            try:
                fn = self.calls.get_nowait()
            except queue.Empty:
                break
            fn()
        self.root.after(100, self._pump)

    # -- esconder / mostrar ---------------------------------------------

    def hide(self):
        if self.hidden:
            return
        self.anim = None
        self.expanded = False
        self.hidden = True
        self.root.withdraw()
        self._update_tray()

    def unhide(self):
        if not self.hidden:
            return
        self.hidden = False
        self.root.deiconify()
        ex = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        self.root.attributes("-topmost", True)
        self.render()
        self._update_tray()

    def toggle_hidden(self):
        if self.hidden:
            self.unhide()
        else:
            self.hide()

    def reveal(self):
        """Abriram o app de novo: mostra o widget (e expande se já estava visível)."""
        if self.hidden:
            self.unhide()
        elif not self.expanded and not self.anim:
            self.toggle()

    # -- bandeja do Windows ---------------------------------------------

    def _tray_image(self):
        n = 64
        S = n * 4
        im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle((0, 0, S - 1, S - 1), S * 0.24, fill=BG + (255,))
        pct = self.st.session_pct if self.st.data else 72
        color = cor(pct) if self.st.data else ACCENT
        if self.st.error and not self.st.data:
            color = MUTED
        w, m = S * 0.14, S * 0.18
        box = (m, m, S - m, S - m)
        d.ellipse(box, outline=(52, 52, 55, 255), width=int(w))
        if pct > 0:
            end = -90 + 360 * min(pct, 100) / 100
            d.arc(box, -90, end, fill=color + (255,), width=int(w))
            c, r = S / 2, (S - 2 * m - w) / 2
            for ang in (-90, end):
                a = math.radians(ang)
                x, y = c + r * math.cos(a), c + r * math.sin(a)
                d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=color + (255,))
        return im.resize((n, n), Image.LANCZOS)

    def _tray_title(self):
        if self.st.data:
            return (f"Fôlego — sessão {self.st.session_pct:.0f}% · semana {self.st.week_pct:.0f}%\n"
                    f"{falta(self.st.session_reset)}")
        return "Fôlego — " + self.st.short_status()

    def _start_tray(self):
        if not pystray:
            return None
        call = self.calls.put
        Item = pystray.MenuItem
        menu = pystray.Menu(
            Item(lambda _: "Mostrar widget" if self.hidden else "Esconder widget",
                 lambda: call(self.toggle_hidden), default=True),
            Item("Atualizar agora", lambda: call(lambda: self.fetch(force=True))),
            Item("Iniciar com o Windows", lambda: call(lambda: self.set_autostart(not self.autostart.get())),
                 checked=lambda _: self.auto_on),
            pystray.Menu.SEPARATOR,
            Item("Sair", lambda: call(self.quit)),
        )
        try:
            icon = pystray.Icon("Folego", self._tray_image(), "Fôlego", menu)
            icon.run_detached()
            return icon
        except Exception:
            return None

    def _update_tray(self):
        if not self.tray:
            return
        try:
            self.tray.icon = self._tray_image()
            self.tray.title = self._tray_title()[:127]
            self.tray.update_menu()
        except Exception:
            pass

    def quit(self):
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        try:
            self.sock.close()
        except Exception:
            pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", PORT))
        sock.listen(2)
    except OSError:
        # já está aberto: só pede para a janela existente expandir
        try:
            socket.create_connection(("127.0.0.1", PORT), timeout=2).close()
        except OSError:
            pass
        return
    app = App.__new__(App)
    app.sock = sock
    App.__init__(app)
    app.run()


if __name__ == "__main__":
    main()
