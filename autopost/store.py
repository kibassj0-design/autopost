"""SQLite record of what was posted where, and the stats seen over time."""
import sqlite3
import time

from .config import DATA

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos(
  id INTEGER PRIMARY KEY, source TEXT, title TEXT, created_at REAL,
  parent_id INTEGER, boosts INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS posts(
  id INTEGER PRIMARY KEY, video_id INTEGER, platform TEXT, remote_id TEXT,
  url TEXT, published_at REAL, error TEXT);
CREATE TABLE IF NOT EXISTS schedule(
  id INTEGER PRIMARY KEY, path TEXT, title TEXT, caption TEXT, note TEXT,
  platforms TEXT, at REAL, status TEXT DEFAULT 'pending', result TEXT, created REAL);
CREATE TABLE IF NOT EXISTS stats(
  post_id INTEGER, ts REAL, views INTEGER, likes INTEGER, comments INTEGER, shares INTEGER);
"""


def connect():
    DATA.mkdir(exist_ok=True)
    db = sqlite3.connect(DATA / "autopost.db")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def add_video(db, source, title, parent_id=None):
    cur = db.execute("INSERT INTO videos(source,title,created_at,parent_id) VALUES(?,?,?,?)",
                     (str(source), title, time.time(), parent_id))
    db.commit()
    return cur.lastrowid


def add_post(db, video_id, platform, remote_id=None, url=None, error=None):
    db.execute("INSERT INTO posts(video_id,platform,remote_id,url,published_at,error)"
               " VALUES(?,?,?,?,?,?)",
               (video_id, platform, remote_id, url, time.time(), error))
    db.commit()


def update_post(db, post_id, remote_id=None, url=None, error=None):
    db.execute("UPDATE posts SET remote_id=?, url=?, published_at=?, error=? WHERE id=?",
               (remote_id, url, time.time(), error, post_id))
    db.commit()


def add_stats(db, post_id, s):
    db.execute("INSERT INTO stats VALUES(?,?,?,?,?,?)",
               (post_id, time.time(), s["views"], s["likes"], s["comments"], s["shares"]))
    db.commit()


def last_stats(db, post_id, before):
    return db.execute("SELECT * FROM stats WHERE post_id=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                      (post_id, before)).fetchone()
