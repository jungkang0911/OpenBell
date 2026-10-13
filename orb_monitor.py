"""
ORB 多標的盤中監控：yfinance 1 分 K（延遲資料）+ ntfy 推播 + 終端機即時表格。

  python orb.py run                 # 建議：讀 config.toml（先執行 python orb.py init）
  python orb_monitor.py --symbols 2330.TW,2317.TW --notify print
  python orb_monitor.py --demo            # 用 data/*.csv 快轉重播，看畫面用
"""

import argparse
import os
import time
from collections import deque
from datetime import datetime, timedelta

import pytz

import feeds
import notifiers
import orb_config
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


fetch_multi_1m = feeds.fetch_yahoo  # 舊名稱相容


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
    def __init__(self, args, symbols, tz, board, fetch=fetch_multi_1m, clock=None, sleep=time.sleep, channels=None):
        self.args, self.symbols, self.tz, self.board = args, symbols, tz, board
        self.channels = channels or []
        self.fetch = fetch
        self.clock = clock or (lambda: now_in(tz))
        self.sleep = sleep
        self.mopen = core.parse_hhmm(args.market_open)
        self.mclose = core.parse_hhmm(args.market_close)
        self.sent = {s: set() for s in symbols}
        self.attempts = {}
        self.pending = {}
        self.day = None
        self.summarized = None
        self.title_prefix = "[假資料] " if getattr(args, "source", "") == "fake" else ""

    def notify(self, title, msg, tags=None, priority=3, click=None, style="", only=None):
        """
        送到所有（或 only 指定的）通知管道並記到事件欄。回傳送失敗的管道名稱 list，空 list 代表全部送達。
        """
        title = self.title_prefix + title
        retry = only is not None
        self.board.log(f"{title} ｜ {msg.splitlines()[0]}" + ("（重試）" if retry else ""), style)
        failed = []
        for ch in self.channels:
            if only is not None and ch.name not in only:
                continue
            err = ch.send(title, msg, tags=tags, priority=priority, click=click)
            if err:
                failed.append(ch.name)
                self.board.log(f"{ch.name} 送出失敗：{err}", "red")
        return failed

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
                self.attempts = {}
                self.pending = {}
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
            age = now - (sig.ts + timedelta(minutes=1))
            title, msg, tags = core.format_signal(sig)
            style = "bold " + (UP if sig.side == "long" else DOWN)
            if age > timedelta(minutes=a.max_signal_age):
                self.sent[sym].add(key)
                self.board.log(f"{title}（{sig.ts:%H:%M}，啟動前已發生，不推播）", "dim")
            else:
                failed = self.notify(title, msg, tags=tags, priority=4, click=core.chart_url(sym), style=style,
                                     only=self.pending.get((sym, key)))
                if not failed:
                    self.sent[sym].add(key)
                    self.pending.pop((sym, key), None)
                else:  # 只重送失敗的管道，下次輪詢再試，最多 3 次
                    self.pending[(sym, key)] = set(failed)
                    n = self.attempts[(sym, key)] = self.attempts.get((sym, key), 0) + 1
                    if n >= 3:
                        self.sent[sym].add(key)
                        self.board.log(f"{title}：{'、'.join(failed)} 送出失敗 3 次，放棄", "red")
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
    """舊介面相容：指定 CSV 檔平移到 start_day。"""
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        for f in files:
            shutil.copy(f, d)
        return feeds.load_demo_csvs(tz, start_day, d)


def build_parser():
    ap = argparse.ArgumentParser(description="ORB 多標的盤中監控（終端機即時表格 + ntfy 推播）")
    ap.add_argument("--config", help="設定檔路徑（預設讀專案資料夾的 config.toml）")
    ap.add_argument("--source", choices=list(orb_config.SOURCES), default="yahoo",
                    help="資料來源：yahoo / shioaji（永豐）/ fake（假資料）/ demo（重播 data/*.csv）")
    ap.add_argument("--symbols", type=str, help="逗號分隔代號：2330.TW,2317.TW,0050.TW")
    ap.add_argument("--symbols-file", type=str, help="每行一檔的清單檔（# 開頭為註解）")
    ap.add_argument("--open-mins", type=int, default=15, help="開盤區間分鐘數")
    ap.add_argument("--poll-secs", type=int, default=30, help="抓資料間隔秒數")
    ap.add_argument("--timezone", type=str, default="Asia/Taipei")
    ap.add_argument("--market-open", default="09:00", help="開盤時間 HH:MM")
    ap.add_argument("--market-close", default="13:30", help="收盤時間 HH:MM")
    ap.add_argument("--vol-factor", type=float, default=1.5, help="量能門檻：成交量 >= 最近 10 根（含當根）平均 × 倍數")
    ap.add_argument("--stop-mode", choices=["range", "mid"], default="range", help="停損放區間另一端或區間中線")
    ap.add_argument("--target-r", type=float, default=2.0, help="目標價 = 進場 ± 風險 × R")
    ap.add_argument("--every-bar", action="store_true", help="每根新 K 棒符合條件都通知（預設每檔每方向每天一次）")
    ap.add_argument("--max-signal-age", type=int, default=5, help="超過幾分鐘的舊訊號不推播（中途啟動時用）")
    ap.add_argument("--notify", default=None,
                    help="通知管道，逗號分隔：" + ",".join(notifiers.CHANNELS) + "；print = 只顯示在終端機。"
                         "不指定時自動使用 .env 已設定好的管道")
    ap.add_argument("--ntfy-topic", help="ntfy 主題（建議放 .env 的 NTFY_TOPIC）")
    ap.add_argument("--ntfy-server", default="https://ntfy.sh", help="自架 ntfy 伺服器網址")
    ap.add_argument("--ntfy-token", help="ntfy 存取權杖（建議放 .env 的 NTFY_TOKEN）")
    ap.add_argument("--no-summary", dest="summary", action="store_false", help="收盤不推總結")
    ap.add_argument("--exit-after-close", action="store_true", help="收盤（或判定休市）後結束程式；排程執行時用")
    ap.add_argument("--no-tui", dest="tui", action="store_false", help="不用即時表格，改成一行一行的 log")
    ap.add_argument("--shioaji-simulation", action="store_true", help="永豐使用模擬環境")
    ap.add_argument("--demo", action="store_true", help="等同 --source demo：用 data/*.csv 快轉重播一個早盤（不推播）")
    ap.add_argument("--sim-speed", "--demo-speed", dest="sim_speed", type=float, default=60.0,
                    help="fake / demo 的快轉倍數（1 = 跟真實時間同步）")
    ap.add_argument("--fake-seed", type=int, default=0, help="假資料亂數種子（0 = 每次不同）")
    return ap


def parse_args(argv=None):
    """命令列 > config.toml > 預設值；密鑰從環境變數 / .env 讀。"""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config")
    known, _ = pre.parse_known_args(argv)
    orb_config.load_env()
    ap = build_parser()
    ap.set_defaults(**orb_config.load_config(known.config))
    args = ap.parse_args(argv)
    if args.demo:
        args.source = "demo"
    args.ntfy_topic = args.ntfy_topic or os.environ.get("NTFY_TOPIC")
    args.ntfy_token = args.ntfy_token or os.environ.get("NTFY_TOKEN")
    return args


def resolve_symbols(args):
    symbols = []
    if args.symbols:
        symbols += [s.strip() for s in str(args.symbols).split(",") if s.strip()]
    path = args.symbols_file
    if not symbols and not path and os.path.exists(os.path.join(orb_config.ROOT, "symbols.txt")):
        path = os.path.join(orb_config.ROOT, "symbols.txt")
    if path:
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                s = line.split("#", 1)[0].strip()
                if s:
                    symbols.append(s)
    return list(dict.fromkeys(symbols))


def resolve_channels(args):
    if args.source == "demo":
        return []
    names = notifiers.parse_channels(args.notify) if args.notify is not None else notifiers.configured()
    try:
        return notifiers.build(names, ntfy_server=args.ntfy_server, ntfy_topic=args.ntfy_topic,
                               ntfy_token=args.ntfy_token)
    except SystemExit as e:
        if args.source == "fake":  # 試跑時不要因為通知沒設好就停下來
            print(f"{e}\n（假資料試跑改成只顯示在終端機）", flush=True)
            return []
        raise


def build_source(args, tz, symbols):
    """依 args.source 回傳 (symbols, fetch, clock, sleep)；clock 為 None 表示用真實時間。"""
    if args.source == "yahoo":
        return symbols, feeds.fetch_yahoo, None, time.sleep
    if args.source == "shioaji":
        key, secret = orb_config.shioaji_keys()
        feed = feeds.ShioajiFeed(key, secret, tz, simulation=args.shioaji_simulation,
                                 market_open=core.parse_hhmm(args.market_open))
        return symbols, feed.fetch, None, time.sleep

    day = datetime.now(tz).date()
    mopen = core.parse_hhmm(args.market_open)
    if args.source == "demo":
        full = feeds.load_demo_csvs(tz, day)
        if not full:
            raise SystemExit("找不到 data/*.csv")
        symbols = list(full)
        args.max_signal_age = 10**6
        last_bar = max(d.index[-1] for d in full.values())
        args.market_close = (last_bar + timedelta(minutes=1)).strftime("%H:%M")
    else:  # fake
        mclose = core.parse_hhmm(args.market_close)
        full, scen = {}, {}
        for i, s in enumerate(symbols):
            seed = args.fake_seed * 1009 + i if args.fake_seed else None
            full[s], scen[s] = feeds.fake_day(s, day, tz, seed=seed, market_open=mopen, market_close=mclose,
                                              open_mins=args.open_mins)
        args.fake_scenarios = scen
    if args.sim_speed > 1:
        args.exit_after_close = True
        args.poll_secs = min(args.poll_secs, 30)
        start = tz.localize(datetime.combine(day, mopen)) - timedelta(minutes=1)
    else:
        start = datetime.now(tz)
    sim = feeds.SimFeed(full, start, args.sim_speed, time.sleep)
    return symbols, sim.fetch, sim.clock, sim.sleep


def main(argv=None):
    args = parse_args(argv)
    tz = pytz.timezone(args.timezone)
    symbols = resolve_symbols(args)
    if args.source != "demo" and not symbols:
        raise SystemExit("沒有設定股票：請執行 python orb.py init，或用 --symbols / --symbols-file 指定")
    channels = resolve_channels(args)
    symbols, fetch, clock, sleep = build_source(args, tz, symbols)

    console = Console() if Console else None
    args.tui = bool(args.tui and console and console.is_terminal)
    board = Board(args, symbols)
    mon = Monitor(args, symbols, tz, board, fetch=fetch, clock=clock, sleep=sleep, channels=channels)
    board.log("通知管道：" + ("、".join(c.label for c in channels) if channels else "只顯示在終端機"), "dim")
    if not args.tui:
        print(f"Start ORB | source={args.source} | N={len(symbols)} | ORB={args.open_mins}m | volF={args.vol_factor}",
              flush=True)
        print("Symbols:", ", ".join(symbols), flush=True)
    if getattr(args, "fake_scenarios", None):
        board.log("假資料情境：" + "、".join(f"{s} {feeds.SCENARIOS[v]}" for s, v in args.fake_scenarios.items()),
                  "dim")

    try:
        if args.tui:
            with Live(console=console, get_renderable=board.render, refresh_per_second=4, screen=False):
                mon.run()
        else:
            mon.run()
    except KeyboardInterrupt:
        print("Stopped by user.")


if __name__ == "__main__":
    main()
