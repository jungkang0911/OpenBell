import os
import sys
from datetime import datetime, time, timedelta

import pandas as pd
import pytz

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import orb_core as core  # noqa: E402
import orb_monitor as mon  # noqa: E402

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
    m.notify = lambda title, msg, **kw: sent.append((clock["t"].strftime("%H:%M"), title)) or []
    m.run()
    titles = [t for _, t in sent]
    assert sorted(titles[:2]) == ["2317.TW 空頭跌破 98.61", "2330.TW 多頭突破 102.14"]
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


def test_notify_failure_is_retried_then_given_up():
    day = datetime.now(TZ).date()
    full = mon.demo_feed(TZ, [os.path.join(ROOT, "data", "2330.TW.csv")], day)
    args = mon.build_parser().parse_args(["--symbols", "x", "--notify", "print", "--no-tui"])
    args.tui = False
    now = TZ.localize(datetime.combine(day, time(9, 31)))
    m = mon.Monitor(args, list(full), TZ, mon.Board(args, list(full)), fetch=lambda s: {k: v.copy() for k, v in full.items()},
                    clock=lambda: now)
    calls = []

    class Ch:
        def __init__(self, name, ok):
            self.name, self.ok = name, ok

        def send(self, title, msg, **kw):
            calls.append((self.name, title))
            return None if self.ok else "boom"

    m.channels = [Ch("good", True), Ch("bad", False)]
    for _ in range(5):
        m.poll(now)
    t = "2330.TW 多頭突破 102.14"
    # 第一次兩個都送；之後只重送失敗的 bad，第 3 次失敗後放棄
    assert calls == [("good", t), ("bad", t), ("bad", t), ("bad", t)]


def test_config_toml_and_env(tmp_path, monkeypatch):
    import orb_config

    cfg = tmp_path / "config.toml"
    cfg.write_text(orb_config.render_config({
        "source": "fake", "poll_secs": 20, "symbols": ["2330.TW", "6488.TWO"], "open_mins": 30,
        "vol_factor": 2.0, "stop_mode": "mid", "target_r": 1.5, "channels": ["telegram", "discord"],
        "shioaji_simulation": True,
    }), encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text("NTFY_TOPIC=from-env\nSHIOAJI_API_KEY='k'\n", encoding="utf-8")
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.delenv("SHIOAJI_API_KEY", raising=False)
    monkeypatch.setattr(orb_config, "DEFAULT_ENV", str(env))
    orb_config.load_env(str(env))
    args = mon.parse_args(["--config", str(cfg), "--vol-factor", "1.2"])
    assert args.source == "fake" and args.poll_secs == 20 and args.open_mins == 30
    assert args.symbols == "2330.TW,6488.TWO" and args.stop_mode == "mid" and args.shioaji_simulation is True
    assert args.vol_factor == 1.2  # 命令列優先
    assert args.notify == "telegram,discord"
    assert args.ntfy_topic == "from-env" and os.environ["SHIOAJI_API_KEY"] == "k"


def test_fake_scenarios_behave():
    import feeds

    day = datetime(2026, 10, 12).date()
    expect = {"breakout_up": "long", "breakdown": "short", "fake_up": "long", "fake_down": "short"}
    for sc, side in expect.items():
        df, got = feeds.fake_day("2330.TW", day, TZ, seed=1, scenario=sc)
        assert got == sc and df.index[0].date() < day  # 第一根是昨收
        sigs = core.scan_day(df.iloc[1:], "X", vol_factor=1.5)
        assert sigs and sigs[0].side == side
        if sc.startswith("fake"):
            assert core.simulate_exit(df.iloc[1:], sigs[0])["reason"] == "停損"
    df, _ = feeds.fake_day("2330.TW", day, TZ, seed=1, scenario="range")
    assert len(df) == 1 + 271  # 09:00–13:30


def test_shioaji_kbars_conversion_and_feed():
    import feeds
    from types import SimpleNamespace

    # 永豐的 ts：奈秒、數值為台北時間；第一根 09:01 代表 09:00–09:01
    ts = [int(pd.Timestamp(f"2026-10-13 09:{m:02d}").value) for m in range(1, 4)]
    kb = SimpleNamespace(ts=ts, Open=[10, 11, 12], High=[11, 12, 13], Low=[9, 10, 11], Close=[11, 12, 13],
                         Volume=[100, 200, 300], Amount=[0, 0, 0])
    df = feeds.kbars_to_df(kb, TZ)
    assert [t.strftime("%H:%M") for t in df.index] == ["09:00", "09:01", "09:02"]
    assert str(df.index.tz) == "Asia/Taipei" and df["Close"].tolist() == [11, 12, 13]

    class FakeApi:
        Contracts = SimpleNamespace(Stocks={"2330": SimpleNamespace(code="2330")})

        def kbars(self, contract, start, end):
            return kb

        def snapshots(self, contracts):
            return [SimpleNamespace(close=13.0, change_price=1.5)]

    feed = feeds.ShioajiFeed(None, None, TZ, api=FakeApi())
    out = feed.fetch(["2330.TW"])["2330.TW"]
    assert out["Close"].iloc[0] == 11.5 and out.index[0].date() < out.index[1].date()  # 昨收 = 13 - 1.5


def test_cli_helpers(tmp_path):
    import orb

    assert orb.normalize_symbols("2330, 2317.tw，6488.two  0050 2330") == ["2330.TW", "2317.TW", "6488.TWO", "0050.TW"]
    env = tmp_path / ".env"
    env.write_text("# keep\nNTFY_TOPIC=old\nOTHER=1\n", encoding="utf-8")
    orb.write_env(str(env), {"NTFY_TOPIC": "new", "NTFY_TOKEN": None, "SHIOAJI_API_KEY": "k"})
    assert env.read_text(encoding="utf-8").splitlines() == ["# keep", "NTFY_TOPIC=new", "OTHER=1", "SHIOAJI_API_KEY=k"]
    assert (env.stat().st_mode & 0o777) == 0o600


def test_notifier_payloads(monkeypatch):
    import notifiers

    posts = []

    class Resp:
        status_code, text = 200, "ok"

    monkeypatch.setattr(notifiers.requests, "post", lambda url, **kw: posts.append((url, kw)) or Resp())
    args = ("2330.TW 多頭突破 102.14", "做多 102.14（09:30）\n停損 98.80")
    kw = dict(tags=["red_circle"], priority=4, click="https://example.com/chart")

    cases = {
        "ntfy": {"NTFY_TOPIC": "t", "NTFY_TOKEN": "tk", "server": "https://ntfy.example"},
        "telegram": {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "42"},
        "discord": {"DISCORD_WEBHOOK_URL": "https://discord.example/hook"},
        "slack": {"SLACK_WEBHOOK_URL": "https://hooks.slack.example/x"},
        "line": {"LINE_CHANNEL_ACCESS_TOKEN": "L", "LINE_TO": "U1"},
        "pushover": {"PUSHOVER_TOKEN": "a", "PUSHOVER_USER": "u"},
        "webhook": {"WEBHOOK_URL": "https://hook.example"},
    }
    for name, cfg in cases.items():
        assert notifiers.CHANNELS[name](**cfg).send(*args, **kw) is None, name
    got = dict(zip(cases, posts))
    url, k = got["ntfy"]
    assert url == "https://ntfy.example/" and k["json"]["title"] == args[0] and k["headers"]["Authorization"] == "Bearer tk"
    url, k = got["telegram"]
    assert url == "https://api.telegram.org/bot123:abc/sendMessage" and k["json"]["chat_id"] == "42"
    assert "停損" in k["json"]["text"] and "example.com/chart" in k["json"]["text"]
    assert got["discord"][1]["json"]["embeds"][0]["color"] == 0xE74C3C  # 多 = 紅
    assert got["slack"][1]["json"]["text"].startswith("*2330.TW")
    url, k = got["line"]
    assert url.endswith("/v2/bot/message/push") and k["json"]["to"] == "U1" and k["headers"]["Authorization"] == "Bearer L"
    assert got["pushover"][1]["data"]["priority"] == 1
    assert got["webhook"][1]["json"]["click"] == "https://example.com/chart"


def test_notifier_errors_and_email(monkeypatch):
    import notifiers

    class Bad:
        status_code, text = 401, "unauthorized"

    monkeypatch.setattr(notifiers.requests, "post", lambda url, **kw: Bad())
    err = notifiers.Slack(SLACK_WEBHOOK_URL="x").send("t", "m")
    assert "401" in err and "金鑰" in err

    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent["server"] = (host, port)

        def starttls(self):
            sent["tls"] = True

        def login(self, u, p):
            sent["login"] = u

        def send_message(self, msg):
            sent["msg"] = msg

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(notifiers.smtplib, "SMTP", FakeSMTP)
    e = notifiers.Email(SMTP_HOST="smtp.example", SMTP_USER="me@x", SMTP_PASSWORD="p", EMAIL_TO="a@x,b@x")
    assert e.send("2330.TW 多頭突破", "內容", click="https://c") is None
    assert sent["server"] == ("smtp.example", 587) and sent["tls"] and sent["msg"]["To"].replace(" ", "") == "a@x,b@x"
    assert "https://c" in sent["msg"].get_content()


def test_channel_parsing_and_missing_config(monkeypatch):
    import notifiers
    import pytest

    assert notifiers.parse_channels("ntfy, Telegram") == ["ntfy", "telegram"]
    assert notifiers.parse_channels("print") == [] and notifiers.parse_channels(["discord"]) == ["discord"]
    with pytest.raises(SystemExit):
        notifiers.parse_channels("fax")
    for k in ("DISCORD_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SystemExit, match="TELEGRAM_BOT_TOKEN"):
        notifiers.build(["telegram"])
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://d")
    assert "discord" in notifiers.configured()


def test_errors_do_not_leak_secrets(monkeypatch):
    import notifiers

    def boom(url, **kw):
        raise ConnectionError(f"cannot reach {url}")

    monkeypatch.setattr(notifiers.requests, "post", boom)
    err = notifiers.Telegram(TELEGRAM_BOT_TOKEN="123456:SECRET", TELEGRAM_CHAT_ID="1").send("t", "m")
    assert "SECRET" not in err and "***" in err
    err = notifiers.Discord(DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/1/tok").send("t", "m")
    assert "tok" not in err


def test_env_inline_comments(tmp_path, monkeypatch):
    import orb_config

    env = tmp_path / ".env"
    env.write_text("A_TOKEN=          # 選填\nB_PORT=587  # 註解\nC_URL=https://x/#frag\nD_Q=\"has # hash\"  # c\n#E=1\n",
                   encoding="utf-8")
    for k in ("A_TOKEN", "B_PORT", "C_URL", "D_Q", "E"):
        monkeypatch.delenv(k, raising=False)
    orb_config.load_env(str(env))
    assert os.environ["A_TOKEN"] == "" and os.environ["B_PORT"] == "587"
    assert os.environ["C_URL"] == "https://x/#frag" and os.environ["D_Q"] == "has # hash" and "E" not in os.environ
