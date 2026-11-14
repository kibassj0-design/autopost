"""The publishing loop: posts what is queued in inbox/, spaced out, and tracks momentum."""
import logging
import shutil
import threading
import time
from pathlib import Path

from . import config, momentum, pipeline, store

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
        self.release = False      # publish the next one now, ignoring the gap
        self.first = None         # file name to publish ahead of the others
        self.current = None       # file name being published right now
        self.last_post = 0.0
        self.last_check = 0.0
        self._sizes = {}
        self._logged = None
        self._db = None
        self.lock = threading.Lock()

    def queued(self):
        """Videos waiting in inbox/, in publishing order."""
        files = sorted(p for p in self.inbox.iterdir() if p.suffix.lower() in VIDEO_EXT
                       and not p.name.endswith(".alt.mp4"))
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
        for extra in ("", ".txt", ".alt.mp4"):
            (path.with_suffix(extra) if extra else path).unlink(missing_ok=True)

    def _stable(self, path):
        """True once a file's size stopped changing between two scans (copy finished)."""
        size = path.stat().st_size
        done = size > 0 and self._sizes.get(path) == size
        self._sizes[path] = size
        return done

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
            _, results, _ = pipeline.publish(kept, self.cfg, note=note, db=self._db)
            if all(str(r).startswith("FAILED") for r in results.values()):
                target = self.failed
        except Exception as e:
            log.error("%s: %s", path.name, e)
            target = self.failed
        if target is self.failed:
            shutil.move(kept, self.failed / kept.name)
            if kept.with_suffix(".alt.mp4").exists():
                shutil.move(kept.with_suffix(".alt.mp4"), self.failed / kept.with_suffix(".alt.mp4").name)
        else:
            self.last_post = time.time()
        if note_file.exists():
            shutil.move(note_file, target / note_file.name)
        self._sizes.pop(path, None)
        self.current = None

    def step(self):
        if self._db is None:  # SQLite connections belong to the thread that made them
            self._db = store.connect()
            row = self._db.execute(
                "SELECT MAX(p.published_at) t FROM posts p JOIN videos v ON v.id = p.video_id"
                " WHERE p.error IS NULL AND v.parent_id IS NULL").fetchone()
            self.last_post = row["t"] or 0.0
        cfg, m = self.cfg, self.cfg["momentum"]
        accounts = any(config.connected(p) for p in cfg["platforms"])

        ready = [p for p in self.queued() if self._stable(p)]
        wait = self.next_post_at() - time.time()
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

        if (accounts and m["enabled"]
                and time.time() - self.last_check >= m["check_every_minutes"] * 60):
            self.last_check = time.time()
            try:
                queued = bool(self.queued())
                if momentum.check(cfg, self._db, cut=not queued) and queued:
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
