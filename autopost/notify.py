"""Notifications on the phone: when a clip is posted, when posting fails, when new
clips are ready, when a video takes off.

Two free ways, no account needed for the first:
- ntfy: install the ntfy app (Android / iPhone), subscribe to the tool's private topic
  (the tool makes one and shows it as a QR code). Messages go through ntfy.sh.
- Telegram: a bot of your own (from @BotFather); the tool sends to your chat with it.
"""
import logging
import os
import secrets
import threading

import requests

from . import config

log = logging.getLogger("autopost.notify")

NTFY = "https://ntfy.sh"
EVENTS = {"posted": "نُشر مقطع", "failed": "فشل النشر", "clips": "مقاطع جديدة جاهزة",
          "momentum": "مقطع ينتشر", "paused": "توقف النشر"}


def settings(cfg):
    return cfg.get("notify", {})


def topic(cfg):
    """The private ntfy topic of this tool (made once: long and random, so nobody else
    can guess it and read the messages)."""
    t = os.environ.get("NTFY_TOPIC")
    if not t:
        t = "autopost-" + secrets.token_urlsafe(12).replace("_", "x").replace("-", "y").lower()
        config.write_env({"NTFY_TOPIC": t})
    return t


def _ntfy(cfg, title, text, link=None, priority=3, tags=""):
    body = {"topic": topic(cfg), "title": title, "message": text, "priority": priority}
    if tags:
        body["tags"] = [tags]
    if link:
        body["click"] = link
    r = requests.post(NTFY, json=body, timeout=20)  # JSON keeps Arabic text intact
    r.raise_for_status()


def _telegram(cfg, title, text, link=None, **_):
    token, chat = os.environ.get("TG_BOT_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not token or not chat:
        return
    body = f"<b>{_esc(title)}</b>\n{_esc(text)}" + (f"\n{link}" if link else "")
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=20,
                      data={"chat_id": chat, "text": body, "parse_mode": "HTML",
                            "disable_web_page_preview": "true"})
    r.raise_for_status()


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send(cfg, event, title, text, link=None, wait=False):
    """Send in the background (never slows or breaks the tool). event: a key of EVENTS."""
    s = settings(cfg)
    if not s.get("enabled") or not s.get("on_" + event, True):
        return
    priority = {"failed": 4, "paused": 4, "momentum": 4}.get(event, 3)
    tags = {"posted": "white_check_mark", "failed": "x", "clips": "scissors",
            "momentum": "fire", "paused": "pause_button"}.get(event, "")

    def go():
        for name, fn in (("ntfy", _ntfy), ("telegram", _telegram)):
            if name == "ntfy" and not s.get("ntfy", True):
                continue
            try:
                fn(cfg, title, text, link=link, priority=priority, tags=tags)
            except Exception as e:
                log.info("Notification by %s failed: %s", name, str(e)[:120])

    if wait:
        go()
    else:
        threading.Thread(target=go, daemon=True).start()


def telegram_find_chat():
    """After the user sends any message to their bot: their chat id, from the bot's
    recent updates. Saves it and returns the name, or raises with what to do."""
    token = os.environ.get("TG_BOT_TOKEN")
    if not token:
        raise ValueError("الصق توكن البوت أولا.")
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20)
    if r.status_code == 401:
        raise ValueError("توكن البوت غير صحيح.")
    r.raise_for_status()
    for u in reversed(r.json().get("result", [])):
        chat = (u.get("message") or u.get("my_chat_member") or {}).get("chat")
        if chat:
            config.write_env({"TG_CHAT_ID": str(chat["id"])})
            return chat.get("first_name") or chat.get("title") or str(chat["id"])
    raise ValueError("افتح بوتك في تيليجرام وأرسل له أي رسالة (مثل /start)، ثم اضغط مرة أخرى.")
