"""Instagram Graph API: Reels via resumable upload (no public video URL needed)."""
import os
import time

import requests

from .. import config

VERSION = "v23.0"


def _base():
    host = config.env("IG_GRAPH_HOST", required=False) or "graph.facebook.com"
    return f"https://{host}/{VERSION}"


def publish(path, text, cfg):
    from . import PlatformError, check
    token, user = config.env("IG_ACCESS_TOKEN"), config.env("IG_USER_ID")
    container = check(requests.post(f"{_base()}/{user}/media", timeout=60, data={
        "media_type": "REELS", "upload_type": "resumable", "caption": text["caption"],
        "share_to_feed": "true", "access_token": token}), "Instagram container").json()

    with open(path, "rb") as f:
        check(requests.post(container["uri"], data=f, timeout=1800, headers={
            "Authorization": f"OAuth {token}", "offset": "0",
            "file_size": str(os.path.getsize(path))}), "Instagram upload")

    for _ in range(60):  # Instagram transcodes before it lets us publish
        st = check(requests.get(f"{_base()}/{container['id']}", timeout=30, params={
            "fields": "status_code,status", "access_token": token}), "Instagram status").json()
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise PlatformError(f"Instagram rejected the video: {st.get('status')}")
        time.sleep(5)
    else:
        raise PlatformError("Instagram was still processing the video after 5 minutes")

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
