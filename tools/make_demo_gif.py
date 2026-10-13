"""
把 --demo 的即時表格錄成 README 用的 docs/demo.gif（用 Pillow 逐格畫 rich 的輸出）。

  pip install pillow
  python tools/make_demo_gif.py

需要 DejaVu Sans Mono 與文泉驛正黑字型（Debian/Ubuntu：fonts-dejavu-core fonts-wqy-zenhei）。
"""
import glob, os, sys, datetime as dt
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.argv = sys.argv[:1] + ["--demo"]
import pytz
from PIL import Image, ImageDraw, ImageFont
from rich.console import Console
from rich.cells import cell_len
from rich.color import ColorType
import orb_multi_yf_ntfy as m

COLS, FS = 104, 17
LAT = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", FS)
LATB = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", FS)
CJK = ImageFont.truetype("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", FS, index=2)
CW = round(LAT.getlength("M")); LH = round(FS * 1.45); PAD = 18
BG = (22, 27, 34); FG = (220, 225, 230)
PALETTE = {  # 標準 ANSI 色（GitHub dark 風格）
    "red": (255, 92, 87), "green": (60, 210, 120), "yellow": (240, 200, 80), "blue": (60, 120, 230),
    "magenta": (220, 120, 255), "cyan": (90, 200, 230), "white": (240, 240, 240), "black": (0, 0, 0),
}

def rgb(color, default):
    if color is None or color.type == ColorType.DEFAULT:
        return default
    if color.type in (ColorType.STANDARD, ColorType.EIGHT_BIT) and color.name in PALETTE:
        return PALETTE[color.name]
    t = color.get_truecolor(); return (t.red, t.green, t.blue)

def mix(a, b, k): return tuple(round(x * (1 - k) + y * k) for x, y in zip(a, b))

def draw_frame(renderable):
    con = Console(width=COLS, force_terminal=True, color_system="truecolor", legacy_windows=False)
    lines = con.render_lines(renderable, con.options.update(width=COLS), pad=True)
    lines = [l for l in lines]
    # 視窗外框（仿終端機）
    W = COLS * CW + PAD * 2; H = len(lines) * LH + PAD * 2 + 30
    img = Image.new("RGB", (W, H), BG); d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 30], fill=(40, 46, 54))
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([14 + i * 22, 9, 26 + i * 22, 21], fill=c)
    d.text((W // 2, 15), "python orb.py demo", font=CJK, fill=(160, 168, 178), anchor="mm")
    y = 30 + PAD
    for line in lines:
        x = PAD
        for seg in line:
            st = seg.style
            fg = rgb(st.color if st else None, FG); bg = rgb(st.bgcolor if st else None, None)
            if st and st.reverse:
                fg, bg = (bg or BG), fg
            if st and st.dim:
                fg = mix(fg, BG, 0.45)
            bold = bool(st and st.bold)
            for ch in seg.text:
                w = cell_len(ch)
                if bg:
                    d.rectangle([x, y, x + w * CW, y + LH], fill=bg)
                if ch != " ":
                    if ch in "─━│╭╮╰╯":
                        _box(d, ch, x, y, fg)
                    else:
                        f = CJK if ord(ch) >= 0x2E80 else (LATB if bold else LAT)
                        cx = x + w * CW / 2
                        d.text((cx, y + LH / 2), ch, font=f, fill=fg, anchor="mm")
                        if bold and f is CJK:
                            d.text((cx + 0.7, y + LH / 2), ch, font=f, fill=fg, anchor="mm")
                x += w * CW
        y += LH
    return img

def _box(d, ch, x, y, c):
    my, mx = y + LH // 2, x + CW // 2
    th = 2 if ch == "━" else 1
    if ch in "─━": d.line([x, my, x + CW, my], fill=c, width=th)
    elif ch == "│": d.line([mx, y, mx, y + LH], fill=c)
    elif ch == "╭": d.line([mx, my, x + CW, my], fill=c); d.line([mx, my, mx, y + LH], fill=c)
    elif ch == "╮": d.line([x, my, mx, my], fill=c); d.line([mx, my, mx, y + LH], fill=c)
    elif ch == "╰": d.line([mx, y, mx, my], fill=c); d.line([mx, my, x + CW, my], fill=c)
    elif ch == "╯": d.line([mx, y, mx, my], fill=c); d.line([x, my, mx, my], fill=c)

tz = pytz.timezone("Asia/Taipei")
args = m.build_parser().parse_args(); day = dt.date(2026, 10, 13)
full = m.demo_feed(tz, sorted(glob.glob(os.path.join(ROOT, "data", "*.csv"))), day)
args.notify, args.tui, args.exit_after_close = "print", True, True
args.max_signal_age = 10**6; args.market_close = "10:30"
sim = {"t": tz.localize(dt.datetime(2026, 10, 13, 8, 58, 30))}
b = m.Board(args, list(full))
frames = []

def want(t):
    hm = t.strftime("%H:%M:%S")
    if hm < "09:00:00": return 900
    if hm < "09:16:00": return 350 if t.second == 0 else None
    if "09:28:00" <= hm <= "09:36:00" or "09:40:00" <= hm <= "09:43:00": return 450
    if "10:04:00" <= hm <= "10:08:00": return 450
    if hm >= "10:35:00": return 5000
    if t.minute % 4 == 0 and t.second == 0: return 300
    return None

def sleep(x):
    dur = want(sim["t"])
    if dur:
        b.clock = sim["t"]
        frames.append((draw_frame(b.render()), dur))
    sim["t"] += dt.timedelta(seconds=x)

mon = m.Monitor(args, list(full), tz, b,
                fetch=lambda s: {k: full[k][full[k].index <= sim["t"]].copy() for k in s},
                clock=lambda: sim["t"], sleep=sleep)
mon.run()
b.clock = sim["t"]; b.status = "已收盤"
frames.append((draw_frame(b.render()), 6000))
H = max(f.height for f, _ in frames)
imgs = []
for f, _ in frames:
    canvas = Image.new("RGB", (frames[0][0].width, H), BG); canvas.paste(f, (0, 0)); imgs.append(canvas)
pal = [im.quantize(colors=128, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE) for im in imgs]
out = os.path.join(ROOT, "docs", "demo.gif")
pal[0].save(out, save_all=True, append_images=pal[1:], duration=[d for _, d in frames], loop=0, optimize=True, disposal=1)
print(f"{out}: {len(frames)} frames, {imgs[0].size[0]}x{imgs[0].size[1]}")
