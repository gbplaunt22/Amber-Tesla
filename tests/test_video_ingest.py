import datetime as dt,json,time
import pytest
cv2=pytest.importorskip("cv2")
np=pytest.importorskip("numpy")
from amber import db,config as C
from amber.vision import enforce_capacity,run,iou,diverse_frames
from amber.ingest import scan

def active():
    now=time.time()
    db.execute("INSERT INTO alerts(id,plate,plate_raw,created,expires,expiry_default) VALUES ('a','TEST01','TEST01',?,?,1)",(now,now+100))

def sighting(key,lane,created,state='pending'):
    db.execute("INSERT INTO sightings(id,clip_id,camera,capture,created,lane,state,frames,alert_versions) VALUES (?,'clip','front',?,?,?,?,'[]','{}')",(key,created,created,lane,state))

def test_capacity_evicts_oldest_low_priority(monkeypatch):
    monkeypatch.setattr(C,"MAX_JOBS",2)
    sighting("verify","verify",1);sighting("old","other",2);sighting("new","other",3)
    with db.transaction() as conn:enforce_capacity(conn)
    rows={r["id"]:r["state"] for r in db.query("SELECT id,state FROM sightings")}
    assert rows=={"verify":"pending","old":"skipped","new":"pending"}

def test_crash_recovery_returns_working_job_to_queue():
    sighting("recover","likely",1,"working")
    run(once=True) # No active alerts, so no inference/model downloads.
    assert db.query("SELECT state FROM sightings")[0]["state"]=="pending"

def test_frame_selection_is_temporally_distinct_and_bounded():
    frames=[{"t":i*.01,"sharpness":i} for i in range(100)]
    selected=diverse_frames(frames,3)
    assert len(selected)==3
    assert all(abs(a["t"]-b["t"])>=.2 for i,a in enumerate(selected) for b in selected[i+1:])
    assert iou([0,0,10,10],[20,20,30,30])==0

def test_ingestion_skips_incomplete_and_deduplicates_snapshots(tmp_path,monkeypatch):
    source=tmp_path/"source";source.mkdir()
    snapshots=tmp_path/"snapshots";snapshots.mkdir()
    monkeypatch.setattr(C,"SOURCE",source);monkeypatch.setattr(C,"SNAPSHOTS",snapshots);monkeypatch.setattr(C,"FREE_FLOOR",0)
    active()
    name=dt.datetime.fromtimestamp(time.time()-5).strftime("%Y-%m-%d_%H-%M-%S")+"-front.mp4"
    clip=snapshots/name;clip.write_bytes(b"incomplete recording")
    (source/name).symlink_to(clip)
    scan();assert not db.query("SELECT * FROM clips")
    writer=cv2.VideoWriter(str(clip),cv2.VideoWriter_fourcc(*"mp4v"),10,(160,120))
    for _ in range(10):writer.write(np.zeros((120,160,3),dtype=np.uint8))
    writer.release()
    scan();scan()
    rows=db.query("SELECT * FROM clips")
    assert len(rows)==1 and rows[0]["state"]=="pending"
    # A second link to the identical recording must not create another job.
    (source/"saved").mkdir();(source/"saved"/name).symlink_to(clip)
    scan();assert len(db.query("SELECT * FROM clips"))==1


def test_embedded_utc_handles_different_car_timezone(tmp_path,monkeypatch):
    from amber import ingest
    source=tmp_path/"source";source.mkdir()
    snapshots=tmp_path/"snapshots";snapshots.mkdir()
    monkeypatch.setattr(C,"SOURCE",source);monkeypatch.setattr(C,"SNAPSHOTS",snapshots);monkeypatch.setattr(C,"FREE_FLOOR",0)
    monkeypatch.setattr(ingest,"_clock_sample",None)
    active();capture=time.time()-5
    name=dt.datetime.fromtimestamp(capture+5*3600).strftime("%Y-%m-%d_%H-%M-%S")+"-back.mp4"
    clip=snapshots/name;clip.write_bytes(b"fake completed clip")
    (source/name).symlink_to(clip)
    monkeypatch.setattr(ingest,"probe_metadata",lambda path:(1,160,120,capture))
    scan()
    rows=db.query("SELECT * FROM clips")
    assert len(rows)==1 and abs(rows[0]["capture"]-capture)<.001


def test_actual_frame_identity_is_used_for_confirmation():
    from amber.vision import store_frame
    image=np.zeros((100,100,3),dtype=np.uint8)
    first=store_frame('clip','track',image,[0,0,100,100],1.0,36)
    repeated=store_frame('clip','track',image,[0,0,100,100],1.01,36)
    other=store_frame('clip','track',image,[0,0,100,100],1.1,40)
    assert first['key']==repeated['key'] and first['key']!=other['key']
