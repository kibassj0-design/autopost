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


# ---------------------------------------------------------------- Meta's request limit

RATE_CODES = {4, 17, 32, 613, 80002}  # "Application/User request limit reached" and kin
_cooldown = [0.0]


def cooling_down():
    """Seconds until Instagram should be asked again after hitting its limit, or 0."""
    return max(0.0, _cooldown[0] - time.time())


def _checked(r, what):
    """Like platforms.check, but a request-limit answer stops all Instagram calls for an
    hour (more calls would only extend the block) and says so plainly."""
    from . import PlatformError, check
    if r.status_code >= 400:
        try:
            err = r.json().get("error", {})
        except ValueError:
            err = {}
        if err.get("code") in RATE_CODES or "request limit" in str(err.get("message", "")).lower():
            _cooldown[0] = time.time() + 3600
            raise PlatformError(
                "Instagram: وصلت حد الطلبات المسموح لتطبيقك عند Meta في الساعة. الأداة "
                "توقفت عن مراسلة انستغرام ساعة كاملة، وتعيد نشر هذا المقطع وحدها بعدها. "
                f"({err.get('message', '')[:120]})")
    return check(r, what)


def _not_cooling(PlatformError):
    wait = cooling_down()
    if wait:
        raise PlatformError(f"Instagram: الأداة تنتظر انتهاء حد الطلبات (بعد {int(wait // 60) + 1} "
                            "دقيقة)، ثم تنشر هذا المقطع وحدها.")


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


def fb_check_app():
    """Check the App ID and App secret with Facebook before sending the user to sign in,
    so a wrong secret is reported here instead of after the whole sign-in."""
    from . import PlatformError
    app_id = config.env("FB_APP_ID").strip()
    secret = config.env("FB_APP_SECRET").strip()
    if not app_id.isdigit():
        raise PlatformError(f"App ID يجب أن يكون أرقاما فقط (المكتوب: {len(app_id)} خانة).")
    try:
        r = requests.get(f"https://graph.facebook.com/{VERSION}/oauth/access_token", timeout=20,
                         params={"client_id": app_id, "client_secret": secret,
                                 "grant_type": "client_credentials"})
    except requests.RequestException:
        return  # offline check failed; let the sign-in report any problem
    if r.status_code < 400:
        return
    try:
        message = r.json().get("error", {}).get("message", "")
    except ValueError:
        message = r.text[:200]
    if "secret" in message.lower():
        raise PlatformError(
            f"App secret غير صحيح: المحفوظ طوله {len(secret)} حرفا، ومفتاح فيسبوك الصحيح طوله "
            "32 حرفا (أرقام وحروف a-f). في developers.facebook.com: إعدادات التطبيق ← أساسي ← "
            "المفتاح السري للتطبيق ← إظهار، ثم انقر داخل الخانة واضغط Ctrl+A ثم Ctrl+C. "
            "أو اضغط «إعادة تعيين» وانسخ المفتاح الجديد.")
    raise PlatformError("Facebook: " + message)


def fb_auth_url(state):
    """Facebook Login: the connection Instagram's direct video upload is made for."""
    import urllib.parse
    fb_check_app()
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


UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"}


def _host_litterbox(path):
    with open(path, "rb") as f:
        r = requests.post("https://litterbox.catbox.moe/resources/internals/api.php",
                          data={"reqtype": "fileupload", "time": "1h"},
                          files={"fileToUpload": (Path(path).name, f, "video/mp4")}, timeout=1800,
                          headers=UA)
    r.raise_for_status()
    if not r.text.strip().startswith("https://"):
        raise RuntimeError(r.text[:120])
    return r.text.strip()


def _host_uguu(path):
    with open(path, "rb") as f:
        r = requests.post("https://uguu.se/upload", files={"files[]": (Path(path).name, f, "video/mp4")},
                          timeout=1800, headers=UA)
    r.raise_for_status()
    return r.json()["files"][0]["url"]


def _host_tmpfiles(path):
    with open(path, "rb") as f:
        r = requests.post("https://tmpfiles.org/api/v1/upload",
                          files={"file": (Path(path).name, f, "video/mp4")}, timeout=1800,
                          headers=UA)
    r.raise_for_status()
    url = r.json()["data"]["url"]  # the page; the file itself is under /dl/
    return url.replace("tmpfiles.org/", "tmpfiles.org/dl/", 1).replace("http://", "https://", 1)


def _host_catbox(path):
    """Last resort: catbox keeps the file (no expiry). The video is public on
    Instagram anyway, but it stays on catbox until deleted there."""
    with open(path, "rb") as f:
        r = requests.post("https://catbox.moe/user/api.php", data={"reqtype": "fileupload"},
                          files={"fileToUpload": (Path(path).name, f, "video/mp4")},
                          timeout=1800, headers=UA)
    r.raise_for_status()
    if not r.text.strip().startswith("https://"):
        raise RuntimeError(r.text[:120])
    return r.text.strip()


HOSTS = [("litterbox.catbox.moe", _host_litterbox), ("tmpfiles.org", _host_tmpfiles),
         ("uguu.se", _host_uguu), ("catbox.moe", _host_catbox)]


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
    # Each check counts against Meta's hourly request limit: wait a little longer each
    # time (10 s, 15 s, ... up to 45 s) instead of asking every 5 seconds.
    waited, pause = 0, 10
    time.sleep(pause)
    while waited < 15 * 60:
        st = _checked(requests.get(f"{_base()}/{container['id']}", timeout=30, params={
            "fields": "status_code,status", "access_token": token}), "Instagram status").json()
        if st.get("status_code") == "FINISHED":
            return
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise PlatformError(f"Instagram rejected the video: {st.get('status')}")
        time.sleep(pause)
        waited += pause
        pause = min(pause + 5, 45)
    raise PlatformError("Instagram was still processing the video after 15 minutes")


def _container(token, user, text, **source):
    from . import check
    return _checked(requests.post(f"{_base()}/{user}/media", timeout=60, data={
        "media_type": "REELS", "caption": text["caption"], "share_to_feed": "true",
        "thumb_offset": "300",  # the clip's first second is its designed cover
        "access_token": token, **source}), "Instagram container").json()


def _media_publish(token, user, creation_id):
    """Instagram sometimes answers "media not ready" for a few seconds after the video
    is processed; wait and ask again instead of failing a finished upload."""
    from . import check
    for attempt in range(4):
        r = requests.post(f"{_base()}/{user}/media_publish", timeout=60, data={
            "creation_id": creation_id, "access_token": token})
        if r.status_code < 400 or attempt == 3:
            return _checked(r, "Instagram publish").json()
        try:
            err = r.json().get("error", {})
        except ValueError:
            err = {}
        not_ready = err.get("code") == 9007 or err.get("error_subcode") == 2207027 or \
            "not ready" in str(err.get("message", "")).lower()
        if not not_ready:
            return _checked(r, "Instagram publish").json()
        time.sleep(15)


def publish(path, text, cfg):
    from . import PlatformError, check
    _not_cooling(PlatformError)
    token, user = config.env("IG_ACCESS_TOKEN"), config.env("IG_USER_ID")
    container = _container(token, user, text, upload_type="resumable")

    size = os.path.getsize(path)
    with open(path, "rb") as f:
        r = requests.post(container["uri"], data=f, timeout=1800, headers={
            "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size),
            "Content-Type": "application/octet-stream"})
    if r.status_code >= 400 and "ProcessingFailedError" in r.text:
        # Second try exactly as Meta's own example sends it (curl --data-binary: the
        # whole file in one body, no explicit content type), on a fresh container.
        log.info("Instagram refused the upload; retrying it the way Meta's example does")
        container = _container(token, user, text, upload_type="resumable")
        r = requests.post(container["uri"], data=Path(path).read_bytes(), timeout=1800,
                          headers={"Authorization": f"OAuth {token}", "offset": "0",
                                   "file_size": str(size)})
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
                if cooling_down():
                    raise  # the request limit: more attempts would only extend it
                last = f"via {host}: {e}"
                log.warning("Instagram could not use the copy on %s: %s", host, e)
        else:
            raise PlatformError(last + " (and every video_url attempt failed; see the log)")

    media = _media_publish(token, user, container["id"])
    link = requests.get(f"{_base()}/{media['id']}", timeout=30, params={
        "fields": "permalink", "access_token": token})
    return media["id"], link.json().get("permalink") if link.ok else None


_stats_cache = {}       # remote id -> (time, numbers): each post is read every 2 hours
_no_insights = [False]  # views need a permission this account may not have


def stats(remote_id):
    """A post's numbers, read at most every 2 hours (requests count against Meta's limit)."""
    from . import PlatformError
    if cooling_down():
        raise PlatformError("Instagram request limit: stats skipped for now")
    cached = _stats_cache.get(remote_id)
    if cached and time.time() - cached[0] < 2 * 3600:
        return cached[1]
    out = _read_stats(remote_id)
    _stats_cache[remote_id] = (time.time(), out)
    return out


def _read_stats(remote_id):
    from . import check
    token = config.env("IG_ACCESS_TOKEN")
    r = None
    if not _no_insights[0]:
        r = requests.get(f"{_base()}/{remote_id}/insights", timeout=30, params={
            "metric": "views,likes,comments,shares", "access_token": token})
        if r.status_code >= 400:
            _checked(r, "Instagram insights") if "request limit" in r.text.lower() else None
            if "permission" in r.text.lower() or '"code":10' in r.text or '"code":200' in r.text:
                _no_insights[0] = True  # don't spend a request on it again this run
    if r is None or r.status_code >= 400:
        # Views need the instagram_manage_insights permission; likes and comments
        # come with the basic one, so engagement is still tracked without it.
        basic = _checked(requests.get(f"{_base()}/{remote_id}", timeout=30, params={
            "fields": "like_count,comments_count", "access_token": token}),
            "Instagram likes").json()
        return {"views": None, "likes": int(basic.get("like_count") or 0),
                "comments": int(basic.get("comments_count") or 0), "shares": 0}
    out = {"views": 0, "likes": 0, "comments": 0, "shares": 0}
    for item in r.json().get("data", []):
        if "total_value" in item:
            out[item["name"]] = int(item["total_value"].get("value", 0))
        elif item.get("values"):
            out[item["name"]] = int(item["values"][0].get("value", 0))
    return out
