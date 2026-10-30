import json
import shutil
import time
from pathlib import Path
from . import config as C, db


def remove_owned(path):
    path = Path(path)
    root = C.DATA.resolve()
    resolved = path.resolve()
    if root not in resolved.parents: raise ValueError("Refusing cleanup outside pipeline data")
    if path.is_dir(): shutil.rmtree(path)
    else: path.unlink(missing_ok=True)


def expire_clip(clip_id, state="expired"):
    rows = db.query("SELECT path FROM clips WHERE id=?",(clip_id,))
    db.execute("UPDATE clips SET state=? WHERE id=?",(state,clip_id))
    db.execute("UPDATE sightings SET state=? WHERE clip_id=? AND state IN ('collecting','pending','working')",(state,clip_id))
    if rows: remove_owned(rows[0]["path"])
    remove_owned(C.CROPS / clip_id)


def tree_size(path):
    total = 0
    for file in path.rglob("*"):
        try:
            if file.is_file(): total += file.stat().st_size
        except FileNotFoundError: pass
    return total


def cleanup(now=None):
    now = time.time() if now is None else now
    clips = db.query("SELECT * FROM clips WHERE state NOT IN ('expired','evicted') ORDER BY admitted")
    total = tree_size(C.SPOOL)+tree_size(C.CROPS)
    for clip in clips:
        if now-clip["admitted"] > C.SPOOL_AGE or total > C.SPOOL_BYTES:
            before = clip["size"] + tree_size(C.CROPS / clip["id"])
            if clip["state"] in ("pending","working"):
                db.increment("skipped_clips")
            expire_clip(clip["id"],"expired" if now-clip["admitted"]>C.SPOOL_AGE else "evicted")
            total -= before
    # Manual bookmarks cannot bypass the age or byte retention limits.
    events = db.query("SELECT * FROM events ORDER BY saved,created")
    total = tree_size(C.EVIDENCE)
    for event in events:
        if now-event["created"] > C.EVIDENCE_AGE or total > C.EVIDENCE_BYTES:
            folder = C.EVIDENCE / event["id"]
            total -= tree_size(folder)
            remove_owned(folder)
            db.execute("DELETE FROM events WHERE id=?",(event["id"],))
    # Remove interrupted writes/orphan files after grace period.
    for file in C.SPOOL.glob("*.part"):
        if now-file.stat().st_mtime > 300: remove_owned(file)
    # A crash between rename and the DB commit must not leave footage untracked.
    live_clips={r['id'] for r in db.query("SELECT id FROM clips WHERE state NOT IN ('expired','evicted')")}
    for file in C.SPOOL.glob('*.mp4'):
        if file.stem not in live_clips and now-file.stat().st_mtime>300:remove_owned(file)
    for folder in C.CROPS.iterdir():
        if folder.name not in live_clips and now-folder.stat().st_mtime>300:remove_owned(folder)
    known = {r["id"] for r in db.query("SELECT id FROM events")}
    for folder in C.EVIDENCE.iterdir():
        if folder.name not in known and now-folder.stat().st_mtime > 300: remove_owned(folder)
    # Keep dedup tombstones for two days, not indefinitely.
    db.execute("DELETE FROM sightings WHERE state NOT IN ('pending','working') AND created<? AND id NOT IN (SELECT sighting_id FROM events)",(now-2*86400,))
    db.execute("DELETE FROM clips WHERE state IN ('expired','evicted','failed') AND admitted<?",(now-2*86400,))
    db.execute("DELETE FROM drafts WHERE created<? AND id NOT IN (SELECT draft_id FROM alerts WHERE active=1 AND expires>?)",(now-C.EVIDENCE_AGE,now))
    db.metric("storage", {"spool_bytes":max(0,tree_size(C.SPOOL)+tree_size(C.CROPS)),"evidence_bytes":tree_size(C.EVIDENCE),"free_bytes":shutil.disk_usage(C.ROOT).free})

