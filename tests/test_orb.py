import os
import sys
from datetime import datetime, time, timedelta

import pandas as pd
import pytz

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import orb_core as core  # noqa: E402
import orb_multi_yf_ntfy as mon  # noqa: E402

TZ = pytz.timezone("Asia/Taipei")


def load(sym):
    d = pd.read_csv(os.path.join(ROOT, "data", f"{sym}.csv"), encoding="utf-8-sig", parse_dates=["Datetime"])
    return d.set_index("Datetime")


def bars(rows, day="2025-09-30"):
    """rows: list of (HH:MM, open, high, low, close, volume)"""
    idx = pd.DatetimeIndex([pd.Timestamp(f"{day} {r[0]}") for r in rows])
    return pd.DataFrame([r[1:] for r in rows], index=idx, columns=["Open", "High", "Low", "Close", "Volume"])


def test_sample_data_signals():
    s = core.scan_day(load("2330.TW"), "2330.TW", vol_factor=1.5)
    assert [(x.side, x.ts.strftime("%H:%M"), round(x.price, 2)) for x in s] == [("long", "09:30", 102.14)]
    s = core.scan_day(load("2317.TW"), "2317.TW", vol_factor=1.5)
    assert [(x.side, x.ts.strftime("%H:%M")) for x in s] == [("short", "09:30")]


def test_opening_range_waits_for_full_window():
    df = load("2330.TW")
    assert core.opening_range(df.iloc[:14], 15, time(9, 0)) is None  # 09:00–09:13
    orh, orl, win_end = core.opening_range(df.iloc[:15], 15, time(9, 0))  # 09:14 收完
    assert win_end == pd.Timestamp("2025-09-30 09:15")
    assert orh == df.iloc[:15]["High"].max() and orl == df.iloc[:15]["Low"].min()


def test_once_per_direction_vs_every_bar():
    df = load("2330.TW")
    once = core.scan_day(df, "X", vol_factor=1.0)
    every = core.scan_day(df, "X", vol_factor=1.0, every_bar=True)
    assert len(once) == 1 and len(every) > 1


def test_stop_and_target():
    rows = [(f"09:{m:02d}", 100, 101, 99, 100, 100) for m in range(15)]
    rows += [("09:15", 100, 102, 100, 102, 1000)]  # 帶量突破 ORH=101
    df = bars(rows)
    (sig,) = core.scan_day(df, "X", vol_factor=1.5)
    assert sig.side == "long" and sig.stop == 99 and sig.target == 102 + 2 * 3
    assert core.scan_day(df, "X", vol_factor=1.5, stop_mode="mid")[0].stop == 100

    hit_target = bars(rows + [("09:16", 102, 108.5, 101, 108, 100)])
    assert core.simulate_exit(hit_target, sig)["reason"] == "達標"
    hit_stop = bars(rows + [("09:16", 102, 103, 98.5, 99, 100)])
    r = core.simulate_exit(hit_stop, sig)
    assert r["reason"] == "停損" and r["r"] == -1
    both = bars(rows + [("09:16", 102, 109, 98, 100, 100)])
    assert core.simulate_exit(both, sig)["reason"] == "停損"  # 同根都碰到算停損


def test_monitor_full_day_simulation():
    """模擬時鐘跑一個早盤：只推播一次、跳過進行中的 K 棒、收盤推總結後結束。"""
    day = datetime.now(TZ).date()
    files = [os.path.join(ROOT, "data", f) for f in ("2330.TW.csv", "2317.TW.csv")]
    full = mon.demo_feed(TZ, files, day)
    args = mon.build_parser().parse_args(["--symbols", "x", "--notify", "print", "--no-tui",
                                          "--exit-after-close", "--market-close", "10:30"])
    args.tui = False
    clock = {"t": TZ.localize(datetime.combine(day, time(8, 50)))}

    def sleep(secs):
        clock["t"] += timedelta(seconds=secs)

    def fetch(syms):
        return {s: full[s][full[s].index <= clock["t"]].copy() for s in syms}

    board = mon.Board(args, list(full))
    m = mon.Monitor(args, list(full), TZ, board, fetch=fetch, clock=lambda: clock["t"], sleep=sleep)
    sent = []
    m.notify = lambda title, msg, **kw: sent.append((clock["t"].strftime("%H:%M"), title))
    m.run()
    titles = [t for _, t in sent]
    assert titles[:2] == ["2330.TW 多頭突破 102.14", "2317.TW 空頭跌破 98.61"]
    assert all(ts >= "09:31" for ts, _ in sent[:2])  # 09:30 那根收完才判斷
    assert titles[2].startswith("ORB 收盤總結") and len(sent) == 3


def test_monitor_ignores_previous_session():
    """開盤前 yfinance 回傳的是前一天的資料，不能拿來判斷或推播。"""
    day = datetime.now(TZ).date()
    files = [os.path.join(ROOT, "data", "2330.TW.csv")]
    yesterday = mon.demo_feed(TZ, files, day - timedelta(days=1))
    args = mon.build_parser().parse_args(["--symbols", "x", "--notify", "print", "--no-tui"])
    args.tui = False
    now = TZ.localize(datetime.combine(day, time(9, 5)))
    board = mon.Board(args, list(yesterday))
    m = mon.Monitor(args, list(yesterday), TZ, board, fetch=lambda s: {k: v.copy() for k, v in yesterday.items()},
                    clock=lambda: now)
    assert m.poll(now) is False
    assert not board.rows["2330.TW"].get("signals")


def test_backtest_fake_breakout_hits_stop():
    import backtest

    data = backtest.load_csv([os.path.join(ROOT, "data", f"{s}.csv") for s in ("2330.TW", "2317.TW", "2454.TW")], TZ)
    t = backtest.run(data, 15, 1.5, time(9, 0), "range", 2.0)
    got = {r.symbol: (r.side, r.entry_time, r.reason) for r in t.itertuples()}
    assert got == {"2330.TW": ("多", "09:30", "收盤"), "2317.TW": ("空", "09:30", "收盤"), "2454.TW": ("多", "09:41", "停損")}
    assert t.loc[t.symbol == "2454.TW", "r"].iloc[0] == -1
