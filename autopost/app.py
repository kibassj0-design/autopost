"""The desktop app: a local page (127.0.0.1 only) to paste links, watch the work,
manage the queue, see what was published and edit keys and settings."""
import collections
import json
import logging
import mimetypes
import os
import queue
import secrets
import shutil
import subprocess
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import checks, config, llm, store, video
from .platforms import tiktok, youtube
from .publisher import Publisher

log = logging.getLogger("autopost")

PORT = 8765  # also the YouTube sign-in redirect, so the app can finish that login itself
SECRET_KEYS = ["GEMINI_API_KEY", "MISTRAL_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY",
               "CEREBRAS_API_KEY", "ANTHROPIC_API_KEY", "YT_CLIENT_SECRET", "IG_ACCESS_TOKEN",
               "TT_CLIENT_SECRET"]
PLAIN_KEYS = ["YT_CLIENT_ID", "IG_USER_ID", "IG_GRAPH_HOST", "TT_CLIENT_KEY",
              "TT_REDIRECT_URI"]
# Settings the page may change: name -> type (nested ones as "section.name").
SETTINGS = {
    "platforms": list, "language": str, "post_language": str, "niche": str, "brand_voice": str,
    "default_hashtags": list, "orientation": str, "ai_provider": str,
    "min_minutes_between_posts": int, "fps": int, "youtube_mode": str, "tiktok_mode": str,
    "clipper.min_score": int, "clipper.min_seconds": int, "clipper.max_seconds": int,
    "clipper.whisper_model": str, "clipper.whisper_device": str, "clipper.captions": bool,
    "clipper.captions_language": str, "clipper.layout": str,
    "momentum.enabled": bool, "momentum.views_per_hour": int,
}


class LogBuffer(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = collections.deque(maxlen=400)

    def emit(self, record):
        self.lines.append({"t": time.strftime("%H:%M:%S", time.localtime(record.created)),
                           "level": record.levelname, "msg": record.getMessage()})


class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.token = secrets.token_urlsafe(24)
        self.oauth_state = secrets.token_urlsafe(16)
        self.logs = LogBuffer()
        logging.getLogger().addHandler(self.logs)
        self.publisher = Publisher(cfg)
        self.jobs = []
        self.todo = queue.Queue()
        self.durations = {}
        threading.Thread(target=self.publisher.run_forever, daemon=True).start()
        threading.Thread(target=self._work, daemon=True).start()

    # ---- clip jobs

    def add_job(self, url, count=None, start=None, end=None):
        job = {"id": len(self.jobs) + 1, "url": url, "title": "", "status": "queued",
               "stage": "queued", "fraction": None, "detail": "", "warnings": [],
               "error": "", "clips": 0, "count": count, "start": start, "end": end}
        self.jobs.append(job)
        self.todo.put(job)
        return job

    def _work(self):
        from . import clipper
        while True:
            job = self.todo.get()
            job["status"] = "running"

            def progress(stage, fraction, detail, job=job):
                if stage == "warning":
                    job["warnings"].append(detail)
                    return
                job["stage"], job["fraction"] = stage, fraction
                if stage in ("audio", "transcribe") and detail:
                    job["title"] = detail
                elif stage == "render":
                    job["detail"] = detail

            try:
                clips = clipper.run(job["url"], self.cfg, count=job["count"],
                                    start=job["start"], end=job["end"], progress=progress)
                job.update(status="done", stage="done", fraction=1, clips=len(clips))
            except Exception as e:
                log.error("Could not process %s: %s", job["url"], e)
                job.update(status="error", error=str(e)[:400])

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
                          "seconds": self._duration(p)})
        db = store.connect()
        posts = [dict(r) for r in db.execute(
            "SELECT p.id, p.platform, p.url, p.error, p.published_at, v.title, v.parent_id,"
            " (SELECT views FROM stats s WHERE s.post_id = p.id ORDER BY ts DESC LIMIT 1) views,"
            " (SELECT likes FROM stats s WHERE s.post_id = p.id ORDER BY ts DESC LIMIT 1) likes"
            " FROM posts p JOIN videos v ON v.id = p.video_id ORDER BY p.id DESC LIMIT 100")]
        db.close()
        for post in posts:
            post["title"] = post["title"].splitlines()[0] if post["title"] else ""
        connected = {p: config.connected(p) for p in ("youtube", "instagram", "tiktok")}
        tokens = config.get_tokens()
        accounts = {p: tokens.get(p, {}).get("username", "") if connected[p] else ""
                    for p in connected}
        return {
            "jobs": self.jobs[-20:][::-1],
            "queue": clips,
            "posts": posts,
            "publisher": {
                "paused": pub.paused, "current": pub.current,
                "next_post_at": pub.next_post_at(), "now": time.time(),
                "has_account": any(connected[p] for p in self.cfg["platforms"]),
            },
            "connected": connected,
            "accounts": accounts,
            "enabled": self.cfg["platforms"],
            "ai": llm.providers(self.cfg),
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
        if changes.get("fps") not in (None, 30, 60):
            raise ValueError("عدد الإطارات: 30 أو 60.")
        if changes:
            config.save_config(self.cfg, changes)

    # ---- account sign-in

    def auth_start(self, platform):
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
        config.TOKENS.write_text(json.dumps(tokens, indent=2))
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
                return self._media(query.get("name", [""])[0])
            self._send(404, {"error": "not found"})

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

        def _media(self, name):
            path = app.publisher.inbox / Path(name).name  # queued clips only
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
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                self._send(200, self._act(url.path, body) or {"ok": True})
            except (ValueError, RuntimeError, KeyError) as e:
                self._send(400, {"error": str(e)})
            except Exception as e:
                log.error("app: %s", e)
                self._send(500, {"error": str(e)})

        def _act(self, path, body):
            pub = app.publisher
            if path == "/api/clip":
                link = str(body.get("url", "")).strip()
                if not link:
                    raise ValueError("الصق رابطا أولا.")
                app.add_job(link, None, body.get("start") or None, body.get("end") or None)
            elif path == "/api/settings":
                app.save(body)
                return app.settings()
            elif path == "/api/publisher":
                pub.paused = bool(body.get("paused"))
            elif path == "/api/queue/publish":
                pub.publish_now(body.get("name"))
                pub.paused = False
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
                folder = {"inbox": pub.inbox, "done": pub.done, "failed": pub.failed}[
                    body.get("folder", "inbox")]
                _open_folder(folder)
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
