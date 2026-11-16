"""Keeps the tool up to date from its GitHub repository, in place.

The newest version is downloaded as a zip and copied over the program files. The
user's own files are never touched: keys (.env), settings (config.json), the data
folder (accounts, database, schedule), the clip folders and the installed packages.
"""
import io
import json
import logging
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

import requests

from . import config

log = logging.getLogger("autopost.update")

REPO = "kibassj0-design/autopost"
BRANCH = "main"
KEEP = {".env", "config.json", "data", "inbox", "done", "failed", ".venv", ".git"}
VERSION_FILE = "version.json"
RESTART_CODE = 3  # start.bat starts the tool again on this exit code


def _repo(cfg):
    return cfg.get("update_repo") or REPO


def installed():
    path = config.DATA / VERSION_FILE
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def latest(cfg):
    """(commit id, its date) of the newest version on GitHub."""
    r = requests.get(f"https://api.github.com/repos/{_repo(cfg)}/commits/{BRANCH}",
                     timeout=20, headers={"Accept": "application/vnd.github+json"})
    r.raise_for_status()
    data = r.json()
    return data["sha"], data["commit"]["committer"]["date"]


def check(cfg):
    """{'available': bool, 'current': sha or None, 'latest': sha, 'date': ...}"""
    sha, date = latest(cfg)
    current = installed().get("sha")
    return {"available": sha != current, "current": current, "latest": sha, "date": date}


def _download(cfg, sha):
    r = requests.get(f"https://codeload.github.com/{_repo(cfg)}/zip/{sha}", timeout=120)
    r.raise_for_status()
    return r.content


def apply(cfg, sha=None):
    """Download and install the newest version over this folder. Returns the new sha."""
    sha = sha or latest(cfg)[0]
    data = _download(cfg, sha)
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            z.extractall(tmp)
        # The zip holds one top folder (repo-sha/); its contents are the program.
        top = next(p for p in Path(tmp).iterdir() if p.is_dir())
        if not (top / "autopost" / "__main__.py").exists():
            raise RuntimeError("the downloaded update doesn't look like autopost")
        for item in top.iterdir():
            if item.name in KEEP:
                continue
            dest = config.ROOT / item.name
            if item.name in ("start.bat", "start.sh"):
                # The launcher is running right now; it swaps the new one in itself.
                if not dest.exists() or dest.read_bytes() != item.read_bytes():
                    shutil.copy2(item, dest.with_name(item.name + ".new"))
                continue
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)
    config.DATA.mkdir(exist_ok=True)
    (config.DATA / VERSION_FILE).write_text(json.dumps({"sha": sha, "at": time.time()}))
    log.info("Updated to %s; restarting", sha[:7])
    return sha
