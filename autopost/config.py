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
    "niche": "",
    "brand_voice": "",
    "default_hashtags": ["#explore", "#fyp", "#reels", "#shorts"],
    "max_seconds": 180,
    "youtube_privacy": "public",
    "tiktok_privacy": "PUBLIC_TO_EVERYONE",
    "watch_interval_seconds": 30,
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
        momentum = {**cfg["momentum"], **user.pop("momentum", {})}
        cfg.update(user)
        cfg["momentum"] = momentum
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
