#!/usr/bin/env python3
"""
ORB 回測：用和盤中監控完全相同的訊號邏輯，看最近一段時間如果照通知操作會怎樣。

  # 用 Yahoo Finance 最近 30 天 1 分 K（Yahoo 只提供最近 30 天的 1 分 K）
  python backtest.py --symbols 2330.TW,2317.TW,2454.TW

  # 比較不同量能門檻
  python backtest.py --symbols-file symbols.txt --vol-factor 1.0,1.5,2.0

  # 用自己的 CSV
  python backtest.py --csv data/2330.TW.csv

出場規則：先碰到停損或目標就出場（同一根 K 棒兩者都碰到算停損），都沒碰到就收盤平倉。
不含手續費、證交稅與滑價。
"""

import argparse
import os
from datetime import datetime, timedelta

import pandas as pd
import pytz

import orb_core as core


def load_yahoo(symbols, days, tz):
    import yfinance as yf

    end = datetime.now(tz).date() + timedelta(days=1)
    start = end - timedelta(days=min(days, 29))
    frames = {s: [] for s in symbols}
    cur = start
    while cur < end:  # Yahoo 1 分 K 每次最多 8 天
        nxt = min(cur + timedelta(days=7), end)
        df = yf.download(" ".join(symbols), start=cur, end=nxt, interval="1m", progress=False,
                         group_by="ticker", auto_adjust=False, threads=True)
        for s in symbols:
            try:
                d = df[s] if isinstance(df.columns, pd.MultiIndex) else df
            except KeyError:
                continue
            d = d.rename(columns=str.title).dropna(how="all")
            if not d.empty:
                frames[s].append(d)
        cur = nxt
    out = {}
    for s, parts in frames.items():
        if parts:
            d = pd.concat(parts)
            d = d[~d.index.duplicated()].sort_index()
            d.index = d.index.tz_localize(tz) if d.index.tz is None else d.index.tz_convert(tz)
            out[s] = d
    return out


def load_csv(paths, tz):
    out = {}
    for p in paths:
        d = pd.read_csv(p, encoding="utf-8-sig").rename(columns=str.title)
        d["Datetime"] = pd.to_datetime(d["Datetime"])
        d = d.set_index("Datetime").sort_index()
        d.index = d.index.tz_localize(tz) if d.index.tz is None else d.index.tz_convert(tz)
        out[os.path.splitext(os.path.basename(p))[0]] = d
    return out


def run(data, open_mins, vol_factor, mopen, stop_mode, target_r):
    trades = []
    for sym, df in data.items():
        for day, ddf in df.groupby(df.index.date):
            for sig in core.scan_day(ddf, sym, open_mins, vol_factor, mopen, stop_mode, target_r):
                res = core.simulate_exit(ddf, sig)
                trades.append({
                    "date": day, "symbol": sym, "side": "多" if sig.side == "long" else "空",
                    "entry_time": sig.ts.strftime("%H:%M"), "entry": sig.price, "stop": sig.stop,
                    "target": sig.target, "exit_time": res["exit_ts"].strftime("%H:%M"),
                    "exit": res["exit_price"], "reason": res["reason"], "r": res["r"], "pct": res["pct"],
                })
    return pd.DataFrame(trades)


def stats(t):
    if t.empty:
        return {"筆數": 0}
    wins = t[t["r"] > 0]
    return {
        "筆數": len(t),
        "勝率": f"{len(wins) / len(t) * 100:.0f}%",
        "平均R": f"{t['r'].mean():+.2f}",
        "累計R": f"{t['r'].sum():+.1f}",
        "平均報酬": f"{t['pct'].mean():+.2f}%",
        "達標": int((t["reason"] == "達標").sum()),
        "停損": int((t["reason"] == "停損").sum()),
        "收盤": int((t["reason"] == "收盤").sum()),
    }


def main():
    ap = argparse.ArgumentParser(description="ORB 回測（與盤中監控相同邏輯）")
    ap.add_argument("--symbols", help="逗號分隔代號")
    ap.add_argument("--symbols-file", help="每行一檔的清單檔（# 開頭為註解）")
    ap.add_argument("--csv", nargs="+", help="改用 CSV（檔名當代號）")
    ap.add_argument("--days", type=int, default=30, help="Yahoo 回測天數（上限約 30）")
    ap.add_argument("--timezone", default="Asia/Taipei")
    ap.add_argument("--market-open", default="09:00")
    ap.add_argument("--open-mins", type=int, default=15)
    ap.add_argument("--vol-factor", default="1.5", help="可給多個比較，例如 1.0,1.5,2.0")
    ap.add_argument("--stop-mode", choices=["range", "mid"], default="range")
    ap.add_argument("--target-r", type=float, default=2.0)
    ap.add_argument("--trades", action="store_true", help="列出每筆交易")
    ap.add_argument("--out", help="把交易明細存成 CSV")
    args = ap.parse_args()

    tz = pytz.timezone(args.timezone)
    if args.csv:
        data = load_csv(args.csv, tz)
    else:
        symbols = []
        if args.symbols:
            symbols += [s.strip() for s in args.symbols.split(",") if s.strip()]
        if args.symbols_file:
            with open(args.symbols_file, encoding="utf-8-sig") as f:
                symbols += [ln.split("#", 1)[0].strip() for ln in f if ln.split("#", 1)[0].strip()]
        symbols = list(dict.fromkeys(symbols))
        if not symbols:
            raise SystemExit("請用 --symbols / --symbols-file / --csv 指定資料")
        print(f"下載 {len(symbols)} 檔最近 {args.days} 天 1 分 K…")
        data = load_yahoo(symbols, args.days, tz)
    if not data:
        raise SystemExit("沒有資料")
    days = sorted({d for df in data.values() for d in df.index.date})
    print(f"資料：{len(data)} 檔，{len(days)} 個交易日（{days[0]} ~ {days[-1]}）")
    print(f"設定：區間 {args.open_mins} 分｜停損 {args.stop_mode}｜目標 {args.target_r:g}R｜不含交易成本\n")

    mopen = core.parse_hhmm(args.market_open)
    vfs = [float(v) for v in args.vol_factor.split(",")]
    summary, last = [], None
    for vf in vfs:
        t = run(data, args.open_mins, vf, mopen, args.stop_mode, args.target_r)
        summary.append({"量能門檻": f"{vf:g}x", **stats(t)})
        last = t

    pd.set_option("display.width", 200)
    pd.set_option("display.unicode.east_asian_width", True)
    print(pd.DataFrame(summary).fillna("-").to_string(index=False))

    if last is not None and not last.empty:
        print(f"\n各檔表現（量能門檻 {vfs[-1]:g}x）：")
        per = pd.DataFrame([{"代號": s, **stats(g)} for s, g in last.groupby("symbol")])
        print(per.to_string(index=False))
        if args.trades:
            print()
            print(last.round({"entry": 2, "stop": 2, "target": 2, "exit": 2, "r": 2, "pct": 2}).to_string(index=False))
        if args.out:
            last.to_csv(args.out, index=False, encoding="utf-8-sig")
            print(f"\n交易明細已存到 {args.out}")


if __name__ == "__main__":
    main()
