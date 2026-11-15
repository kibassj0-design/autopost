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
    "post_language": "swap",
    "ai_provider": "auto",
    "gemini_model": "",
    "ai_models": {},
    "niche": "",
    "brand_voice": "",
    "default_hashtags": ["#explore", "#fyp", "#reels", "#shorts"],
    "max_seconds": 180,
    "orientation": "original",
    "fps": 60,
    "youtube_mode": "short",
    "youtube_privacy": "public",
    "tiktok_privacy": "PUBLIC_TO_EVERYONE",
    "watch_interval_seconds": 30,
    "min_minutes_between_posts": 90,
    "clipper": {
        "count": 6,
        "min_seconds": 30,
        "max_seconds": 80,
        "whisper_model": "small",
        "whisper_device": "cpu",
        "language": None,
        "captions": True,
        "captions_language": "swap",
        "caption_font": "Arial",
        "layout": "auto",
        "max_height": 1080,
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
    _load_env()
    cfg = json.loads(json.dumps(DEFAULTS))
    path = ROOT / "config.json"
    if path.exists():
        user = json.loads(path.read_text(encoding="utf-8"))
        for key, value in user.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
    DATA.mkdir(exist_ok=True)
    return cfg


def env(name, required=True):
    val = os.environ.get(name, "")
    if required and not val:
        raise RuntimeError(f"{name} is not set (see .env.example)")
    return val


def get_tokens():
    return json.loads(TOKENS.read_text()) if TOKENS.exists() else {}


def save_tokens(platform, values):
    tokens = get_tokens()
    tokens[platform] = {**tokens.get(platform, {}), **values}
    DATA.mkdir(exist_ok=True)
    TOKENS.write_text(json.dumps(tokens, indent=2))
    TOKENS.chmod(0o600)


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
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def connected(platform):
    """Whether this platform has what it needs to publish."""
    if platform in ("youtube", "tiktok"):
        return bool(get_tokens().get(platform, {}).get("refresh_token"))
    return bool(os.environ.get("IG_ACCESS_TOKEN") and os.environ.get("IG_USER_ID"))


def save_config(cfg, changes):
    """Apply changes to the live settings and to config.json."""
    path = ROOT / "config.json"
    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(value)
            saved.setdefault(key, {}).update(value)
        else:
            cfg[key] = value
            saved[key] = value
    path.write_text(json.dumps(saved, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
