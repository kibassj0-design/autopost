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


FB_SCOPES = ("instagram_basic,instagram_content_publish,pages_show_list,"
             "pages_read_engagement,business_management")
FB_REDIRECT = "http://localhost:8765/fb"


def fb_auth_url(state):
    """Facebook Login: the connection Instagram's direct video upload is made for."""
    import urllib.parse
    params = {"client_id": config.env("FB_APP_ID"), "redirect_uri": FB_REDIRECT,
              "state": state, "response_type": "code"}
    config_id = config.env("FB_CONFIG_ID", required=False)
    if config_id:  # Facebook Login for Business configuration, when the app has one
        params["config_id"] = config_id
    else:
        params["scope"] = FB_SCOPES
    return f"https://www.facebook.com/{VERSION}/dialog/oauth?" + urllib.parse.urlencode(params)


def fb_exchange(code):
    """Code -> long-lived user token -> the Page token of the Page linked to the
    Instagram account (a Page token made from a long-lived token does not expire)."""
    from . import PlatformError
    base = f"https://graph.facebook.com/{VERSION}"
    app_id, secret = config.env("FB_APP_ID"), config.env("FB_APP_SECRET")

    def get(path, **params):
        r = requests.get(f"{base}/{path}", params=params, timeout=30)
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code >= 400:
            raise PlatformError("Facebook: " + body.get("error", {}).get("message", r.text[:200]))
        return body

    short = get("oauth/access_token", client_id=app_id, client_secret=secret,
                redirect_uri=FB_REDIRECT, code=code)["access_token"]
    long_lived = get("oauth/access_token", grant_type="fb_exchange_token", client_id=app_id,
                     client_secret=secret, fb_exchange_token=short)["access_token"]
    pages = get("me/accounts", access_token=long_lived,
                fields="name,access_token,instagram_business_account{id,username}")
    for page in pages.get("data", []):
        ig = page.get("instagram_business_account")
        if ig:
            config.write_env({"IG_ACCESS_TOKEN": page["access_token"], "IG_USER_ID": ig["id"],
                              "IG_GRAPH_HOST": "graph.facebook.com"})
            config.save_tokens("instagram", {"username": ig.get("username", ""),
                                             "via": "facebook", "page": page.get("name", ""),
                                             "refreshed_at": time.time()})
            return ig.get("username", "")
    raise PlatformError("None of your Facebook Pages is linked to an Instagram professional "
                        "account. Link them first: Facebook Page settings -> Linked accounts "
                        "-> Instagram.")


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


def _host_litterbox(path):
    with open(path, "rb") as f:
        r = requests.post("https://litterbox.catbox.moe/resources/internals/api.php",
                          data={"reqtype": "fileupload", "time": "1h"},
                          files={"fileToUpload": (Path(path).name, f, "video/mp4")}, timeout=1800)
    r.raise_for_status()
    if not r.text.strip().startswith("https://"):
        raise RuntimeError(r.text[:120])
    return r.text.strip()


def _host_uguu(path):
    with open(path, "rb") as f:
        r = requests.post("https://uguu.se/upload", files={"files[]": (Path(path).name, f, "video/mp4")},
                          timeout=1800)
    r.raise_for_status()
    return r.json()["files"][0]["url"]


def _host_tmpfiles(path):
    with open(path, "rb") as f:
        r = requests.post("https://tmpfiles.org/api/v1/upload",
                          files={"file": (Path(path).name, f, "video/mp4")}, timeout=1800)
    r.raise_for_status()
    url = r.json()["data"]["url"]  # the page; the file itself is under /dl/
    return url.replace("tmpfiles.org/", "tmpfiles.org/dl/", 1).replace("http://", "https://", 1)


HOSTS = [("litterbox.catbox.moe", _host_litterbox), ("uguu.se", _host_uguu),
         ("tmpfiles.org", _host_tmpfiles)]


def _temporary_urls(path):
    """Public addresses Instagram can download the video from, one host after another.
    All three are free temporary hosts that delete the file within hours (the post
    itself is public anyway)."""
    problems = []
    for name, upload in HOSTS:
        try:
            yield name, upload(path)
        except Exception as e:
            problems.append(f"{name}: {str(e)[:100]}")
            log.warning("Temporary host %s failed: %s", name, str(e)[:120])
    if problems:
        log.warning("Temporary hosts tried: %s", " | ".join(problems))


def _wait(token, container):
    """Instagram processes the video before it can be published."""
    from . import PlatformError, check
    for _ in range(120):
        st = check(requests.get(f"{_base()}/{container['id']}", timeout=30, params={
            "fields": "status_code,status", "access_token": token}), "Instagram status").json()
        if st.get("status_code") == "FINISHED":
            return
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise PlatformError(f"Instagram rejected the video: {st.get('status')}")
        time.sleep(5)
    raise PlatformError("Instagram was still processing the video after 10 minutes")


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
    if r.status_code < 400:
        _wait(token, container)
    else:
        # Direct upload refused (it is meant for Facebook-login apps; Instagram-login
        # apps get "ProcessingFailedError"). The other official way: Instagram
        # downloads the video from a public address. Each temporary host is tried in
        # turn until Instagram accepts one.
        log.warning("Instagram refused the direct upload (%s); trying video_url", r.text[:160])
        last = f"Instagram upload: HTTP {r.status_code} {r.text[:200]}"
        for host, url in _temporary_urls(path):
            try:
                container = _container(token, user, text, video_url=url)
                _wait(token, container)
                log.info("Instagram took the video from %s", host)
                break
            except PlatformError as e:
                last = f"via {host}: {e}"
                log.warning("Instagram could not use the copy on %s: %s", host, e)
        else:
            raise PlatformError(last + " (and every video_url attempt failed; see the log)")

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
