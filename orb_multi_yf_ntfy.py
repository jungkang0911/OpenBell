import time
import argparse
import pandas as pd
import numpy as np
import yfinance as yf
import pytz
import requests
from datetime import datetime, timedelta

def ntfy_send(topic: str, title: str, msg: str):
    url = f"https://ntfy.sh/{topic}"
    data = f"{title}\n{msg}".encode("utf-8")
    try:
        r = requests.post(url, data=data, timeout=5)
        if r.status_code >= 300:
            print(f"[WARN] ntfy 回應碼：{r.status_code}")
    except Exception as e:
        print(f"[WARN] ntfy 失敗：{e}")

def compute_vwap(df: pd.DataFrame) -> pd.Series:
    tp = (df["High"] + df["Low"] + df["Close"]) / 3.0
    cum_vol = df["Volume"].cumsum().replace(0, np.nan)
    return (tp * df["Volume"]).cumsum() / cum_vol

def fetch_multi_1m(symbols):
    df = yf.download(
        tickers=" ".join(symbols),
        period="1d",
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

def fmt_alert(sym, side, ts, price, orh, orl, vwap):
    return f"{sym} {side.upper()} @ {price:.2f} | {ts} | ORH={orh:.2f} ORL={orl:.2f} VWAP={vwap:.2f}"

def new_state():
    return {"day": None, "orh": None, "orl": None, "win_end": None,
            "last_long": None, "last_short": None}

def main():
    ap = argparse.ArgumentParser(description="ORB 多標的掃描（ntfy 通知版；yfinance 延遲資料）")
    ap.add_argument("--symbols", type=str, help="逗號分隔代號：2330.TW,2317.TW,0050.TW")
    ap.add_argument("--symbols-file", type=str, help="每行一檔的清單檔")
    ap.add_argument("--open-mins", type=int, default=15)
    ap.add_argument("--poll-secs", type=int, default=30)
    ap.add_argument("--timezone", type=str, default="Asia/Taipei")
    ap.add_argument("--vol-factor", type=float, default=1.5)
    ap.add_argument("--notify", type=str, default="ntfy", choices=["ntfy","print"])
    ap.add_argument("--ntfy-topic", type=str, default=None, help="ntfy topic，例如 Chailease 對應 https://ntfy.sh/Chailease")
    ap.add_argument("--every-bar", action="store_true",
                    help="每根新 K 棒符合條件都通知（預設每檔每方向每天只通知一次）")
    args = ap.parse_args()

    symbols = []
    if args.symbols:
        symbols += [s.strip() for s in args.symbols.split(",") if s.strip()]
    if args.symbols_file:
        with open(args.symbols_file, "r", encoding="utf-8-sig") as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith("#"):
                    symbols.append(s)
    symbols = list(dict.fromkeys(symbols))
    if not symbols:
        raise SystemExit("請用 --symbols 或 --symbols-file 指定標的清單")

    if args.notify == "ntfy" and not args.ntfy_topic:
        raise SystemExit("請提供 --ntfy-topic，例如 Chailease（https://ntfy.sh/Chailease）")

    tz = pytz.timezone(args.timezone)
    state = {s: new_state() for s in symbols}

    def notify(title, msg):
        if args.notify == "ntfy":
            ntfy_send(args.ntfy_topic, title, msg)
        print(f"[{title}]", msg)

    print(f"Start ORB multi (ntfy) | N={len(symbols)} | ORB={args.open_mins}m | tz={args.timezone} | volF={args.vol_factor}")
    print("Symbols:", ", ".join(symbols))

    while True:
        try:
            data_map = fetch_multi_1m(symbols)
            now = datetime.now(tz)

            for sym, df in data_map.items():
                try:
                    if df.index.tz is None:
                        df.index = df.index.tz_localize(tz)
                    else:
                        df.index = df.index.tz_convert(tz)

                    # period="1d" 在開盤前會回傳前一個交易日的資料，不能拿來判斷
                    day = df.index[-1].date()
                    if day != now.date():
                        continue
                    df = df[df.index.date == day].copy()

                    st = state[sym]
                    if st["day"] != day:
                        state[sym] = st = new_state()
                        st["day"] = day

                    # 只看已收完的 K 棒；最後一根通常還在進行中，成交量不完整
                    df = df[df.index + timedelta(minutes=1) <= now]
                    if df.empty:
                        continue

                    df["VWAP"] = compute_vwap(df)
                    df["VolMA10"] = df["Volume"].rolling(10, min_periods=1).mean()

                    if st["win_end"] is None:
                        win_end = df.index[0] + timedelta(minutes=args.open_mins)
                        # 開盤區間必須完整走完才定 ORH/ORL
                        if df.index[-1] + timedelta(minutes=1) < win_end:
                            continue
                        win = df[df.index < win_end]
                        st["win_end"] = win_end
                        st["orh"] = float(win["High"].max())
                        st["orl"] = float(win["Low"].min())
                        print(f"{sym} ORB 區間確定 | ORH={st['orh']:.2f} ORL={st['orl']:.2f}")

                    after = df[df.index >= st["win_end"]]
                    if after.empty:
                        continue

                    last = after.iloc[-1]
                    ts = after.index[-1]
                    price = float(last["Close"])
                    vwap = float(last["VWAP"])
                    vol = 0.0 if pd.isna(last["Volume"]) else float(last["Volume"])
                    volma = 0.0 if pd.isna(last["VolMA10"]) else float(last["VolMA10"])
                    orh, orl = st["orh"], st["orl"]

                    bar_id = str(ts.floor("min"))
                    vol_ok = volma > 0 and vol >= volma * args.vol_factor
                    long_break = (price > orh) and vol_ok and (price >= vwap)
                    short_break = (price < orl) and vol_ok and (price <= vwap)

                    if long_break and st["last_long"] != bar_id and (args.every_bar or st["last_long"] is None):
                        notify("ORB 多頭突破", fmt_alert(sym, "long", ts, price, orh, orl, vwap))
                        st["last_long"] = bar_id

                    if short_break and st["last_short"] != bar_id and (args.every_bar or st["last_short"] is None):
                        notify("ORB 空頭跌破", fmt_alert(sym, "short", ts, price, orh, orl, vwap))
                        st["last_short"] = bar_id

                except Exception as e:
                    print(f"[ERROR] {sym}: {e}")

            time.sleep(args.poll_secs)

        except KeyboardInterrupt:
            print("Stopped by user.")
            break
        except Exception as e:
            print(f"[ERROR] {e}")
            time.sleep(args.poll_secs)

if __name__ == "__main__":
    main()
