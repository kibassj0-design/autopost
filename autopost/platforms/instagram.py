"""Instagram Graph API: Reels via resumable upload (no public video URL needed).

Two kinds of token work:
- Instagram Login tokens (start with "IG"): graph.instagram.com, refreshable for ever.
- Facebook Login tokens (start with "EAA"): graph.facebook.com, through a Facebook Page.
"""
import logging
import os
import time

from pathlib import Path

import requests

from .. import config

log = logging.getLogger("autopost.instagram")
VERSION = "v23.0"
REFRESH_EVERY = 7 * 24 * 3600


def _base():
    host = config.env("IG_GRAPH_HOST", required=False) or "graph.facebook.com"
    return f"https://{host}/{VERSION}"


def identify(token, user_id=None):
    """(user id, username, graph host) for a token, so only the token has to be pasted.
    A system-user token (EAA...) may see no Facebook Page; then the Instagram account
    id from Business Settings is given as well and checked directly."""
    from . import PlatformError
    token = token.strip()
    if token.startswith("EAA") and user_id:
        r = requests.get(f"https://graph.facebook.com/{VERSION}/{user_id.strip()}",
                         timeout=30, params={"fields": "id,username", "access_token": token})
        if r.status_code >= 400:
            raise PlatformError("Instagram: " + r.json().get("error", {}).get("message", r.text[:200]))
        return r.json()["id"], r.json().get("username", ""), "graph.facebook.com"
    if token.startswith("EAA"):
        r = requests.get(f"https://graph.facebook.com/{VERSION}/me/accounts", timeout=30,
                         params={"fields": "name,instagram_business_account{id,username}",
                                 "access_token": token})
        if r.status_code >= 400:
            raise PlatformError(f"Instagram: {r.json().get('error', {}).get('message', r.text[:200])}")
        for page in r.json().get("data", []):
            ig = page.get("instagram_business_account")
            if ig:
                return ig["id"], ig.get("username", ""), "graph.facebook.com"
        raise PlatformError("This token sees no Facebook Page with an Instagram account. "
                            "Add the Instagram account ID too (Business Settings -> "
                            "Accounts -> Instagram accounts)")
    r = requests.get(f"https://graph.instagram.com/{VERSION}/me", timeout=30,
                     params={"fields": "user_id,username,account_type", "access_token": token})
    if r.status_code >= 400:
        try:
            msg = r.json()["error"]["message"]
        except (ValueError, KeyError):
            msg = r.text[:200]
        raise PlatformError(f"Instagram rejected the token: {msg}")
    me = r.json()
    if me.get("account_type") == "PERSONAL":
        raise PlatformError("This is a personal Instagram account. Switch it to a "
                            "Professional (Creator or Business) account first")
    return str(me.get("user_id") or me["id"]), me.get("username", ""), "graph.instagram.com"


def connect(token, user_id=None):
    user_id, username, host = identify(token, user_id)
    config.write_env({"IG_ACCESS_TOKEN": token.strip(), "IG_USER_ID": user_id,
                      "IG_GRAPH_HOST": host})
    config.save_tokens("instagram", {"username": username, "refreshed_at": time.time()})
    return username


def refresh_if_due(log=None):
    """Instagram Login tokens last 60 days; renewing weekly keeps them alive."""
    token = os.environ.get("IG_ACCESS_TOKEN", "")
    if not token.startswith("IG"):
        return
    state = config.get_tokens().get("instagram", {})
    if time.time() - state.get("refreshed_at", 0) < REFRESH_EVERY:
        return
    r = requests.get("https://graph.instagram.com/refresh_access_token", timeout=30,
                     params={"grant_type": "ig_refresh_token", "access_token": token})
    if r.status_code < 400 and r.json().get("access_token"):
        config.write_env({"IG_ACCESS_TOKEN": r.json()["access_token"]})
        config.save_tokens("instagram", {"refreshed_at": time.time()})
        if log:
            log.info("Instagram token renewed")
    elif log:
        # A token under a day old can't be renewed yet; try again tomorrow.
        config.save_tokens("instagram", {"refreshed_at": time.time() - REFRESH_EVERY + 86400})
        log.warning("Instagram token renewal failed: %s", r.text[:160])


def _temporary_url(path):
    """Instagram fetches the video from a public address in the video_url method. The
    file goes to litterbox.catbox.moe, a free temporary host, and is deleted there
    after an hour (the post itself is public anyway)."""
    from . import PlatformError
    with open(path, "rb") as f:
        r = requests.post("https://litterbox.catbox.moe/resources/internals/api.php",
                          data={"reqtype": "fileupload", "time": "1h"},
                          files={"fileToUpload": (Path(path).name, f, "video/mp4")},
                          timeout=1800)
    if r.status_code >= 400 or not r.text.strip().startswith("https://"):
        raise PlatformError(f"Temporary video host: HTTP {r.status_code} {r.text[:200]}")
    return r.text.strip()


def _container(token, user, text, **source):
    from . import check
    return check(requests.post(f"{_base()}/{user}/media", timeout=60, data={
        "media_type": "REELS", "caption": text["caption"], "share_to_feed": "true",
        "access_token": token, **source}), "Instagram container").json()


def publish(path, text, cfg):
    from . import PlatformError, check
    token, user = config.env("IG_ACCESS_TOKEN"), config.env("IG_USER_ID")
    container = _container(token, user, text, upload_type="resumable")

    size = os.path.getsize(path)
    with open(path, "rb") as f:
        r = requests.post(container["uri"], data=f, timeout=1800, headers={
            "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size),
            "Content-Type": "application/octet-stream"})
    if r.status_code >= 400 and "ProcessingFailedError" in r.text:
        # Direct upload refused (it is meant for Facebook-login apps; Instagram-login
        # apps get this error). The other official way: Instagram downloads the video
        # from a public address.
        log.warning("Instagram refused the direct upload; sending it by video_url instead")
        container = _container(token, user, text, video_url=_temporary_url(path))
    else:
        check(r, "Instagram upload")

    for _ in range(120):  # Instagram transcodes before it lets us publish
        st = check(requests.get(f"{_base()}/{container['id']}", timeout=30, params={
            "fields": "status_code,status", "access_token": token}), "Instagram status").json()
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise PlatformError(f"Instagram rejected the video: {st.get('status')}")
        time.sleep(5)
    else:
        raise PlatformError("Instagram was still processing the video after 10 minutes")

    media = check(requests.post(f"{_base()}/{user}/media_publish", timeout=60, data={
        "creation_id": container["id"], "access_token": token}), "Instagram publish").json()
    link = requests.get(f"{_base()}/{media['id']}", timeout=30, params={
        "fields": "permalink", "access_token": token})
    return media["id"], link.json().get("permalink") if link.ok else None


def stats(remote_id):
    from . import check
    r = check(requests.get(f"{_base()}/{remote_id}/insights", timeout=30, params={
        "metric": "views,likes,comments,shares",
        "access_token": config.env("IG_ACCESS_TOKEN")}), "Instagram insights")
    out = {"views": 0, "likes": 0, "comments": 0, "shares": 0}
    for item in r.json().get("data", []):
        if "total_value" in item:
            out[item["name"]] = int(item["total_value"].get("value", 0))
        elif item.get("values"):
            out[item["name"]] = int(item["values"][0].get("value", 0))
    return out
