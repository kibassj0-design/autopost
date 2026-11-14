from . import instagram, tiktok, youtube

ALL = {"youtube": youtube, "instagram": instagram, "tiktok": tiktok}


class PlatformError(RuntimeError):
    pass


def check(response, what):
    """Raise with the platform's own error text, which is what the user needs to see."""
    if response.status_code >= 400:
        raise PlatformError(f"{what}: HTTP {response.status_code} {response.text[:500]}")
    return response
