#!/usr/bin/env python3
"""
orb-ntfy 主入口。

  python orb.py init        互動設定精靈（資料來源、永豐金鑰、股票、推播）
  python orb.py check       檢查設定、資料來源、推播是否正常
  python orb.py fake        用假資料完整試跑一次（快轉，含推播）
  python orb.py demo        重播 data/*.csv（快轉，不推播）
  python orb.py run         盤中監控（讀 config.toml）
  python orb.py backtest    回測（參數同 backtest.py）

run / fake / demo 後面可以接任何 orb_multi_yf_ntfy.py 的參數，例如：
  python orb.py run --vol-factor 2 --notify print
"""

import os
import secrets
import sys
from datetime import datetime

import orb_config

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Confirm, FloatPrompt, IntPrompt, Prompt
    from rich.table import Table
except ImportError:  # pragma: no cover
    raise SystemExit("請先安裝套件：pip install -r requirements.txt")

console = Console()


# ---------------------------------------------------------------- 小工具
def normalize_symbols(text):
    """'2330, 2317.TW 6488.two' → ['2330.TW', '2317.TW', '6488.TWO']；純數字預設上市 .TW。"""
    out = []
    for raw in text.replace("，", ",").replace(" ", ",").split(","):
        s = raw.strip().upper()
        if not s:
            continue
        if "." not in s:
            s += ".TW"
        out.append(s)
    return list(dict.fromkeys(out))


def write_env(path, values):
    """更新 .env 裡的指定鍵，保留其他內容；權限設成只有自己能讀。"""
    lines, seen = [], set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f.read().splitlines():
                key = line.split("=", 1)[0].strip()
                if key in values:
                    if values[key] is not None:
                        lines.append(f"{key}={values[key]}")
                    seen.add(key)
                else:
                    lines.append(line)
    for k, v in values.items():
        if k not in seen and v is not None:
            lines.append(f"{k}={v}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def test_shioaji_login(key, secret, simulation):
    try:
        import shioaji as sj
    except ImportError:
        return "還沒安裝 shioaji（pip install shioaji）"
    try:
        api = sj.Shioaji(simulation=simulation)
        accounts = api.login(api_key=key, secret_key=secret, subscribe_trade=False)
        c = api.Contracts.Stocks["2330"]
        name = getattr(c, "name", "2330")
        api.logout()
        return f"OK：登入成功（{len(accounts)} 個帳戶），查得到 2330 {name}"
    except Exception as e:
        return f"失敗：{e}"


def send_test(topic, server, token, prefix="測試通知"):
    import orb_core as core

    return core.ntfy_publish(topic, f"orb-ntfy {prefix}", f"收到這則就代表推播設定正確 ✅\n{datetime.now():%Y-%m-%d %H:%M:%S}",
                             tags=["white_check_mark"], server=server, token=token)


# ---------------------------------------------------------------- init
def cmd_init(argv):
    cfg_path, env_path = orb_config.DEFAULT_CONFIG, orb_config.DEFAULT_ENV
    orb_config.load_env()
    console.print(Panel.fit("[bold]orb-ntfy 設定精靈[/bold]\n一步一步設定資料來源、股票清單和手機推播。\n"
                            "一般設定寫進 [cyan]config.toml[/cyan]，密鑰寫進 [cyan].env[/cyan]（不會被 git 追蹤）。\n"
                            "隨時按 Ctrl+C 離開，不會寫入任何檔案。", border_style="blue"))
    if os.path.exists(cfg_path) and not Confirm.ask("已經有 config.toml，要覆蓋嗎？", default=False):
        console.print("沒有變更。")
        return
    env = {}

    # 1. 資料來源
    console.print("\n[bold cyan]① 行情資料來源[/bold cyan]")
    keys = list(orb_config.SOURCES)
    for i, k in enumerate(keys, 1):
        console.print(f"  {i}. [bold]{k}[/bold]  {orb_config.SOURCES[k]}")
    source = keys[IntPrompt.ask("選擇", choices=[str(i) for i in range(1, len(keys) + 1)], default=1) - 1]
    simulation = False

    if source == "shioaji":
        console.print("\n[bold cyan]② 永豐 Shioaji 設定[/bold cyan]\n"
                      "  到永豐金證券網站「API 管理」申請 API Key 與 Secret Key（只看行情不需要憑證 CA）。\n"
                      "  金鑰只會存在這台電腦的 .env。")
        old_key, old_secret = orb_config.shioaji_keys()
        key = Prompt.ask("  API Key", **({"default": old_key, "show_default": False} if old_key else {}))
        secret = Prompt.ask("  Secret Key" + ("（直接 Enter 沿用目前的）" if old_secret else ""), password=True,
                            **({"default": old_secret, "show_default": False} if old_secret else {}))
        simulation = Confirm.ask("  使用模擬環境？（正式行情選 n）", default=False)
        env.update(SHIOAJI_API_KEY=key, SHIOAJI_SECRET_KEY=secret)
        if Confirm.ask("  現在測試登入？", default=True):
            with console.status("登入中…"):
                console.print("  " + test_shioaji_login(key, secret, simulation))

    # 3. 股票
    console.print("\n[bold cyan]③ 監控的股票[/bold cyan]  用逗號或空白分隔；純數字視為上市（.TW），上櫃請加 .TWO")
    symbols = normalize_symbols(Prompt.ask("  股票", default="2330, 2317, 2454"))
    console.print("  → " + "、".join(symbols))

    # 4. 推播
    console.print("\n[bold cyan]④ 手機推播（ntfy）[/bold cyan]\n"
                  "  手機安裝 ntfy App，訂閱同一個主題就會收到通知。\n"
                  "  [yellow]注意：ntfy.sh 上沒有設定存取控制的主題，任何知道名稱的人都能讀寫，"
                  "請用猜不到的名稱，也不要推送帳戶或持股資訊。[/yellow]")
    use_ntfy = Confirm.ask("  要推播到手機嗎？（n = 只顯示在終端機）", default=True)
    notify = "ntfy" if use_ntfy else "print"
    if use_ntfy:
        topic = Prompt.ask("  主題名稱", default=os.environ.get("NTFY_TOPIC") or f"orb-{secrets.token_urlsafe(9)}")
        server = Prompt.ask("  ntfy 伺服器", default="https://ntfy.sh")
        token = Prompt.ask("  存取權杖（有設定存取控制才需要，沒有就直接 Enter）", password=True,
                           default=os.environ.get("NTFY_TOKEN") or "", show_default=False) or None
        env.update(NTFY_TOPIC=topic, NTFY_TOKEN=token)
        console.print(f"  手機 App 訂閱：[bold]{topic}[/bold]（瀏覽器也可以開 {server.rstrip('/')}/{topic}）")
        if Confirm.ask("  現在送一則測試通知？", default=True):
            err = send_test(topic, server, token)
            console.print("  ✓ 已送出，請看手機" if not err else f"  [red]送出失敗：{err}[/red]")

    # 5. 策略
    console.print("\n[bold cyan]⑤ 策略參數[/bold cyan]  開盤區間 15 分、量能 1.5 倍、停損在區間另一端、目標 2R")
    p = {"open_mins": 15, "vol_factor": 1.5, "stop_mode": "range", "target_r": 2.0}
    if not Confirm.ask("  使用預設值？", default=True):
        p["open_mins"] = IntPrompt.ask("  開盤區間分鐘數", default=15)
        p["vol_factor"] = FloatPrompt.ask("  量能門檻倍數", default=1.5)
        p["stop_mode"] = Prompt.ask("  停損位置（range=區間另一端 / mid=區間中線）", choices=["range", "mid"], default="range")
        p["target_r"] = FloatPrompt.ask("  目標 R 倍數", default=2.0)

    poll = 30
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(orb_config.render_config({"source": source, "poll_secs": poll, "symbols": symbols, "notify": notify,
                                          "shioaji_simulation": simulation, **p}))
    if env:
        write_env(env_path, env)
    console.print(Panel.fit(
        f"已寫入 [cyan]{os.path.relpath(cfg_path)}[/cyan]" + (f" 與 [cyan]{os.path.relpath(env_path)}[/cyan]" if env else "") +
        "\n\n接下來：\n"
        "  [bold]python orb.py fake[/bold]    用假資料完整試跑（約 2 分鐘，會推播 [假資料] 通知）\n"
        "  [bold]python orb.py check[/bold]   檢查資料來源與推播\n"
        "  [bold]python orb.py run[/bold]     開始盤中監控", title="完成", border_style="green"))


# ---------------------------------------------------------------- check
def cmd_check(argv):
    import pytz

    import orb_multi_yf_ntfy as mon

    args = mon.parse_args(argv)
    ok = True
    t = Table(title="目前設定", show_header=False, box=None)
    has_cfg = os.path.exists(args.config or orb_config.DEFAULT_CONFIG)
    t.add_row("設定檔", (args.config or "config.toml") if has_cfg else "[yellow]沒有（使用預設值，可執行 python orb.py init）[/yellow]")
    t.add_row("資料來源", f"{args.source}　{orb_config.SOURCES[args.source]}")
    symbols = mon.resolve_symbols(args)
    t.add_row("股票", "、".join(symbols) or "[red]沒有設定[/red]")
    t.add_row("策略", f"區間 {args.open_mins} 分｜量能 {args.vol_factor}x｜停損 {args.stop_mode}｜目標 {args.target_r:g}R")
    topic = args.ntfy_topic
    t.add_row("推播", "只顯示在終端機" if args.notify == "print" else
              f"ntfy　主題 {topic[:4] + '…' if topic else '[red]沒有設定 NTFY_TOPIC[/red]'}"
              f"　{args.ntfy_server}" + ("　（有權杖）" if args.ntfy_token else ""))
    console.print(t)
    if not symbols and args.source != "demo":
        ok = False

    console.print("\n[bold]資料來源測試[/bold]")
    tz = pytz.timezone(args.timezone)
    try:
        if args.source == "yahoo":
            with console.status("向 Yahoo 抓資料…"):
                data = mon.feeds.fetch_yahoo(symbols[:3])
            for s in symbols[:3]:
                d = data.get(s)
                if d is None or d.empty:
                    console.print(f"  [yellow]{s}：沒有資料（代號錯誤、上櫃忘了 .TWO，或 Yahoo 暫時擋掉）[/yellow]")
                    ok = False
                else:
                    last = d.index[-1].tz_convert(tz) if d.index.tz else d.index[-1]
                    console.print(f"  ✓ {s}：最新一根 {last:%Y-%m-%d %H:%M}，收 {d['Close'].iloc[-1]:.2f}")
            console.print("  [dim]Yahoo 標示台股延遲 20 分鐘；上面的時間是 K 棒時間，不是你收到資料的時間。[/dim]")
        elif args.source == "shioaji":
            key, secret = orb_config.shioaji_keys()
            with console.status("登入永豐…"):
                feed = mon.feeds.ShioajiFeed(key, secret, tz, simulation=args.shioaji_simulation)
                data = feed.fetch(symbols[:3])
            console.print("  ✓ 登入成功")
            for s in symbols[:3]:
                d = data.get(s)
                if d is None or len(d) <= 1:
                    console.print(f"  [yellow]{s}：今天還沒有 1 分 K（盤前或休市是正常的）[/yellow]")
                else:
                    console.print(f"  ✓ {s}：最新一根 {d.index[-1]:%H:%M}，收 {d['Close'].iloc[-1]:.2f}，昨收 {d['Close'].iloc[0]:.2f}")
        else:
            console.print(f"  ✓ {args.source} 不需要連線")
    except SystemExit as e:
        console.print(f"  [red]{e}[/red]")
        ok = False
    except Exception as e:
        console.print(f"  [red]失敗：{e}[/red]")
        ok = False

    if args.notify == "ntfy" and topic:
        console.print("\n[bold]推播測試[/bold]")
        if Confirm.ask("  送一則測試通知到手機？", default=True):
            err = send_test(topic, args.ntfy_server, args.ntfy_token)
            console.print("  ✓ 已送出（請確認手機有收到）" if not err else f"  [red]失敗：{err}[/red]")
            ok = ok and not err
    elif args.notify == "ntfy":
        ok = False

    console.print("\n[bold green]檢查完成，可以執行 python orb.py run[/bold green]" if ok else
                  "\n[bold yellow]有項目需要處理，見上方說明[/bold yellow]")
    return 0 if ok else 1


# ---------------------------------------------------------------- dispatch
def main():
    argv = sys.argv[1:]
    cmd = argv[0] if argv and not argv[0].startswith("-") else "help"
    rest = argv[1:]
    if cmd == "init":
        return cmd_init(rest)
    if cmd == "check":
        return cmd_check(rest)
    if cmd in ("run", "fake", "demo"):
        import orb_multi_yf_ntfy as mon

        extra = [] if cmd == "run" else ["--source", cmd]
        return mon.main(extra + rest)
    if cmd == "backtest":
        import backtest

        sys.argv = ["backtest.py"] + rest
        return backtest.main()
    console.print(__doc__)
    return 0 if cmd == "help" else 2


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        console.print("\n已取消。")
        sys.exit(130)
