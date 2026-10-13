"""
設定檔載入：config.toml（一般設定）+ .env（密鑰）。
優先順序：命令列參數 > config.toml > 程式預設值。密鑰只從環境變數 / .env 讀。
"""

import os

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(ROOT, "config.toml")
DEFAULT_ENV = os.path.join(ROOT, ".env")

SOURCES = {
    "yahoo": "Yahoo Finance（免費；Yahoo 標示台股延遲 20 分鐘）",
    "shioaji": "永豐 Shioaji（即時，需要 API Key）",
    "fake": "假資料（每次隨機產生一天行情，試跑用）",
    "demo": "重播 data/*.csv（固定的示範資料）",
}

# config.toml 的 [區段] 鍵  →  argparse 的參數名稱
KEYMAP = {
    ("data", "source"): "source",
    ("data", "poll_secs"): "poll_secs",
    ("symbols", "list"): "symbols",
    ("symbols", "file"): "symbols_file",
    ("strategy", "open_mins"): "open_mins",
    ("strategy", "vol_factor"): "vol_factor",
    ("strategy", "stop_mode"): "stop_mode",
    ("strategy", "target_r"): "target_r",
    ("strategy", "every_bar"): "every_bar",
    ("strategy", "max_signal_age"): "max_signal_age",
    ("market", "open"): "market_open",
    ("market", "close"): "market_close",
    ("market", "timezone"): "timezone",
    ("notify", "method"): "notify",  # 舊名稱
    ("notify", "channels"): "notify",
    ("notify", "ntfy_topic"): "ntfy_topic",
    ("notify", "ntfy_server"): "ntfy_server",
    ("notify", "summary"): "summary",
    ("shioaji", "simulation"): "shioaji_simulation",
    ("fake", "speed"): "sim_speed",
    ("fake", "seed"): "fake_seed",
}


def load_env(path=DEFAULT_ENV):
    """讀 .env（KEY=VALUE），不覆蓋已存在的環境變數。"""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if v[:1] in ('"', "'") and v[1:].find(v[0]) >= 0:  # 有引號：取引號內
                v = v[1:1 + v[1:].find(v[0])]
            else:  # 沒引號：去掉行尾註解（# 前面要有空白）
                v = (" " + v).split(" #", 1)[0].strip()
            if k and k not in os.environ:
                os.environ[k] = v


def load_config(path=None):
    """回傳可直接給 argparse.set_defaults 的 dict；找不到檔案回傳空 dict。"""
    path = path or DEFAULT_CONFIG
    if not os.path.exists(path):
        return {}
    if tomllib is None:
        raise SystemExit("讀取 config.toml 需要 Python 3.11 以上，或先 pip install tomli")
    with open(path, "rb") as f:
        try:
            raw = tomllib.load(f)
        except Exception as e:
            raise SystemExit(f"config.toml 格式錯誤：{e}")
    out = {}
    for (section, key), dest in KEYMAP.items():
        if key in raw.get(section, {}):
            val = raw[section][key]
            if dest in ("symbols", "notify") and isinstance(val, list):
                val = ",".join(str(s) for s in val) or "print"
            if dest == "symbols" and isinstance(val, list):
                val = ",".join(str(s) for s in val)
            if val == "" or val is None:
                continue
            out[dest] = val
    if out.get("source") and out["source"] not in SOURCES:
        raise SystemExit(f"config.toml 的 data.source 只能是 {', '.join(SOURCES)}，目前是 {out['source']!r}")
    return out


def shioaji_keys():
    return os.environ.get("SHIOAJI_API_KEY", ""), os.environ.get("SHIOAJI_SECRET_KEY", "")


def toml_str(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(toml_str(x) for x in v) + "]"
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_config(c):
    """依設定精靈的結果產生帶註解的 config.toml。"""
    return f"""# orb-ntfy 設定檔（python orb.py init 產生，可以直接編輯）
# 密鑰（ntfy 主題、永豐 API Key）放在 .env，不要寫在這裡。

[data]
# yahoo   = Yahoo Finance，免費；Yahoo 標示台股延遲 20 分鐘
# shioaji = 永豐 Shioaji，即時，需要在 .env 設定 SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY
# fake    = 假資料試跑（每次隨機產生一天行情）
# demo    = 重播 data/*.csv
source = {toml_str(c['source'])}
poll_secs = {toml_str(c['poll_secs'])}        # 幾秒抓一次資料

[symbols]
# 上市加 .TW、上櫃加 .TWO；也可以改用 file = "symbols.txt"
list = {toml_str(c['symbols'])}

[strategy]
open_mins = {toml_str(c['open_mins'])}         # 開盤區間分鐘數
vol_factor = {toml_str(c['vol_factor'])}      # 量能門檻：成交量 >= 最近 10 根（含當根）平均 × 倍數
stop_mode = {toml_str(c['stop_mode'])}   # range = 區間另一端；mid = 區間中線
target_r = {toml_str(c['target_r'])}        # 目標價 = 進場 ± 風險 × R
every_bar = false      # true = 每根符合的 K 棒都通知

[market]
open = "09:00"
close = "13:30"
timezone = "Asia/Taipei"

[notify]
# 可多選：ntfy telegram discord slack line email pushover webhook
# 空的 [] = 只顯示在終端機。各管道的金鑰放在 .env（見 .env.example）
channels = {toml_str(c['channels'])}
ntfy_server = "https://ntfy.sh"
summary = true         # 收盤推總結

[shioaji]
simulation = {toml_str(c['shioaji_simulation'])}     # true = 永豐模擬環境

[fake]
speed = 60             # 假資料快轉倍數；1 = 跟真實時間同步
seed = 0               # 0 = 每次不同；固定數字 = 每次產生一樣的行情
"""
