"""Budgeted discovery and vehicle-crop ALPR. All model paths are explicit/local."""
import hashlib
import json
import os
import time
from pathlib import Path
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS","threads;1")
import cv2
import numpy as np
import onnxruntime as ort
ort.disable_telemetry_events()
from . import config as C, db
from .domain import FairScheduler, priority, add_observation

cv2.setNumThreads(1)
CLASSES = {2:"car",3:"motorcycle",5:"bus",7:"truck"}


def ort_options():
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.log_severity_level = 3
    return options


class VehicleDetector:
    def __init__(self):
        path = C.MODELS/"yolox_nano.onnx"
        if not path.is_file(): raise RuntimeError("Local vehicle model missing")
        self.session = ort.InferenceSession(str(path),providers=["CPUExecutionProvider"],sess_options=ort_options())
        self.input = self.session.get_inputs()[0].name
        self.size = int(self.session.get_inputs()[0].shape[2])
        grids=[];strides=[]
        for s in (8,16,32):
            h=w=self.size//s
            x,y=np.meshgrid(np.arange(w),np.arange(h))
            grids.append(np.stack((x,y),2).reshape(1,-1,2))
            strides.append(np.full((1,h*w,1),s))
        self.grid=np.concatenate(grids,1)
        self.strides=np.concatenate(strides,1)

    def predict(self,image):
        h,w=image.shape[:2]; ratio=min(self.size/h,self.size/w)
        resized=cv2.resize(image,(int(w*ratio),int(h*ratio)),interpolation=cv2.INTER_LINEAR)
        padded=np.full((self.size,self.size,3),114,dtype=np.uint8)
        padded[:resized.shape[0],:resized.shape[1]]=resized
        tensor=np.ascontiguousarray(padded.transpose(2,0,1)[None],dtype=np.float32)
        out=self.session.run(None,{self.input:tensor})[0].copy()
        if out.shape[1] != self.grid.shape[1] or out.shape[2] != 85: raise RuntimeError("Unexpected YOLOX model output")
        out[...,:2]=(out[...,:2]+self.grid)*self.strides
        out[...,2:4]=np.exp(out[...,2:4])*self.strides
        out=out[0]
        class_ids=np.argmax(out[:,5:],axis=1)
        scores=out[:,4]*out[np.arange(len(out)),class_ids+5]
        mask=np.isin(class_ids,list(CLASSES))&(scores>=.25)
        boxes=out[mask,:4]/ratio; scores=scores[mask]; class_ids=class_ids[mask]
        boxes[:,:2]-=boxes[:,2:]/2
        result=[]
        if len(boxes):
            keep=cv2.dnn.NMSBoxes(boxes.tolist(),scores.tolist(),.25,.45)
            for i in np.asarray(keep).reshape(-1):
                x,y,bw,bh=boxes[i]
                box=[max(0,int(x)),max(0,int(y)),min(w,int(x+bw)),min(h,int(y+bh))]
                if box[2]-box[0]<40 or box[3]-box[1]<25: continue
                result.append({"box":box,"type":CLASSES[int(class_ids[i])],"confidence":float(scores[i])})
        return result


def make_alpr():
    from fast_alpr import ALPR
    from open_image_models.detection.core.yolo_v9.inference import YoloV9Detector
    for filename in ("plate.onnx","ocr.onnx","ocr.yaml"):
        if not (C.MODELS/filename).is_file(): raise RuntimeError("Local ALPR asset missing: "+filename)
    detector=YoloV9Detector(C.MODELS/"plate.onnx",class_labels=["license_plate"],conf_thresh=.4,
                            providers=["CPUExecutionProvider"],sess_options=ort_options())
    return ALPR(detector=detector,ocr_model=None,ocr_device="cpu",ocr_providers=["CPUExecutionProvider"],
                ocr_model_path=C.MODELS/"ocr.onnx",ocr_config_path=C.MODELS/"ocr.yaml",ocr_sess_options=ort_options())


def iou(a,b):
    overlap=max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))
    area=(a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-overlap
    return overlap/max(1,area)


def crop_vehicle(image,box):
    x1,y1,x2,y2=box; dx=int((x2-x1)*.08);dy=int((y2-y1)*.08)
    return image[max(0,y1-dy):min(image.shape[0],y2+dy),max(0,x1-dx):min(image.shape[1],x2+dx)]


def color_of(crop):
    # A deliberately weak prioritizer. Glare/shadow/uncertainty never excludes OCR.
    h,w=crop.shape[:2]
    roi=crop[int(h*.35):int(h*.8),int(w*.15):int(w*.85)]
    if not roi.size: return "unknown"
    hsv=cv2.cvtColor(cv2.resize(roi,(64,64)),cv2.COLOR_BGR2HSV)
    hue,sat,value=np.median(hsv.reshape(-1,3),axis=0)
    if value<25: return "unknown"
    if value<65: return "black"
    if sat<35: return "white" if value>185 else "gray"
    if hue<10 or hue>170:return "red"
    if hue<25:return "brown" if value<155 else "orange"
    if hue<38:return "yellow"
    if hue<85:return "green"
    if hue<135:return "blue"
    return "unknown"


def frame_at(cap,t):
    if not cap.set(cv2.CAP_PROP_POS_MSEC,t*1000):return None
    ok,image=cap.read()
    return image if ok else None


def store_frame(clip_id,track_id,image,box,t,source_frame=None):
    crop=crop_vehicle(image,box)
    if crop.size==0: return None
    folder=C.CROPS/clip_id/track_id;folder.mkdir(parents=True,exist_ok=True)
    key=(f"f{source_frame:08d}" if source_frame is not None and source_frame>=0
         else hashlib.blake2b(image.tobytes(),digest_size=12).hexdigest())
    path=folder/(key+".jpg")
    gray=cv2.cvtColor(crop,cv2.COLOR_BGR2GRAY)
    sharp=float(cv2.Laplacian(gray,cv2.CV_64F).var())
    if not cv2.imwrite(str(path),crop,[cv2.IMWRITE_JPEG_QUALITY,95]): raise OSError("Could not write crop")
    return {"key":key,"t":round(t,3),"path":str(path),"box":box,"sharpness":sharp,"done":False}


def diverse_frames(frames,limit=12):
    selected=[]
    for frame in sorted(frames,key=lambda f:f["sharpness"],reverse=True):
        if all(abs(frame["t"]-s["t"])>=.2 for s in selected):selected.append(frame)
        if len(selected)>=limit:break
    return selected


def enforce_capacity(conn):
    count=conn.execute("SELECT COUNT(*) FROM sightings WHERE state IN ('pending','working','collecting')").fetchone()[0]
    excess=count-C.MAX_JOBS
    if excess>0:
        victims=conn.execute("""SELECT id FROM sightings WHERE state IN ('pending','collecting')
            ORDER BY CASE lane WHEN 'other' THEN 0 WHEN 'likely' THEN 1 ELSE 2 END, created LIMIT ?""",(excess,)).fetchall()
        for victim in victims:conn.execute("UPDATE sightings SET state='skipped',error='Queue capacity' WHERE id=?",(victim[0],))
        row=conn.execute("SELECT value FROM metrics WHERE key='skipped_sightings'").fetchone()
        n=int(json.loads(row[0])) if row else 0
        conn.execute("INSERT OR REPLACE INTO metrics VALUES ('skipped_sightings',?,?)",(str(n+len(victims)),time.time()))


def discover_step(clip,detector,budget=3,sampling_scale=1):
    started=time.monotonic();cap=cv2.VideoCapture(clip["path"])
    if not cap.isOpened(): raise OSError("Clip no longer readable")
    period=(1.0 if clip["camera"] in ("front","back") else 4.0)*sampling_scale
    t=float(clip["frame_cursor"])
    alerts=db.active();versions={a["id"]:a["version"] for a in alerts}
    try:
        while t<clip["duration"]-.1 and time.monotonic()-started<budget:
            decoded=time.monotonic();image=frame_at(cap,t)
            decode_s=time.monotonic()-decoded
            if image is None:
                db.increment("decode_failures");t+=period;continue
            inference=time.monotonic();detections=detector.predict(image);inference_s=time.monotonic()-inference
            tracks=db.query("SELECT * FROM sightings WHERE clip_id=? AND state='collecting'",(clip["id"],))
            used=set()
            for det in detections:
                candidates=[track for track in tracks if track["id"] not in used and track["vehicle_type"]==det["type"] and t-track["end_t"]<=2*period]
                matched=max(candidates,key=lambda tr:iou(json.loads(tr["last_box"]),det["box"]),default=None)
                if matched and iou(json.loads(matched["last_box"]),det["box"])<.15: matched=None
                key=matched["id"] if matched else hashlib.sha256(f"{clip['id']}:{t}:{det['box']}".encode()).hexdigest()[:32]
                used.add(key)
                crop=crop_vehicle(image,det["box"])
                color=color_of(crop)
                if matched and color=="unknown":color=matched["color"]
                lane=priority(det["type"],color,alerts)
                frame=store_frame(clip["id"],key,image,det["box"],t,int(round(cap.get(cv2.CAP_PROP_POS_FRAMES)))-1)
                if not frame:continue
                before=json.loads(matched["frames"]) if matched else []
                frames=diverse_frames(list({f["key"]:f for f in before+[frame]}.values()))
                kept_paths={f["path"] for f in frames}
                for discarded in before+[frame]:
                    if discarded["path"] not in kept_paths:Path(discarded["path"]).unlink(missing_ok=True)
                with db.transaction() as conn:
                    if matched:
                        conn.execute("UPDATE sightings SET frames=?,last_box=?,end_t=?,color=?,lane=? WHERE id=? AND state='collecting'",
                            (json.dumps(frames),json.dumps(det["box"]),t,color,lane,key))
                    else:
                        conn.execute("""INSERT OR IGNORE INTO sightings(id,clip_id,camera,capture,created,lane,state,vehicle_type,color,frames,alert_versions,last_box,end_t)
                            VALUES (?,?,?,?,?,?,'collecting',?,?,?,?,?,?)""",(key,clip["id"],clip["camera"],clip["capture"],time.time(),lane,det["type"],color,
                            json.dumps(frames),json.dumps(versions),json.dumps(det["box"]),t))
                    enforce_capacity(conn)
            with db.transaction() as conn:
                conn.execute("UPDATE sightings SET state='pending' WHERE clip_id=? AND state='collecting' AND end_t<?",(clip["id"],t-2*period))
                conn.execute("UPDATE clips SET frame_cursor=? WHERE id=?",(t+period,clip["id"]))
            db.metric("last_discovery_frame",{"decode_seconds":decode_s,"detector_seconds":inference_s,"vehicles":len(detections),"camera":clip["camera"]})
            t+=period
        if t>=clip["duration"]-.1:
            with db.transaction() as conn:
                conn.execute("UPDATE clips SET state='complete',frame_cursor=? WHERE id=?",(t,clip["id"]))
                conn.execute("UPDATE sightings SET state='pending' WHERE clip_id=? AND state='collecting'",(clip["id"],))
    finally:cap.release()


def expand_frames(sighting,frames,detector):
    clips=db.query("SELECT * FROM clips WHERE id=?",(sighting["clip_id"],))
    if not clips or not Path(clips[0]["path"]).exists():return frames
    clip=clips[0];cap=cv2.VideoCapture(clip["path"])
    try:
        # Localize again; don't reuse an old box at a different timestamp.
        for anchor in list(frames)[:3]:
            for offset in (-.25,.25,.5):
                if len(frames)>=12:return frames
                t=round(anchor["t"]+offset,3)
                if t<0 or t>=clip["duration"] or any(abs(f["t"]-t)<.2 for f in frames):continue
                image=frame_at(cap,t)
                if image is None:continue
                detections=[d for d in detector.predict(image) if d["type"]==sighting["vehicle_type"]]
                found=max(detections,key=lambda d:iou(anchor["box"],d["box"]),default=None)
                if not found or iou(anchor["box"],found["box"])<.2:continue
                frame=store_frame(clip["id"],sighting["id"],image,found["box"],t,int(round(cap.get(cv2.CAP_PROP_POS_FRAMES)))-1)
                if frame and not any(f["key"]==frame["key"] for f in frames):frames.append(frame)
        return frames
    finally:cap.release()


def ocr_step(sighting,alpr,detector):
    frames=json.loads(sighting["frames"])
    pending=[f for f in frames if not f.get("done")]
    expand=sighting["promising"] or sighting["lane"] in ("verify","likely")
    if (not pending or sighting["examined"]>=3) and expand and not sighting["expanded"] and len(frames)<12:
        frames=expand_frames(sighting,frames,detector)
        db.execute("UPDATE sightings SET frames=?,expanded=1 WHERE id=?",(json.dumps(frames),sighting["id"]))
        pending=[f for f in frames if not f.get("done")]
    if not pending or sighting["examined"]>=12 or (sighting["examined"]>=3 and not expand):
        db.execute("UPDATE sightings SET state='complete' WHERE id=?",(sighting["id"],));return
    frame=pending[0];image=cv2.imread(frame["path"])
    outcomes=[]
    if image is not None:
        results=alpr.predict(image)
        for result in results:
            if not result.ocr:continue
            confidence=result.ocr.confidence
            # Conservative score: every recognized character must clear threshold.
            score=float(min(confidence)) if isinstance(confidence,(list,tuple)) and confidence else float(confidence or 0)
            def evidence_factory(event_id,index):
                folder=C.EVIDENCE/event_id;folder.mkdir(parents=True,exist_ok=True)
                names=[]
                vehicle_name=f"{index:02d}-vehicle.jpg"
                if cv2.imwrite(str(folder/vehicle_name),image):names.append(vehicle_name)
                b=result.detection.bounding_box
                crop=image[max(0,b.y1):min(image.shape[0],b.y2),max(0,b.x1):min(image.shape[1],b.x2)]
                plate_name=f"{index:02d}-plate.jpg"
                if crop.size and cv2.imwrite(str(folder/plate_name),crop):names.append(plate_name)
                return names
            outcomes+=add_observation(sighting,result.ocr.text,score,frame,evidence_factory)
    frame["done"]=True
    promising=bool(sighting["promising"] or outcomes)
    lane="verify" if outcomes else sighting["lane"]
    state="complete" if "strong" in outcomes else "pending"
    db.execute("UPDATE sightings SET frames=?,examined=examined+1,promising=?,lane=?,state=? WHERE id=? AND state NOT IN ('expired','evicted','skipped')",
               (json.dumps(frames),int(promising),lane,state,sighting["id"]))
    if outcomes:
        db.metric("last_match_delay_seconds",max(0,time.time()-sighting["capture"]-frame["t"]))


def run(once=False):
    db.init()
    db.execute("UPDATE sightings SET state='pending' WHERE state='working'")
    db.execute("UPDATE clips SET state='pending' WHERE state='working'")
    detector=None;alpr=None;scheduler=FairScheduler();last_alerts=None
    discovery_virtual=0.;ocr_virtual=0.
    while True:
        try:
            alerts=db.active()
            if not alerts:
                db.health("video","idle","No active plate alerts")
                if once:return
                time.sleep(3);continue
            try:temp=int(Path("/sys/class/thermal/thermal_zone0/temp").read_text())/1000
            except OSError:temp=0
            if temp>=80:
                db.health("video","paused","CPU is hot; recording gets priority")
                time.sleep(5);continue
            if detector is None:
                db.health("video","loading","Loading local ONNX models")
                detector=VehicleDetector();alpr=make_alpr()
            versions={a["id"]:a["version"] for a in alerts}
            if versions != last_alerts:
                for row in db.query("SELECT * FROM sightings WHERE state='pending'"):
                    lane="verify" if row["promising"] else priority(row["vehicle_type"],row["color"],alerts)
                    db.execute("UPDATE sightings SET lane=?,alert_versions=? WHERE id=?",(lane,json.dumps(versions),row["id"]))
                last_alerts=versions
            clips=db.query("SELECT * FROM clips WHERE state='pending' ORDER BY admitted,camera LIMIT 1")
            queues=db.query("SELECT DISTINCT lane FROM sightings WHERE state='pending'")
            # Reserve 70% of video compute for discovery when both stages are busy.
            if not queues:ocr_virtual=discovery_virtual
            if not clips:discovery_virtual=ocr_virtual
            if clips and (discovery_virtual<=ocr_virtual or not queues):
                db.health("video","discovering",clips[0]["camera"])
                age=max(0,time.time()-clips[0]["capture"])
                scale=4 if age>180 else 2 if age>120 else 1
                db.metric("sampling",{"scale":scale,"front_back_fps":1/scale,"side_fps":.25/scale,
                                     "reason":"backlog pressure" if scale>1 else "baseline"})
                discovery_started=time.monotonic()
                try:discover_step(clips[0],detector,sampling_scale=scale)
                except (OSError,cv2.error):
                    db.execute("UPDATE clips SET state='failed',error='Unreadable during discovery' WHERE id=?",(clips[0]["id"],))
                    db.execute("UPDATE sightings SET state='pending' WHERE clip_id=? AND state='collecting'",(clips[0]["id"],))
                    db.increment("skipped_clips")
                discovery_virtual+=(time.monotonic()-discovery_started)/.7
            lane=scheduler.choose([q["lane"] for q in db.query("SELECT DISTINCT lane FROM sightings WHERE state='pending'")])
            if lane:
                with db.transaction() as conn:
                    row=conn.execute("SELECT * FROM sightings WHERE state='pending' AND lane=? ORDER BY created LIMIT 1",(lane,)).fetchone()
                    if row:conn.execute("UPDATE sightings SET state='working' WHERE id=?",(row["id"],))
                if row:
                    started=time.monotonic();db.health("video","recognizing",lane)
                    try:ocr_step(dict(row),alpr,detector)
                    except (OSError,cv2.error):
                        db.execute("UPDATE sightings SET state='expired',error='Crop expired' WHERE id=?",(row["id"],))
                    duration=time.monotonic()-started
                    ocr_virtual+=duration/.3
                    scheduler.charge(lane,duration);db.metric("last_ocr_job",{"lane":lane,"seconds":duration})
            elif not clips:
                db.health("video","ready","Waiting for new completed recordings")
                time.sleep(2)
        except Exception as exc:
            db.health("video","degraded",type(exc).__name__+": "+str(exc)[:180])
            # Transient failures must not leave claimed jobs stuck forever.
            db.execute("UPDATE sightings SET state='pending' WHERE state='working'")
            if once:raise
            time.sleep(5)
        if once:return
