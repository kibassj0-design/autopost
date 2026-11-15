"""YouTube Data API v3: resumable upload (vertical <=3 min is listed as a Short)."""
import os
import time
import urllib.parse

import requests

from .. import config, video

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REDIRECT = "http://localhost:8765"
SCOPES = ("https://www.googleapis.com/auth/youtube.upload "
          "https://www.googleapis.com/auth/youtube.readonly")
API = "https://www.googleapis.com/youtube/v3"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"


def auth_url(state=""):
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "state": state,
        "client_id": config.env("YT_CLIENT_ID"), "redirect_uri": REDIRECT,
        "response_type": "code", "scope": SCOPES,
        "access_type": "offline", "prompt": "consent"})


def exchange(code):
    from . import check
    r = check(requests.post(TOKEN_URL, data={
        "code": code, "client_id": config.env("YT_CLIENT_ID"),
        "client_secret": config.env("YT_CLIENT_SECRET"),
        "redirect_uri": REDIRECT, "grant_type": "authorization_code"}, timeout=30),
        "YouTube token exchange")
    config.save_tokens("youtube", {"refresh_token": r.json()["refresh_token"]})
    try:  # the channel name, shown in the app
        ch = requests.get(f"{API}/channels", params={"part": "snippet", "mine": "true"},
                          headers={"Authorization": f"Bearer {r.json()['access_token']}"},
                          timeout=30).json()
        config.save_tokens("youtube", {"username": ch["items"][0]["snippet"]["title"]})
    except Exception:
        pass


def _headers():
    from . import PlatformError, check
    refresh = config.get_tokens().get("youtube", {}).get("refresh_token")
    if not refresh:
        raise PlatformError("YouTube is not connected: run `python -m autopost auth youtube`")
    r = check(requests.post(TOKEN_URL, data={
        "client_id": config.env("YT_CLIENT_ID"),
        "client_secret": config.env("YT_CLIENT_SECRET"),
        "refresh_token": refresh, "grant_type": "refresh_token"}, timeout=30),
        "YouTube token refresh")
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _link(vid, path):
    meta = video.probe(path)
    if meta["height"] > meta["width"]:
        return f"https://www.youtube.com/shorts/{vid}"
    return f"https://youtu.be/{vid}"


def publish(path, text, cfg):
    from . import PlatformError, check
    headers = _headers()
    size = os.path.getsize(path)
    meta = {
        "snippet": {"title": text["title"], "description": text["description"],
                    "tags": text["tags"], "categoryId": "22"},
        "status": {"privacyStatus": cfg["youtube_privacy"],
                   "selfDeclaredMadeForKids": False},
    }
    init = check(requests.post(
        UPLOAD, params={"uploadType": "resumable", "part": "snippet,status"},
        headers={**headers, "X-Upload-Content-Type": "video/mp4",
                 "X-Upload-Content-Length": str(size)},
        json=meta, timeout=60), "YouTube upload init")
    session = init.headers["Location"]

    offset = 0
    for attempt in range(5):
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                r = requests.put(session, data=f, timeout=1800, headers={
                    **headers, "Content-Type": "video/mp4",
                    "Content-Length": str(size - offset),
                    "Content-Range": f"bytes {offset}-{size - 1}/{size}"})
            if r.status_code in (200, 201):
                vid = r.json()["id"]
                return vid, _link(vid, path)
            if r.status_code < 500:
                check(r, "YouTube upload")
        except requests.ConnectionError:
            pass
        time.sleep(2 ** attempt)
        # Ask how many bytes arrived, then resume from there.
        q = requests.put(session, timeout=60, headers={
            **headers, "Content-Length": "0", "Content-Range": f"bytes */{size}"})
        if q.status_code in (200, 201):
            vid = q.json()["id"]
            return vid, _link(vid, path)
        offset = int(q.headers["Range"].split("-")[1]) + 1 if "Range" in q.headers else 0
    raise PlatformError("YouTube upload kept failing after 5 attempts")


def stats(remote_id):
    from . import check
    r = check(requests.get(f"{API}/videos", params={"part": "statistics", "id": remote_id},
                           headers=_headers(), timeout=30), "YouTube stats")
    items = r.json().get("items", [])
    if not items:
        return None
    s = items[0]["statistics"]
    return {"views": int(s.get("viewCount", 0)), "likes": int(s.get("likeCount", 0)),
            "comments": int(s.get("commentCount", 0)), "shares": 0}
