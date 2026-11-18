"""Settings: .env for secrets, config.json for behaviour, data/ for state."""
import json
import os
from pathlib import Path

ROOT = Path(os.environ.get("AUTOPOST_HOME", Path(__file__).resolve().parent.parent))
DATA = ROOT / "data"
TOKENS = DATA / "tokens.json"

DEFAULTS = {
    "platforms": ["youtube", "instagram", "tiktok"],
    "language": "ar",
    "post_language": "both",  # post text in Arabic and English
    "ai_provider": "groq",  # tried first when its key is set; the others follow
    "gemini_model": "",
    "ai_models": {},
    "niche": "",
    "brand_voice": "",
    "default_hashtags": ["#explore", "#fyp", "#reels", "#shorts"],
    "max_seconds": 180,
    "orientation": "original",
    "fps": 60,
    "resolution": "1080",
    "speed_mode": "fast",
    "auto_update": True,
    "encoder": "auto",
    "youtube_mode": "short",
    "youtube_privacy": "public",
    "tiktok_privacy": "PUBLIC_TO_EVERYONE",
    "tiktok_mode": "direct",
    "watch_interval_seconds": 30,
    "min_minutes_between_posts": 90,
    "clipper": {
        "count": 0,
        "min_score": 70,
        "min_seconds": 30,
        "max_seconds": 80,
        "whisper_model": "small",
        "whisper_device": "cpu",
        "language": None,
        "captions": True,
        "captions_language": "swap",
        "caption_style": "capcut",
        "caption_font": "Arial",
        "layout": "auto",
        "title_overlay": True,  # a short hook headline at the top in the first seconds
        "jump_cuts": True,      # cut dead air (no speech, near silence) out of clips
        "max_height": 1080,
    },
    "virality": {
        "enabled": True,      # score every clip before it is posted
        "ai_judge": True,     # let the AI watch stills and read the speech (uses AI quota)
        "best_first": True,   # post the clip with the best chance first
        "min_score": 0,       # clips under this stay in the queue for you to decide (0 = off)
    },
    "notify": {
        "enabled": False, "ntfy": True,
        "on_posted": True, "on_failed": True, "on_clips": True, "on_momentum": True,
        "on_paused": True,
    },
    "smart_timing": {
        "enabled": True,          # in a busy hour, post the next clip without the usual gap
        "peak_gap_minutes": 45,   # ...but never closer than this to the previous post
        "peaks_only": False,      # hold queued clips until a busy hour
    },
    "momentum": {
        "enabled": True,
        "check_every_minutes": 30,
        "views_per_hour": 500,
        "window_hours": 72,
        "max_boosts_per_video": 1,
        "boost_clip_seconds": 30,
    },
}


def atomic_write(path, text):
    """Write a file so that a crash or power cut never leaves it half written."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _backup():
    from . import backup
    backup.save()


def _load_env():
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if v.strip():
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


def load():
    from . import backup
    backup.restore_missing()  # keys or accounts lost or damaged: put the saved copy back
    _load_env()
    cfg = json.loads(json.dumps(DEFAULTS))
    path = ROOT / "config.json"
    if path.exists():
        try:
            user = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:  # a damaged config.json must not stop the tool
            user = {}
        for key, value in user.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
    if cfg.get("ai_provider") in ("auto", "", None):
        cfg["ai_provider"] = "groq"  # "auto" from older versions: Groq first
    DATA.mkdir(exist_ok=True)
    from . import video
    # "auto" from older versions now means the fixed 1080p default.
    video.QUALITY["resolution"] = "1440" if str(cfg["resolution"]) == "1440" else "1080"
    video.QUALITY["encoder"] = cfg.get("encoder", "auto")
    _backup()
    return cfg


def env(name, required=True):
    val = os.environ.get(name, "")
    if required and not val:
        raise RuntimeError(f"{name} is not set (see .env.example)")
    return val


def get_tokens():
    try:
        return json.loads(TOKENS.read_text(encoding="utf-8")) if TOKENS.exists() else {}
    except ValueError:
        return {}


def save_tokens(platform, values):
    tokens = get_tokens()
    tokens[platform] = {**tokens.get(platform, {}), **values}
    DATA.mkdir(exist_ok=True)
    atomic_write(TOKENS, json.dumps(tokens, indent=2))
    try:
        TOKENS.chmod(0o600)
    except OSError:
        pass
    _backup()


def write_env(values):
    """Set KEY=value lines in .env, keeping everything else in the file."""
    path = ROOT / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for key, value in values.items():
        for i, line in enumerate(lines):
            if line.split("=", 1)[0].strip() == key:
                lines[i] = f"{key}={value}"
                break
        else:
            lines.append(f"{key}={value}")
        os.environ[key] = value
    atomic_write(path, "\n".join(lines) + "\n")
    _backup()


def connected(platform):
    """Whether this platform has what it needs to publish."""
    if platform in ("youtube", "tiktok"):
        return bool(get_tokens().get(platform, {}).get("refresh_token"))
    return bool(os.environ.get("IG_ACCESS_TOKEN") and os.environ.get("IG_USER_ID"))


def save_config(cfg, changes):
    """Apply changes to the live settings and to config.json."""
    path = ROOT / "config.json"
    try:
        saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError:  # damaged file: start it again from the live settings
        saved = {}
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(value)
            saved.setdefault(key, {}).update(value)
        else:
            cfg[key] = value
            saved[key] = value
    atomic_write(path, json.dumps(saved, ensure_ascii=False, indent=2) + "\n")
    _backup()
