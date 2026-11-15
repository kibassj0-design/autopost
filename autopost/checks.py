"""Verify the setup, testing each login for real. Used by the CLI and the app."""
import re
import shutil

from . import config, llm
from .platforms import instagram, tiktok, youtube


def _attempt(results, name, fn):
    try:
        fn()
        results.append((name, True, "login works"))
    except Exception as e:
        # Error text can quote the request URL; never show the token in it.
        detail = re.sub(r"access_token=[^&\s)']+", "access_token=***", str(e))[:200]
        results.append((name, False, detail))


def _instagram():
    instagram.identify(config.env("IG_ACCESS_TOKEN"))


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
            _attempt(results, "AI model (Gemini)", lambda: llm.gemini_models(cfg))
        else:
            _attempt(results, f"AI model ({name})",
                     lambda name=name: llm.compat_models(name, cfg))
    if "youtube" in cfg["platforms"]:
        _attempt(results, "YouTube", youtube._headers)
    if "instagram" in cfg["platforms"]:
        _attempt(results, "Instagram", _instagram)
    if "tiktok" in cfg["platforms"]:
        _attempt(results, "TikTok", lambda: tiktok._call(
            "/post/publish/creator_info/query/", {}, "TikTok"))
    return results
