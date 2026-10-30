import contextlib
import datetime as dt
import time

from amber import config as C, db, ingest


def setup_clips(tmp_path, monkeypatch, ages, alert_age=30, duration=60):
    now = time.time()
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    monkeypatch.setattr(C, "SOURCE", snapshots)
    monkeypatch.setattr(C, "SNAPSHOTS", snapshots)
    monkeypatch.setattr(C, "FREE_FLOOR", 0)
    monkeypatch.setattr(ingest, "_clock_sample", None)
    monkeypatch.setattr(ingest, "snapshot_lock", contextlib.nullcontext)
    monkeypatch.setattr(ingest, "cleanup", lambda: None)
    db.execute("INSERT INTO alerts(id,plate,plate_raw,created,expires,expiry_default) VALUES ('a','TEST01','TEST01',?,?,1)",
               (now-alert_age, now+3600))
    for age in ages:
        name = dt.datetime.fromtimestamp(now-age).strftime("%Y-%m-%d_%H-%M-%S") + "-front.mp4"
        (snapshots/name).write_bytes(b"completed recording")
    monkeypatch.setattr(ingest, "probe_metadata", lambda path: (duration, 160, 120, None))
    return now


def test_rejects_pre_activation_clip_before_copy(tmp_path, monkeypatch):
    setup_clips(tmp_path, monkeypatch, [120], alert_age=30, duration=60)
    monkeypatch.setattr(ingest.shutil, "copyfileobj", lambda *a: (_ for _ in ()).throw(AssertionError("obsolete clip copied")))
    ingest.scan()
    assert not db.query("SELECT * FROM clips")
    assert not list(C.SPOOL.glob("*.part"))


def test_preserves_clip_overlapping_alert_activation(tmp_path, monkeypatch):
    setup_clips(tmp_path, monkeypatch, [40], alert_age=30, duration=60)
    ingest.scan()
    assert len(db.query("SELECT * FROM clips")) == 1


def test_scan_admits_at_most_six_newest_then_rescans(tmp_path, monkeypatch):
    setup_clips(tmp_path, monkeypatch, list(range(10, 90, 10)), alert_age=300)
    ingest.scan()
    first = db.query("SELECT * FROM clips ORDER BY admitted")
    assert len(first) == 6
    assert first[0]["capture"] > first[-1]["capture"]
    ingest.scan()
    assert len(db.query("SELECT * FROM clips")) == 8


def test_copy_completion_rechecks_current_time(tmp_path, monkeypatch):
    now = setup_clips(tmp_path, monkeypatch, [10], alert_age=30)
    clock = [now]
    monkeypatch.setattr(ingest.time, "time", lambda: clock[0])
    original = ingest.shutil.copyfileobj
    def slow_copy(*args):
        original(*args)
        clock[0] += C.SPOOL_AGE + 1
    monkeypatch.setattr(ingest.shutil, "copyfileobj", slow_copy)
    ingest.scan()
    assert not db.query("SELECT * FROM clips")
    assert not list(C.SPOOL.glob("*.part"))
