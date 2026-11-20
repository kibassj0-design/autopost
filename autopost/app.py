"""The desktop app: a local page (127.0.0.1 only) to paste links, watch the work,
manage the queue, see what was published and edit keys and settings."""
import collections
import json
import re
import logging
import mimetypes
import os
import secrets
import shutil
import subprocess
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import backup, checks, config, llm, notify, store, timing, updater, video, virality
from .platforms import tiktok, youtube
from .publisher import Publisher

log = logging.getLogger("autopost")

PORT = 8765  # also the YouTube sign-in redirect, so the app can finish that login itself
SECRET_KEYS = ["FB_APP_SECRET", "GEMINI_API_KEY", "MISTRAL_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY",
               "CEREBRAS_API_KEY", "ANTHROPIC_API_KEY", "YT_CLIENT_SECRET", "IG_ACCESS_TOKEN",
               "TT_CLIENT_SECRET", "TG_BOT_TOKEN"]
PLAIN_KEYS = ["FB_APP_ID", "FB_CONFIG_ID", "YT_CLIENT_ID", "IG_USER_ID", "IG_GRAPH_HOST", "TT_CLIENT_KEY",
              "TT_REDIRECT_URI"]
# Settings the page may change: name -> type (nested ones as "section.name").
SETTINGS = {
    "platforms": list, "language": str, "post_language": str, "niche": str, "brand_voice": str,
    "default_hashtags": list, "orientation": str, "ai_provider": str,
    "min_minutes_between_posts": int, "fps": int, "resolution": str, "speed_mode": str, "auto_update": bool, "encoder": str, "youtube_mode": str, "tiktok_mode": str,
    "clipper.min_score": int, "clipper.min_seconds": int, "clipper.max_seconds": int,
    "clipper.whisper_model": str, "clipper.whisper_device": str, "clipper.captions": bool,
    "clipper.captions_language": str, "clipper.caption_style": str, "clipper.layout": str,
    "clipper.title_overlay": bool, "clipper.jump_cuts": bool, "clipper.cover": bool,
    "clipper.cover_ms": int,
    "notify.enabled": bool, "notify.ntfy": bool, "notify.on_posted": bool,
    "notify.on_failed": bool, "notify.on_clips": bool, "notify.on_momentum": bool,
    "notify.on_paused": bool,
    "momentum.enabled": bool, "momentum.views_per_hour": int,
    "virality.enabled": bool, "virality.ai_judge": bool, "virality.best_first": bool,
    "virality.min_score": int,
    "smart_timing.enabled": bool, "smart_timing.peak_gap_minutes": int,
    "smart_timing.peaks_only": bool,
}


TIMED = ("download", "audio", "transcribe", "transcribe_clips", "pick", "translate", "render",
         "waiting")
SCHEDULED = "scheduled"  # folder under data/ holding videos waiting for their time


# What each kind of key looks like, so one pasted into the wrong field is caught on Save.
KEY_SHAPES = [
    (r"^GOCSPX-", "YT_CLIENT_SECRET", "Client secret الخاص بيوتيوب (Google)"),
    (r"\.apps\.googleusercontent\.com$", "YT_CLIENT_ID", "Client ID الخاص بيوتيوب"),
    (r"^(AIza|AQ\.)", "GEMINI_API_KEY", "مفتاح Gemini"),
    (r"^gsk_", "GROQ_API_KEY", "مفتاح Groq"),
    (r"^sk-or-", "OPENROUTER_API_KEY", "مفتاح OpenRouter"),
    (r"^csk-", "CEREBRAS_API_KEY", "مفتاح Cerebras"),
    (r"^sk-ant-", "ANTHROPIC_API_KEY", "مفتاح Claude"),
    (r"^(EAA|IG)", "IG_ACCESS_TOKEN", "توكن انستغرام"),
]
FIELD_NAMES = {"YT_CLIENT_SECRET": "Client secret يوتيوب", "YT_CLIENT_ID": "Client ID يوتيوب",
               "GEMINI_API_KEY": "Gemini", "GROQ_API_KEY": "Groq", "MISTRAL_API_KEY": "Mistral",
               "OPENROUTER_API_KEY": "OpenRouter", "CEREBRAS_API_KEY": "Cerebras",
               "ANTHROPIC_API_KEY": "Claude", "FB_APP_SECRET": "App secret فيسبوك",
               "TT_CLIENT_SECRET": "Client secret تيك توك", "TT_CLIENT_KEY": "Client key تيك توك",
               "IG_ACCESS_TOKEN": "توكن انستغرام", "FB_APP_ID": "App ID فيسبوك",
               "TG_BOT_TOKEN": "توكن بوت تيليجرام"}


def _wrong_field(field, value):
    """An Arabic explanation when a value clearly belongs to another field, else ''."""
    if not value:
        return ""
    for pattern, owner, what in KEY_SHAPES:
        if re.search(pattern, value) and owner != field and field in FIELD_NAMES:
            if field == "IG_ACCESS_TOKEN" and owner == "IG_ACCESS_TOKEN":
                continue
            return (f"الذي لصقته في خانة «{FIELD_NAMES[field]}» يبدو {what}، لا يخص هذه "
                    f"الخانة، فلم يُحفظ. الصقه في خانته الصحيحة.")
    if field == "YT_CLIENT_SECRET" and not value.startswith("GOCSPX-"):
        return ("Client secret يوتيوب يبدأ بـ GOCSPX- ، والذي لصقته لا يبدأ بها فلم يُحفظ. "
                "انسخه من Google Cloud ← Credentials، أو استورد ملف JSON.")
    return ""


class LogBuffer(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = collections.deque(maxlen=400)

    def emit(self, record):
        msg = re.sub(r"(access_token|key|token)=[^&\s)'\"]+", r"\1=***", record.getMessage())
        self.lines.append({"t": time.strftime("%H:%M:%S", time.localtime(record.created)),
                           "level": record.levelname, "msg": msg})


class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.token = secrets.token_urlsafe(24)
        self.oauth_state = secrets.token_urlsafe(16)
        self.logs = LogBuffer()
        logging.getLogger().addHandler(self.logs)
        self.publisher = Publisher(cfg)
        self.jobs = []
        self.stops = {}
        self.cancels = {}
        self.durations = {}
        threading.Thread(target=self.publisher.run_forever, daemon=True).start()
        self.update = {"available": False, "latest": None, "current": updater.installed().get("sha"),
                       "checked": None, "error": "", "busy": False}
        threading.Thread(target=self._update_loop, daemon=True).start()
        threading.Thread(target=self._backup_loop, daemon=True).start()
        threading.Thread(target=self._predict_loop, daemon=True).start()
        threading.Thread(target=self._tools_loop, daemon=True).start()
        threading.Thread(target=self._fetch_assets, daemon=True).start()
        self._resume_jobs()

    def _fetch_assets(self):
        """Get the caption font and the face detector in the background at start, so the
        first clip doesn't wait for them."""
        from . import assets
        assets.fonts_dir()
        try:
            assets.face_net()
        except Exception as e:
            log.info("Face detector unavailable: %s", e)

    def _predict_loop(self):
        """Score each new clip (queue and schedule) once, in the background."""
        db = store.connect()
        self.publisher.scores.update(virality.all_scores(db))
        failed = set()
        while True:
            time.sleep(10)
            if not self.cfg.get("virality", {}).get("enabled", True):
                continue
            path = None
            try:
                todo = [p for p in self.publisher.queued()
                        if p.name not in self.publisher.scores and p.name not in failed]
                todo += [Path(r["path"]) for r in self.schedule_list()
                         if r["status"] == "pending" and r["exists"]
                         and r["name"] not in self.publisher.scores and r["name"] not in failed]
                if not todo:
                    continue
                path = todo[0]
                if path.stat().st_size == 0 or time.time() - path.stat().st_mtime < 5:
                    continue  # still being written
                note_file = path.with_suffix(".txt")
                note = note_file.read_text(encoding="utf-8") if note_file.exists() else ""
                data = virality.predict(path, note, self.cfg, db)
                self.publisher.scores[path.name] = data["score"]
                log.info("Virality %s: %d/100 (%s)", path.name, data["score"], data["level"])
            except Exception as e:
                if path is not None:
                    failed.add(path.name)  # not again this run; "أعد التقييم" can retry it
                log.warning("Virality prediction failed: %s", str(e)[:200])

    def _tools_loop(self):
        """Keep yt-dlp current: YouTube and Twitch change often, and an old yt-dlp stops
        downloading. Checked at start and every 2 days; the new one is used for the next
        link (no restart needed for links not yet started)."""
        import subprocess
        import sys
        stamp = config.DATA / "yt-dlp-updated"
        while True:
            try:
                last = float(stamp.read_text()) if stamp.exists() else 0
            except ValueError:
                last = 0
            if time.time() - last > 2 * 86400:
                try:
                    r = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "-q",
                                        "--disable-pip-version-check", "yt-dlp[default]"],
                                       capture_output=True, text=True, timeout=300)
                    if r.returncode == 0:
                        stamp.write_text(str(time.time()))
                        log.info("yt-dlp is up to date")
                    else:
                        log.info("yt-dlp update skipped: %s", r.stderr.strip()[-160:])
                except Exception as e:
                    log.info("yt-dlp update skipped: %s", e)
            time.sleep(6 * 3600)

    def _backup_loop(self):
        while True:  # posts and schedule change all day; keys are saved when they change
            time.sleep(3600)
            backup.save()

    # ---- clip jobs

    JOBS_FILE = "jobs.json"  # jobs not finished yet, so a restart carries them on

    def _save_jobs(self):
        keep = [{k: j.get(k) for k in ("url", "title", "start", "end", "back", "past_only",
                                        "live", "work", "began", "last_seen")}
                for j in self.jobs if j["status"] in ("queued", "running")]
        try:
            config.atomic_write(config.DATA / self.JOBS_FILE,
                                json.dumps(keep, ensure_ascii=False, default=str))
        except OSError as e:
            log.info("Could not save the job list: %s", e)

    def _resume_jobs(self):
        """Links that were still being worked on when the tool closed (an update, a crash,
        a power cut) start again on their own, from where they were."""
        try:
            saved = json.loads((config.DATA / self.JOBS_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for j in saved:
            if j.get("live") and not j.get("past_only"):
                # Follow the stream again, and first clip the minutes missed meanwhile.
                gap = time.time() - (j.get("last_seen") or time.time())
                back = int(min(max(gap + 90, 120), 6 * 3600))
                log.info("Resuming the live stream %s (and the %d min missed)", j["url"],
                         back // 60)
                self.add_job(j["url"], back=back)
            else:
                log.info("Resuming %s from where it stopped", j.get("title") or j["url"])
                self.add_job(j["url"], None, j.get("start"), j.get("end"), j.get("back"),
                             bool(j.get("past_only")), work=j.get("work"))

    def add_job(self, url, count=None, start=None, end=None, back=None, past_only=False,
                work=None):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        job = {"id": len(self.jobs) + 1, "url": url, "title": "", "status": "queued",
               "stage": "queued", "fraction": None, "detail": "", "warnings": [],
               "error": "", "clips": 0, "start": start, "end": end, "live": False,
               "back": back, "past_only": past_only, "last_seen": time.time(),
               "work": work or str(config.DATA / "clips" / f"{stamp}-{len(self.jobs) + 1}"),
               "began": time.time(), "ended": None, "timings": {}, "stage_at": time.time()}
        self.jobs.append(job)
        self.stops[job["id"]] = threading.Event()
        self.cancels[job["id"]] = threading.Event()
        # Each job has its own thread, so a live stream being followed doesn't hold up
        # other links; the heavy part of each job still runs one at a time.
        threading.Thread(target=self._work, args=(job,), daemon=True).start()
        self._save_jobs()
        return job

    def stop_job(self, job_id):
        """A normal link is cancelled at once. A live stream first stops recording and
        clips what it has; pressing again while it finishes cancels it outright."""
        job = next((j for j in self.jobs if j["id"] == job_id), None)
        if job is None or job_id not in self.stops:
            return
        if job["live"] and not job.get("stopping"):
            job["stopping"] = True
            self.stops[job_id].set()
        else:
            self.cancels[job_id].set()
            self.stops[job_id].set()

    def _work(self, job):
        from . import clipper
        job["status"] = "running"
        self._save_jobs()

        def progress(stage, fraction, detail, job=job):
            if time.time() - job.get("last_seen", 0) > 15:
                job["last_seen"] = time.time()  # how far a live stream got, for a restart
                self._save_jobs()
            if stage == "title":
                job["title"] = detail
                return
            if stage == "warning":
                if detail not in job["warnings"]:
                    job["warnings"].append(detail)
                return
            if stage != job["stage"]:  # time spent per step, shown when the job ends
                now = time.time()
                if job["stage"] in TIMED:
                    job["timings"][job["stage"]] = round(
                        job["timings"].get(job["stage"], 0) + now - job["stage_at"])
                job["stage_at"] = now
            job["stage"], job["fraction"] = stage, fraction
            if stage in ("audio", "transcribe") and detail and not job["live"]:
                job["title"] = detail
            elif stage == "live":
                job["live"], job["detail"] = True, detail
                m = re.search(r"(\d+) clips", detail or "")
                made = int(m.group(1)) if m else 0
                if made > job.get("notified", 0):
                    notify.send(self.cfg, "clips", "مقاطع جديدة من البث",
                                f"{job['title'] or 'البث المباشر'}: {made} مقطع حتى الآن، "
                                f"آخرها دخل طابور النشر.")
                    job["notified"] = made
            elif stage == "render":
                job["detail"] = detail

        result = {}
        try:
            clips = clipper.run(job["url"], self.cfg, start=job["start"], end=job["end"],
                                progress=progress, stop=self.stops[job["id"]],
                                cancel=self.cancels[job["id"]], back=job.get("back"),
                                past_only=job.get("past_only", False), work=job["work"])
            result = dict(status="done", stage="done", fraction=1, clips=len(clips))
            if clips and not job.get("live"):
                notify.send(self.cfg, "clips", "مقاطع جديدة جاهزة",
                            f"{job['title'] or job['url']}: {len(clips)} مقطع في طابور النشر.")
        except clipper.Cancelled:
            result = dict(status="cancelled", stage="cancelled")
        except Exception as e:
            log.error("Could not process %s: %s", job["url"], e)
            result = dict(status="error", error=str(e)[:400])
            notify.send(self.cfg, "failed", "تعذّر صنع المقاطع",
                        f"{job['title'] or job['url']}: {str(e)[:200]}")
        job["ended"] = time.time()
        if job["stage"] in TIMED:  # close the last step's timing
            job["timings"][job["stage"]] = round(
                job["timings"].get(job["stage"], 0) + job["ended"] - job["stage_at"])
        job.update(result)
        self._save_jobs()

    # ---- updates

    def idle(self):
        """Nothing that an update and restart would interrupt."""
        busy_jobs = any(j["status"] in ("queued", "running") for j in self.jobs)
        return not busy_jobs and self.publisher.current is None

    def check_update(self):
        try:
            info = updater.check(self.cfg)
            self.update.update(available=info["available"], latest=info["latest"],
                               current=info["current"], checked=time.time(), error="")
        except Exception as e:
            self.update.update(checked=time.time(), error=str(e)[:200])
        return self.update

    def apply_update(self):
        """Install the newest version, then exit so start.bat starts the new one."""
        self.update["busy"] = True
        backup.save()
        try:
            updater.apply(self.cfg, self.update.get("latest"))
        except Exception as e:
            self.update.update(busy=False, error=str(e)[:200])
            raise RuntimeError(f"تعذّر التحديث: {e}")
        threading.Timer(1.5, lambda: os._exit(updater.RESTART_CODE)).start()

    def update_when_free(self, limit=15 * 60):
        """Update as soon as the post being published is done (at most `limit` seconds
        later); no new post starts meanwhile."""
        if self.update.get("pending"):
            return
        self.update["pending"] = True
        self.publisher.hold = True

        def wait():
            began = time.time()
            while self.publisher.current is not None and time.time() - began < limit:
                time.sleep(3)
            try:
                self.apply_update()
            except Exception as e:
                log.warning("Update failed: %s", e)
                self.update["pending"] = False
                self.publisher.hold = False

        threading.Thread(target=wait, daemon=True).start()

    def _update_loop(self):
        time.sleep(20)  # let the app settle first
        while True:
            self.check_update()
            if self.update["available"] and self.cfg.get("auto_update", True):
                # Wait for a quiet moment: never in the middle of a clip or a post.
                while not self.idle():
                    time.sleep(30)
                try:
                    log.info("Installing the new version automatically")
                    self.apply_update()
                    return
                except Exception as e:
                    log.warning("Automatic update failed: %s", e)
            time.sleep(6 * 3600)

    # ---- schedule

    def schedule_dir(self):
        d = config.DATA / SCHEDULED
        d.mkdir(parents=True, exist_ok=True)
        return d

    def schedule_add(self, file, at, caption="", platforms=None, note="", title=""):
        path = self.schedule_dir() / Path(file).name
        if not path.is_file():
            raise ValueError("الملف غير موجود، ارفعه من جديد.")
        db = store.connect()
        db.execute("INSERT INTO schedule(path,title,caption,note,platforms,at,status,created)"
                   " VALUES(?,?,?,?,?,?, 'pending', ?)",
                   (str(path), title or re.sub(r"^\d+-", "", path.stem), caption, note,
                    json.dumps(platforms or self.cfg["platforms"]), float(at), time.time()))
        db.commit()
        db.close()

    def schedule_from_queue(self, name, at, caption=""):
        """Move a queued clip (with its note and YouTube copy) out of the queue to its time."""
        src = self.publisher.inbox / Path(name).name
        if not src.is_file():
            raise ValueError("المقطع لم يعد في الطابور.")
        if src.name == self.publisher.current:
            raise ValueError("هذا المقطع يُنشر الآن.")
        dst = self.schedule_dir() / f"{int(time.time())}-{src.name}"
        note_file = src.with_suffix(".txt")
        note = note_file.read_text(encoding="utf-8") if note_file.exists() else ""
        title = note.splitlines()[0].removeprefix("Clip title: ") if note else ""
        alt = src.with_suffix(".alt.mp4")
        shutil.move(src, dst)
        if alt.exists():
            shutil.move(alt, dst.with_suffix(".alt.mp4"))
        note_file.unlink(missing_ok=True)
        self.schedule_add(dst.name, at, caption, None, note, title)

    def schedule_update(self, item_id, at=None, caption=None, platforms=None):
        db = store.connect()
        row = db.execute("SELECT * FROM schedule WHERE id=?", (item_id,)).fetchone()
        if row is None or row["status"] not in ("pending", "failed", "cancelled"):
            db.close()
            raise ValueError("لا يمكن تعديل هذا المنشور الآن.")
        db.execute("UPDATE schedule SET at=?, caption=?, platforms=?, status='pending' WHERE id=?",
                   (float(at if at is not None else row["at"]),
                    caption if caption is not None else row["caption"],
                    json.dumps(platforms) if platforms is not None else row["platforms"],
                    item_id))
        db.commit()
        db.close()

    def schedule_delete(self, item_id):
        db = store.connect()
        row = db.execute("SELECT * FROM schedule WHERE id=?", (item_id,)).fetchone()
        if row is not None:
            if row["status"] == "publishing":
                db.close()
                raise ValueError("هذا المنشور يُنشر الآن.")
            if row["status"] == "pending":  # a published one keeps its file for the record
                for f in (Path(row["path"]), Path(row["path"]).with_suffix(".alt.mp4")):
                    f.unlink(missing_ok=True)
            db.execute("DELETE FROM schedule WHERE id=?", (item_id,))
            db.commit()
        db.close()

    def schedule_list(self):
        db = store.connect()
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM schedule WHERE status!='done' OR at > ? ORDER BY at",
            (time.time() - 7 * 86400,))]
        db.close()
        for r in rows:
            r["platforms"] = json.loads(r["platforms"] or "[]")
            r["result"] = json.loads(r["result"]) if r["result"] else None
            r["name"] = Path(r["path"]).name
            r["exists"] = Path(r["path"]).is_file()
            r["viral"] = self.publisher.scores.get(r["name"])
        return rows

    # ---- state for the page

    def _duration(self, path):
        key = (path.name, path.stat().st_size)
        if key not in self.durations:
            try:
                self.durations[key] = video.probe(path)["duration"]
            except Exception:
                self.durations[key] = None
        return self.durations[key]

    def state(self):
        pub = self.publisher
        clips = []
        for p in pub.queued():
            note = p.with_suffix(".txt")
            title = ""
            if note.exists():
                first = note.read_text(encoding="utf-8").splitlines()[:1]
                title = first[0].removeprefix("Clip title: ") if first else ""
            clips.append({"name": p.name, "title": title or p.stem,
                          "seconds": self._duration(p), "viral": pub.scores.get(p.name),
                          "cover": p.with_suffix(".jpg").name
                          if p.with_suffix(".jpg").exists() else ""})
        db = store.connect()
        posts = [dict(r) for r in db.execute(
            "SELECT p.id, p.platform, p.url, p.error, p.published_at, v.title, v.parent_id,"
            " v.source,"
            " (SELECT views FROM stats s WHERE s.post_id = p.id ORDER BY ts DESC LIMIT 1) views,"
            " (SELECT likes FROM stats s WHERE s.post_id = p.id ORDER BY ts DESC LIMIT 1) likes,"
            " (SELECT comments FROM stats s WHERE s.post_id = p.id ORDER BY ts DESC LIMIT 1) comments,"
            " (SELECT shares FROM stats s WHERE s.post_id = p.id ORDER BY ts DESC LIMIT 1) shares"
            " FROM posts p JOIN videos v ON v.id = p.video_id ORDER BY p.id DESC LIMIT 100")]
        db.close()
        for post in posts:
            post["title"] = post["title"].splitlines()[0] if post["title"] else ""
            source = post.pop("source") or ""
            post["can_retry"] = bool(post["error"] and source and Path(source).exists())
            post["retrying"] = post["id"] in self.publisher.retries
        connected = {p: config.connected(p) for p in ("youtube", "instagram", "tiktok")}
        tokens = config.get_tokens()
        accounts = {p: tokens.get(p, {}).get("username", "") if connected[p] else ""
                    for p in connected}
        account_errors = {p: tokens.get(p, {}).get("error", "") for p in connected
                          if not connected[p] and tokens.get(p, {}).get("error")}
        return {
            "jobs": self.jobs[-20:][::-1],
            "queue": clips,
            "posts": posts,
            "account_errors": account_errors,
            "publisher": {
                "paused": pub.paused, "pause_reason": pub.pause_reason, "current": pub.current,
                "next_post_at": pub.next_post_at(), "now": time.time(),
                "has_account": any(connected[p] for p in self.cfg["platforms"]),
            },
            "connected": connected,
            "accounts": accounts,
            "enabled": self.cfg["platforms"],
            "ai": llm.providers(self.cfg),
            "ai_status": llm.status(self.cfg),
            "tiktok_inbox_wait": tiktok.inbox_wait() if config.connected("tiktok") else 0,
            "encoder": video.encoder()[0],
            "schedule": self.schedule_list(),
            "update": {k: v for k, v in self.update.items()},
            "logs": list(self.logs.lines)[-200:],
        }

    def settings(self):
        keys = {}
        for k in SECRET_KEYS:
            v = os.environ.get(k, "")
            keys[k] = {"set": bool(v), "hint": ("…" + v[-4:]) if len(v) > 8 else ""}
        for k in PLAIN_KEYS:
            keys[k] = {"set": bool(os.environ.get(k)), "value": os.environ.get(k, "")}
        values = {}
        for name in SETTINGS:
            section, _, key = name.rpartition(".")
            values[name] = self.cfg[section][key] if section else self.cfg[key]
        return {"keys": keys, "values": values}

    def save(self, body):
        env = {}
        for k, v in (body.get("keys") or {}).items():
            if k not in SECRET_KEYS + PLAIN_KEYS or not isinstance(v, str):
                continue
            v = v.strip().strip("\"'")
            if k == "ANTHROPIC_API_KEY" and v and not v.startswith("sk-ant-"):
                raise ValueError("مفتاح Claude يبدأ بـ sk-ant- . هذه القيمة ليست مفتاح Claude.")
            wrong = _wrong_field(k, v)
            if wrong:
                raise ValueError(wrong)
            env[k] = v
        for k in body.get("clear") or []:
            if k in SECRET_KEYS + PLAIN_KEYS:
                env[k] = ""
        env = {k: v for k, v in env.items() if v or k in (body.get("clear") or [])}
        if env.get("IG_ACCESS_TOKEN", "").startswith("IG"):
            env["IG_GRAPH_HOST"] = "graph.instagram.com"  # Instagram-login tokens
        if env:
            config.write_env(env)
            # A new key may see different models, and may revive a failed service.
            llm._gemini_models = None
            llm._compat_models.clear()
            llm._dead.clear()
        changes = {}
        for name, value in (body.get("values") or {}).items():
            kind = SETTINGS.get(name)
            if kind is None:
                continue
            if kind is int:
                value = int(value)
            elif kind is bool:
                value = bool(value)
            elif kind is list:
                value = [str(x).strip() for x in value if str(x).strip()]
            else:
                value = str(value).strip()
            section, _, key = name.rpartition(".")
            if section:
                changes.setdefault(section, {})[key] = value
            else:
                changes[key] = value
        clip = {**self.cfg["clipper"], **changes.get("clipper", {})}
        if not 5 <= clip["min_seconds"] <= clip["max_seconds"] <= 180:
            raise ValueError("مدة المقطع: الحد الأدنى يجب أن يكون أصغر من الأقصى، والأقصى 180 ثانية.")
        if changes.get("youtube_mode") not in (None, "short", "both", "video"):
            raise ValueError("youtube_mode: short / both / video")
        if changes.get("clipper", {}).get("whisper_device") not in (None, "cpu", "gpu"):
            raise ValueError("whisper_device: cpu / gpu")
        if changes.get("encoder") not in (None, "auto", "cpu"):
            raise ValueError("encoder: auto / cpu")
        if changes.get("encoder"):
            video.QUALITY["encoder"] = changes["encoder"]
            video._encoder = None  # test again with the new choice
        if changes.get("resolution"):
            video.QUALITY["resolution"] = str(changes["resolution"])
        if changes.get("fps") not in (None, 30, 60):
            raise ValueError("عدد الإطارات: 30 أو 60.")
        if changes:
            config.save_config(self.cfg, changes)

    # ---- account sign-in

    def auth_start(self, platform):
        if platform == "facebook":
            from .platforms import instagram
            return instagram.fb_auth_url(self.oauth_state)
        if platform == "youtube":
            return youtube.auth_url(self.oauth_state)
        return tiktok.auth_url()

    def instagram_connect(self, token, user_id=""):
        from .platforms import instagram
        if not token.strip():
            raise ValueError("الصق التوكن أولا.")
        name = instagram.connect(token, user_id.strip() or None)
        if "instagram" not in self.cfg["platforms"]:
            config.save_config(self.cfg, {"platforms": self.cfg["platforms"] + ["instagram"]})
        return name

    def disconnect(self, platform):
        tokens = config.get_tokens()
        tokens.pop(platform, None)
        config.atomic_write(config.TOKENS, json.dumps(tokens, indent=2))
        if platform == "instagram":
            config.write_env({"IG_ACCESS_TOKEN": "", "IG_USER_ID": ""})
        config.save_config(self.cfg, {"platforms": [p for p in self.cfg["platforms"]
                                                    if p != platform]})

    def auth_finish(self, platform, pasted):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
        code = query.get("code", [pasted.strip()])[0]
        {"youtube": youtube, "tiktok": tiktok}[platform].exchange(code)
        if platform not in self.cfg["platforms"]:
            config.save_config(self.cfg, {"platforms": self.cfg["platforms"] + [platform]})


PAGE = Path(__file__).with_name("ui.html")


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False)
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _local(self):
            # Only this computer's own browser, addressed as localhost: blocks other
            # websites from reaching the app through a look-alike host name.
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return host in ("127.0.0.1", "localhost")

        def _authorized(self, query):
            given = self.headers.get("X-Token") or query.get("t", [""])[0]
            return secrets.compare_digest(given, app.token)

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(url.query)
            if not self._local():
                return self._send(403, {"error": "forbidden"})
            if url.path == "/fb":  # Facebook sending the user back after sign-in
                return self._facebook_return(query)
            if url.path == "/":
                if "code" in query:  # Google sending the user back after YouTube sign-in
                    return self._youtube_return(query)
                page = PAGE.read_text(encoding="utf-8").replace("__TOKEN__", app.token)
                return self._send(200, page, "text/html; charset=utf-8")
            if not self._authorized(query):
                return self._send(403, {"error": "forbidden"})
            if url.path == "/api/state":
                return self._send(200, app.state())
            if url.path == "/api/settings":
                return self._send(200, app.settings())
            if url.path == "/media":
                return self._media(query.get("name", [""])[0], query.get("in", ["inbox"])[0])
            self._send(404, {"error": "not found"})

        def _facebook_return(self, query):
            from .platforms import instagram
            ok = secrets.compare_digest(query.get("state", [""])[0], app.oauth_state)
            if not ok:
                message = "تعذّر الربط: الطلب لم يبدأ من هذه الأداة."
            elif "code" not in query:
                message = "لم تتم الموافقة: " + query.get("error_description", [""])[0]
            else:
                try:
                    name = instagram.fb_exchange(query["code"][0])
                    if "instagram" not in app.cfg["platforms"]:
                        config.save_config(app.cfg, {"platforms": app.cfg["platforms"] + ["instagram"]})
                    message = f"تم ربط انستغرام عبر فيسبوك: @{name}. يمكنك إغلاق هذه الصفحة."
                except Exception as e:
                    message = f"تعذّر الربط: {e}"
            html = (f'<!doctype html><meta charset="utf-8"><body dir="rtl" style="font-family:'
                    f'Segoe UI,Tahoma,sans-serif;background:#0f1216;color:#e8eaed;padding:48px">'
                    f"<h2>{message}</h2><p><a style='color:#6ea8ff' href='/'>فتح الأداة</a></p>")
            self._send(200, html, "text/html; charset=utf-8")

        def _youtube_return(self, query):
            ok = secrets.compare_digest(query.get("state", [""])[0], app.oauth_state)
            message = "تعذّر ربط يوتيوب: الطلب لم يبدأ من هذه الأداة."
            if ok:
                try:
                    app.auth_finish("youtube", "?code=" + urllib.parse.quote(query["code"][0]))
                    message = "تم ربط يوتيوب. يمكنك إغلاق هذه الصفحة والرجوع للأداة."
                except Exception as e:
                    message = f"تعذّر ربط يوتيوب: {e}"
            html = (f'<!doctype html><meta charset="utf-8"><body dir="rtl" style="font-family:'
                    f'Segoe UI,Tahoma,sans-serif;background:#0f1216;color:#e8eaed;padding:48px">'
                    f"<h2>{message}</h2><p><a style='color:#6ea8ff' href='/'>فتح الأداة</a></p>")
            self._send(200, html, "text/html; charset=utf-8")

        def _media(self, name, where="inbox"):
            folder = app.schedule_dir() if where == "scheduled" else app.publisher.inbox
            path = folder / Path(name).name  # queued or scheduled videos only
            if not path.is_file():
                return self._send(404, {"error": "not found"})
            size = path.stat().st_size
            start, end = 0, size - 1
            rng = self.headers.get("Range", "")
            if rng.startswith("bytes="):
                a, _, b = rng[6:].partition("-")
                start = int(a) if a else max(0, size - int(b or 0))
                end = min(int(b), size - 1) if a and b else size - 1
            if start > end:
                return self._send(416, {"error": "range"})
            self.send_response(206 if rng else 200)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if rng:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                try:
                    while left > 0:
                        chunk = f.read(min(256 * 1024, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the player stopped or seeked

        def do_POST(self):
            url = urllib.parse.urlparse(self.path)
            if not self._local() or not self._authorized({}):
                return self._send(403, {"error": "forbidden"})
            if url.path == "/api/schedule/upload":
                return self._upload()
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                self._send(200, self._act(url.path, body) or {"ok": True})
            except (ValueError, RuntimeError, KeyError) as e:
                self._send(400, {"error": str(e)})
            except Exception as e:
                log.error("app: %s", e)
                self._send(500, {"error": str(e)})

        def _upload(self):
            """Stream a video from the browser to data/scheduled/ without holding it in memory."""
            name = urllib.parse.unquote(self.headers.get("X-Filename") or "video.mp4")
            name = re.sub(r"[^\w.\- ]+", "_", Path(name).name, flags=re.UNICODE)[-120:]
            if Path(name).suffix.lower() not in (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"):
                return self._send(400, {"error": "اختر ملف فيديو (mp4 أو mov أو mkv ...)."})
            length = int(self.headers.get("Content-Length") or 0)
            dst = app.schedule_dir() / f"{int(time.time() * 1000)}-{name}"
            part = dst.with_name(dst.name + ".part")
            try:
                with open(part, "wb") as f:
                    left = length
                    while left > 0:
                        chunk = self.rfile.read(min(1 << 20, left))
                        if not chunk:
                            break
                        f.write(chunk)
                        left -= len(chunk)
                if left > 0:
                    raise RuntimeError("upload interrupted")
                part.rename(dst)
                video.probe(dst)  # a real video, or say so now rather than at posting time
            except Exception as e:
                part.unlink(missing_ok=True)
                dst.unlink(missing_ok=True)
                return self._send(400, {"error": f"تعذّر رفع الفيديو: {e}"})
            return self._send(200, {"file": dst.name})

        def _act(self, path, body):
            pub = app.publisher
            if path == "/api/clip":
                link = str(body.get("url", "")).strip()
                if not link:
                    raise ValueError("الصق رابطا أولا.")
                back = body.get("back") or None
                if back not in (None, "start", "at"):
                    back = int(back)  # minutes before now
                    back = back * 60 if back > 0 else None
                app.add_job(link, None, body.get("start") or None, body.get("end") or None, back,
                            bool(body.get("past_only")))
            elif path == "/api/settings":
                app.save(body)
                return app.settings()
            elif path == "/api/publisher":
                pub.paused = bool(body.get("paused"))
                if not pub.paused:
                    pub.failures, pub.pause_reason = 0, ""
            elif path == "/api/queue/publish":
                pub.publish_now(body.get("name"))
                pub.paused = False
            elif path == "/api/update/check":
                return app.check_update()
            elif path == "/api/update/apply":
                if app.publisher.current is not None:
                    # A post is going out: hold new ones, and update once it is done.
                    app.update_when_free()
                    return {"pending": True}
                app.apply_update()  # running links resume by themselves after the restart
                return {"restarting": True}
            elif path == "/api/publisher/cancel":
                if not pub.current:
                    raise ValueError("لا يوجد نشر جارٍ الآن.")
                pub.cancel_flag.set()
            elif path == "/api/schedule/add":
                app.schedule_add(body["file"], body["at"], body.get("caption", ""),
                                 body.get("platforms"))
            elif path == "/api/schedule/from-queue":
                app.schedule_from_queue(body["name"], body["at"], body.get("caption", ""))
            elif path == "/api/schedule/update":
                app.schedule_update(int(body["id"]), body.get("at"), body.get("caption"),
                                    body.get("platforms"))
            elif path == "/api/schedule/delete":
                app.schedule_delete(int(body["id"]))
            elif path == "/api/schedule/now":
                app.schedule_update(int(body["id"]), at=time.time())
            elif path == "/api/job/stop":
                app.stop_job(int(body["id"]))
            elif path == "/api/jobs/clear":
                app.jobs = [j for j in app.jobs if j["status"] in ("queued", "running")]
            elif path == "/api/queue/clear":
                for item in pub.queued():
                    try:
                        pub.delete(item.name)
                    except RuntimeError:
                        pass  # the one being published right now stays
            elif path == "/api/queue/delete":
                pub.delete(body["name"])
            elif path == "/api/check":
                return {"results": [{"name": n, "ok": ok, "detail": d}
                                    for n, ok, d in checks.run(app.cfg)]}
            elif path == "/api/auth/start":
                return {"url": app.auth_start(body["platform"])}
            elif path == "/api/instagram/connect":
                return {"username": app.instagram_connect(str(body.get("token", "")),
                                                      str(body.get("user_id", "")))}
            elif path == "/api/disconnect":
                if body.get("platform") not in ("youtube", "instagram", "tiktok"):
                    raise ValueError("unknown platform")
                app.disconnect(body["platform"])
            elif path == "/api/auth/finish":
                app.auth_finish(body["platform"], str(body.get("pasted", "")))
            elif path == "/api/open-folder":
                folder = {"inbox": pub.inbox, "done": pub.done, "failed": pub.failed,
                          "backup": backup.folder()}[body.get("folder", "inbox")]
                folder.mkdir(parents=True, exist_ok=True)
                _open_folder(folder)
            elif path == "/api/posts/retry":
                app.publisher.retry(int(body["id"]))
            elif path == "/api/virality":
                db = store.connect()
                try:
                    if body.get("rescore"):
                        name = Path(str(body["name"])).name
                        src = next((p for p in [pub.inbox / name, app.schedule_dir() / name]
                                    if p.is_file()), None)
                        if src is None:
                            raise ValueError("المقطع غير موجود.")
                        note_file = src.with_suffix(".txt")
                        note = note_file.read_text(encoding="utf-8") if note_file.exists() else ""
                        data = virality.predict(src, note, app.cfg, db)
                        pub.scores[name] = data["score"]
                        return data
                    if body.get("name"):
                        return virality.get(db, str(body["name"])) or {}
                    return virality.accuracy(db)
                finally:
                    db.close()
            elif path == "/api/notify/info":
                t = notify.topic(app.cfg)
                return {"topic": t, "url": f"{notify.NTFY}/{t}",
                        "telegram": bool(os.environ.get("TG_BOT_TOKEN")),
                        "telegram_chat": bool(os.environ.get("TG_CHAT_ID"))}
            elif path == "/api/notify/test":
                if not app.cfg.get("notify", {}).get("enabled"):
                    raise ValueError("فعّل الإشعارات أولا ثم احفظ.")
                notify.send(app.cfg, "posted", "تجربة من autopost",
                            "الإشعارات تعمل. ستصلك هنا عند نشر كل مقطع أو فشله.", wait=True)
            elif path == "/api/notify/telegram":
                return {"name": notify.telegram_find_chat()}
            elif path == "/api/ai/probe":
                return {"status": llm.probe(app.cfg)}
            elif path == "/api/tiktok/inbox-cleared":
                tiktok.inbox_cleared()
                db = store.connect()
                db.execute("UPDATE retries SET next_at=? WHERE post_id IN (SELECT id FROM posts "
                           "WHERE platform='tiktok')", (time.time(),))
                db.commit()
                db.close()
            elif path == "/api/insights":
                db = store.connect()
                try:
                    return timing.summary(app.cfg, db)
                finally:
                    db.close()
            elif path == "/api/backup/export":
                backup.save()
                return backup.export()
            elif path == "/api/backup/import":
                count = backup.import_(body.get("data"))
                app.cfg.update(config.load())
                return {"keys": count}
            elif path == "/api/backup/info":
                backup.save()
                latest = backup.folder() / "latest"
                return {"folder": str(backup.folder()),
                        "files": sorted(str(f.relative_to(latest)) for f in latest.rglob("*")
                                        if f.is_file()) if latest.exists() else []}
            else:
                raise ValueError("unknown action")

    return Handler


def _open_folder(path):
    if os.name == "nt":
        os.startfile(path)  # noqa: S606 - a fixed folder of ours
    else:
        opener = "open" if shutil.which("open") else "xdg-open"
        subprocess.Popen([opener, str(path)])


def _open_window(url):
    """A plain app window (no tabs or address bar) when Edge or Chrome is there."""
    candidates = []
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                     os.environ.get("LocalAppData")):
            if base:
                candidates += [Path(base) / "Microsoft/Edge/Application/msedge.exe",
                               Path(base) / "Google/Chrome/Application/chrome.exe"]
    for exe in candidates:
        if exe.is_file():
            try:
                subprocess.Popen([str(exe), f"--app={url}", "--window-size=1180,860"])
                return
            except OSError:
                pass
    webbrowser.open(url)


def run(cfg, open_window=True, port=PORT):
    app = App(cfg)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    except OSError:
        # Most likely the app is already open: show that one instead of a second copy.
        log.info("Port %d is in use; opening the app that is already running", port)
        if open_window:
            _open_window(f"http://localhost:{port}/")
        return
    server.daemon_threads = True
    url = f"http://localhost:{port}/"
    log.info("autopost is running at %s  (keep this window open; Ctrl+C to stop)", url)
    if open_window:
        _open_window(url)
    try:
        server.serve_forever()
    finally:
        server.server_close()
