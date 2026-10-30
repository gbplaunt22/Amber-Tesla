import argparse
import fcntl
import json
import os
import statistics
import time
from pathlib import Path
from . import config as C, db


def benchmark(clip,frames):
    from .vision import VehicleDetector,make_alpr,frame_at,crop_vehicle
    import cv2,psutil
    detector=VehicleDetector();alpr=make_alpr();cap=cv2.VideoCapture(clip)
    duration=cap.get(cv2.CAP_PROP_FRAME_COUNT)/max(1,cap.get(cv2.CAP_PROP_FPS))
    decode=[];detect=[];recognize=[];full_alpr=[];vehicles=0;reads=0
    for i in range(frames):
        stamp=(i+.5)*duration/frames
        start=time.monotonic();image=frame_at(cap,stamp);decode.append(time.monotonic()-start)
        if image is None:continue
        start=time.monotonic();dets=detector.predict(image);detect.append(time.monotonic()-start)
        vehicles+=len(dets)
        for det in dets[:3]:
            start=time.monotonic();results=alpr.predict(crop_vehicle(image,det["box"]));recognize.append(time.monotonic()-start)
            reads+=sum(bool(r.ocr) for r in results)
        start=time.monotonic();alpr.predict(image);full_alpr.append(time.monotonic()-start)
    cap.release()
    def stats(values):
        return {"n":len(values),"mean_s":statistics.mean(values),"max_s":max(values)} if values else {"n":0}
    output={"clip":Path(clip).name,"clip_duration_s":duration,"decode":stats(decode),"vehicle_detector":stats(detect),
        "vehicle_crop_alpr":stats(recognize),"full_frame_alpr":stats(full_alpr),"vehicle_detections":vehicles,"plate_reads_count":reads,
        "rss_mib":psutil.Process().memory_info().rss/1024**2,
        "note":"Timing sample only. Plates are not logged. This does not measure recall or live end-to-end latency."}
    print(json.dumps(output,indent=2),flush=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("role",choices=["ingest","video","parser","web","init","benchmark"])
    parser.add_argument("--once",action="store_true")
    parser.add_argument("--clip")
    parser.add_argument("--frames",type=int,default=6)
    args=parser.parse_args()
    C.initialize();db.init()
    if args.role=="init":return
    if args.role=="benchmark":
        if not args.clip:parser.error("benchmark requires --clip")
        benchmark(args.clip,args.frames);return
    lock=open(C.DATA/(args.role+".lock"),"w")
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    if args.role=="web":
        import uvicorn
        uvicorn.run("amber.api:app",host="0.0.0.0",port=C.PORT,workers=1,access_log=False,proxy_headers=False)
    else:
        if args.role=="ingest":from .ingest import run
        elif args.role=="video":from .vision import run
        else:from .parser import run
        run(once=args.once)


if __name__=="__main__":main()
