"""When to post: the known busy hours of each platform, and the hours that worked best
for this account, learned from its own posts.

All hours are the computer's local time, which is taken to be the audience's time.
The built-in hours are a starting point for an Arab audience (Iraq and the Gulf: the
evening and late night are the busiest, and the weekend is Friday and Saturday). Once
enough posts have been measured, the account's own best hours replace them.
"""
import statistics
import time

from . import store

DAYS = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]  # tm_wday 0..6

# Starting points, gathered from the large studies (Buffer, Sprout Social, RivalIQ,
# Later, SocialPilot) and shifted to how Arab audiences use their phones.
GUIDE = {
    "tiktok": {
        "name": "تيك توك",
        "peaks": [(18, 23, "الذروة الأقوى: بعد العمل والدراسة حتى منتصف الليل"),
                  (12, 14, "استراحة الظهر"),
                  (8, 10, "الصباح الباكر")],
        "days": [3, 4, 1],  # Thursday, Friday, Tuesday
        "notes": [
            "أول ساعة بعد النشر هي الأهم: الخوارزمية تعرض المقطع لمجموعة صغيرة، وإذا شاهدوه "
            "حتى النهاية وتفاعلوا يُعرض لعدد أكبر.",
            "انشر 1 إلى 4 مقاطع يوميا، بفاصل ساعتين على الأقل، أفضل من نشر كثير دفعة واحدة.",
            "أول 2–3 ثوان تحدد إن كان المشاهد سيكمل: ابدأ باللقطة الأقوى.",
            "تيك توك يوزع المقطع أياما وأسابيع، فالتوقيت يساعد البداية فقط ولا يحدد النجاح وحده.",
        ],
    },
    "instagram": {
        "name": "انستغرام (Reels)",
        "peaks": [(19, 23, "الذروة الأقوى: المساء"),
                  (11, 13, "قبل الظهر"),
                  (22, 1, "آخر الليل: يتفاعل فيه جمهور الريلز كثيرا")],
        "days": [0, 3, 5],  # Monday, Thursday, Saturday
        "notes": [
            "الريلز يُعرض لغير المتابعين أيضا: المشاهدة حتى النهاية والمشاركة (إرسال لصديق) "
            "أقوى إشارتين.",
            "ريل واحد إلى اثنين يوميا كاف، والاستمرار أهم من الكثرة.",
            "تفاعلك مع التعليقات في أول ساعة يرفع وصول المنشور.",
        ],
    },
    "youtube": {
        "name": "يوتيوب Shorts",
        "peaks": [(18, 22, "المساء"), (12, 15, "الظهر")],
        "days": [4, 5, 6],  # Friday, Saturday, Sunday
        "notes": ["Shorts يحصل على أغلب مشاهداته خلال 48 ساعة، والعطلة الأسبوعية أقوى."],
    },
}

MIN_POSTS = 12    # measured posts needed before the account's own hours are trusted
MIN_PER_SLOT = 2  # posts needed in a 3-hour slot to judge it
SLOT = 3          # hours per slot


def _in(hour, start, end):
    return start <= hour < end if start < end else hour >= start or hour < end


def default_peak(hour, wday, platforms):
    """True when the hour is a main busy time on any platform being used."""
    for p in platforms:
        g = GUIDE.get(p)
        if g and _in(hour, *g["peaks"][0][:2]):
            return True
    return False


def measured(db, horizon=24 * 3600):
    """[(published_at, views)]: for each post at least a day old, the views it had
    about a day after publishing (the part of its life timing can influence)."""
    now = time.time()
    rows = db.execute(
        "SELECT p.id, p.published_at,"
        " (SELECT MAX(s.views) FROM stats s WHERE s.post_id = p.id"
        "    AND s.ts <= p.published_at + ?) views"
        " FROM posts p JOIN videos v ON v.id = p.video_id"
        " WHERE p.error IS NULL AND p.remote_id IS NOT NULL AND v.parent_id IS NULL"
        "   AND p.published_at <= ?", (horizon * 1.5, now - horizon)).fetchall()
    return [(r["published_at"], r["views"]) for r in rows if r["views"] is not None]


def learned(db):
    """The account's own slots: [{'start': h, 'end': h, 'ratio': x, 'posts': n}], best
    first, or [] while there are too few measured posts."""
    data = measured(db)
    if len(data) < MIN_POSTS:
        return []
    median = statistics.median(v for _, v in data) or 1
    slots = {}
    for at, views in data:
        h = time.localtime(at).tm_hour // SLOT * SLOT
        slots.setdefault(h, []).append(views / median)
    out = [{"start": h, "end": (h + SLOT) % 24, "ratio": round(statistics.median(r), 2),
            "posts": len(r)} for h, r in slots.items() if len(r) >= MIN_PER_SLOT]
    return sorted(out, key=lambda s: -s["ratio"])


def learned_days(db):
    data = measured(db)
    if len(data) < MIN_POSTS:
        return []
    median = statistics.median(v for _, v in data) or 1
    days = {}
    for at, views in data:
        days.setdefault(time.localtime(at).tm_wday, []).append(views / median)
    out = [{"day": d, "name": DAYS[d], "ratio": round(statistics.median(r), 2), "posts": len(r)}
           for d, r in days.items() if len(r) >= MIN_PER_SLOT]
    return sorted(out, key=lambda s: -s["ratio"])


def peak_now(cfg, db, now=None):
    """(is it a good moment to post, why) for the current hour."""
    t = time.localtime(now or time.time())
    own = [s for s in learned(db) if s["ratio"] >= 1.15]
    if own:
        for s in own:
            if _in(t.tm_hour, s["start"], s["end"]):
                return True, f"own:{s['start']}-{s['end']}"
        return False, "own"
    platforms = cfg.get("platforms") or ["tiktok", "instagram"]
    if default_peak(t.tm_hour, t.tm_wday, platforms):
        return True, "guide"
    return False, "guide"


def next_peak(cfg, db, now=None):
    """Seconds until the next good hour starts (0 when it is one now)."""
    now = now or time.time()
    base = now - now % 3600
    for i in range(0, 25):
        if peak_now(cfg, db, base + i * 3600)[0]:
            return max(0, base + i * 3600 - now)
    return None


def engagement(db, days=30):
    """Totals over the last `days` from each post's latest numbers."""
    rows = db.execute(
        "SELECT p.platform,"
        " (SELECT views FROM stats s WHERE s.post_id=p.id ORDER BY ts DESC LIMIT 1) views,"
        " (SELECT likes FROM stats s WHERE s.post_id=p.id ORDER BY ts DESC LIMIT 1) likes,"
        " (SELECT comments FROM stats s WHERE s.post_id=p.id ORDER BY ts DESC LIMIT 1) comments,"
        " (SELECT shares FROM stats s WHERE s.post_id=p.id ORDER BY ts DESC LIMIT 1) shares"
        " FROM posts p WHERE p.error IS NULL AND p.published_at >= ?",
        (time.time() - days * 86400,)).fetchall()
    out = {}
    for r in rows:
        if r["views"] is None and r["likes"] is None:
            continue  # not measured yet
        t = out.setdefault(r["platform"], {"posts": 0, "views": 0, "likes": 0,
                                           "comments": 0, "shares": 0, "_acts": 0})
        t["posts"] += 1
        for k in ("views", "likes", "comments", "shares"):
            t[k] += r[k] or 0
        if r["views"]:  # the rate only over posts whose views are known
            t["_acts"] += (r["likes"] or 0) + (r["comments"] or 0) + (r["shares"] or 0)
    for t in out.values():
        acts = t.pop("_acts")
        t["rate"] = round(100 * acts / t["views"], 1) if t["views"] else None
    return out


def summary(cfg, db):
    ok, why = peak_now(cfg, db)
    wait = next_peak(cfg, db)
    return {
        "peak_now": ok, "source": "own" if why.startswith("own") else "guide",
        "next_peak_minutes": None if wait is None else round(wait / 60),
        "learned": learned(db), "learned_days": learned_days(db),
        "measured": len(measured(db)), "needed": MIN_POSTS,
        "engagement": engagement(db),
        "guide": {p: {"name": g["name"], "days": [DAYS[d] for d in g["days"]],
                      "peaks": [{"start": a, "end": b, "note": n} for a, b, n in g["peaks"]],
                      "notes": g["notes"]} for p, g in GUIDE.items()},
        "timezone": time.strftime("%Z") or "",
    }
