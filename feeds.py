"""
行情資料來源。每個來源都提供 fetch(symbols) -> {symbol: DataFrame}：
DataFrame 為 1 分 K（Open/High/Low/Close/Volume），index 是 K 棒「開始」時間（含時區），
並且至少帶一根前一交易日的 K 棒，讓監控程式拿最後一根當昨收。

  yahoo    Yahoo Finance（yfinance），免費；Yahoo 標示台股延遲 20 分鐘
  shioaji  永豐 Shioaji API，需要 API Key
  fake     隨機產生一天行情（突破、假突破、盤整…），試跑用
  demo     重播 data/*.csv
"""

import glob
import os
import zlib
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd


# ---------------------------------------------------------------- Yahoo
def fetch_yahoo(symbols):
    """抓最近兩天的 1 分 K（多抓一天是為了拿昨收算漲跌）。"""
    import yfinance as yf

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


# ---------------------------------------------------------------- 永豐 Shioaji
def shioaji_code(symbol):
    """2330.TW / 2330.TWO / 2330 → 2330"""
    return symbol.split(".")[0].strip()


def kbars_to_df(kbars, tz, market_open=time(9, 0)):
    """
    把 Shioaji 的 KBars 轉成 DataFrame。
    - ts 是奈秒整數、數值上就是台北時間（不是 UTC），直接 localize。
    - 如果每天第一根是 09:01，代表時間戳標的是該分鐘「結束」，
      就整體往前移 1 分鐘，統一成 K 棒開始時間（與 Yahoo、CSV 一致）。
    """
    cols = ["Open", "High", "Low", "Close", "Volume"]
    data = {c: list(getattr(kbars, c)) for c in ["ts"] + cols}
    df = pd.DataFrame(data)
    if df.empty:
        return pd.DataFrame(columns=cols)
    idx = pd.to_datetime(df.pop("ts"))
    df.index = pd.DatetimeIndex(idx).tz_localize(tz)
    df = df.sort_index()
    first_by_day = df.groupby(df.index.date).head(1).index
    open_plus_1 = (datetime.combine(date.min, market_open) + timedelta(minutes=1)).time()
    if len(first_by_day) and all(t.time() == open_plus_1 for t in first_by_day):
        df.index = df.index - timedelta(minutes=1)
    return df[cols].astype(float)


class ShioajiFeed:
    """
    每次輪詢用 api.kbars() 抓當天 1 分 K。昨收由 snapshot 的 close - change_price 推回，每天只查一次。
    永豐有行情查詢次數限制，監控檔數多時請拉長 poll_secs。
    """

    def __init__(self, api_key, secret_key, tz, simulation=False, market_open=time(9, 0), api=None):
        self.tz, self.market_open = tz, market_open
        if api is None:
            if not api_key or not secret_key:
                raise SystemExit("使用永豐資料需要在 .env 設定 SHIOAJI_API_KEY 與 SHIOAJI_SECRET_KEY（可執行 python orb.py init）")
            try:
                import shioaji as sj
            except ImportError:
                raise SystemExit("使用永豐資料請先安裝：pip install shioaji")
            api = sj.Shioaji(simulation=simulation)
            api.login(api_key=api_key, secret_key=secret_key, subscribe_trade=False)
        self.api = api
        self._contracts = {}
        self._prev_close = {}

    def contract(self, symbol):
        if symbol not in self._contracts:
            c = self.api.Contracts.Stocks[shioaji_code(symbol)]
            if c is None:
                raise ValueError(f"永豐找不到代號 {shioaji_code(symbol)}")
            self._contracts[symbol] = c
        return self._contracts[symbol]

    def prev_close(self, symbol, day):
        key = (symbol, day)
        if key not in self._prev_close:
            snap = self.api.snapshots([self.contract(symbol)])[0]
            self._prev_close[key] = float(snap.close) - float(snap.change_price)
        return self._prev_close[key]

    def fetch(self, symbols):
        today = datetime.now(self.tz).date()
        out = {}
        for sym in symbols:
            kb = self.api.kbars(self.contract(sym), start=today.isoformat(), end=today.isoformat())
            df = kbars_to_df(kb, self.tz, self.market_open)
            if df.empty:
                continue
            out[sym] = with_prev_close(df, self.prev_close(sym, today))
        return out


def with_prev_close(df, prev_close):
    """在最前面補一根前一天的 K 棒（OHLC 都是昨收、量 0）。"""
    prev_ts = df.index[0] - timedelta(days=1)
    prev = pd.DataFrame({c: [prev_close] for c in ["Open", "High", "Low", "Close"]} | {"Volume": [0.0]},
                        index=pd.DatetimeIndex([prev_ts]))
    return pd.concat([prev, df])


# ---------------------------------------------------------------- 假資料
SCENARIOS = {
    "breakout_up": "帶量突破後續漲",
    "breakdown": "帶量跌破後續跌",
    "fake_up": "假突破（突破後跌回）",
    "fake_down": "假跌破（跌破後彈回）",
    "range": "整天在區間內盤整",
}


def fake_day(symbol, day, tz, seed=None, scenario=None, market_open=time(9, 0), market_close=time(13, 30),
             open_mins=15):
    """
    產生一個交易日的合成 1 分 K（含前一天收盤那根）。只用來測試流程，不代表任何真實行情。
    回傳 (DataFrame, scenario)。
    """
    rng = np.random.default_rng(seed)
    scenario = scenario or rng.choice(list(SCENARIOS), p=[0.25, 0.25, 0.15, 0.15, 0.2])
    start = tz.localize(datetime.combine(day, market_open))
    n = int((datetime.combine(day, market_close) - datetime.combine(day, market_open)).total_seconds() // 60) + 1
    idx = pd.date_range(start, periods=n, freq="min")

    base = 20 + zlib.crc32(symbol.encode()) % 980  # 每檔固定的價位
    prev_close = round(base * (1 + rng.normal(0, 0.01)), 2)
    center = prev_close * (1 + rng.normal(0, 0.004))
    band = center * rng.uniform(0.004, 0.009)  # 開盤區間半寬
    t_break = open_mins + int(rng.integers(5, 100))
    sign = 1 if scenario in ("breakout_up", "fake_up") else -1

    tgt = np.empty(n)
    phase = rng.uniform(0, 6.28)
    for i in range(n):
        if i < open_mins:
            tgt[i] = center + band * 0.8 * np.sin(i / 2.5 + phase)
        elif scenario == "range" or i < t_break:
            tgt[i] = center + band * 0.5 * np.sin(i / 4 + phase)
        elif scenario in ("breakout_up", "breakdown"):
            tgt[i] = center + sign * band * min(1.5 + 0.06 * (i - t_break), 6)
        else:  # 假突破：撐幾根後反向穿過區間另一端
            k = i - t_break
            tgt[i] = center + sign * band * (1.5 if k < 6 else max(1.5 - 0.12 * (k - 6), -1.8))

    close = np.empty(n)
    x = center
    for i in range(n):
        x = x + 0.35 * (tgt[i] - x) + rng.normal(0, band * 0.1)
        if i < open_mins:
            x = min(max(x, center - band), center + band)
        elif (scenario == "range" or i < t_break):
            x = min(max(x, center - band * 0.9), center + band * 0.9)
        if i == t_break and scenario != "range":
            x = center + sign * band * 1.6  # 突破那根確實收在區間外
        close[i] = x

    opens = np.concatenate([[center], close[:-1]])
    wick = np.abs(rng.normal(0, band * 0.12, size=(2, n)))
    high = np.maximum(opens, close) + wick[0]
    low = np.minimum(opens, close) - wick[1]
    in_or = np.arange(n) < open_mins
    high[in_or] = np.minimum(high[in_or], center + band * 1.05)
    low[in_or] = np.maximum(low[in_or], center - band * 1.05)

    u = np.linspace(0, 1, n)
    vol_base = rng.integers(300, 3000)
    vol = vol_base * (0.6 + 1.6 * (1 - u) ** 6 + 0.8 * u ** 6) * rng.lognormal(0, 0.3, n)
    if scenario != "range":
        vol[t_break] *= rng.uniform(4, 7)

    df = pd.DataFrame({"Open": opens, "High": high, "Low": low, "Close": close, "Volume": vol.round()},
                      index=idx).round({"Open": 2, "High": 2, "Low": 2, "Close": 2})
    return with_prev_close(df, prev_close), str(scenario)


def fake_days(symbols, days, tz, seed=0, **kw):
    """回測用：最近 days 個工作日的假資料。"""
    out = {s: [] for s in symbols}
    d = datetime.now(tz).date()
    picked = []
    while len(picked) < days:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            picked.append(d)
    for j, day in enumerate(sorted(picked)):
        for k, s in enumerate(symbols):
            df, _ = fake_day(s, day, tz, seed=None if not seed else seed * 100003 + j * 101 + k, **kw)
            out[s].append(df.iloc[1:])
    return {s: pd.concat(parts) for s, parts in out.items()}


# ---------------------------------------------------------------- 模擬時鐘
class SimFeed:
    """把整天的資料依模擬時鐘逐步釋出，模擬盤中輪詢。"""

    def __init__(self, full, start, speed, real_sleep):
        self.full, self.t, self.speed, self.real_sleep = full, start, speed, real_sleep

    def clock(self):
        return self.t

    def sleep(self, secs):
        if self.speed > 0:
            self.real_sleep(secs / self.speed)
        self.t += timedelta(seconds=secs)

    def fetch(self, symbols):
        return {s: self.full[s][self.full[s].index <= self.t].copy() for s in symbols if s in self.full}


def load_demo_csvs(tz, day, folder=None):
    """data/*.csv 平移到指定日期，並補一根昨收（= 第一根開盤價）。"""
    folder = folder or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    full = {}
    for f in sorted(glob.glob(os.path.join(folder, "*.csv"))):
        sym = os.path.splitext(os.path.basename(f))[0]
        d = pd.read_csv(f, encoding="utf-8-sig").rename(columns=str.title)
        d["Datetime"] = pd.to_datetime(d["Datetime"])
        d = d.set_index("Datetime").sort_index()
        d.index = pd.DatetimeIndex([tz.localize(datetime.combine(day, t.time())) for t in d.index])
        full[sym] = with_prev_close(d, float(d["Open"].iloc[0]))
    return full
