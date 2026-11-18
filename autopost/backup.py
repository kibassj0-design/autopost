"""Keeps a copy of the user's keys, accounts and settings outside the program folder.

If the program folder is deleted, downloaded again, or a file gets damaged, the tool
puts the saved copy back by itself on the next start, so nothing has to be entered again.

Saved: .env (keys), config.json (settings), data/tokens.json (connected accounts) and
data/autopost.db (posts, schedule). The folder is ~/autopost-backup (on Windows
C:\\Users\\<name>\\autopost-backup), with one dated copy per day for the last 14 days.
"""
import json
import logging
import os
import shutil
import time
from pathlib import Path

from . import config

log = logging.getLogger("autopost.backup")

FILES = (".env", "config.json", "data/tokens.json", "data/autopost.db")
KEEP_DAYS = 14
# Only the tool's own keys are taken from a backup file, never other settings of the computer.
KEY_PREFIXES = ("YT_", "FB_", "IG_", "TT_", "GEMINI_", "MISTRAL_", "OPENROUTER_", "GROQ_",
                "CEREBRAS_", "ANTHROPIC_", "TG_", "NTFY_")


def folder():
    return Path(os.environ.get("AUTOPOST_BACKUP") or Path.home() / "autopost-backup")


def _useful(path):
    """A file worth saving or restoring: present and not empty."""
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _env_has_keys(path):
    try:
        return any("=" in line and line.split("=", 1)[1].strip()
                   for line in path.read_text(encoding="utf-8").splitlines()
                   if line.strip() and not line.lstrip().startswith("#"))
    except (OSError, UnicodeDecodeError):
        return False


def _valid_json(path):
    try:
        json.loads(path.read_text(encoding="utf-8"))
        return True
    except (OSError, ValueError):
        return False


def _good(name, path):
    if not _useful(path):
        return False
    if name == ".env":
        return _env_has_keys(path)
    if name.endswith(".json"):
        return _valid_json(path)
    return True


def save():
    """Copy the user's files to the backup folder. Never raises."""
    try:
        latest = folder() / "latest"
        day = folder() / time.strftime("%Y-%m-%d")
        saved = 0
        for name in FILES:
            src = config.ROOT / name
            if not _good(name, src):
                continue  # never overwrite a good copy with a missing or broken file
            for dest_dir in (latest, day):
                dest = dest_dir / name
                if dest.exists() and dest.read_bytes() == src.read_bytes():
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_name(dest.name + ".tmp")
                if name.endswith(".db"):
                    _copy_db(src, tmp)
                else:
                    shutil.copy2(src, tmp)
                os.replace(tmp, dest)
                saved += 1
        _prune()
        return saved
    except Exception as e:  # a backup problem must never stop the tool
        log.warning("Could not save the backup: %s", e)
        return 0


def _copy_db(src, dest):
    """SQLite's own copy, which stays consistent while the tool is writing to it."""
    import sqlite3
    if dest.exists():
        dest.unlink()
    with sqlite3.connect(src) as a, sqlite3.connect(dest) as b:
        a.backup(b)
    b.close()
    a.close()


def _prune():
    if not folder().is_dir():
        return
    days = sorted(p for p in folder().iterdir() if p.is_dir() and p.name[:2] == "20")
    for old in days[:-KEEP_DAYS]:
        shutil.rmtree(old, ignore_errors=True)


def restore_missing():
    """Put back any saved file that is missing or broken in the program folder.
    Returns the names restored. Never raises."""
    restored = []
    try:
        latest = folder() / "latest"
        for name in FILES:
            src, dest = latest / name, config.ROOT / name
            if _good(name, dest) or not _good(name, src):
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            restored.append(name)
        if restored:
            log.info("Restored from the backup in %s: %s", folder(), ", ".join(restored))
    except Exception as e:
        log.warning("Could not read the backup: %s", e)
    return restored


def export():
    """Everything needed to set the tool up again, as one JSON document."""
    env = {}
    path = config.ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                if v.strip():
                    env[k.strip()] = v.strip()
    try:
        settings = json.loads((config.ROOT / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    return {"autopost_backup": 1, "saved_at": time.time(), "env": env,
            "tokens": config.get_tokens(), "config": settings}


def import_(data):
    """Load a document made by export(). Returns how many keys were loaded."""
    if not isinstance(data, dict) or data.get("autopost_backup") != 1:
        raise ValueError("هذا ليس ملف نسخة احتياطية من autopost.")
    env = {str(k): str(v).strip() for k, v in (data.get("env") or {}).items()
           if str(k).startswith(KEY_PREFIXES) and str(k).replace("_", "").isalnum()
           and "\n" not in str(v)}
    if env:
        config.write_env(env)
    for platform, values in (data.get("tokens") or {}).items():
        if isinstance(values, dict):
            config.save_tokens(platform, values)
    if isinstance(data.get("config"), dict) and data["config"]:
        path = config.ROOT / "config.json"
        config.atomic_write(path, json.dumps(data["config"], ensure_ascii=False, indent=2) + "\n")
    save()
    return len(env)
