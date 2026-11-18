"""Verify the setup, testing each login for real. Used by the CLI and the app."""
import re
import shutil

from . import config, llm
from .platforms import instagram, tiktok, youtube


def _attempt(results, name, fn, ok_text="يعمل"):
    try:
        fn()
        results.append((name, True, ok_text))
    except Exception as e:
        # Error text can quote the request URL; never show a token or key in it.
        detail = re.sub(r"(access_token|client_secret|key)=[^&\s)']+", r"\1=***", str(e))[:300]
        results.append((name, False, detail))


def _env(*names):
    return all(config.env(n, required=False) for n in names)


def _instagram():
    instagram.identify(config.env("IG_ACCESS_TOKEN"), config.env("IG_USER_ID", required=False))


def run(cfg):
    """[(name, ok, detail)]"""
    results = [("ffmpeg", bool(shutil.which("ffmpeg") and shutil.which("ffprobe")), "")]
    for mod in ("yt_dlp", "faster_whisper", "cv2", "numpy"):
        try:
            __import__(mod)
            results.append((f"python package {mod}", True, ""))
        except ImportError:
            results.append((f"python package {mod}", False, "not installed"))
    names = llm.providers(cfg)
    if not names:
        results.append(("AI model (any key)", False, "without one: weaker moment "
                        "picking, no translation, basic captions"))
    for name in names:
        if name == "claude":
            results.append(("AI model (Claude)", True, "key set"))
        elif name == "gemini":
            # A real (tiny) request: express-mode keys have no model list to test with.
            _attempt(results, "AI model (Gemini)", lambda: llm._gemini(
                "Answer with the JSON object {\"ok\": true}.", "ok",
                {"type": "object", "properties": {"ok": {"type": "boolean"}}}, cfg, (),
                "low", 50) if llm._vertex_key() else llm.gemini_models(cfg))
        else:
            _attempt(results, f"AI model ({name})",
                     lambda name=name: llm.compat_models(name, cfg))
    if "youtube" in cfg["platforms"]:
        if config.connected("youtube"):
            _attempt(results, "YouTube", youtube._headers, "مربوط ويعمل")
        elif _env("YT_CLIENT_ID", "YT_CLIENT_SECRET"):
            _attempt(results, "YouTube: Client ID + secret", youtube.check_client,
                     "المفاتيح صحيحة، بقي الضغط على «ربط يوتيوب»")
        else:
            results.append(("YouTube", False, "غير مربوط"))
    if "instagram" in cfg["platforms"]:
        if config.connected("instagram"):
            _attempt(results, "Instagram", _instagram, "مربوط ويعمل")
        else:
            results.append(("Instagram", False, "غير مربوط"))
        if _env("FB_APP_ID", "FB_APP_SECRET"):
            _attempt(results, "Facebook App ID + secret", instagram.fb_check_app, "صحيحة")
    if "tiktok" in cfg["platforms"]:
        if config.connected("tiktok"):
            _attempt(results, "TikTok", lambda: tiktok._call(
                "/post/publish/creator_info/query/", {}, "TikTok"), "مربوط ويعمل")
        else:
            results.append(("TikTok", False, "غير مربوط"))
    return results
