"""The publishing loop: posts what is queued in inbox/, spaced out, and tracks momentum."""
import json
import logging
import shutil
import threading
import time
from pathlib import Path

from . import config, momentum, notify, pipeline, store, timing
from .platforms import instagram

log = logging.getLogger("autopost")
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}


class Publisher:
    def __init__(self, cfg):
        self.cfg = cfg
        self.inbox, self.done, self.failed = (config.ROOT / d
                                              for d in ("inbox", "done", "failed"))
        for d in (self.inbox, self.done, self.failed):
            d.mkdir(exist_ok=True)
        self.paused = False
        self.pause_reason = ""
        self.failures = 0
        self.cancel_flag = threading.Event()
        self.release = False      # publish the next one now, ignoring the gap
        self.first = None         # file name to publish ahead of the others
        self.current = None       # file name being published right now
        self.last_post = 0.0
        self.last_check = 0.0
        self.last_renew = 0.0
        self._sizes = {}
        self._logged = None
        self._db = None
        self.lock = threading.Lock()
        self.retries = []         # failed posts to send again, one platform each
        self.hold = False         # an update is waiting: start no new post
        self.scores = {}          # clip name -> virality score (filled by the predictor)

    def retry(self, post_id):
        """Send a failed post again to its platform, at the next pass (no waiting gap:
        the user asked for it)."""
        if post_id not in self.retries:
            self.retries.append(post_id)

    def _retry(self, post_id):
        self.current = f"retry-{post_id}"
        self.cancel_flag.clear()
        try:
            result = pipeline.retry(self._db, post_id, self.cfg, stop=self.cancel_flag.is_set)
            log.info("Retry of post %d: %s", post_id, result)
            row = self._db.execute("SELECT p.platform, v.title FROM posts p JOIN videos v ON "
                                   "v.id=p.video_id WHERE p.id=?", (post_id,)).fetchone()
            if row:
                self._tell(row["title"], {row["platform"]: result})
            vid = self._db.execute("SELECT video_id FROM posts WHERE id=?", (post_id,)).fetchone()
            if str(result).startswith("FAILED") and vid:
                self._plan_retries(vid["video_id"])
            else:
                self._db.execute("DELETE FROM retries WHERE post_id=?", (post_id,))
                self._db.commit()
        except Exception as e:
            log.error("Retry of post %d: %s", post_id, e)
            store.update_post(self._db, post_id, error=str(e))
        finally:
            if post_id in self.retries:
                self.retries.remove(post_id)
            self.current = None

    def queued(self):
        """Videos waiting in inbox/, in publishing order."""
        files = sorted(p for p in self.inbox.iterdir() if p.suffix.lower() in VIDEO_EXT
                       and not p.name.endswith(".alt.mp4"))
        v = self.cfg.get("virality", {})
        if v.get("enabled", True) and v.get("best_first", True):
            # the best chance first; clips not scored yet keep their place after them
            files.sort(key=lambda p: -self.scores.get(p.name, -1))
        if self.first:
            files.sort(key=lambda p: p.name != self.first)
        return files

    def next_post_at(self):
        return self.last_post + self.cfg["min_minutes_between_posts"] * 60

    def publish_now(self, name=None):
        self.first = Path(name).name if name else None
        self.release = True

    def delete(self, name):
        path = self.inbox / Path(name).name  # the name only: never leave inbox/
        if path.name == self.current:
            raise RuntimeError("This clip is being published right now")
        for extra in ("", ".txt", ".alt.mp4", ".jpg"):
            (path.with_suffix(extra) if extra else path).unlink(missing_ok=True)

    def _scored(self, path):
        """With the predictor on, a clip waits (up to 10 minutes) for its score, so the
        best one goes first; one under the minimum score waits for the user."""
        v = self.cfg.get("virality", {})
        if not v.get("enabled", True) or path.name == self.first:
            return True
        score = self.scores.get(path.name)
        if score is None:
            return time.time() - path.stat().st_mtime > 600
        return score >= v.get("min_score", 0)

    def _stable(self, path):
        """True once a file's size stopped changing between two scans (copy finished)."""
        size = path.stat().st_size
        done = size > 0 and self._sizes.get(path) == size
        self._sizes[path] = size
        return done

    RETRY_AFTER = (15 * 60, 45 * 60, 2 * 3600)  # automatic retries of a failed platform

    def _plan_retries(self, video_id):
        """A platform that refused a clip gets it again by itself, a few times, spaced out:
        most failures (a busy server, a slow video processing, a limit) pass."""
        from .platforms import tiktok
        for row in self._db.execute("SELECT id, platform FROM posts WHERE video_id=? AND error "
                                    "IS NOT NULL", (video_id,)).fetchall():
            have = self._db.execute("SELECT attempts FROM retries WHERE post_id=?",
                                    (row["id"],)).fetchone()
            attempts = have["attempts"] if have else 0
            if attempts >= len(self.RETRY_AFTER):
                self._db.execute("DELETE FROM retries WHERE post_id=?", (row["id"],))
                continue
            delay = self.RETRY_AFTER[attempts]
            if row["platform"] == "tiktok":
                delay = max(delay, tiktok.inbox_wait())  # a full TikTok inbox: after it empties
            if row["platform"] == "instagram":
                from .platforms import instagram
                delay = max(delay, instagram.cooling_down() + 120)  # after Meta's limit lifts
            self._db.execute("INSERT OR REPLACE INTO retries VALUES(?,?,?)",
                             (row["id"], attempts, time.time() + delay))
            log.info("Post %d failed; trying it again in %d min", row["id"], delay // 60)
        self._db.commit()

    def _due_retry(self):
        row = self._db.execute("SELECT post_id, attempts FROM retries WHERE next_at<=? "
                               "ORDER BY next_at LIMIT 1", (time.time(),)).fetchone()
        if row is None:
            return None
        self._db.execute("UPDATE retries SET attempts=attempts+1, next_at=? WHERE post_id=?",
                         (time.time() + 10 ** 9, row["post_id"]))  # not again until planned
        self._db.commit()
        return row["post_id"]

    def _tell(self, title, results):
        """Notify the phone about one post: the links, or what failed."""
        names = {"youtube": "يوتيوب", "instagram": "انستغرام", "tiktok": "تيك توك"}
        ok = {p: r for p, r in results.items() if not str(r).startswith("FAILED")}
        bad = {p: r for p, r in results.items() if str(r).startswith("FAILED")}
        title = (title or "مقطع").splitlines()[0][:80]
        if ok:
            lines = []
            for p, r in ok.items():
                r = str(r)
                lines.append(names.get(p, p) + ": " + ("في صندوق تيك توك، انشره من التطبيق"
                                                       if r == "tiktok:inbox" else
                                                       "خاص" if r == "tiktok:private" else r))
            link = next((str(r) for r in ok.values() if str(r).startswith("http")), None)
            notify.send(self.cfg, "posted", f"نُشر: {title}", "\n".join(lines), link)
        if bad:
            notify.send(self.cfg, "failed", f"فشل النشر: {title}", "\n".join(
                f"{names.get(p, p)}: {str(r).replace('FAILED: ', '')[:160]}"
                for p, r in bad.items()))

    def _publish(self, path):
        # An optional "<name>.txt" beside the video is the creator's note.
        note_file = path.with_suffix(".txt")
        note = note_file.read_text(encoding="utf-8").strip() if note_file.exists() else ""
        self.current = path.name
        # Move first: the database must point at where the file will live.
        kept = Path(shutil.move(path, self.done / f"{int(time.time())}-{path.name}"))
        alt = path.with_suffix(".alt.mp4")
        if alt.exists():  # the other-orientation copy for YouTube stays beside it
            shutil.move(alt, kept.with_suffix(".alt.mp4"))
        target = self.done
        try:
            self.cancel_flag.clear()
            video_id, results, text = pipeline.publish(kept, self.cfg, note=note, db=self._db,
                                                       stop=self.cancel_flag.is_set)
            self._tell(text.get("youtube", {}).get("title") or path.stem, results)
            if any(not str(r).startswith("FAILED") for r in results.values()):
                self._plan_retries(video_id)  # the others went up: retry just the failed ones
            if all(str(r).startswith("FAILED") for r in results.values()):
                target = self.failed
        except pipeline.PublishCancelled:
            # Back to the queue as it was, and no new post right after it.
            shutil.move(kept, path)
            if kept.with_suffix(".alt.mp4").exists():
                shutil.move(kept.with_suffix(".alt.mp4"), alt)
            self.paused = True
            self.last_post = time.time()
            self._sizes.pop(path, None)
            self.current = None
            log.info("Publishing of %s cancelled; it is back in the queue (paused)", path.name)
            return
        except Exception as e:
            log.error("%s: %s", path.name, e)
            notify.send(self.cfg, "failed", "فشل النشر", f"{path.name}: {str(e)[:200]}")
            target = self.failed
        self.last_post = time.time()  # the gap applies after a failure too
        if target is self.failed:
            # Every platform refused it, which is almost always an account problem, not
            # the clip. Put it back in the queue rather than burn through every clip,
            # and pause after a few in a row until the accounts are fixed.
            shutil.move(kept, path)
            if kept.with_suffix(".alt.mp4").exists():
                shutil.move(kept.with_suffix(".alt.mp4"), alt)
            self.failures += 1
            if self.failures >= 3:
                self.paused = True
                self.pause_reason = "failing"
                log.error("Publishing paused: the last %d clips failed on every platform. "
                          "Check the accounts (Settings -> check), then resume.", self.failures)
                notify.send(self.cfg, "paused", "توقف النشر",
                            "آخر 3 مقاطع فشلت على كل المنصات، فأوقفت الأداة النشر. افحص "
                            "الحسابات من الإعدادات ← فحص الإعداد، ثم اضغط «تشغيل النشر».")
            target = self.inbox
        else:
            self.failures = 0
        if note_file.exists() and target is not self.inbox:
            shutil.move(note_file, target / note_file.name)
        cover = path.with_suffix(".jpg")
        if cover.exists() and target is not self.inbox:
            cover.unlink(missing_ok=True)  # the cover is inside the video; the preview goes
        self._sizes.pop(path, None)
        self.current = None

    def _publish_scheduled(self, row):
        path = Path(row["path"])
        self.current = path.name
        self._db.execute("UPDATE schedule SET status='publishing' WHERE id=?", (row["id"],))
        self._db.commit()
        status, result = "done", {}
        try:
            if not path.exists():
                raise RuntimeError("the video file is gone")
            platforms = [p for p in json.loads(row["platforms"] or "[]")
                         if config.connected(p)] or None
            self.cancel_flag.clear()
            video_id, result, text = pipeline.publish(path, self.cfg, note=row["note"] or "",
                                               platforms=platforms, db=self._db,
                                               own_caption=row["caption"],
                                               stop=self.cancel_flag.is_set)
            self._tell(row["title"] or text.get("youtube", {}).get("title") or path.stem,
                       result)
            if any(not str(r).startswith("FAILED") for r in result.values()):
                self._plan_retries(video_id)
            if all(str(r).startswith("FAILED") for r in result.values()):
                status = "failed"
        except pipeline.PublishCancelled:
            status, result = "cancelled", {}
        except Exception as e:
            log.error("Scheduled post %s: %s", path.name, e)
            status, result = "failed", {"error": str(e)[:300]}
        self._db.execute("UPDATE schedule SET status=?, result=? WHERE id=?",
                         (status, json.dumps(result, ensure_ascii=False), row["id"]))
        self._db.commit()
        self.current = None

    def step(self):
        if self._db is None:  # SQLite connections belong to the thread that made them
            self._db = store.connect()
            # A post cut off by closing the app stays "publishing" for ever: mark it so
            # it can be retried (not re-sent blindly, it may have reached some platforms).
            self._db.execute("UPDATE schedule SET status='failed', result=? WHERE status="
                             "'publishing'", (json.dumps({"error": "interrupted: the app was "
                                                          "closed while publishing"}),))
            self._db.commit()
            row = self._db.execute(
                "SELECT MAX(p.published_at) t FROM posts p JOIN videos v ON v.id = p.video_id"
                " WHERE p.error IS NULL AND v.parent_id IS NULL").fetchone()
            self.last_post = row["t"] or 0.0
        if self.hold:
            return  # the tool is about to restart for an update
        cfg, m = self.cfg, self.cfg["momentum"]
        accounts = any(config.connected(p) for p in cfg["platforms"])

        ready = [p for p in self.queued() if self._stable(p) and self._scored(p)]
        wait = self.next_post_at() - time.time()
        st = cfg.get("smart_timing", {})
        if ready and not self.release and st.get("enabled", True) and not (
                self.paused or not accounts):
            peak, why = timing.peak_now(cfg, self._db)
            since = time.time() - self.last_post
            if peak and wait > 0 and since >= st.get("peak_gap_minutes", 45) * 60:
                log.info("Busy hour (%s): publishing the next clip now instead of waiting "
                         "%.0f more min", "your best hours" if why.startswith("own")
                         else "platform peak", wait / 60)
                self.release = True
            elif not peak and st.get("peaks_only"):
                if self._logged != ("hold", len(ready)):
                    log.info("%d clip(s) held for the next busy hour", len(ready))
                    self._logged = ("hold", len(ready))
                ready = []
        if ready and (self.paused or not accounts):
            ready = []
        elif ready and wait > 0 and not self.release:
            if self._logged != len(ready):
                log.info("%d video(s) queued; next post in %.0f min", len(ready), wait / 60)
                self._logged = len(ready)
            ready = []
        # One per pass: posts are spaced out, not dumped all at once.
        for path in ready[:1]:
            self.release, self.first, self._logged = False, None, None
            self._publish(path)

        if not self.retries and accounts:
            due = self._due_retry()
            if due is not None:
                self.retries.append(due)
        if self.retries:
            self._retry(self.retries[0])

        # Scheduled posts go out at their time, whatever the gap between queue posts
        # and even while the queue is paused: the user picked that time.
        if accounts:
            due = self._db.execute(
                "SELECT * FROM schedule WHERE status='pending' AND at<=? ORDER BY at LIMIT 1",
                (time.time(),)).fetchone()
            if due:
                self._publish_scheduled(due)

        if time.time() - self.last_renew > 3600:
            self.last_renew = time.time()
            try:
                instagram.refresh_if_due(log)
            except Exception as e:
                log.warning("Instagram token renewal: %s", type(e).__name__)

        # The numbers of recent posts are read always (engagement, best hours);
        # momentum acts on them only when it is switched on.
        if accounts and time.time() - self.last_check >= m["check_every_minutes"] * 60:
            self.last_check = time.time()
            try:
                queued = bool(self.queued())
                if momentum.check(cfg, self._db, cut=not queued, boost=m["enabled"]) \
                        and queued:
                    self.release = True
            except Exception as e:
                log.error("momentum check: %s", e)

    def run_forever(self):
        while True:
            try:
                self.step()
            except Exception as e:  # the loop must survive a bad file or a network blip
                log.error("publisher: %s", e)
            time.sleep(min(self.cfg["watch_interval_seconds"], 30))
