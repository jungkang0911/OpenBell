#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
replay_min.py
模擬開盤重播 1 分鐘 K CSV，符合 ORB（開盤區間突破）條件時發送 ntfy 通知。
用來確認手機收得到通知、通知長什麼樣子。想看即時表格請用 orb_multi_yf_ntfy.py --demo。
"""

import argparse
import time

import pandas as pd

import orb_core as core


def main():
    ap = argparse.ArgumentParser(description="重播 1m CSV 模擬開盤，符合 ORB 條件即推 ntfy 通知")
    ap.add_argument("--csv", required=True, help="CSV 檔路徑 (含 Datetime,OHLCV 欄位)")
    ap.add_argument("--symbol", default="SYMBOL", help="股票代號顯示名稱")
    ap.add_argument("--open-mins", type=int, default=15, help="開盤區間分鐘數")
    ap.add_argument("--market-open", default="09:00", help="開盤時間 HH:MM")
    ap.add_argument("--vol-factor", type=float, default=1.0, help="量能門檻 (成交量 >= MA10*factor)")
    ap.add_argument("--stop-mode", choices=["range", "mid"], default="range")
    ap.add_argument("--target-r", type=float, default=2.0)
    ap.add_argument("--speed", type=float, default=0.5, help="每分鐘K播放間隔秒數")
    ap.add_argument("--notify", default="ntfy", choices=["ntfy", "print"], help="print = 只印在終端機，不推播")
    ap.add_argument("--ntfy-topic", help="ntfy 主題，例如 Chailease")
    ap.add_argument("--ntfy-server", default="https://ntfy.sh")
    ap.add_argument("--every-bar", action="store_true", help="每根新 K 棒符合條件都通知（預設每方向只通知一次）")
    args = ap.parse_args()
    if args.notify == "ntfy" and not args.ntfy_topic:
        raise SystemExit("請提供 --ntfy-topic，或用 --notify print 只印在終端機")

    df = pd.read_csv(args.csv, encoding="utf-8-sig").rename(columns=str.title)
    for col in ["Datetime", "Open", "High", "Low", "Close", "Volume"]:
        if col not in df.columns:
            raise SystemExit(f"CSV 缺少欄位: {col}")
    df["Datetime"] = pd.to_datetime(df["Datetime"])
    df = df.set_index("Datetime").sort_index()
    mopen = core.parse_hhmm(args.market_open)

    print(f"▶ Replaying {args.symbol} | ORB={args.open_mins}m | speed={args.speed}s/min")
    sent = set()
    rng_shown = False
    for i in range(1, len(df) + 1):
        done = df.iloc[:i]
        if not rng_shown:
            rng = core.opening_range(done, args.open_mins, mopen)
            if rng:
                print(f"  開盤區間 ORH={rng[0]:.2f} ORL={rng[1]:.2f}")
                rng_shown = True
        for sig in core.scan_day(done, args.symbol, args.open_mins, args.vol_factor, mopen,
                                 args.stop_mode, args.target_r, args.every_bar):
            if (sig.side, sig.ts) in sent:
                continue
            sent.add((sig.side, sig.ts))
            title, msg, tags = core.format_signal(sig)
            print(f"🔔 {title}\n   " + msg.replace("\n", "\n   "))
            if args.notify == "ntfy":
                core.ntfy_publish(args.ntfy_topic, title, msg, tags=tags, priority=4,
                                  click=core.chart_url(args.symbol), server=args.ntfy_server)
        time.sleep(max(0.0, args.speed))


if __name__ == "__main__":
    main()
