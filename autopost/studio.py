"""Creator studio: the account's growth at a glance, its content, a growth plan the AI
writes from the account's own results, and an analysis engine that revisits the plan.

- Followers are recorded every few hours per platform (Instagram followers, YouTube
  subscribers, TikTok followers when the app may read them).
- The plan: a follower goal, reels per week and weekly themes with tasks. Each week the
  AI looks at what happened (posts, views, followers, tasks done) and rewrites it,
  keeping what it learned as rules. Every step is written to the analysis log.
"""
import json
import logging
import time

import requests

from . import config, llm, timing

log = logging.getLogger("autopost.studio")

TABLE = """CREATE TABLE IF NOT EXISTS followers(platform TEXT, ts REAL, count INTEGER,
  name TEXT, picture TEXT)"""
EVERY = 3 * 3600  # followers are read this often
PLAN_FILE = "studio_plan.json"
LOG_FILE = "studio_log.json"
REVISE_EVERY = 7 * 86400


def _table(db):
    db.execute(TABLE)


SETTINGS_FILE = "studio_settings.json"
DEFAULTS = {"platforms": ["youtube", "instagram"], "since": 0,
            "exclude": ["GTA 6", "GTA", "جي تي اي"], "niche": ""}


def settings():
    return {**DEFAULTS, **_read(SETTINGS_FILE, {})}


def save_settings(**changes):
    s = settings()
    s.update({k: v for k, v in changes.items() if k in DEFAULTS})
    _write(SETTINGS_FILE, s)
    return s


# ---------------------------------------------------------------- followers

def _instagram():
    from .platforms import instagram
    if instagram.cooling_down():
        return None
    import os
    token, user = os.environ.get("IG_ACCESS_TOKEN"), os.environ.get("IG_USER_ID")
    if not token or not user:
        return None
    r = requests.get(f"{instagram._base()}/{user}", timeout=30, params={
        "fields": "username,followers_count,media_count,profile_picture_url",
        "access_token": token})
    d = instagram._checked(r, "Instagram profile").json()
    return d.get("followers_count"), d.get("username", ""), d.get("profile_picture_url", "")


def _youtube():
    from .platforms import youtube
    r = requests.get(f"{youtube.API}/channels", timeout=30, headers=youtube._headers(),
                     params={"part": "statistics,snippet", "mine": "true"})
    r.raise_for_status()
    item = (r.json().get("items") or [{}])[0]
    count = item.get("statistics", {}).get("subscriberCount")
    thumb = (item.get("snippet", {}).get("thumbnails", {}).get("default") or {}).get("url", "")
    return (int(count) if count is not None else None,
            item.get("snippet", {}).get("title", ""), thumb)


def _tiktok():
    from .platforms import tiktok
    r = requests.get("https://open.tiktokapis.com/v2/user/info/", timeout=30,
                     headers=tiktok._headers(),
                     params={"fields": "display_name,avatar_url,follower_count"})
    body = r.json()
    user = (body.get("data") or {}).get("user") or {}
    if "follower_count" not in user:
        return None  # the app may not read stats (user.info.stats scope)
    return user["follower_count"], user.get("display_name", ""), user.get("avatar_url", "")


AUDIENCE_FILE = "studio_audience.json"


def _instagram_reach():
    """Reach over the last 30 days and how much of it came from non-followers."""
    import os
    from .platforms import instagram
    if instagram.cooling_down():
        return None
    token, user = os.environ.get("IG_ACCESS_TOKEN"), os.environ.get("IG_USER_ID")
    r = requests.get(f"{instagram._base()}/{user}/insights", timeout=30, params={
        "metric": "reach", "period": "day", "metric_type": "total_value",
        "breakdown": "follow_type", "since": int(time.time() - 28 * 86400),
        "until": int(time.time()), "access_token": token})
    if r.status_code >= 400:
        instagram._checked(r, "Instagram reach") if "request limit" in r.text.lower() else None
        return {"error": "permission" if "permission" in r.text.lower() or '"code":10' in r.text
                else r.text[:160]}
    data = (r.json().get("data") or [{}])[0].get("total_value", {})
    total = data.get("value")
    parts = {}
    for b in data.get("breakdowns", []):
        for res in b.get("results", []):
            parts[(res.get("dimension_values") or ["?"])[0]] = res.get("value", 0)
    non = parts.get("NON_FOLLOWER")
    return {"reach": total, "non_follower_share":
            round(100 * non / total, 1) if total and non is not None else None}


def audience():
    return _read(AUDIENCE_FILE, {})


def record(cfg, db, force=False):
    """Read follower counts (at most every EVERY seconds per platform)."""
    _table(db)
    for name, fn in (("instagram", _instagram), ("youtube", _youtube), ("tiktok", _tiktok)):
        if not config.connected(name):
            continue
        last = db.execute("SELECT MAX(ts) t FROM followers WHERE platform=?", (name,)).fetchone()
        if not force and last and last["t"] and time.time() - last["t"] < EVERY:
            continue
        try:
            got = fn()
        except Exception as e:
            log.info("Followers of %s unavailable: %s", name, str(e)[:120])
            got = None
        if got and got[0] is not None:
            db.execute("INSERT INTO followers VALUES(?,?,?,?,?)",
                       (name, time.time(), int(got[0]), got[1] or "", got[2] or ""))
        if name == "instagram":
            try:
                reach = _instagram_reach()
                if reach:
                    _write(AUDIENCE_FILE, {**reach, "at": time.time()})
            except Exception as e:
                log.info("Instagram reach unavailable: %s", str(e)[:120])
    db.commit()


def followers(db, days=30):
    """{platform: {"now", "name", "picture", "delta14", "series": [[day_ts, count]]}}"""
    _table(db)
    out = {}
    since = time.time() - days * 86400
    for row in db.execute("SELECT DISTINCT platform FROM followers").fetchall():
        p = row["platform"]
        if p not in settings()["platforms"]:
            continue
        rows = db.execute("SELECT ts, count, name, picture FROM followers WHERE platform=? "
                          "ORDER BY ts", (p,)).fetchall()
        if not rows:
            continue
        daily = {}
        for r in rows:
            if r["ts"] >= since:
                daily[int(r["ts"] // 86400) * 86400] = r["count"]
        two_weeks = [r for r in rows if r["ts"] <= time.time() - 14 * 86400]
        base = (two_weeks[-1] if two_weeks else rows[0])["count"]
        out[p] = {"now": rows[-1]["count"], "name": rows[-1]["name"],
                  "picture": rows[-1]["picture"], "delta14": rows[-1]["count"] - base,
                  "series": sorted(daily.items())}
    return out


# ---------------------------------------------------------------- numbers and content

def _posts(db, days=30):
    """Posts on the engine's platforms, since its fresh start if there was one."""
    st = settings()
    since = max(time.time() - days * 86400, float(st.get("since") or 0))
    return [p for p in _all_posts(db, since) if p["platform"] in st["platforms"]]


def _all_posts(db, since):
    return [dict(r) for r in db.execute(
        "SELECT p.id, p.platform, p.url, p.published_at, v.id video_id, v.title,"
        " (SELECT views FROM stats s WHERE s.post_id=p.id ORDER BY ts DESC LIMIT 1) views,"
        " (SELECT likes FROM stats s WHERE s.post_id=p.id ORDER BY ts DESC LIMIT 1) likes,"
        " (SELECT comments FROM stats s WHERE s.post_id=p.id ORDER BY ts DESC LIMIT 1) comments,"
        " (SELECT shares FROM stats s WHERE s.post_id=p.id ORDER BY ts DESC LIMIT 1) shares"
        " FROM posts p JOIN videos v ON v.id=p.video_id"
        " WHERE p.error IS NULL AND p.published_at >= ? ORDER BY p.published_at DESC",
        (since,))]


def overview(db, days=30):
    posts = _posts(db, days)
    views = sum(p["views"] or 0 for p in posts)
    acts = sum((p["likes"] or 0) + (p["comments"] or 0) + (p["shares"] or 0) for p in posts)
    measured = [p for p in posts if p["views"]]
    by_platform = {}
    for p in measured:
        by_platform[p["platform"]] = by_platform.get(p["platform"], 0) + p["views"]
    top = max(by_platform, key=by_platform.get) if by_platform else None
    videos = len({p["video_id"] for p in posts})
    # the views pulse: views gathered per day by the posts (from the snapshots)
    pulse = {}
    for r in db.execute(
            "SELECT s.post_id, s.ts, s.views, p.platform, p.published_at FROM stats s "
            "JOIN posts p ON p.id=s.post_id WHERE s.ts >= ? AND s.views IS NOT NULL "
            "ORDER BY s.ts", (time.time() - days * 86400,)).fetchall():
        if r["platform"] not in settings()["platforms"] or \
                r["published_at"] < float(settings().get("since") or 0):
            continue
        day = int(r["ts"] // 86400) * 86400
        pulse.setdefault(day, {})[r["post_id"]] = r["views"]
    running, series = {}, []
    for day in sorted(pulse):
        running.update(pulse[day])
        series.append([day, sum(running.values())])
    return {"views": views, "interactions": acts,
            "rate": round(100 * acts / views, 2) if views else None,
            "per_video": round(views / len(measured)) if measured else None,
            "videos": videos, "top_platform": top, "views_series": series,
            "followers": followers(db, days)}


def content(db, days=60):
    posts = _posts(db, days)
    by_video = {}
    for p in posts:
        v = by_video.setdefault(p["video_id"], {"video_id": p["video_id"], "title":
                                (p["title"] or "").splitlines()[0][:90], "at": p["published_at"],
                                "views": 0, "likes": 0, "comments": 0, "links": {}})
        v["views"] += p["views"] or 0
        v["likes"] += p["likes"] or 0
        v["comments"] += p["comments"] or 0
        if str(p["url"] or "").startswith("http"):
            v["links"][p["platform"]] = p["url"]
        v["thumb"] = f"{p['video_id']}.jpg" if (config.DATA / "thumbs" /
                                                 f"{p['video_id']}.jpg").exists() else ""
    items = sorted(by_video.values(), key=lambda v: -v["at"])
    median = sorted(v["views"] for v in items)[len(items) // 2] if items else 0
    for v in items:
        v["vs_median"] = round(v["views"] / median, 2) if median else None
    return items


def save_thumb(video_id, path):
    """A small picture of a posted clip for the studio (from its cover second)."""
    from . import video
    out = config.DATA / "thumbs" / f"{video_id}.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        video._run(["ffmpeg", "-y", "-v", "error", "-ss", "0.25", "-i", str(path),
                    "-frames:v", "1", "-vf", "scale=270:-2", "-q:v", "5", str(out)], timeout=60)
    except Exception as e:
        log.info("Thumbnail skipped: %s", e)


# ---------------------------------------------------------------- analysis log

def _read(name, default):
    try:
        return json.loads((config.DATA / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(name, data):
    config.atomic_write(config.DATA / name, json.dumps(data, ensure_ascii=False, indent=1))


def note(text, kind="info"):
    entries = _read(LOG_FILE, [])
    entries.append({"at": time.time(), "kind": kind, "text": text})
    _write(LOG_FILE, entries[-300:])


def log_entries():
    return _read(LOG_FILE, [])[-120:][::-1]


# ---------------------------------------------------------------- growth plan

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "summary": {"type": "string"},
        "goal_followers": {"type": "integer"},
        "reels_min": {"type": "integer"}, "reels_max": {"type": "integer"},
        "weeks": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "theme": {"type": "string"},
            "reels": {"type": "integer"},
            "tasks": {"type": "array", "items": {"type": "string"}}},
            "required": ["title", "theme", "reels", "tasks"]}},
        "findings": {"type": "array", "items": {"type": "string"}},
        "misses": {"type": "array", "items": {"type": "string"}},
        "ideas": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "why": {"type": "string"},
            "based_on": {"type": "string"}}, "required": ["title", "why"]}},
        "rules": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["headline", "summary", "goal_followers", "reels_min", "reels_max", "weeks",
                 "findings", "misses", "ideas", "rules"],
}

PLAN_SYSTEM = """You are the Growth Engine of a short-video creator: their whole content team. You are the growth strategist of a short-video creator (TikTok, Instagram \
Reels, YouTube Shorts). The creator's tool clips streams and videos and posts them on its \
own. You get the account's real numbers: followers over time, every recent post with its \
views and interactions, the best posting hours found so far, what the virality predictor \
learned, and (when revising) the previous plan with the tasks done and the results since.

Write a 4-week growth plan that is concrete and doable:
- goal_followers: a realistic follower target for the 4 weeks from the current count and \
growth rate (ambitious but reachable).
- reels_min / reels_max: posts per week.
- weeks: 4 weeks, each with a short title (the principle, e.g. "كرر الفكرة، غيّر الزاوية"), \
a theme (what to post, e.g. "4 ريلز · مواضيع الأجهزة"), the number of reels, and 2-4 tasks \
that are specific, checkable actions based on THIS account's data (e.g. "اكتب 4 أفكار عن \
... وابدأ كل فيديو بمشكلة واضحة").
- findings: 3-6 sharp observations from the data about the audience: what they like, what \
they watch to the end, what they interact with most, and WHY it worked (hook, topic, length, \
posting time, platform). Use numbers.
- misses: 2-4 posts or ideas that did not work, each with the most likely reason.
- ideas: 5-8 concrete next videos. Most remake a post that worked in a new way (a new \
angle, a sequel, the same hook on another moment; name it in based_on); a few are fresh \
tests. Each with why it should work.
- rules: lasting lessons for this account (keep the good ones from before, add new ones).
- headline + summary: the plan in one line and two sentences.
When revising, say what changed and why based on the results. Write everything in Modern \
Standard Arabic (names of games, streamers and platforms as usually written)."""


NICHE_SCHEMA = {"type": "object", "properties": {"niches": {"type": "array", "items": {
    "type": "object", "properties": {
        "name": {"type": "string"}, "why": {"type": "string"},
        "audience": {"type": "string"}, "sources": {"type": "string"},
        "first_videos": {"type": "array", "items": {"type": "string"}},
        "difficulty": {"type": "string"}},
    "required": ["name", "why", "audience", "sources", "first_videos", "difficulty"]}}},
    "required": ["niches"]}

NICHE_SYSTEM = """You advise an Arabic-speaking creator (Iraq) who is starting a new account \
from zero on YouTube Shorts and Instagram Reels. Their tool makes short clips from long \
videos and live streams (YouTube, Twitch) with Arabic/English captions, and can also post \
their own videos and AI-made videos. Propose 6 content directions (niches) for 2026 that \
can grow fast from zero for an Arabic audience, that this tool can feed every day, and \
that are allowed to monetise. For each: the niche name, why it can grow now, the audience, \
where the clips come from (kinds of channels/streams/sources, or own/AI-made), 3 concrete \
first videos, and how hard it is (سهل/متوسط/صعب). Avoid every excluded topic completely. \
Avoid content that copies others without adding value (add translation, commentary, \
context). Write in Modern Standard Arabic."""


def niches(cfg):
    st = settings()
    brief = "Excluded topics (never suggest anything about them): " + \
        (", ".join(st["exclude"]) or "none")
    data, _ = llm.ask_json(NICHE_SYSTEM, brief, NICHE_SCHEMA, cfg, effort="high",
                           max_tokens=8000)
    return data["niches"]


def start_fresh(cfg, db, niche, exclude):
    """Start the engine from zero on a new niche: past posts no longer count, the old
    plan and log go, and a plan is written for the new direction."""
    save_settings(since=time.time(), niche=niche.strip(),
                  exclude=[x.strip() for x in exclude if x.strip()])
    for name in (PLAN_FILE, LOG_FILE):
        try:
            (config.DATA / name).unlink()
        except OSError:
            pass
    note(f"بداية جديدة من الصفر: {niche}", "plan")
    config.save_config(cfg, {"niche": niche.strip()})
    return make_plan(cfg, db)


def plan():
    return _read(PLAN_FILE, None)


def _context(cfg, db, previous=None):
    st = settings()
    head = [f"Platforms: {', '.join(st['platforms'])} only (plan for these, nothing else).",
            "Excluded topics (never plan, suggest or mention them): "
            + (", ".join(st["exclude"]) or "none")]
    if st.get("niche"):
        head.append(f"The account's content direction: {st['niche']}")
    if st.get("since"):
        head.append("The account restarted from zero on this direction on "
                    + time.strftime("%Y-%m-%d", time.localtime(st["since"]))
                    + ": earlier posts do not count. With few posts so far, plan how to "
                    "start well: the first videos, formats to test, posting rhythm.")
    return "\n".join(head) + "\n" + _context_numbers(cfg, db, previous)


def _context_numbers(cfg, db, previous=None):
    ov = overview(db, 30)
    posts = content(db, 30)[:40]
    fol = {p: {"now": v["now"], "change_14_days": v["delta14"],
               "series": v["series"][-30:]} for p, v in ov["followers"].items()}
    lines = [f"Niche: {settings().get('niche') or cfg.get('niche') or 'not chosen yet'}",
             f"Followers: {json.dumps(fol)}",
             f"Last 30 days: {ov['videos']} videos, {ov['views']} views, "
             f"{ov['interactions']} interactions ({ov['rate']}% of views), top platform "
             f"{ov['top_platform']}",
             "Posts (newest first): " + json.dumps(
                 [{"title": p["title"], "views": p["views"], "likes": p["likes"],
                   "comments": p["comments"], "x_median": p["vs_median"],
                   "posted": time.strftime("%a %H:%M", time.localtime(p["at"]))}
                  for p in posts], ensure_ascii=False)]
    try:
        own = timing.learned(db)[:4]
        if own:
            lines.append(f"Best posting hours learned: {json.dumps(own)}")
    except Exception:
        pass
    if previous:
        done = [t["text"] for w in previous.get("weeks", []) for t in w.get("tasks", [])
                if t.get("done")]
        lines.append("Previous plan: " + json.dumps(
            {k: previous.get(k) for k in ("headline", "goal_followers", "reels_min",
                                          "reels_max", "rules")}, ensure_ascii=False))
        lines.append(f"Tasks done: {json.dumps(done, ensure_ascii=False)}")
        lines.append(f"Followers when that plan started: {previous.get('start_followers')}")
    return "\n".join(lines)


def make_plan(cfg, db, revise=False):
    """Write (or rewrite) the growth plan with the AI. Returns it."""
    previous = plan() if revise else None
    note("يقرأ أرقام الحساب ويكتب " + ("مراجعة الخطة" if previous else "خطة نمو جديدة") + "…")
    data, who = llm.ask_json(PLAN_SYSTEM, _context(cfg, db, previous), PLAN_SCHEMA, cfg,
                             effort="high", max_tokens=12000)
    fol = followers(db)
    start = sum(v["now"] for v in fol.values()) if fol else None
    new = {**data, "created": time.time(), "by": who, "start_followers": start,
           "revision": (previous or {}).get("revision", 0) + 1 if previous else 1,
           "weeks": [{**w, "tasks": [{"text": t, "done": False} for t in w["tasks"]]}
                     for w in data["weeks"]]}
    _write(PLAN_FILE, new)
    for f in data["findings"]:
        note(f, "finding")
    for m in data.get("misses", []):
        note("لم ينجح: " + m, "miss")
    for i in data.get("ideas", [])[:8]:
        note("فكرة: " + i["title"] + (f" (من: {i['based_on']})" if i.get("based_on") else ""), "idea")
    note(("رُوجعت الخطة: " if previous else "خطة جديدة: ") + data["headline"], "plan")
    return new


def toggle_task(week, task, done):
    p = plan()
    if not p:
        raise ValueError("لا توجد خطة بعد.")
    p["weeks"][week]["tasks"][task]["done"] = bool(done)
    _write(PLAN_FILE, p)
    if done:
        note("أُنجزت مهمة: " + p["weeks"][week]["tasks"][task]["text"], "task")
    return p


def tick(cfg, db):
    """Called by the publishing loop: record followers; once a week revise the plan."""
    record(cfg, db)
    p = plan()
    if p and time.time() - p.get("created", 0) > REVISE_EVERY and llm.providers(cfg):
        try:
            make_plan(cfg, db, revise=True)
        except Exception as e:
            log.info("Plan revision postponed: %s", e)


def guidance():
    """What the growth engine learned, for the clipper and the caption writer to follow."""
    p = plan()
    if not p:
        return ""
    lines = [f"What works for this account (from its growth engine): {r}" for r in p.get("rules", [])[:6]]
    themes = [w.get("theme", "") for w in p.get("weeks", [])[:1]]
    if themes and themes[0]:
        lines.append(f"This week's content theme: {themes[0]}")
    return "\n".join(lines)
