"""
產生文章用的示意圖 docs/orb-concept.png（使用 data/ 的合成資料，不是真實行情）。

  pip install matplotlib
  python tools/make_article_figures.py
"""
import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.transforms
import matplotlib.dates
import logging

logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
from matplotlib import font_manager
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import orb_core as core  # noqa: E402

for f in ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",):
    if os.path.exists(f):
        font_manager.fontManager.addfont(f)
plt.rcParams.update({"font.family": ["WenQuanYi Zen Hei", "DejaVu Sans"], "axes.unicode_minus": False})

SURFACE, INK, INK2, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#8a8984", "#e6e5e1"
VWAP, SIGNAL, BAND = "#2a78d6", "#e34948", "#efeee9"


def load(sym):
    d = pd.read_csv(os.path.join(ROOT, "data", f"{sym}.csv"), encoding="utf-8-sig", parse_dates=["Datetime"])
    return d.set_index("Datetime")


def panel(ax, axv, sym, title, sig_offset=(-8, 18), sig_ha="right"):
    df = load(sym)
    ind = core.add_indicators(df)
    orh, orl, win_end = core.opening_range(df, 15, core.parse_hhmm("09:00"))
    sig = core.scan_day(df, sym, vol_factor=1.5)[0]
    res = core.simulate_exit(df, sig)
    x = df.index

    ax.axvspan(x[0], win_end, color=BAND, zorder=0)
    lo, hi = min(df["Low"].min(), orl), max(df["High"].max(), orh)
    ax.set_ylim(lo - (hi - lo) * 0.06, hi + (hi - lo) * 0.18)
    ax.set_xlim(x[0], x[-1])
    ax.text(x[0] + (win_end - x[0]) / 2, 0.97, "開盤區間\n09:00–09:15", ha="center", va="top", fontsize=9, color=INK2,
            transform=matplotlib.transforms.blended_transform_factory(ax.transData, ax.transAxes))
    for lvl, name in ((orh, "ORH"), (orl, "ORL")):
        ax.axhline(lvl, color=MUTED, lw=1, zorder=1)
        ax.text(x[-1], lvl, f" {name} {lvl:.2f}", va="center", ha="left", fontsize=9, color=INK2)
    ax.plot(x, df["Close"], color=INK2, lw=1.6, zorder=3, label="收盤價")
    ax.plot(x, ind["VWAP"], color=VWAP, lw=2, zorder=2, label="VWAP")
    ax.text(x[-1], ind["VWAP"].iloc[-1], " VWAP", va="center", ha="left", fontsize=9, color=VWAP)

    seg = df[(df.index >= sig.ts) & (df.index <= res["exit_ts"])].index
    ax.hlines(sig.stop, seg[0], seg[-1], colors=INK2, linestyles=(0, (4, 3)), lw=1.2, zorder=2)
    ax.text(seg[0], sig.stop, f"停損 {sig.stop:.2f} ", ha="right", va="bottom", fontsize=9, color=INK2)
    ax.scatter([sig.ts], [sig.price], marker="^", s=90, color=SIGNAL, edgecolors=SURFACE, linewidths=2, zorder=5)
    ax.annotate(f"多頭訊號 {sig.ts:%H:%M}\n收 {sig.price:.2f}", (sig.ts, sig.price), xytext=sig_offset,
                textcoords="offset points", ha=sig_ha, va="center", fontsize=9, color=INK)
    ax.scatter([res["exit_ts"]], [res["exit_price"]], marker="o", s=60, color=SURFACE, edgecolors=INK, linewidths=1.6,
               zorder=5, clip_on=False)
    ax.annotate(f"{res['reason']} {res['exit_ts']:%H:%M}\n{res['r']:+.2f}R", (res["exit_ts"], res["exit_price"]),
                xytext=(0, -30 if res["reason"] == "停損" else 14), textcoords="offset points", ha="center",
                fontsize=9, color=INK)
    ax.set_title(title, loc="left", fontsize=12, color=INK, pad=10)

    colors = [SIGNAL if t == sig.ts else "#c9c8c2" for t in x]
    axv.bar(x, df["Volume"], width=0.0006, color=colors)
    axv.plot(x, ind["VolMA10"] * 1.5, color=INK2, lw=1, ls=(0, (2, 2)))
    axv.text(x[-1], ind["VolMA10"].iloc[-1] * 1.5, " 10 根均量×1.5", va="center", ha="left", fontsize=8, color=INK2)
    axv.set_ylabel("成交量", fontsize=9, color=INK2)

    for a in (ax, axv):
        a.set_facecolor(SURFACE)
        for s in ("top", "right"):
            a.spines[s].set_visible(False)
        for s in ("left", "bottom"):
            a.spines[s].set_color(GRID)
        a.tick_params(colors=MUTED, labelsize=8)
        a.grid(axis="y", color=GRID, lw=0.8)
        a.set_axisbelow(True)
    ax.tick_params(labelbottom=False)
    axv.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%H:%M"))


fig = plt.figure(figsize=(13, 6.2), facecolor=SURFACE)
gs = fig.add_gridspec(2, 2, height_ratios=[3, 1], hspace=0.08, wspace=0.28, left=0.05, right=0.92, top=0.86, bottom=0.12)
specs = (("2330.TW", "突破後續漲（收盤平倉）", (-10, 14), "right"),
         ("2454.TW", "假突破：突破後跌回，觸發停損", (12, 0), "left"))
for col, (sym, title, off, ha) in enumerate(specs):
    ax = fig.add_subplot(gs[0, col])
    panel(ax, fig.add_subplot(gs[1, col], sharex=ax), sym, title, off, ha)
fig.suptitle("ORB 判斷規則示意：收盤價突破 ORH、≥ VWAP、量 ≥ 均量 × 1.5", x=0.05, ha="left", fontsize=14, color=INK)
fig.text(0.05, 0.025, "⚠ 合成資料（repo 的 data/2330.TW.csv、data/2454.TW.csv），只用來說明判斷規則，不是真實行情，也不代表策略績效。",
         fontsize=9, color=INK2)
out = os.path.join(ROOT, "docs", "orb-concept.png")
fig.savefig(out, dpi=150, facecolor=SURFACE)
print(out)


def wizard_png():
    """用模擬輸入實際執行 orb.py init，錄下終端機畫面存成 docs/wizard.png（不連網、不寫入專案設定）。"""
    import tempfile

    from rich.console import Console
    from rich.segment import Segments

    import make_demo_gif
    import orb
    import orb_config

    answers = iter(["1", "2330 2317 6488.two", "1,3", "", "", "https://discord.com/api/webhooks/0000/xxxx",
                    "n", "y"])

    class ScriptedConsole(Console):
        def input(self, prompt="", *, markup=True, emoji=True, password=False, stream=None):
            self.print(prompt, end="")
            ans = next(answers)
            self.print("•" * 12 if password and ans else ans, style="bold")
            return ans

    con = ScriptedConsole(width=96, record=True, force_terminal=True, color_system="truecolor", highlight=False)
    with tempfile.TemporaryDirectory() as d:
        old = (orb.console, orb_config.DEFAULT_CONFIG, orb_config.DEFAULT_ENV, os.getcwd(), dict(os.environ))
        for k in list(os.environ):
            if k.startswith(("NTFY", "DISCORD", "TELEGRAM", "SLACK", "LINE", "SMTP", "EMAIL", "PUSHOVER", "WEBHOOK")):
                del os.environ[k]
        import rich

        old_global = rich._console
        rich._console = con  # Prompt / Confirm 用的是 rich 的全域 console
        orb.console = con
        orb_config.DEFAULT_CONFIG, orb_config.DEFAULT_ENV = os.path.join(d, "config.toml"), os.path.join(d, ".env")
        os.chdir(d)
        try:
            orb.cmd_init([])
        finally:
            orb.console, orb_config.DEFAULT_CONFIG, orb_config.DEFAULT_ENV = old[:3]
            rich._console = old_global
            os.chdir(old[3])
            os.environ.clear()
            os.environ.update(old[4])
    segs = list(con._record_buffer)
    img = make_demo_gif.draw_frame(Segments(segs), title="python orb.py init", cols=96)
    out = os.path.join(ROOT, "docs", "wizard.png")
    img.save(out)
    print(out, img.size)


sys.path.insert(0, os.path.join(ROOT, "tools"))
wizard_png()
