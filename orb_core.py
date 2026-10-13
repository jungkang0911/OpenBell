"""
ORB（開盤區間突破）共用邏輯：指標、訊號、出場模擬、ntfy 推播。
orb_multi_yf_ntfy.py（盤中監控）、replay_min.py（重播）、backtest.py（回測）共用同一套判斷。
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta

import numpy as np
import pandas as pd
import requests


@dataclass
class Signal:
    symbol: str
    side: str  # "long" / "short"
    ts: pd.Timestamp  # 訊號 K 棒的開始時間
    price: float  # 進場價（訊號 K 棒收盤）
    orh: float
    orl: float
    vwap: float
    vol_ratio: float  # 成交量 / MA10
    stop: float
    target: float

    @property
    def risk(self) -> float:
        return abs(self.price - self.stop)


def parse_hhmm(s: str) -> time:
    return datetime.strptime(s, "%H:%M").time()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    tp = (df["High"] + df["Low"] + df["Close"]) / 3.0
    cum_vol = df["Volume"].cumsum().replace(0, np.nan)
    df["VWAP"] = (tp * df["Volume"]).cumsum() / cum_vol
    df["VolMA10"] = df["Volume"].rolling(10, min_periods=1).mean()
    return df


def opening_range(df: pd.DataFrame, open_mins: int, market_open: time):
    """回傳 (orh, orl, win_end)；開盤區間還沒走完或沒有資料則回傳 None。df 須為單日 1 分 K。"""
    if df.empty:
        return None
    start = df.index[0].replace(hour=market_open.hour, minute=market_open.minute, second=0, microsecond=0)
    win_end = start + timedelta(minutes=open_mins)
    if df.index[-1] + timedelta(minutes=1) < win_end:
        return None
    win = df[(df.index >= start) & (df.index < win_end)]
    if win.empty:
        return None
    return float(win["High"].max()), float(win["Low"].min()), win_end


def scan_day(
    df: pd.DataFrame,
    symbol: str,
    open_mins: int = 15,
    vol_factor: float = 1.5,
    market_open: time = time(9, 0),
    stop_mode: str = "range",
    target_r: float = 2.0,
    every_bar: bool = False,
) -> list:
    """
    掃描單日已收完的 1 分 K，回傳 Signal 清單。
    預設每個方向只取當天第一個訊號；every_bar=True 則每根符合條件的 K 棒都算。
    """
    rng = opening_range(df, open_mins, market_open)
    if rng is None:
        return []
    orh, orl, win_end = rng
    mid = (orh + orl) / 2.0
    df = add_indicators(df)
    after = df[df.index >= win_end]

    signals = []
    seen = set()
    for ts, row in after.iterrows():
        price = float(row["Close"])
        vwap = float(row["VWAP"])
        vol = 0.0 if pd.isna(row["Volume"]) else float(row["Volume"])
        volma = 0.0 if pd.isna(row["VolMA10"]) else float(row["VolMA10"])
        if not (volma > 0 and vol >= volma * vol_factor):
            continue

        if price > orh and price >= vwap:
            side, stop = "long", (orl if stop_mode == "range" else mid)
        elif price < orl and price <= vwap:
            side, stop = "short", (orh if stop_mode == "range" else mid)
        else:
            continue
        if side in seen and not every_bar:
            continue
        seen.add(side)

        risk = abs(price - stop)
        target = price + target_r * risk if side == "long" else price - target_r * risk
        signals.append(Signal(symbol, side, ts, price, orh, orl, vwap, vol / volma, stop, target))
    return signals


def simulate_exit(df: pd.DataFrame, sig: Signal) -> dict:
    """
    從訊號之後的 K 棒模擬出場：先碰停損或目標就出場，都沒碰到就以最後一根收盤出場。
    同一根 K 棒同時碰到停損和目標時保守地算停損。
    """
    after = df[df.index > sig.ts]
    for ts, row in after.iterrows():
        if sig.side == "long":
            if row["Low"] <= sig.stop:
                return exit_result(sig, ts, sig.stop, "停損")
            if row["High"] >= sig.target:
                return exit_result(sig, ts, sig.target, "達標")
        else:
            if row["High"] >= sig.stop:
                return exit_result(sig, ts, sig.stop, "停損")
            if row["Low"] <= sig.target:
                return exit_result(sig, ts, sig.target, "達標")
    if after.empty:
        return exit_result(sig, sig.ts, sig.price, "收盤")
    return exit_result(sig, after.index[-1], float(after["Close"].iloc[-1]), "收盤")


def exit_result(sig: Signal, ts, price: float, reason: str) -> dict:
    direction = 1 if sig.side == "long" else -1
    r = (price - sig.price) * direction / sig.risk if sig.risk > 0 else 0.0
    pct = (price - sig.price) * direction / sig.price * 100
    return {"exit_ts": ts, "exit_price": float(price), "reason": reason, "r": r, "pct": pct}


def chart_url(symbol: str) -> str:
    return f"https://tw.stock.yahoo.com/quote/{symbol}/technical-analysis"


def format_signal(sig: Signal):
    """回傳 (title, message, tags)。"""
    long = sig.side == "long"
    stop_pct = (sig.stop - sig.price) / sig.price * 100
    tgt_pct = (sig.target - sig.price) / sig.price * 100
    r_mult = abs(sig.target - sig.price) / sig.risk if sig.risk > 0 else 0
    title = f"{sig.symbol} {'多頭突破' if long else '空頭跌破'} {sig.price:.2f}"
    msg = (
        f"{'做多' if long else '做空'} {sig.price:.2f}（{sig.ts:%H:%M}）\n"
        f"停損 {sig.stop:.2f}（{stop_pct:+.1f}%）\n"
        f"目標 {sig.target:.2f}（{r_mult:.0f}R，{tgt_pct:+.1f}%）\n"
        f"ORH {sig.orh:.2f} / ORL {sig.orl:.2f} ｜ VWAP {sig.vwap:.2f} ｜ 量 {sig.vol_ratio:.1f}x"
    )
    # 台股習慣：紅＝多、綠＝空
    return title, msg, ["red_circle" if long else "green_circle", "chart_with_upwards_trend" if long else "chart_with_downwards_trend"]


def ntfy_publish(topic, title, message, tags=None, priority=3, click=None, server="https://ntfy.sh", token=None):
    """
    用 JSON 發佈（標題可以放中文）。成功回傳 None，失敗回傳錯誤訊息。
    token：有設定存取控制的 topic 用的權杖（ntfy 的 access token，tk_ 開頭）。
    """
    payload = {"topic": topic, "title": title, "message": message, "priority": priority}
    if tags:
        payload["tags"] = tags
    if click:
        payload["click"] = click
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        r = requests.post(server.rstrip("/") + "/", json=payload, headers=headers, timeout=10)
    except Exception as e:
        return f"連線失敗：{e}"
    if r.status_code >= 300:
        hint = "（權杖錯誤或沒有這個 topic 的寫入權限）" if r.status_code in (401, 403) else ""
        return f"HTTP {r.status_code}{hint} {r.text[:200]}"
    return None
