import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from . import config as C, db
from .storage import cleanup

NAME = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})-(front|back|left_repeater|right_repeater|left_pillar|right_pillar)\.mp4$")
_clock_sample = None


def probe_metadata(path):
    result = subprocess.run(["ffprobe","-v","error","-select_streams","v:0","-show_entries",
        "stream=width,height:format=duration,size:format_tags=creation_time","-of","json",str(path)],capture_output=True,text=True,timeout=12,check=True)
    meta = json.loads(result.stdout)
    stream = meta["streams"][0]
    duration = float(meta["format"]["duration"])
    if duration <= .1 or duration > 180 or stream["width"] <= 0: raise ValueError("Incomplete or unexpected clip")
    creation=meta["format"].get("tags",{}).get("creation_time")
    timestamp=dt.datetime.fromisoformat(creation.replace("Z","+00:00")).timestamp() if creation else None
    return duration,stream["width"],stream["height"],timestamp


def probe(path):
    return probe_metadata(path)[:3]


@contextlib.contextmanager
def snapshot_lock():
    fd = os.open(C.SNAPSHOTS, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def scan():
    global _clock_sample
    now = time.time()
    alerts = db.active()
    if not alerts:
        db.health("ingest","idle","No active plate alerts; recordings are not copied")
        return
    alert_start = min(alert["created"] for alert in alerts)
    if shutil.disk_usage(C.ROOT).free < C.FREE_FLOOR:
        db.health("ingest","paused","Less than 20 GiB free; preserving Tesla recording capacity")
        return
    candidates = {}; discovered=[]
    # All directories are local TeslaUSB snapshot links, not the live drive.
    for path in C.SOURCE.rglob("*.mp4"):
        match = NAME.match(path.name)
        if not match: continue
        capture = dt.datetime.strptime(match[1],"%Y-%m-%d_%H-%M-%S").timestamp()
        try:
            size = path.stat().st_size
            if size == 0: continue
        except OSError: continue
        key = hashlib.sha256(f"{path.name}:{size}".encode()).hexdigest()[:32]
        discovered.append((key,path,capture,match[2],size))
    discovered.sort(key=lambda x:x[2],reverse=True)
    # Tesla names use the car's local clock. Embedded UTC remains authoritative
    # across timezone changes; calibrate cheap filename prefiltering from a new clip.
    offset=0.0
    for key,path,capture,camera,size in discovered[:12]:
        if _clock_sample and _clock_sample[0]==key:
            offset=_clock_sample[1];break
        try:
            stamp=probe_metadata(path)[3]
            offset=stamp-capture if stamp is not None else 0.0
            _clock_sample=(key,offset)
            break
        except (OSError,ValueError,subprocess.SubprocessError,KeyError,IndexError):continue
    for key,path,capture,camera,size in discovered:
        capture+=offset
        if now-capture>C.SPOOL_AGE or capture-now>120 or capture+180<alert_start:continue
        candidates[key]=(path,capture,camera,size)
    known = {r["id"] for r in db.query("SELECT id FROM clips")}
    # Bound each pass and favor fresh footage instead of backfilling the buffer.
    attempts = 0
    for key,(path,capture,camera,size) in sorted(candidates.items(),key=lambda x:x[1][1],reverse=True):
        if key in known: continue
        if attempts >= 6: break
        current = time.time()
        if current-capture>C.SPOOL_AGE or capture-current>120:continue
        if shutil.disk_usage(C.ROOT).free-size < C.FREE_FLOOR: break
        attempts += 1
        destination = C.SPOOL / (key+".mp4")
        partial = destination.with_suffix(".part")
        try:
            with snapshot_lock():
                # Resolve under the same lock TeslaUSB uses to delete snapshots.
                resolved = path.resolve(strict=True)
                if not str(resolved).startswith(("/tmp/snapshots/", str(C.SNAPSHOTS.resolve())+"/")):
                    raise ValueError("Recording is not in a TeslaUSB snapshot")
                # Reject clips ending before activation before paying the copy cost.
                duration,width,height,embedded_capture = probe_metadata(resolved)
                capture=embedded_capture if embedded_capture is not None else capture
                current=time.time()
                if current-capture>C.SPOOL_AGE or capture-current>120 or capture+duration<alert_start:
                    continue
                # One clip per lock. No decoding or inference while held.
                with open(resolved,"rb") as src, open(partial,"wb") as dest:
                    shutil.copyfileobj(src,dest,1024*1024)
                    dest.flush(); os.fsync(dest.fileno())
            if partial.stat().st_size != size: raise ValueError("Clip changed during discovery")
            duration,width,height,embedded_capture = probe_metadata(partial)
            capture=embedded_capture if embedded_capture is not None else capture
            current=time.time()
            if current-capture>C.SPOOL_AGE or capture-current>120 or capture+duration<alert_start:
                partial.unlink(missing_ok=True)
                continue
            os.replace(partial,destination)
            db.execute("""INSERT OR IGNORE INTO clips(id,source_name,camera,capture,path,size,duration,width,height,admitted)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",(key,path.name,camera,capture,str(destination),size,duration,width,height,time.time()))
            db.metric("latest_ingest_delay_seconds",max(0,time.time()-capture))
        except BlockingIOError:
            break  # TeslaUSB gets priority. Retry the next pass.
        except (OSError, ValueError, subprocess.SubprocessError, KeyError, IndexError):
            partial.unlink(missing_ok=True)
            db.increment("clip_retries")
        cleanup()
    db.health("ingest","ready",f"Discovered {len(candidates)} recent clip variants")


def run(once=False):
    db.init()
    while True:
        try:
            cleanup()
            scan()
        except Exception as exc:
            db.health("ingest","degraded",type(exc).__name__+": ingestion will retry")
        if once: return
        time.sleep(5)
