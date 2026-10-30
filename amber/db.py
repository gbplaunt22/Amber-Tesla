import contextlib
import json
import sqlite3
import time
import uuid
from . import config as C

SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
 id TEXT PRIMARY KEY, text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 result TEXT, issues TEXT NOT NULL DEFAULT '[]', error TEXT, created REAL NOT NULL,
 updated REAL NOT NULL, model TEXT, reviewed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS alerts (
 id TEXT PRIMARY KEY, reference TEXT, plate TEXT NOT NULL, plate_raw TEXT NOT NULL,
 jurisdiction TEXT, color TEXT, vehicle_type TEXT, make TEXT, model TEXT, year TEXT,
 source TEXT, draft_id TEXT, created REAL NOT NULL, expires REAL NOT NULL,
 expiry_default INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1,
 version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS clips (
 id TEXT PRIMARY KEY, source_name TEXT NOT NULL, camera TEXT NOT NULL,
 capture REAL NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL,
 duration REAL NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
 admitted REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pending', error TEXT,
 frame_cursor REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sightings (
 id TEXT PRIMARY KEY, clip_id TEXT NOT NULL, camera TEXT NOT NULL, capture REAL NOT NULL,
 created REAL NOT NULL, lane TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 vehicle_type TEXT, color TEXT, frames TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0,
 examined INTEGER NOT NULL DEFAULT 0, promising INTEGER NOT NULL DEFAULT 0,
 alert_versions TEXT NOT NULL, last_box TEXT, end_t REAL, error TEXT,
 expanded INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, alert_id TEXT NOT NULL, alert_version INTEGER NOT NULL,
 sighting_id TEXT NOT NULL, plate TEXT NOT NULL, camera TEXT NOT NULL,
 capture REAL NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
 strong INTEGER NOT NULL DEFAULT 0, observations TEXT NOT NULL DEFAULT '[]',
 evidence TEXT NOT NULL DEFAULT '[]', review TEXT NOT NULL DEFAULT 'new',
 saved INTEGER NOT NULL DEFAULT 0,
 UNIQUE(alert_id, alert_version, sighting_id)
);
CREATE TABLE IF NOT EXISTS metrics (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS service_health (name TEXT PRIMARY KEY, updated REAL NOT NULL, state TEXT NOT NULL, detail TEXT);
CREATE INDEX IF NOT EXISTS clip_queue ON clips(state, admitted);
CREATE INDEX IF NOT EXISTS sighting_queue ON sightings(state, lane, created);
CREATE INDEX IF NOT EXISTS event_order ON events(updated);
PRAGMA user_version=1;
"""


def connect():
    C.initialize()
    conn = sqlite3.connect(C.DB, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def init():
    with contextlib.closing(connect()) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)


@contextlib.contextmanager
def transaction():
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def query(sql, args=()):
    with contextlib.closing(connect()) as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def execute(sql, args=()):
    with transaction() as conn:
        return conn.execute(sql, args).rowcount


def uid():
    return uuid.uuid4().hex


def active(conn=None, now=None):
    args = (time.time() if now is None else now,)
    sql = "SELECT * FROM alerts WHERE active=1 AND expires>?"
    return [dict(r) for r in conn.execute(sql, args)] if conn else query(sql, args)


def metric(key, value):
    execute("INSERT INTO metrics VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated=excluded.updated", (key, json.dumps(value), time.time()))


def increment(key, n=1):
    with transaction() as conn:
        row = conn.execute("SELECT value FROM metrics WHERE key=?", (key,)).fetchone()
        value = json.loads(row[0]) if row else 0
        conn.execute("INSERT OR REPLACE INTO metrics VALUES (?,?,?)", (key, json.dumps(value+n), time.time()))


def health(name, state, detail=""):
    execute("INSERT OR REPLACE INTO service_health VALUES (?,?,?,?)", (name, time.time(), state, detail))

