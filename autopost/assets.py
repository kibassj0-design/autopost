"""Extra files that make the clips better, fetched once on first use and checked by their
fingerprint (SHA-256) so a damaged or altered download is never used:

- Tajawal ExtraBold: a clean, bold Arabic + Latin font for captions and titles
  (Google Fonts, SIL Open Font License).
- OpenCV's ResNet-10 SSD face detector: finds faces (and streamers' facecams) far more
  reliably than the old Haar method, also with glasses, side views and small cams.
"""
import hashlib
import logging
import threading

import requests

from . import config

log = logging.getLogger("autopost.assets")

FILES = {
    "fonts/Tajawal-ExtraBold.ttf": (
        "https://raw.githubusercontent.com/google/fonts/main/ofl/tajawal/Tajawal-ExtraBold.ttf",
        "3d1298b747c22579f9e1bdeb0d4165064cb17f7e66145e2f43f9deb12ad7a2cf"),
    "face.prototxt": (
        "https://raw.githubusercontent.com/opencv/opencv/4.x/samples/dnn/face_detector/"
        "deploy.prototxt",
        "dcd661dc48fc9de0a341db1f666a2164ea63a67265c7f779bc12d6b3f2fa67e9"),
    "face.caffemodel": (
        "https://raw.githubusercontent.com/opencv/opencv_3rdparty/"
        "dnn_samples_face_detector_20170830/res10_300x300_ssd_iter_140000.caffemodel",
        "2a56a11a57a4a295956b0660b4a3d76bbdca2206c4961cea8efe7d95c7cb2f2d"),
}
_lock = threading.Lock()
_failed = set()


def folder():
    d = config.DATA / "assets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get(name):
    """Path of the file, downloading it the first time; None when it can't be had (the
    caller then uses its plain fallback)."""
    path = folder() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path
    if name in _failed:
        return None
    url, digest = FILES[name]
    with _lock:
        if path.exists():
            return path
        try:
            r = requests.get(url, timeout=120)
            r.raise_for_status()
            if hashlib.sha256(r.content).hexdigest() != digest:
                raise RuntimeError("the file is not the expected one")
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(r.content)
            tmp.replace(path)
            log.info("Downloaded %s", name)
            return path
        except Exception as e:
            _failed.add(name)
            log.info("Could not get %s (%s); using the plain fallback", name, str(e)[:120])
            return None


def fonts_dir():
    """Folder holding the caption font, or None."""
    return folder() / "fonts" if get("fonts/Tajawal-ExtraBold.ttf") else None


_net = None


def face_net():
    """OpenCV's DNN face detector, or None."""
    global _net
    if _net is None:
        proto, model = get("face.prototxt"), get("face.caffemodel")
        if not proto or not model:
            return None
        import cv2
        _net = cv2.dnn.readNetFromCaffe(str(proto), str(model))
    return _net
