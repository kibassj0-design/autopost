"""TikTok Content Posting API: direct post by chunked file upload."""
import logging
import os
import time
import urllib.parse

import requests

from .. import config

API = "https://open.tiktokapis.com/v2"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
log = logging.getLogger("autopost.tiktok")
INBOX = "tiktok:inbox"      # sent to the creator's TikTok inbox, not on the profile yet
PRIVATE = "tiktok:private"  # posted, but only the creator can see it
CHUNK = 10 * 1024 * 1024  # TikTok allows 5-64 MB per chunk


def auth_url():
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_key": config.env("TT_CLIENT_KEY"), "scope": config.env("TT_SCOPES", required=False) or "user.info.basic,video.publish,video.upload",
        "response_type": "code", "redirect_uri": config.env("TT_REDIRECT_URI"),
        "state": "autopost"})


def _token_request(data):
    from . import PlatformError, check
    r = check(requests.post(f"{API}/oauth/token/", timeout=30, data={
        "client_key": config.env("TT_CLIENT_KEY"),
        "client_secret": config.env("TT_CLIENT_SECRET"), **data}), "TikTok token")
    body = r.json()
    if "access_token" not in body:
        raise PlatformError(f"TikTok token: {body}")
    # TikTok rotates the refresh token, so keep whichever one came back.
    config.save_tokens("tiktok", {
        "access_token": body["access_token"], "refresh_token": body["refresh_token"],
        "expires_at": time.time() + body["expires_in"] - 300})
    return body["access_token"]


def exchange(code):
    _token_request({"code": code, "grant_type": "authorization_code",
                    "redirect_uri": config.env("TT_REDIRECT_URI")})
    try:  # the account name, shown in the app
        info = _call("/post/publish/creator_info/query/", {}, "TikTok creator info")
        config.save_tokens("tiktok", {"username": info.get("creator_nickname")
                                      or info.get("creator_username", "")})
    except Exception:
        pass


def _headers():
    from . import PlatformError
    tok = config.get_tokens().get("tiktok", {})
    if not tok.get("refresh_token"):
        raise PlatformError("TikTok is not connected: run `python -m autopost auth tiktok`")
    access = tok["access_token"]
    if time.time() >= tok.get("expires_at", 0):
        access = _token_request({"grant_type": "refresh_token",
                                 "refresh_token": tok["refresh_token"]})
    return {"Authorization": f"Bearer {access}",
            "Content-Type": "application/json; charset=UTF-8"}


def _call(path, payload, what, params=None):
    from . import PlatformError
    r = requests.post(f"{API}{path}", json=payload, params=params,
                      headers=_headers(), timeout=60)
    try:
        body = r.json()
    except ValueError:
        raise PlatformError(f"{what}: HTTP {r.status_code} {r.text[:300]}")
    err = body.get("error", {})
    if r.status_code >= 400 or err.get("code", "ok") != "ok":
        raise PlatformError(f"{what}: {err.get('code')} {err.get('message')}")
    return body["data"]


def publish(path, text, cfg):
    from . import PlatformError, check
    if cfg.get("tiktok_mode") == "draft":
        return _upload_draft(path, text, cfg)
    creator = _call("/post/publish/creator_info/query/", {}, "TikTok creator info")
    options = creator.get("privacy_level_options", [])
    privacy = cfg["tiktok_privacy"]
    if privacy not in options:
        # Apps that have not passed TikTok's audit may only post privately.
        log.warning("TikTok does not allow %s for this app/account; posting as %s",
                    privacy, options[0] if options else "SELF_ONLY")
        privacy = options[0] if options else "SELF_ONLY"

    size, chunk, count = _chunks(path)
    try:
        init = _call("/post/publish/video/init/", {
            "post_info": {"title": text["caption"], "privacy_level": privacy,
                          "disable_comment": bool(creator.get("comment_disabled")),
                          "disable_duet": bool(creator.get("duet_disabled")),
                          "disable_stitch": bool(creator.get("stitch_disabled"))},
            "source_info": {"source": "FILE_UPLOAD", "video_size": size,
                            "chunk_size": chunk, "total_chunk_count": count},
        }, "TikTok upload init")
    except PlatformError as e:
        if "unaudited_client_can_only_post_to_private_accounts" not in str(e):
            raise
        # Before TikTok's audit, direct posting only works on private accounts. Rather
        # than lose the post, send it to the TikTok inbox: posted from the app with one
        # tap, publicly.
        log.warning("TikTok: the app isn't audited yet and the account is public, so "
                    "direct posting is refused; sent to your TikTok inbox instead")
        return _upload_draft(path, text, cfg)

    _send(path, init["upload_url"], size, chunk, count)

    publish_id = init["publish_id"]
    for _ in range(60):
        st = _call("/post/publish/status/fetch/", {"publish_id": publish_id}, "TikTok status")
        if st["status"] == "PUBLISH_COMPLETE":
            ids = st.get("publicaly_available_post_id") or []
            if not ids or privacy != "PUBLIC_TO_EVERYONE":
                # Private posts get no public id, so there are no stats to track for them.
                log.info("TikTok: posted as %s (only you can see it)", privacy)
                return None, PRIVATE
            return str(ids[0]), None
        if st["status"] == "FAILED":
            raise PlatformError(f"TikTok rejected the video: {st.get('fail_reason')}")
        time.sleep(5)
    raise PlatformError("TikTok was still processing the video after 5 minutes")


def _chunks(path):
    size = os.path.getsize(path)
    chunk = size if size < CHUNK else CHUNK
    return size, chunk, max(1, size // chunk)  # the last chunk carries the remainder


def _send(path, url, size, chunk, count):
    from . import check
    with open(path, "rb") as f:
        for i in range(count):
            start = i * chunk
            end = size - 1 if i == count - 1 else start + chunk - 1
            f.seek(start)
            check(requests.put(url, data=f.read(end - start + 1), timeout=900,
                               headers={"Content-Type": "video/mp4",
                                        "Content-Range": f"bytes {start}-{end}/{size}"}),
                  "TikTok upload")



def stats(remote_id):
    data = _call("/video/query/", {"filters": {"video_ids": [remote_id]}}, "TikTok stats",
                 params={"fields": "id,view_count,like_count,comment_count,share_count"})
    videos = data.get("videos", [])
    if not videos:
        return None
    v = videos[0]
    return {"views": v.get("view_count", 0), "likes": v.get("like_count", 0),
            "comments": v.get("comment_count", 0), "shares": v.get("share_count", 0)}


def _upload_draft(path, text, cfg):
    """Send the video to the creator's TikTok inbox. They post it from the app with
    one tap, publicly, which needs no app audit. The caption goes in the app's notes."""
    from . import PlatformError
    size, chunk, count = _chunks(path)
    init = _call("/post/publish/inbox/video/init/", {
        "source_info": {"source": "FILE_UPLOAD", "video_size": size,
                        "chunk_size": chunk, "total_chunk_count": count},
    }, "TikTok draft init")
    _send(path, init["upload_url"], size, chunk, count)
    for _ in range(60):
        st = _call("/post/publish/status/fetch/", {"publish_id": init["publish_id"]},
                   "TikTok status")
        if st["status"] in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
            log.info("TikTok: sent to your inbox. Open TikTok and post it. Caption: %s",
                     text["caption"][:300])
            # Not on the profile yet: the app shows this instead of "published".
            return None, INBOX
        if st["status"] == "FAILED":
            raise PlatformError(f"TikTok rejected the video: {st.get('fail_reason')}")
        time.sleep(5)
    raise PlatformError("TikTok was still processing the video after 5 minutes")

