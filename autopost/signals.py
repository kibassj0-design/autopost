"""What happened in a stream, second by second, from everything but the words:
chat (how fast, and what people type), money (bits, subs, gifts, raids, tips, Super Chats),
the sound (loud reactions, sudden bursts such as explosions and crashes), the picture
(sudden action and flashes) and fast heated talk (arguments in the party).

Chat is a list of events: {"t": seconds into the video, "text": str, "kind": "msg" |
"money" | "sub" | "raid", "usd": amount}. Older code passed plain times; both work.
"""
import json
import logging
import re
import subprocess

log = logging.getLogger("autopost.signals")

# ---------------------------------------------------------------- chat words

LAUGH = re.compile(
    r"\b(kekw|lul|lulw|omegalul|lmao+|lmfao|lol+|xd+|icant|ica?n'?t|dead|💀)\b|😂|🤣|😹|💀|"
    r"(ه){3,}|(خ){3,}|(ه\s?){3,}|ضحك|يموت|هههه", re.I)
HYPE = re.compile(
    r"\b(pog+|pogchamp|poggers|pogu|w+|lets+ ?go+|letsgo+|hype|goat|insane|clutch|gg+|"
    r"ez|sheesh|fire|banger|huge)\b|🔥|🐐|💪|👑|كفو|كفوو+|اسطور|وحش|يا سلام|ابداع|خرافي|"
    r"عظمه|عظمة|جلد|فخم|يااا+", re.I)
SHOCK = re.compile(
    r"\b(wtf|omg|what+|no ?way|monkas|monkaw|holy|bruh|wait+|huh|ayo)\b|\?{2,}|!{3,}|😱|😳|"
    r"🤯|😨|شنو|شصار|ايش صار|يا الهي|ياربي|يا ربي|مستحيل|معقول|صدمه|صدمة|وش صار", re.I)
CLIP = re.compile(r"\bclip+\b|!clip|clip ?(it|that)|كليب|قصها|قص هذا|سجل(وا)?|صورها", re.I)

DONATION_BOTS = {"streamelements", "streamlabs", "nightbot", "fossabot", "moobot",
                 "botrixoficial", "wizebot", "sery_bot", "kofistreambot", "tipeeestream"}
DONATION_WORDS = re.compile(r"tip|donat|cheer|bits|superchat|super chat|تبرع|دعم|دونيت", re.I)

# Rough value of 1 unit in US dollars: enough to tell a big donation from a small one.
RATES = {"$": 1, "usd": 1, "us$": 1, "€": 1.08, "eur": 1.08, "£": 1.27, "gbp": 1.27,
         "sar": 0.27, "ر.س": 0.27, "aed": 0.27, "د.إ": 0.27, "kwd": 3.25, "د.ك": 3.25,
         "qar": 0.27, "bhd": 2.65, "omr": 2.6, "iqd": 0.00076, "د.ع": 0.00076,
         "egp": 0.02, "ج.م": 0.02, "jod": 1.41, "try": 0.03, "₺": 0.03, "₹": 0.012,
         "inr": 0.012, "¥": 0.0067, "jpy": 0.0067, "₩": 0.00072, "krw": 0.00072,
         "r$": 0.18, "brl": 0.18, "ca$": 0.73, "cad": 0.73, "a$": 0.65, "aud": 0.65,
         "mx$": 0.05, "mxn": 0.05, "₽": 0.011, "rub": 0.011, "pln": 0.25, "zł": 0.25}
_CODES = (r"usd|eur|gbp|sar|aed|kwd|qar|bhd|omr|iqd|egp|jod|try|inr|jpy|krw|brl|cad|aud|"
          r"mxn|rub|pln|zł|ر\.س|د\.إ|د\.ك|د\.ع|ج\.م")
AMOUNT = re.compile(
    r"(?P<pre>us\$|ca\$|a\$|r\$|mx\$|[$€£¥₩₹₺₽]|\b(?:" + _CODES + r"))\s?"
    r"(?P<a>\d[\d,]*(?:\.\d+)?)|"
    r"(?P<b>\d[\d,]*(?:\.\d+)?)\s?(?P<post>" + _CODES + r")(?!\w)", re.I)


def usd(text):
    """The first money amount written in a text, in US dollars, or 0."""
    m = AMOUNT.search(text or "")
    if not m:
        return 0.0
    unit = (m.group("pre") or m.group("post") or "$").lower()
    number = float((m.group("a") or m.group("b")).replace(",", ""))
    return number * RATES.get(unit, 1)


def _t(event):
    return event["t"] if isinstance(event, dict) else float(event)


def times(chat):
    return [_t(e) for e in chat]


# ---------------------------------------------------------------- chat sources

def _runs(message):
    out = []
    for r in (message or {}).get("runs", []):
        if "text" in r:
            out.append(r["text"])
        elif "emoji" in r:
            e = r["emoji"]
            out.append((e.get("shortcuts") or [e.get("emojiId", "")])[0])
    return "".join(out)


def youtube_chat_line(line, started=None):
    """One line of yt-dlp's YouTube live_chat.json (a replay or a live stream) -> events."""
    try:
        data = json.loads(line)
    except ValueError:
        return []
    offset = None
    if "replayChatItemAction" in data:
        offset = data["replayChatItemAction"].get("videoOffsetTimeMsec")
        actions = data["replayChatItemAction"].get("actions", [])
    else:
        offset = data.get("videoOffsetTimeMsec")
        actions = data.get("actions") or [data]
    events = []
    for action in actions:
        item = (action.get("addChatItemAction") or {}).get("item") or {}
        for kind, r in item.items():
            t = None
            if offset is not None:
                t = int(offset) / 1000
            elif started is not None and r.get("timestampUsec"):
                t = int(r["timestampUsec"]) / 1e6 - started
            if t is None:
                continue
            text = _runs(r.get("message"))
            if kind == "liveChatTextMessageRenderer":
                events.append({"t": t, "text": text, "kind": "msg", "usd": 0})
            elif kind in ("liveChatPaidMessageRenderer", "liveChatPaidStickerRenderer"):
                amount = (r.get("purchaseAmountText") or {}).get("simpleText", "")
                events.append({"t": t, "text": f"Super Chat {amount}: {text}", "kind": "money",
                               "usd": usd(amount) or 1})
            elif kind in ("liveChatMembershipItemRenderer",
                          "liveChatSponsorshipsGiftPurchaseAnnouncementRenderer"):
                header = _runs((r.get("headerSubtext") or r.get("header", {})
                                .get("liveChatSponsorshipsHeaderRenderer", {})
                                .get("primaryText")))
                count = re.search(r"(\d+)", header or "")
                n = int(count.group(1)) if count else 1
                events.append({"t": t, "text": f"membership {header}", "kind": "sub",
                               "usd": 5 * n})
    return events


def twitch_rechat(items):
    events = []
    for msg in items:
        t = float(msg.get("content_offset_seconds", 0))
        body = msg.get("message") or {}
        text = body.get("body") or ""
        bits = int(body.get("bits_spent") or 0)
        events.append({"t": t, "text": text, "kind": "money" if bits else "msg",
                       "usd": bits / 100})
    return events


IRC_TAGS = re.compile(r"^@(?P<tags>\S+) ")


def irc_line(line, t):
    """One raw Twitch IRC line (with tags) -> an event, or None."""
    tags = {}
    m = IRC_TAGS.match(line)
    if m:
        for part in m.group("tags").split(";"):
            k, _, v = part.partition("=")
            tags[k] = v.replace("\\s", " ")
    if " PRIVMSG #" in line:
        text = line.split(" PRIVMSG #", 1)[1].split(" :", 1)[-1]
        user = (tags.get("display-name") or "").lower()
        bits = int(tags.get("bits") or 0)
        if bits:
            return {"t": t, "text": f"{bits} bits: {text}", "kind": "money", "usd": bits / 100}
        if user in DONATION_BOTS and DONATION_WORDS.search(text):
            amount = usd(text)
            if amount:
                return {"t": t, "text": text, "kind": "money", "usd": amount}
        return {"t": t, "text": text, "kind": "msg", "usd": 0}
    if " USERNOTICE #" in line:
        kind = tags.get("msg-id", "")
        text = tags.get("system-msg", kind)
        if kind in ("sub", "resub", "subgift", "anonsubgift", "primepaidupgrade",
                    "giftpaidupgrade"):
            return {"t": t, "text": text, "kind": "sub", "usd": 5}
        if kind in ("submysterygift", "anonsubmysterygift"):
            n = int(tags.get("msg-param-mass-gift-count") or 1)
            return {"t": t, "text": text, "kind": "money", "usd": 5 * n}
        if kind == "raid":
            viewers = int(tags.get("msg-param-viewerCount") or 0)
            return {"t": t, "text": text, "kind": "raid", "usd": min(viewers / 20, 50)}
    return None


# ---------------------------------------------------------------- per-second curves

def _z(x):
    import numpy as np
    x = np.asarray(x, dtype=np.float32)
    if not len(x):
        return x
    s = x.std()
    return (x - x.mean()) / s if s > 0 else np.zeros_like(x)


def _smooth(x, width):
    import numpy as np
    return np.convolve(x, np.ones(width) / width, "same")


def chat_curves(chat, n):
    """Per second: message rate, laughing, hype, shock, clip requests, money (USD)."""
    import numpy as np
    out = {k: np.zeros(n, dtype=np.float32) for k in
           ("rate", "laugh", "hype", "shock", "clip", "money")}
    if not n:
        return out
    for e in chat:
        i = int(_t(e))
        if not 0 <= i < n:
            continue
        out["rate"][i] += 1
        if not isinstance(e, dict):
            continue
        text = e.get("text") or ""
        if e.get("kind") != "msg":
            out["money"][i] += e.get("usd") or 0
        if LAUGH.search(text):
            out["laugh"][i] += 1
        if HYPE.search(text):
            out["hype"][i] += 1
        if SHOCK.search(text):
            out["shock"][i] += 1
        if CLIP.search(text):
            out["clip"][i] += 1
    return out


def audio_impacts(wav, n):
    """Per second: sudden broadband bursts (explosions, crashes, gunfire, slams),
    measured as spectral flux, the jump in sound energy across all frequencies."""
    import wave

    import numpy as np
    try:
        with wave.open(str(wav)) as w:
            rate = w.getframerate()
            a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    except Exception:
        return None
    hop, size = rate // 20, rate // 10  # 50 ms steps, 100 ms windows
    count = (len(a) - size) // hop + 1
    if count < 20:
        return None
    win = np.hanning(size).astype(np.float32)
    offsets = np.arange(size)
    flux = np.zeros(n, dtype=np.float32)
    prev = None
    for k0 in range(0, count, 4000):  # in blocks, to keep memory small
        k = np.arange(k0, min(count, k0 + 4000))
        frames = a[k[:, None] * hop + offsets] * win
        spec = np.log1p(np.abs(np.fft.rfft(frames, axis=1)))
        full = spec if prev is None else np.vstack([prev[None, :], spec])
        diff = np.maximum(full[1:] - full[:-1], 0).mean(axis=1)
        frame_ids = k[1:] if prev is None else k
        prev = spec[-1]
        secs = (frame_ids * hop // rate).astype(int)
        ok = secs < n
        np.maximum.at(flux, secs[ok], diff[ok].astype(np.float32))
    return flux


def visual_activity(src, n):
    """Per second: how much the picture changes (sudden action, explosions, crashes)
    and sudden flashes. Only for video files on this computer; None otherwise."""
    import numpy as np
    w, h, fps = 64, 36, 4
    try:
        p = subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-an", "-vf",
                            f"fps={fps},scale={w}:{h},format=gray", "-f", "rawvideo", "-"],
                           capture_output=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired):
        return None
    frames = np.frombuffer(p.stdout, dtype=np.uint8)
    count = len(frames) // (w * h)
    if count < fps * 4:
        return None
    frames = frames[: count * w * h].reshape(count, w * h).astype(np.float32)
    change = np.abs(np.diff(frames, axis=0)).mean(axis=1)
    bright = frames.mean(axis=1)
    flash = np.maximum(np.diff(bright), 0)
    score = _z(change) + 0.7 * _z(flash)
    out = np.zeros(n, dtype=np.float32)
    for j, v in enumerate(score):
        i = int((j + 1) / fps)
        if i < n:
            out[i] = max(out[i], v)
    return out


def talk_speed(segments, n):
    """Per second: words per second of the speech (fast, overlapping talk = heated)."""
    import numpy as np
    out = np.zeros(n, dtype=np.float32)
    for s in segments:
        words = s.get("words") or []
        count = len(words) or len((s.get("text") or "").split())
        dur = max(s["end"] - s["start"], 0.5)
        a, b = int(s["start"]), min(n, int(s["end"]) + 1)
        if b > a:
            out[a:b] = np.maximum(out[a:b], count / dur)
    return out


class Signals:
    """Everything measured on one video, one value per second."""

    def __init__(self, n, levels=None, chat=(), heat=None, impacts=None, visual=None,
                 segments=()):
        import numpy as np
        self.n = n
        self.loud = _smooth(_z(levels), 3) if levels is not None and len(levels) else np.zeros(n)
        cc = chat_curves(list(chat), n)
        self.has_chat = bool(cc["rate"].sum())
        self.rate = _z(_smooth(cc["rate"], 10)) if self.has_chat else np.zeros(n)
        # reactions as a share of a short window, so a small chat counts as much as a big one
        win = _smooth(cc["rate"], 10) + 0.5
        self.laugh = _smooth(cc["laugh"], 10) / win
        self.hype = _smooth(cc["hype"], 10) / win
        self.shock = _smooth(cc["shock"], 10) / win
        self.clip = _smooth(cc["clip"], 20) * 20
        self.money = cc["money"]
        self.heat = _z(heat) if heat is not None and len(heat) == n else None
        self.impact = _z(_smooth(impacts, 2)) if impacts is not None and len(impacts) == n else None
        self.visual = _smooth(visual, 2) if visual is not None and len(visual) == n else None
        speed = talk_speed(segments, n)
        self.heated = (speed > 3.6) & (self.loud > 0.8)

    def curve(self):
        """One hype score per second, for picking without AI and for the fallback."""
        import numpy as np
        c = self.loud.copy()
        if self.has_chat:
            ahead = lambda x: np.roll(x, -8)  # people type after the moment
            c += 1.5 * ahead(self.rate) + 2 * ahead(self.laugh + self.hype + self.shock)
            c += ahead(np.minimum(self.clip, 3))
        c += np.minimum(_smooth(self.money, 30) * 30 / 10, 3)  # react after the donation
        for extra, w in ((self.heat, 0.8), (self.impact, 0.6), (self.visual, 0.5)):
            if extra is not None:
                c += w * extra
        c += self.heated * 1.0
        return _smooth(c, 15)

    def marks(self, a, b):
        """Emoji marks for the seconds [a, b) (with chat looked at a little later)."""
        a, b = max(0, int(a)), min(self.n, int(b))
        if b <= a:
            return ""
        late = slice(a, min(self.n, b + 9))  # chat and reactions lag the moment
        tag = ""

        def lv(x, cuts):
            return 3 if x > cuts[2] else 2 if x > cuts[1] else 1 if x > cuts[0] else 0

        tag += "🔊" * lv(float(self.loud[a:b].max()), (1.5, 2.2, 3))
        if self.impact is not None:
            tag += "💥" * lv(float(self.impact[a:b].max()), (2.2, 3, 4))
        if self.visual is not None:
            tag += "🎬" * lv(float(self.visual[a:b].max()), (2.2, 3, 4))
        if self.heated[a:b].any():
            tag += "⚡"
        if self.has_chat:
            tag += "💬" * lv(float(self.rate[late].max()), (1.5, 2.2, 3))
            tag += "😂" * lv(float(self.laugh[late].max()), (0.25, 0.4, 0.6))
            tag += "🔥" * lv(float(self.hype[late].max()), (0.25, 0.4, 0.6))
            tag += "😱" * lv(float(self.shock[late].max()), (0.25, 0.4, 0.6))
            if float(self.clip[late].max()) >= 1:
                tag += "✂️"
        money = float(self.money[max(0, a - 30):b].sum())  # a donation, then the reaction
        if money >= 5:
            tag += f"💰${money:.0f}"
        if self.heat is not None:
            tag += "🔁" * lv(float(self.heat[a:b].max()), (1.5, 2.2, 3))
        return tag

    def chat_sample(self, chat, a, b, limit=6):
        """A few of the chat messages typed right after [a, b), to show what people felt."""
        msgs = [e for e in chat if isinstance(e, dict) and a <= e["t"] < b + 10
                and e.get("text")]
        picked = [e for e in msgs if e.get("kind") != "msg"][:2]
        picked += [e for e in msgs if e.get("kind") == "msg"
                   and (LAUGH.search(e["text"]) or HYPE.search(e["text"])
                        or SHOCK.search(e["text"]) or CLIP.search(e["text"]))][:limit]
        return " | ".join(e["text"][:60] for e in picked[:limit])
