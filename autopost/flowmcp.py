"""Adds the Google Flow MCP server (github.com/abdureda1999-ai/google-flow-mcp) to Claude on
this computer, when the user presses the button for it.

- installs uv (which runs the server) into the tool's own Python, if missing
- adds "google-flow" to Claude Desktop's config (a copy of the old file is kept)
- adds it to Claude Code too, when the `claude` command is installed
The server version is pinned to the commit that was read, so later changes to that
repository are not picked up without a review.
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger("autopost.flowmcp")

REPO = "git+https://github.com/abdureda1999-ai/google-flow-mcp@c09d838"
NAME = "google-flow"


def _uvx():
    folder = Path(sys.executable).parent
    for name in ("uvx.exe", "uvx"):
        if (folder / name).exists():
            return str(folder / name)
    return shutil.which("uvx")


def _ensure_uv():
    exe = _uvx()
    if exe:
        return exe
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "uv"],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError("تعذّر تثبيت uv: " + r.stderr.strip()[-200:])
    exe = _uvx()
    if not exe:
        raise RuntimeError("ثُبّت uv لكن لم يُعثر على uvx.")
    return exe


def _desktop_configs():
    """Claude Desktop's config file(s) on this computer (Windows normal and Store
    installs, macOS, Linux)."""
    found = []
    appdata, local = os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA")
    if appdata:
        found.append(Path(appdata) / "Claude" / "claude_desktop_config.json")
    if local:
        for pkg in (Path(local) / "Packages").glob("Claude_*"):
            found.append(pkg / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json")
    home = Path.home()
    found.append(home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json")
    found.append(home / ".config" / "Claude" / "claude_desktop_config.json")
    # the ones whose Claude folder exists (Claude Desktop is installed there)
    return [p for p in found if p.parent.exists()]


def status():
    configs = _desktop_configs()
    added = []
    for p in configs:
        try:
            if NAME in json.loads(p.read_text(encoding="utf-8")).get("mcpServers", {}):
                added.append(str(p))
        except (OSError, ValueError):
            pass
    return {"desktop_found": bool(configs), "added": added,
            "claude_code": bool(shutil.which("claude")), "uv": bool(_uvx())}


def install():
    """Returns a list of what was done."""
    done = []
    uvx = _ensure_uv()
    done.append("uv جاهز")
    entry = {"command": uvx, "args": ["--from", REPO, "google-flow-mcp"]}
    configs = _desktop_configs()
    for p in configs:
        try:
            data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except ValueError:
            data = {}
        if p.exists():
            shutil.copy2(p, p.with_name(p.name + f".backup-{int(time.time())}"))
        data.setdefault("mcpServers", {})[NAME] = entry
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        done.append(f"أُضيف إلى Claude Desktop ({p})")
    claude = shutil.which("claude")
    if claude:
        subprocess.run([claude, "mcp", "remove", "--scope", "user", NAME],
                       capture_output=True, text=True, timeout=60)
        r = subprocess.run([claude, "mcp", "add", "--scope", "user", NAME, "--", uvx, "--from",
                            REPO, "google-flow-mcp"], capture_output=True, text=True, timeout=120)
        done.append("أُضيف إلى Claude Code" if r.returncode == 0
                    else "Claude Code: " + (r.stderr or r.stdout).strip()[-160:])
    if not configs and not claude:
        raise RuntimeError("لم أجد Claude Desktop ولا Claude Code على هذا الكمبيوتر. ثبّت "
                           "Claude Desktop من claude.ai/download ثم اضغط مرة أخرى.")
    # fetch the server once now, so the first use in Claude doesn't wait for it
    try:
        subprocess.run([uvx, "--from", REPO, "google-flow-mcp", "--help"], capture_output=True,
                       text=True, timeout=900)
        done.append("نُزّل خادم Flow")
    except Exception as e:
        log.info("Flow server pre-fetch skipped: %s", e)
    return done
