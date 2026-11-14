"""Verify the setup, testing each login for real. Used by the CLI and the app."""
import re
import shutil

import requests

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
    r = requests.get(f"{instagram._base()}/{config.env('IG_USER_ID')}", timeout=30,
                     params={"fields": "username",
                             "access_token": config.env("IG_ACCESS_TOKEN")})
    if r.status_code >= 400:
        raise RuntimeError(r.text[:200])


def run(cfg):
    """[(name, ok, detail)]"""
    results = [("ffmpeg", bool(shutil.which("ffmpeg") and shutil.which("ffprobe")), "")]
    for mod in ("yt_dlp", "faster_whisper", "cv2", "numpy"):
        try:
            __import__(mod)
            results.append((f"python package {mod}", True, ""))
        except ImportError:
            results.append((f"python package {mod}", False, "not installed"))
    who = llm.provider(cfg)
    if who == "gemini":
        _attempt(results, "AI model (Gemini)", lambda: llm.gemini_models(cfg))
    else:
        results.append(("AI model (Gemini or Claude key)", who is not None,
                        "Claude" if who else "without one: weaker moment picking, "
                        "no translation, basic captions"))
    if "youtube" in cfg["platforms"]:
        _attempt(results, "YouTube", youtube._headers)
    if "instagram" in cfg["platforms"]:
        _attempt(results, "Instagram", _instagram)
    if "tiktok" in cfg["platforms"]:
        _attempt(results, "TikTok", lambda: tiktok._call(
            "/post/publish/creator_info/query/", {}, "TikTok"))
    return results
