"""
通知管道。每個管道的 send() 成功回傳 None、失敗回傳錯誤訊息。
密鑰一律從環境變數（.env）讀，config.toml 只寫要開哪些管道。

  ntfy      NTFY_TOPIC、NTFY_TOKEN（選填）；伺服器在 config 的 notify.ntfy_server
  telegram  TELEGRAM_BOT_TOKEN、TELEGRAM_CHAT_ID
  discord   DISCORD_WEBHOOK_URL
  slack     SLACK_WEBHOOK_URL
  line      LINE_CHANNEL_ACCESS_TOKEN、LINE_TO（Messaging API；LINE Notify 已於 2025-03-31 停止服務）
  email     SMTP_HOST、SMTP_PORT、SMTP_USER、SMTP_PASSWORD、EMAIL_TO、EMAIL_FROM（選填）
  pushover  PUSHOVER_TOKEN、PUSHOVER_USER
  webhook   WEBHOOK_URL（POST JSON：title、message、tags、priority、click、sent_at）
"""

import os
import smtplib
from datetime import datetime
from email.message import EmailMessage

import requests

TIMEOUT = 10


class Notifier:
    name = ""
    label = ""
    env = ()  # (變數名稱, 說明, 是否必填, 是否為密碼)
    help = ""

    def __init__(self, **cfg):
        self.cfg = cfg

    @classmethod
    def from_env(cls, **extra):
        cfg = {k: os.environ.get(k, "") for k, *_ in cls.env}
        cfg.update({k: v for k, v in extra.items() if v})
        return cls(**cfg)

    def missing(self):
        return [k for k, _, required, _ in self.env if required and not self.cfg.get(k)]

    def send(self, title, message, tags=None, priority=3, click=None):
        raise NotImplementedError

    # 共用小工具
    def redact(self, text):
        """錯誤訊息裡的密鑰（例如 Telegram 網址裡的 token、Webhook 網址）換成 ***。"""
        text = str(text)
        for var, _, _, secret in self.env:
            v = self.cfg.get(var)
            if secret and v and len(v) >= 4:
                text = text.replace(v, "***")
        return text

    def _post(self, url, **kw):
        try:
            r = requests.post(url, timeout=TIMEOUT, **kw)
        except Exception as e:
            return self.redact(f"連線失敗：{e}")
        if r.status_code >= 300:
            hint = "（金鑰錯誤或沒有權限）" if r.status_code in (401, 403) else ""
            return self.redact(f"HTTP {r.status_code}{hint} {r.text[:200]}")
        return None

    @staticmethod
    def _text(title, message, click):
        return f"{title}\n{message}" + (f"\n{click}" if click else "")


class Ntfy(Notifier):
    name, label = "ntfy", "ntfy（免費手機推播 App）"
    env = (("NTFY_TOPIC", "主題名稱", True, False), ("NTFY_TOKEN", "存取權杖（有存取控制才需要）", False, True))
    help = "手機安裝 ntfy App 並訂閱同一個主題。ntfy.sh 上沒設存取控制的主題任何人都能讀寫，請用難猜的名稱。"

    def send(self, title, message, tags=None, priority=3, click=None):
        payload = {"topic": self.cfg["NTFY_TOPIC"], "title": title, "message": message, "priority": priority}
        if tags:
            payload["tags"] = tags
        if click:
            payload["click"] = click
        token = self.cfg.get("NTFY_TOKEN")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        server = self.cfg.get("server") or "https://ntfy.sh"
        return self._post(server.rstrip("/") + "/", json=payload, headers=headers)


class Telegram(Notifier):
    name, label = "telegram", "Telegram Bot"
    env = (("TELEGRAM_BOT_TOKEN", "Bot Token（找 @BotFather 建立 bot）", True, True),
           ("TELEGRAM_CHAT_ID", "Chat ID（設定精靈可以自動抓）", True, False))
    help = "在 Telegram 找 @BotFather 輸入 /newbot 取得 Bot Token，然後先傳一則訊息給你的 bot。"

    def send(self, title, message, tags=None, priority=3, click=None):
        url = f"https://api.telegram.org/bot{self.cfg['TELEGRAM_BOT_TOKEN']}/sendMessage"
        body = {"chat_id": self.cfg["TELEGRAM_CHAT_ID"], "text": self._text(title, message, click),
                "disable_web_page_preview": True, "disable_notification": priority <= 2}
        return self._post(url, json=body)

    @staticmethod
    def find_chat_ids(token):
        """回傳最近傳訊息給 bot 的 [(chat_id, 名稱)]。"""
        try:
            r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=TIMEOUT)
        except Exception as e:
            raise RuntimeError(f"連線失敗（{type(e).__name__}）") from None
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}（Bot Token 錯誤？）")
        seen = {}
        for u in r.json().get("result", []):
            chat = (u.get("message") or u.get("channel_post") or {}).get("chat")
            if chat:
                seen[str(chat["id"])] = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
        return list(seen.items())


class Discord(Notifier):
    name, label = "discord", "Discord（頻道 Webhook）"
    env = (("DISCORD_WEBHOOK_URL", "Webhook 網址", True, True),)
    help = "Discord 頻道 → 編輯頻道 → 整合 → Webhook → 新 Webhook → 複製網址。"

    def send(self, title, message, tags=None, priority=3, click=None):
        tags = tags or []
        color = 0xE74C3C if "red_circle" in tags else 0x2ECC71 if "green_circle" in tags else 0x3498DB
        embed = {"title": title, "description": message, "color": color}
        if click:
            embed["url"] = click
        return self._post(self.cfg["DISCORD_WEBHOOK_URL"], json={"embeds": [embed]})


class Slack(Notifier):
    name, label = "slack", "Slack（Incoming Webhook）"
    env = (("SLACK_WEBHOOK_URL", "Incoming Webhook 網址", True, True),)
    help = "在 Slack App 設定開啟 Incoming Webhooks，選頻道後複製 https://hooks.slack.com/... 網址。"

    def send(self, title, message, tags=None, priority=3, click=None):
        text = f"*{title}*\n{message}" + (f"\n<{click}|開啟線圖>" if click else "")
        return self._post(self.cfg["SLACK_WEBHOOK_URL"], json={"text": text})


class Line(Notifier):
    name, label = "line", "LINE（官方帳號 Messaging API）"
    env = (("LINE_CHANNEL_ACCESS_TOKEN", "Channel access token", True, True),
           ("LINE_TO", "接收者的 userId 或 groupId", True, False))
    help = ("LINE Notify 已停止服務。請到 LINE Developers 建立 Messaging API channel，"
            "發行 Channel access token；自己的 userId 在 Basic settings 頁面最下方。免費方案每月有訊息則數上限。")

    def send(self, title, message, tags=None, priority=3, click=None):
        headers = {"Authorization": f"Bearer {self.cfg['LINE_CHANNEL_ACCESS_TOKEN']}"}
        body = {"to": self.cfg["LINE_TO"], "messages": [{"type": "text", "text": self._text(title, message, click)[:5000]}]}
        return self._post("https://api.line.me/v2/bot/message/push", json=body, headers=headers)


class Email(Notifier):
    name, label = "email", "Email（SMTP，例如 Gmail）"
    env = (("SMTP_HOST", "SMTP 伺服器，例如 smtp.gmail.com", True, False),
           ("SMTP_PORT", "連接埠（587 = STARTTLS、465 = SSL）", False, False),
           ("SMTP_USER", "登入帳號", True, False),
           ("SMTP_PASSWORD", "密碼（Gmail 請用「應用程式密碼」）", True, True),
           ("EMAIL_TO", "收件人（多個用逗號分隔）", True, False),
           ("EMAIL_FROM", "寄件人（空白 = 同登入帳號）", False, False))
    help = "Gmail 需要開兩步驟驗證，再到 Google 帳戶 → 安全性 → 應用程式密碼 產生一組 16 碼密碼。"

    def send(self, title, message, tags=None, priority=3, click=None):
        c = self.cfg
        msg = EmailMessage()
        msg["Subject"] = title
        msg["From"] = c.get("EMAIL_FROM") or c["SMTP_USER"]
        msg["To"] = c["EMAIL_TO"]
        msg.set_content(message + (f"\n\n線圖：{click}" if click else "") + f"\n\n— orb-ntfy {datetime.now():%Y-%m-%d %H:%M:%S}")
        port = int(c.get("SMTP_PORT") or 587)
        try:
            if port == 465:
                server = smtplib.SMTP_SSL(c["SMTP_HOST"], port, timeout=TIMEOUT)
            else:
                server = smtplib.SMTP(c["SMTP_HOST"], port, timeout=TIMEOUT)
                server.starttls()
            with server:
                server.login(c["SMTP_USER"], c["SMTP_PASSWORD"])
                server.send_message(msg)
        except smtplib.SMTPAuthenticationError:
            return "SMTP 登入失敗（帳號密碼錯誤；Gmail 要用應用程式密碼）"
        except Exception as e:
            return self.redact(f"寄信失敗：{e}")
        return None


class Pushover(Notifier):
    name, label = "pushover", "Pushover"
    env = (("PUSHOVER_TOKEN", "Application API token", True, True), ("PUSHOVER_USER", "User key", True, True))
    help = "到 pushover.net 建立 Application 取得 API token，User key 在首頁。"

    def send(self, title, message, tags=None, priority=3, click=None):
        body = {"token": self.cfg["PUSHOVER_TOKEN"], "user": self.cfg["PUSHOVER_USER"], "title": title,
                "message": message, "priority": 1 if priority >= 4 else (-1 if priority <= 2 else 0)}
        if click:
            body.update(url=click, url_title="開啟線圖")
        return self._post("https://api.pushover.net/1/messages.json", data=body)


class Webhook(Notifier):
    name, label = "webhook", "自訂 Webhook（POST JSON，可接 IFTTT、n8n、Make…）"
    env = (("WEBHOOK_URL", "網址", True, True),)
    help = 'POST JSON：{"title", "message", "tags", "priority", "click", "sent_at"}'

    def send(self, title, message, tags=None, priority=3, click=None):
        body = {"title": title, "message": message, "tags": tags or [], "priority": priority, "click": click,
                "sent_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        return self._post(self.cfg["WEBHOOK_URL"], json=body)


CHANNELS = {c.name: c for c in (Ntfy, Telegram, Discord, Slack, Line, Email, Pushover, Webhook)}


def configured():
    """環境變數裡必填欄位都有值的管道名稱。"""
    return [name for name, cls in CHANNELS.items() if not cls.from_env().missing()]


def parse_channels(value):
    """'ntfy,telegram' / ['ntfy'] / 'print' / '' → ['ntfy', 'telegram'] / ['ntfy'] / [] / []"""
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = str(value or "").replace(" ", ",").split(",")
    out = []
    for x in items:
        x = str(x).strip().lower()
        if not x or x in ("print", "none", "console"):
            continue
        if x not in CHANNELS:
            raise SystemExit(f"不認識的通知管道 {x!r}，可用：{', '.join(CHANNELS)}、print")
        out.append(x)
    return list(dict.fromkeys(out))


def build(channels, ntfy_server=None, ntfy_topic=None, ntfy_token=None):
    """依名稱建立通知物件；缺少設定時拋 SystemExit 並說明要補哪些環境變數。"""
    out, problems = [], []
    for name in channels:
        cls = CHANNELS[name]
        extra = {"server": ntfy_server, "NTFY_TOPIC": ntfy_topic, "NTFY_TOKEN": ntfy_token} if name == "ntfy" else {}
        n = cls.from_env(**extra)
        miss = n.missing()
        if miss:
            problems.append(f"{cls.label}：.env 缺少 {', '.join(miss)}")
        out.append(n)
    if problems:
        raise SystemExit("通知設定不完整（可執行 python orb.py init 重新設定）：\n  " + "\n  ".join(problems))
    return out
