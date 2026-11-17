"""Virality predictor: a 0-100 estimate of how well a clip can spread, before it is posted.

It looks at what short-video algorithms reward first: a hook in the first seconds that
stops the scroll, energy that keeps people watching to the end, a lively pace, a length
people finish, and a full-screen vertical format. An AI judge (when an AI key is set)
watches stills and reads the speech for emotion and shareability, and writes tips.

The weights start from those general rules, then learn from this account: once enough
predicted clips have real first-day views, each signal is weighted by how well it
actually tracked the views here.
"""
import json
import logging
import re
import statistics
import subprocess
import time
from pathlib import Path

import numpy as np

from . import llm, store, video

log = logging.getLogger("autopost.virality")

WEIGHTS = {"moment": 0.20, "judge": 0.25, "hook": 0.20, "energy": 0.10, "pace": 0.10,
           "length": 0.10, "format": 0.05}
LABELS = {"moment": "قوة اللحظة", "judge": "تقييم الذكاء الاصطناعي", "hook": "بداية تشد (أول 3 ثوان)",
          "energy": "حماس الصوت", "pace": "سرعة الإيقاع", "length": "طول مناسب",
          "format": "شكل الشاشة"}
LEARN_AFTER = 10  # predicted clips with real views before the weights adapt

SCHEMA_SQL = """CREATE TABLE IF NOT EXISTS predictions(
  name TEXT PRIMARY KEY, score INTEGER, data TEXT, created REAL)"""

JUDGE_SYSTEM = """You judge short vertical videos (TikTok, Instagram Reels, YouTube Shorts) \
for how likely they are to go viral with strangers. You get stills from the clip (the first \
one is from its opening seconds) and what is said in it.

Score 0-100 each:
- hook: would the first 2 seconds stop someone scrolling?
- emotion: how strong is the feeling (hype, laughter, shock, tension, awe)?
- shareability: would people send it to a friend or comment on it?
- clarity: does it make sense without any context?
Then `overall` 0-100, and up to 3 short, concrete `tips` to make THIS clip spread better \
(about its opening, the caption text, the cut, the first line). Write the tips in Arabic \
(Modern Standard Arabic). Be honest: most clips are average (40-60)."""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "hook": {"type": "integer"}, "emotion": {"type": "integer"},
        "shareability": {"type": "integer"}, "clarity": {"type": "integer"},
        "overall": {"type": "integer"},
        "tips": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["hook", "emotion", "shareability", "clarity", "overall", "tips"],
}


def _clamp(x):
    return int(max(0, min(100, round(x))))


# ---------------------------------------------------------------- signals from the file

def _loudness(path, rate=8000, step=0.1):
    """Loudness (dB) per 0.1 s of the clip's sound."""
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1",
                        "-ar", str(rate), "-f", "s16le", "-"], capture_output=True, timeout=180)
    a = np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768
    n = int(rate * step)
    if len(a) < n * 10:
        return None
    a = a[: len(a) // n * n].reshape(-1, n)
    return 20 * np.log10(np.sqrt((a ** 2).mean(axis=1)) + 1e-6)


def _cuts(path):
    """Scene changes in the clip (camera cuts, big jumps on screen)."""
    p = subprocess.run(["ffmpeg", "-v", "info", "-i", str(path), "-an", "-vf",
                        "fps=8,scale=96:-2,select='gt(scene,0.32)',showinfo", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=300)
    return len(re.findall(r"pts_time:", p.stderr))


def signals(path):
    meta = video.probe(path)
    dur = max(meta["duration"], 1)
    out = {"seconds": round(dur, 1)}

    # length: completion rate is what spreads a clip; 15-45 s is the sweet spot
    if dur <= 45:
        out["length"] = 100 if dur >= 15 else 60 + dur * 2.5
    else:
        out["length"] = max(35, 100 - (dur - 45) * 1.6)

    # format: full-screen vertical and sharp
    vertical = meta["height"] > meta["width"]
    out["format"] = (70 if vertical else 35) + (30 if min(meta["width"], meta["height"]) >= 1000
                                                  else 15 if min(meta["width"], meta["height"]) >= 700 else 0)

    db = _loudness(path)
    if db is not None:
        voiced = db[db > -50]
        base = float(np.median(voiced)) if len(voiced) else float(np.median(db))
        first = db[:30]  # first 3 seconds
        silence = float((first < -45).mean())
        # hook: the opening is at least as loud as the clip, and not silent
        out["hook"] = _clamp(65 + (float(np.percentile(first, 80)) - base) * 6 - silence * 70)
        spread = float(np.std(voiced)) if len(voiced) else 0
        z = (db - base) / max(spread, 1.5)
        bursts = int(((z[1:] > 1.8) & (z[:-1] <= 1.8)).sum())  # sudden loud moments
        per_min = bursts / (dur / 60)
        dead = float((db < -45).mean())  # long silences lose viewers
        out["energy"] = _clamp(45 + min(per_min, 12) * 4 + min(spread, 10) * 2 - dead * 60)
    else:
        out["hook"], out["energy"] = 30, 25  # no sound: a big handicap on these apps

    out["length"], out["format"] = _clamp(out["length"]), _clamp(out["format"])
    cuts_per_min = _cuts(path) / (dur / 60)
    # 6-20 cuts a minute reads as lively; a static camera can still work (streams)
    out["pace"] = _clamp(55 + min(cuts_per_min, 20) * 2.5 if cuts_per_min <= 30 else 80)
    return out


def _moment_score(note):
    m = re.search(r"Moment score:\s*(\d+)", note or "")
    return int(m.group(1)) if m else None


def judge(path, note, cfg):
    """The AI's view of the clip, or None without an AI key."""
    if not llm.providers(cfg) or not cfg.get("virality", {}).get("ai_judge", True):
        return None
    said = re.search(r"What is said in the clip:\s*(.*)", note or "")
    title = re.search(r"Clip title:\s*(.*)", note or "")
    brief = [f"Clip length: {video.probe(path)['duration']:.0f} s."]
    if title:
        brief.append(f"Clip title: {title.group(1)}")
    if said:
        brief.append(f"What is said: {said.group(1)[:1500]}")
    else:
        brief.append("No transcript is available; judge from the stills.")
    try:
        data, who = llm.ask_json(JUDGE_SYSTEM, "\n".join(brief), JUDGE_SCHEMA, cfg,
                                 images=video.frames_b64(path, count=4), max_tokens=4000)
        data["by"] = who
        return data
    except Exception as e:
        log.info("AI judge unavailable: %s", str(e)[:160])
        return None


# ---------------------------------------------------------------- score

def _weights(db):
    """Default weights, or ones learned from this account's results."""
    learned = _learned(db)
    return (learned or WEIGHTS), bool(learned)


def combine(parts, weights):
    have = {k: v for k, v in parts.items() if k in weights and v is not None}
    total = sum(weights[k] for k in have) or 1
    return _clamp(sum(weights[k] * v for k, v in have.items()) / total)


def _tips(parts, sig, ai):
    tips = []
    if parts.get("hook", 100) < 55:
        tips.append("البداية هادئة: ابدأ المقطع من اللحظة الأقوى أو بجملة تشد في أول ثانيتين.")
    if parts.get("length", 100) < 60:
        tips.append(f"المقطع طويل ({sig['seconds']:.0f} ث): الأقصر من 45 ثانية يُشاهد حتى النهاية أكثر.")
    if parts.get("energy", 100) < 45:
        tips.append("فيه مقاطع صامتة أو هادئة طويلة: قصّها، فالصمت يجعل المشاهد يمرر.")
    if parts.get("format", 100) < 70:
        tips.append("المقطع ليس طوليا ملء الشاشة: الطولي 9:16 يأخذ وصولا أكبر في Reels وShorts.")
    for t in (ai or {}).get("tips", [])[:3]:
        if t and t not in tips:
            tips.append(t)
    return tips[:5]


def predict(path, note, cfg, db):
    """Score a clip and remember it. Returns the stored prediction."""
    sig = signals(path)
    ai = judge(path, note, cfg)
    parts = {k: sig.get(k) for k in ("hook", "energy", "pace", "length", "format")}
    parts["moment"] = _moment_score(note)
    if ai:
        parts["judge"] = _clamp(0.4 * ai["overall"] + 0.25 * ai["hook"] +
                                0.2 * ai["shareability"] + 0.15 * ai["emotion"])
    weights, learned = _weights(db)
    score = combine(parts, weights)
    data = {"score": score, "parts": parts, "signals": sig, "ai": ai, "learned": learned,
            "tips": _tips(parts, sig, ai),
            "level": "عالية" if score >= 75 else "جيدة" if score >= 60 else
                     "متوسطة" if score >= 45 else "ضعيفة"}
    save(db, Path(path).name, data)
    return data


# ---------------------------------------------------------------- storage

def _table(db):
    db.execute(SCHEMA_SQL)


def save(db, name, data):
    _table(db)
    db.execute("INSERT OR REPLACE INTO predictions VALUES(?,?,?,?)",
               (name, data["score"], json.dumps(data, ensure_ascii=False), time.time()))
    db.commit()


def get(db, name):
    _table(db)
    row = db.execute("SELECT data FROM predictions WHERE name=?", (Path(name).name,)).fetchone()
    return json.loads(row["data"]) if row else None


def all_scores(db):
    _table(db)
    return {r["name"]: r["score"] for r in db.execute("SELECT name, score FROM predictions")}


def _outcomes(db):
    """[(prediction data, first-day views)] for published clips that were predicted."""
    _table(db)
    preds = {r["name"]: json.loads(r["data"]) for r in db.execute("SELECT name, data FROM predictions")}
    if not preds:
        return []
    out = []
    rows = db.execute(
        "SELECT v.source, MAX(s.views) views FROM posts p JOIN videos v ON v.id=p.video_id"
        " JOIN stats s ON s.post_id=p.id"
        " WHERE p.error IS NULL AND v.parent_id IS NULL AND s.views IS NOT NULL"
        "   AND s.ts <= p.published_at + 36*3600 AND p.published_at <= ?"
        " GROUP BY v.id", (time.time() - 24 * 3600,)).fetchall()
    for r in rows:
        name = re.sub(r"^\d{10}-", "", Path(r["source"] or "").name)
        if name in preds and r["views"] is not None:
            out.append((preds[name], r["views"]))
    return out


def _rank(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0] * len(xs)
    for pos, i in enumerate(order):
        r[i] = pos
    return r


def _corr(a, b):
    if len(a) < 4 or len(set(a)) < 2 or len(set(b)) < 2:
        return 0.0
    ra, rb = _rank(a), _rank(b)
    ma, mb = statistics.mean(ra), statistics.mean(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = (sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb)) ** 0.5
    return num / den if den else 0.0


def _learned(db):
    data = _outcomes(db)
    if len(data) < LEARN_AFTER:
        return None
    views = [v for _, v in data]
    weights = {}
    for k, w in WEIGHTS.items():
        pairs = [(d["parts"].get(k), v) for d, v in zip((d for d, _ in data), views)
                 if d["parts"].get(k) is not None]
        c = _corr([p for p, _ in pairs], [v for _, v in pairs]) if len(pairs) >= 4 else 0
        # half the general rule, half what worked here; a signal that ran against the
        # views here is turned down, never off
        weights[k] = max(0.02, w * 0.5 + max(c, 0) * 0.5 * w * 4)
    total = sum(weights.values())
    return {k: v / total for k, v in weights.items()}


def accuracy(db):
    """How well past predictions matched the real views: (clips, rank correlation)."""
    data = _outcomes(db)
    if len(data) < 4:
        return {"clips": len(data), "corr": None, "needed": LEARN_AFTER}
    return {"clips": len(data), "corr": round(_corr([d["score"] for d, _ in data],
                                                    [v for _, v in data]), 2),
            "needed": LEARN_AFTER}
