"""
ORB 多標的盤中監控：yfinance 1 分 K（延遲資料）+ ntfy 推播 + 終端機即時表格。

  python orb_multi_yf_ntfy.py --symbols 2330.TW,2317.TW --ntfy-topic MyStockAlert
  python orb_multi_yf_ntfy.py --demo            # 用 data/*.csv 快轉重播，看畫面用
"""

import argparse
import glob
import os
import time
from collections import deque
from datetime import datetime, timedelta

import pandas as pd
import pytz
import yfinance as yf

import orb_core as core

try:
    from rich import box
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ImportError:  # 沒裝 rich 就用純文字模式
    Console = None

UP, DOWN = "red", "green"  # 台股習慣：紅漲綠跌


def fetch_multi_1m(symbols):
    """抓最近兩天的 1 分 K（多抓一天是為了拿昨收算漲跌）。"""
    df = yf.download(
        tickers=" ".join(symbols),
        period="2d",
        interval="1m",
        prepost=False,
        progress=False,
        group_by="ticker",
        threads=True,
        auto_adjust=False,
    )
    out = {}
    if isinstance(df.columns, pd.MultiIndex):
        for sym in symbols:
            try:
                dfi = df[sym].copy()
            except KeyError:
                continue
            dfi = dfi.rename(columns=str.title).dropna(how="all")
            if not dfi.empty:
                out[sym] = dfi
    else:
        dfi = df.rename(columns=str.title).dropna(how="all")
        if not dfi.empty and len(symbols) == 1:
            out[symbols[0]] = dfi
    return out


def now_in(tz):
    return datetime.now(tz)


class Board:
    """盤面狀態；TUI 和純文字模式共用。"""

    def __init__(self, args, symbols):
        self.args = args
        self.symbols = symbols
        self.rows = {s: {} for s in symbols}
        self.events = deque(maxlen=8)
        self.status = "啟動中"
        self.last_update = None
        self.clock = None
        self.close_at = None
        self.open_at = None

    def log(self, text, style=""):
        ts = self.clock.strftime("%H:%M:%S") if self.clock else "--:--:--"
        self.events.append((ts, text, style))
        if not self.args.tui:
            print(f"[{ts}] {text}", flush=True)

    # ---------- TUI ----------
    def render(self):
        a = self.args
        clock = self.clock or datetime.now()
        head = Text()
        head.append(" ORB 開盤區間突破監控 ", style="bold white on blue")
        head.append(f"  {clock:%Y-%m-%d %H:%M:%S}  ", style="bold")
        head.append(self.status, style="bold yellow")
        if self.close_at and self.open_at and self.open_at <= clock < self.close_at:
            left = self.close_at - clock
            head.append(f"  距收盤 {_fmt_td(left)}", style="dim")
        head.append(
            f"\n 區間 {a.open_mins} 分 ｜ 量能 ≥ {a.vol_factor}x ｜ 停損 {'區間另一端' if a.stop_mode == 'range' else '區間中線'}"
            f" ｜ 目標 {a.target_r:g}R ｜ 每 {a.poll_secs} 秒更新"
            + (f" ｜ 上次 {self.last_update:%H:%M:%S}" if self.last_update else ""),
            style="dim",
        )

        t = Table(box=box.SIMPLE_HEAVY, expand=False, header_style="bold cyan", pad_edge=False)
        t.add_column("代號", style="bold", no_wrap=True)
        t.add_column("現價", justify="right", no_wrap=True)
        t.add_column("漲跌", justify="right", no_wrap=True)
        t.add_column("開盤區間", justify="right", no_wrap=True)
        t.add_column("位置", no_wrap=True)
        t.add_column("VWAP", justify="right", no_wrap=True)
        t.add_column("量比", justify="right", no_wrap=True)
        t.add_column("訊號 / 損益", no_wrap=True)
        for i, s in enumerate(self.symbols):
            t.add_row(*self._cells(s), end_section=i < len(self.symbols) - 1)

        ev = Text()
        for i, (ts, text, style) in enumerate(self.events):
            if i:
                ev.append("\n")
            ev.append(f"{ts}  ", style="dim")
            ev.append(text, style=style)
        if not self.events:
            ev.append("（尚無事件）", style="dim")
        return Group(head, t, Panel(ev, title="事件", title_align="left", border_style="dim"))

    def _cells(self, s):
        r = self.rows[s]
        if not r.get("price"):
            return [s, "-", "-", "-", Text("無資料", style="dim"), "-", "-", ""]
        price = r["price"]
        prev = r.get("prev_close")
        if prev:
            chg = (price - prev) / prev * 100
            chg_t = Text(f"{chg:+.2f}%", style=UP if chg > 0 else DOWN if chg < 0 else "")
            price_t = Text(f"{price:.2f}", style=UP if chg > 0 else DOWN if chg < 0 else "")
        else:
            chg_t, price_t = Text("-"), Text(f"{price:.2f}")

        orh, orl = r.get("orh"), r.get("orl")
        if orh is None:
            rng_t = Text("形成中", style="yellow")
            pos_t = Text(f"還剩 {_fmt_td(r['win_left'])}" if r.get("win_left") else "", style="yellow")
        else:
            rng_t = Text(f"H {orh:.2f}\n", style=UP)
            rng_t.append(f"L {orl:.2f}", style=DOWN)
            pos_t = _position_bar(price, orh, orl)

        vwap = r.get("vwap")
        vwap_t = Text("-") if vwap is None else Text(f"{vwap:.2f} {'↑' if price >= vwap else '↓'}", style=UP if price >= vwap else DOWN)
        vr = r.get("vol_ratio")
        vr_t = Text("-") if vr is None else Text(f"{vr:.1f}x", style="bold magenta" if vr >= self.args.vol_factor else "dim")
        return [s, price_t, chg_t, rng_t, pos_t, vwap_t, vr_t, self._signal_cell(r)]

    def _signal_cell(self, r):
        out = Text()
        for sig, res in r.get("signals", []):
            if out:
                out.append("\n")
            long = sig.side == "long"
            out.append(f"{'▲ 多' if long else '▼ 空'} {sig.price:.2f} ", style=f"bold {UP if long else DOWN}")
            pnl = UP if res["r"] > 0 else DOWN if res["r"] < 0 else ""
            if res["reason"] in ("停損", "達標"):
                out.append(f"{res['reason']} {res['r']:+.2f}R", style="bold reverse " + pnl)
            else:
                out.append(f"{res['r']:+.2f}R", style="bold " + pnl)
            out.append(f"\n  {sig.ts:%H:%M} 損 {sig.stop:.2f} 標 {sig.target:.2f}", style="dim")
        return out


def _position_bar(price, orh, orl, width=10):
    if price > orh:
        return Text(f"▲ {(price - orh) / orh * 100:+.2f}%", style=f"bold {UP}")
    if price < orl:
        return Text(f"▼ {(price - orl) / orl * 100:+.2f}%", style=f"bold {DOWN}")
    frac = 0.5 if orh == orl else (price - orl) / (orh - orl)
    i = min(width - 1, max(0, round(frac * (width - 1))))
    t = Text("L", style=DOWN)
    t.append("─" * i, style="dim")
    t.append("●", style="bold yellow")
    t.append("─" * (width - 1 - i), style="dim")
    t.append("H", style=UP)
    return t


def _fmt_td(td):
    secs = max(0, int(td.total_seconds()))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class Monitor:
    def __init__(self, args, symbols, tz, board, fetch=fetch_multi_1m, clock=None, sleep=time.sleep):
        self.args, self.symbols, self.tz, self.board = args, symbols, tz, board
        self.fetch = fetch
        self.clock = clock or (lambda: now_in(tz))
        self.sleep = sleep
        self.mopen = core.parse_hhmm(args.market_open)
        self.mclose = core.parse_hhmm(args.market_close)
        self.sent = {s: set() for s in symbols}
        self.day = None
        self.summarized = None

    def notify(self, title, msg, tags=None, priority=3, click=None, style=""):
        self.board.log(f"{title} ｜ {msg.splitlines()[0]}", style)
        if self.args.notify == "ntfy":
            core.ntfy_publish(self.args.ntfy_topic, title, msg, tags=tags, priority=priority, click=click, server=self.args.ntfy_server)

    def session(self, d):
        return (self.tz.localize(datetime.combine(d, self.mopen)), self.tz.localize(datetime.combine(d, self.mclose)))

    def next_open(self, now):
        d = now.date()
        while True:
            o, _ = self.session(d)
            if d.weekday() < 5 and o > now:
                return o
            d += timedelta(days=1)

    def wait_until(self, target):
        while True:
            now = self.clock()
            self.board.clock = now
            if now >= target:
                return
            self.sleep(min(60, (target - now).total_seconds()))

    def run(self):
        a = self.args
        while True:
            now = self.clock()
            self.board.clock = now
            today = now.date()
            o, c = self.session(today)
            self.board.open_at, self.board.close_at = o, c
            if self.day != today:
                self.day = today
                self.sent = {s: set() for s in self.symbols}
                self.board.rows = {s: {} for s in self.symbols}

            if today.weekday() >= 5 or now >= c + timedelta(minutes=5):
                if today.weekday() < 5 and self.summarized != today and any(self.board.rows[s].get("price") for s in self.symbols):
                    self.poll(now)
                    self.summary(today)
                self.summarized = today
                if a.exit_after_close:
                    self.board.status = "已收盤"
                    return
                nxt = self.next_open(now)
                self.board.status = f"休市，下次開盤 {nxt:%m/%d %H:%M}"
                self.wait_until(nxt - timedelta(minutes=1))
                continue

            if now < o - timedelta(minutes=1):
                self.board.status = f"等待開盤（{_fmt_td(o - now)}）"
                self.wait_until(o - timedelta(minutes=1))
                continue

            got = self.poll(now)
            if not got and now >= o + timedelta(minutes=30):
                self.board.log("開盤 30 分鐘仍沒有今天的行情，判定休市", "yellow")
                self.summarized = today
                if a.exit_after_close:
                    return
                self.wait_until(self.next_open(now) - timedelta(minutes=1))
                continue
            self.board.status = "盤中" if now >= o else "即將開盤"
            self.sleep(a.poll_secs)

    def poll(self, now):
        """抓一次資料、更新盤面、推播新訊號。回傳是否有今天的資料。"""
        try:
            data = self.fetch(self.symbols)
        except Exception as e:
            self.board.log(f"抓資料失敗：{e}", "red")
            return False
        self.board.last_update = now
        got = False
        for sym, df in data.items():
            try:
                got |= self.update_symbol(sym, df, now)
            except Exception as e:
                self.board.log(f"{sym} 處理失敗：{e}", "red")
        return got

    def update_symbol(self, sym, df, now):
        a = self.args
        df.index = df.index.tz_localize(self.tz) if df.index.tz is None else df.index.tz_convert(self.tz)
        today = now.date()
        dates = df.index.date
        day_df = df[dates == today]
        if day_df.empty:
            return False
        prior = df[dates < today]
        row = self.board.rows[sym]
        row["prev_close"] = float(prior["Close"].iloc[-1]) if not prior.empty else None
        row["price"] = float(day_df["Close"].iloc[-1])  # 顯示用最新價（含進行中的 K 棒）

        # 判斷只用已收完的 K 棒
        done = day_df[day_df.index + timedelta(minutes=1) <= now]
        o, _ = self.session(today)
        rng = core.opening_range(done, a.open_mins, self.mopen) if not done.empty else None
        if rng is None:
            row.update(orh=None, orl=None, win_left=o + timedelta(minutes=a.open_mins) - now)
        else:
            if row.get("orh") is None:
                self.board.log(f"{sym} 開盤區間確定 ORH {rng[0]:.2f} / ORL {rng[1]:.2f}", "cyan")
            row.update(orh=rng[0], orl=rng[1], win_left=None)
        if not done.empty:
            ind = core.add_indicators(done)
            last = ind.iloc[-1]
            row["vwap"] = float(last["VWAP"])
            row["vol_ratio"] = float(last["Volume"] / last["VolMA10"]) if last["VolMA10"] > 0 else None

        sigs = core.scan_day(done, sym, a.open_mins, a.vol_factor, self.mopen, a.stop_mode, a.target_r, a.every_bar)
        shown = []
        for sig in sigs:
            res = core.simulate_exit(done, sig)
            if res["reason"] == "收盤":  # 還沒出場：用最新價算未實現損益
                res = core.exit_result(sig, now, row["price"], "持有")
            shown.append((sig, res))
            key = (sig.side, sig.ts)
            if key in self.sent[sym]:
                continue
            self.sent[sym].add(key)
            age = now - (sig.ts + timedelta(minutes=1))
            title, msg, tags = core.format_signal(sig)
            style = "bold " + (UP if sig.side == "long" else DOWN)
            if age <= timedelta(minutes=a.max_signal_age):
                self.notify(title, msg, tags=tags, priority=4, click=core.chart_url(sym), style=style)
            else:
                self.board.log(f"{title}（{sig.ts:%H:%M}，啟動前已發生，不推播）", "dim")
        row["signals"] = shown[-2:] if a.every_bar else shown
        return True

    def summary(self, today):
        lines, total, n = [], 0.0, 0
        for sym in self.symbols:
            for sig, res in self.board.rows[sym].get("signals", []):
                if res["reason"] == "持有":
                    res = dict(res, reason="收盤")
                n += 1
                total += res["r"]
                lines.append(
                    f"{sym} {'多' if sig.side == 'long' else '空'} {sig.price:.2f}→{res['exit_price']:.2f} {res['reason']} {res['r']:+.2f}R（{res['pct']:+.1f}%）"
                )
        if not self.args.summary:
            return
        if n:
            msg = f"合計 {n} 筆，{total:+.2f}R\n" + "\n".join(lines)
        else:
            msg = "今天沒有符合條件的訊號"
        self.notify(f"ORB 收盤總結 {today:%m/%d}", msg, tags=["bar_chart"], priority=2, style="bold")
        for line in lines:
            self.board.log("  " + line)


def demo_feed(tz, files, start_day):
    """把 CSV 平移到今天，依模擬時鐘逐步釋出，模擬盤中行情。"""
    full = {}
    for f in files:
        sym = os.path.splitext(os.path.basename(f))[0]
        d = pd.read_csv(f, encoding="utf-8-sig")
        d = d.rename(columns=str.title)
        d["Datetime"] = pd.to_datetime(d["Datetime"])
        d = d.set_index("Datetime").sort_index()
        d.index = pd.DatetimeIndex([tz.localize(datetime.combine(start_day, t.time())) for t in d.index])
        # 補一根前一天的收盤當作昨收（= 第一根開盤價），讓漲跌欄有值
        prev = d.iloc[[0]].copy()
        prev.index = prev.index - timedelta(days=1)
        prev[["High", "Low", "Close"]] = d["Open"].iloc[0]
        full[sym] = pd.concat([prev, d])
    return full


def build_parser():
    ap = argparse.ArgumentParser(description="ORB 多標的盤中監控（ntfy 通知；yfinance 延遲資料）")
    ap.add_argument("--symbols", type=str, help="逗號分隔代號：2330.TW,2317.TW,0050.TW")
    ap.add_argument("--symbols-file", type=str, help="每行一檔的清單檔（# 開頭為註解）")
    ap.add_argument("--open-mins", type=int, default=15, help="開盤區間分鐘數")
    ap.add_argument("--poll-secs", type=int, default=30, help="抓資料間隔秒數")
    ap.add_argument("--timezone", type=str, default="Asia/Taipei")
    ap.add_argument("--market-open", default="09:00", help="開盤時間 HH:MM")
    ap.add_argument("--market-close", default="13:30", help="收盤時間 HH:MM")
    ap.add_argument("--vol-factor", type=float, default=1.5, help="量能門檻：成交量 >= MA10 × 倍數")
    ap.add_argument("--stop-mode", choices=["range", "mid"], default="range", help="停損放區間另一端或區間中線")
    ap.add_argument("--target-r", type=float, default=2.0, help="目標價 = 進場 ± 風險 × R")
    ap.add_argument("--every-bar", action="store_true", help="每根新 K 棒符合條件都通知（預設每檔每方向每天一次）")
    ap.add_argument("--max-signal-age", type=int, default=5, help="超過幾分鐘的舊訊號不推播（中途啟動時用）")
    ap.add_argument("--notify", default="ntfy", choices=["ntfy", "print"], help="print = 不推播，只顯示在終端機")
    ap.add_argument("--ntfy-topic", default=os.environ.get("NTFY_TOPIC"), help="ntfy topic（也可用環境變數 NTFY_TOPIC）")
    ap.add_argument("--ntfy-server", default="https://ntfy.sh", help="自架 ntfy 伺服器網址")
    ap.add_argument("--no-summary", dest="summary", action="store_false", help="收盤不推總結")
    ap.add_argument("--exit-after-close", action="store_true", help="收盤（或判定休市）後結束程式；排程執行時用")
    ap.add_argument("--no-tui", dest="tui", action="store_false", help="不用即時表格，改成一行一行的 log")
    ap.add_argument("--demo", action="store_true", help="用 data/*.csv 快轉重播一個早盤（不推播）")
    ap.add_argument("--demo-speed", type=float, default=60.0, help="demo 快轉倍數")
    return ap


def main():
    args = build_parser().parse_args()
    tz = pytz.timezone(args.timezone)
    fetch, clock, sleep = fetch_multi_1m, None, time.sleep

    if args.demo:
        files = sorted(glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "*.csv")))
        if not files:
            raise SystemExit("找不到 data/*.csv")
        day = datetime.now(tz).date()
        full = demo_feed(tz, files, day)
        symbols = list(full)
        args.notify, args.exit_after_close, args.max_signal_age = "print", True, 10**6
        args.poll_secs = min(args.poll_secs, 30)
        last_bar = max(d.index[-1] for d in full.values())
        sim = {"t": tz.localize(datetime.combine(day, core.parse_hhmm(args.market_open))) - timedelta(minutes=1)}
        args.market_close = (last_bar + timedelta(minutes=1)).strftime("%H:%M")

        clock = lambda: sim["t"]

        def sleep(secs):
            time.sleep(secs / args.demo_speed)
            sim["t"] += timedelta(seconds=secs)

        def fetch(syms):
            return {s: full[s][full[s].index <= sim["t"]].copy() for s in syms if (full[s].index <= sim["t"]).any()}
    else:
        symbols = []
        if args.symbols:
            symbols += [s.strip() for s in args.symbols.split(",") if s.strip()]
        if args.symbols_file:
            with open(args.symbols_file, "r", encoding="utf-8-sig") as f:
                for line in f:
                    s = line.split("#", 1)[0].strip()
                    if s:
                        symbols.append(s)
        symbols = list(dict.fromkeys(symbols))
        if not symbols:
            raise SystemExit("請用 --symbols 或 --symbols-file 指定標的清單")
        if args.notify == "ntfy" and not args.ntfy_topic:
            raise SystemExit("請提供 --ntfy-topic（或環境變數 NTFY_TOPIC），或用 --notify print 只顯示不推播")

    console = Console() if Console else None
    args.tui = bool(args.tui and console and console.is_terminal)
    board = Board(args, symbols)
    mon = Monitor(args, symbols, tz, board, fetch=fetch, clock=clock, sleep=sleep)
    if not args.tui:
        print(f"Start ORB | N={len(symbols)} | ORB={args.open_mins}m | tz={args.timezone} | volF={args.vol_factor}", flush=True)
        print("Symbols:", ", ".join(symbols), flush=True)

    try:
        if args.tui:
            with Live(console=console, get_renderable=board.render, refresh_per_second=4, screen=False):
                mon.run()
            # 結束後留下最後畫面
        else:
            mon.run()
    except KeyboardInterrupt:
        print("Stopped by user.")


if __name__ == "__main__":
    main()
