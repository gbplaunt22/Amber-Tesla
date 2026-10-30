import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from . import config as C, db
from .domain import valid_plate, normalize_plate
from .storage import remove_owned

STATIC = Path(__file__).parent / "static"


def auth_config():
    return json.loads(C.AUTH_FILE.read_text())


def token(user):
    payload = base64.urlsafe_b64encode(json.dumps({"user":user,"expires":time.time()+12*3600}).encode()).decode()
    return payload+"."+hmac.new(bytes.fromhex(auth_config()["secret"]),payload.encode(),hashlib.sha256).hexdigest()


def logged_in(request):
    try:
        payload,signature = request.cookies.get("amber_session","").split(".")
        expected = hmac.new(bytes.fromhex(auth_config()["secret"]),payload.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected,signature): return False
        contents = json.loads(base64.urlsafe_b64decode(payload))
        return contents["expires"] > time.time() and contents["user"] == auth_config()["username"]
    except (ValueError,KeyError,TypeError,OSError): return False


@asynccontextmanager
async def lifespan(app):
    db.init()
    yield


app = FastAPI(title="Amber Local", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
failures = {}


@app.middleware("http")
async def guard(request, call_next):
    # Validate the actual local socket address, not a client-supplied Host header.
    server = request.scope.get("server")
    if server and server[0] not in (C.AP_IP,"127.0.0.1","::1","testserver"):
        return Response("Use the Pi's local access point",status_code=403)
    if request.method not in ("GET","HEAD"):
        if request.headers.get("x-amber-request") != "1":
            return Response("Missing request header",status_code=403)
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return Response("Origin mismatch",status_code=403)
        try: length = int(request.headers.get("content-length","0"))
        except ValueError: return Response("Invalid content length",status_code=400)
        if length > 32768:
            return Response("Request too large",status_code=413)
    public = request.url.path in ("/","/app.js","/style.css","/login")
    if not public and not logged_in(request): return Response("Sign in required",status_code=401)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    return response


@app.get("/")
def index(): return FileResponse(STATIC/"index.html")


@app.get("/app.js")
def javascript(): return FileResponse(STATIC/"app.js",media_type="text/javascript")


@app.get("/style.css")
def stylesheet(): return FileResponse(STATIC/"style.css",media_type="text/css")


class Login(BaseModel):
    username: str = Field(max_length=80)
    password: str = Field(max_length=200)


@app.post("/login")
def login(body: Login, request: Request, response: Response):
    peer = request.client.host
    now = time.time()
    failures[peer] = [x for x in failures.get(peer,[]) if now-x < 60]
    if len(failures[peer]) >= 8: raise HTTPException(429,"Wait one minute before trying again")
    config = auth_config()
    digest = hashlib.pbkdf2_hmac("sha256",body.password.encode(),bytes.fromhex(config["salt"]),200000).hex()
    if not (hmac.compare_digest(body.username,config["username"]) and hmac.compare_digest(digest,config["password_hash"])):
        failures[peer].append(now)
        raise HTTPException(401,"Incorrect username or password")
    response.set_cookie("amber_session",token(body.username),httponly=True,samesite="strict",max_age=43200)
    return {"ok":True}


@app.post("/logout")
def logout(response:Response):
    response.delete_cookie("amber_session")
    return {"ok":True}


class DraftInput(BaseModel):
    text: str = Field(min_length=8,max_length=9000)


@app.post("/alert-drafts",status_code=202)
def draft_create(body:DraftInput):
    key = db.uid(); now = time.time()
    with db.transaction() as conn:
        if conn.execute("SELECT COUNT(*) FROM drafts WHERE status IN ('pending','processing')").fetchone()[0]>=10:
            raise HTTPException(429,"Parser queue is full. Use manual entry or wait.")
        conn.execute("INSERT INTO drafts(id,text,created,updated) VALUES (?,?,?,?)",(key,body.text,now,now))
    return {"id":key,"status":"pending"}


def decode_draft(row):
    row["result"] = json.loads(row["result"]) if row["result"] else None
    row["issues"] = json.loads(row["issues"])
    return row


@app.get("/alert-drafts")
def drafts(): return [decode_draft(r) for r in db.query("SELECT * FROM drafts ORDER BY created DESC LIMIT 20")]


@app.get("/alert-drafts/{key}")
def draft_get(key:str):
    rows = db.query("SELECT * FROM drafts WHERE id=?",(key,))
    if not rows: raise HTTPException(404,"Draft not found")
    return decode_draft(rows[0])


class Vehicle(BaseModel):
    plate: str = Field(min_length=2,max_length=24)
    jurisdiction: str | None = Field(default=None,max_length=80)
    color: str | None = Field(default=None,max_length=40)
    vehicle_type: str | None = Field(default=None,max_length=40)
    make: str | None = Field(default=None,max_length=80)
    model: str | None = Field(default=None,max_length=80)
    year: str | None = Field(default=None,max_length=20)


class AlertInput(BaseModel):
    vehicles: list[Vehicle] = Field(min_length=1,max_length=4)
    reference: str | None = Field(default=None,max_length=160)
    source: str | None = Field(default=None,max_length=9000)
    draft_id: str | None = None
    expires: datetime | None = None
    reviewed: bool = False


def validate_alert(body):
    if not body.reviewed: raise HTTPException(400,"Review the fields before activation")
    if any(not valid_plate(v.plate) for v in body.vehicles): raise HTTPException(400,"Complete alphanumeric plate required; partial plates are unsupported")
    if body.expires and body.expires.tzinfo is None: raise HTTPException(400,"Expiry must include a timezone")
    expires = body.expires.timestamp() if body.expires else time.time()+86400
    if expires <= time.time(): raise HTTPException(400,"Expiry is already in the past")
    return expires


@app.post("/alerts",status_code=201)
def create_alert(body:AlertInput):
    expires = validate_alert(body)
    ids = []
    with db.transaction() as conn:
        if body.draft_id:
            row = conn.execute("SELECT * FROM drafts WHERE id=?",(body.draft_id,)).fetchone()
            if not row: raise HTTPException(404,"Draft not found")
            if row["reviewed"]: raise HTTPException(409,"Draft already applied; edit the existing alert")
            parsed = json.loads(row["result"]) if row["result"] else {}
            if parsed.get("status") == "cancelled": raise HTTPException(400,"Use cancellation action for cancellation drafts")
            conn.execute("UPDATE drafts SET reviewed=1,updated=? WHERE id=?",(time.time(),body.draft_id))
        for vehicle in body.vehicles:
            key = db.uid(); ids.append(key)
            values = vehicle.model_dump()
            conn.execute("""INSERT INTO alerts(id,reference,plate,plate_raw,jurisdiction,color,vehicle_type,make,model,year,source,draft_id,created,expires,expiry_default)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(key,body.reference,normalize_plate(vehicle.plate),vehicle.plate,
                values["jurisdiction"],values["color"],values["vehicle_type"],values["make"],values["model"],values["year"],
                body.source,body.draft_id,time.time(),expires,int(body.expires is None)))
    return {"ids":ids,"expires":expires,"expiry_default":body.expires is None}


@app.get("/alerts")
def alerts(): return db.query("SELECT * FROM alerts ORDER BY created DESC LIMIT 200")


@app.put("/alerts/{key}")
def update_alert(key:str,body:AlertInput):
    expires = validate_alert(body)
    if len(body.vehicles) != 1: raise HTTPException(400,"Edit one vehicle at a time")
    v = body.vehicles[0]
    count = db.execute("""UPDATE alerts SET reference=?,plate=?,plate_raw=?,jurisdiction=?,color=?,vehicle_type=?,make=?,model=?,year=?,source=?,
        expires=?,expiry_default=?,version=version+1,active=1 WHERE id=?""",(body.reference,normalize_plate(v.plate),v.plate,v.jurisdiction,v.color,
        v.vehicle_type,v.make,v.model,v.year,body.source,expires,int(body.expires is None),key))
    if not count: raise HTTPException(404,"Alert not found")
    return {"ok":True}


class CancelInput(BaseModel):
    draft_id: str | None = None


@app.post("/alerts/{key}/cancel")
def cancel_alert(key:str,body:CancelInput):
    with db.transaction() as conn:
        if body.draft_id:
            row = conn.execute("SELECT * FROM drafts WHERE id=?",(body.draft_id,)).fetchone()
            if not row: raise HTTPException(404,"Draft not found")
            if row["reviewed"]: raise HTTPException(409,"Draft already applied")
            if not row["result"] or json.loads(row["result"])["status"] != "cancelled": raise HTTPException(400,"Not a cancellation draft")
            conn.execute("UPDATE drafts SET reviewed=1 WHERE id=?",(body.draft_id,))
        count = conn.execute("UPDATE alerts SET active=0,version=version+1 WHERE id=?",(key,)).rowcount
        if not count: raise HTTPException(404,"Alert not found")
    return {"ok":True}


def events_payload():
    rows = db.query("""SELECT e.*,a.active AS alert_active,a.expires AS alert_expires,a.version AS current_version
        FROM events e LEFT JOIN alerts a ON a.id=e.alert_id ORDER BY e.updated DESC LIMIT 100""")
    for row in rows:
        for key in ("observations","evidence"): row[key] = json.loads(row[key])
        row["match_kind"] = "exact" if any(normalize_plate(o["text"]) == row["plate"] for o in row["observations"]) else "near"
        row["notify"] = bool(row["strong"] and row["alert_active"] and row["alert_expires"]>time.time() and row["current_version"]==row["alert_version"] and row["review"]=="new")
    return rows


@app.get("/events")
def events(): return events_payload()


class ReviewInput(BaseModel):
    review: Literal["new","reviewed","dismissed"] = "reviewed"
    saved: bool = False


@app.patch("/events/{key}")
def review_event(key:str,body:ReviewInput):
    if not db.execute("UPDATE events SET review=?,saved=? WHERE id=?",(body.review,int(body.saved),key)): raise HTTPException(404,"Event not found")
    return {"ok":True}


@app.delete("/events/{key}")
def delete_event(key:str):
    if not db.execute("DELETE FROM events WHERE id=?",(key,)): raise HTTPException(404,"Event not found")
    if not key.isalnum(): raise HTTPException(400,"Invalid event")
    remove_owned(C.EVIDENCE/key)
    return {"ok":True}


@app.get("/evidence/{key}/{filename}")
def evidence(key:str,filename:str):
    rows = db.query("SELECT evidence FROM events WHERE id=?",(key,))
    if not rows or filename not in json.loads(rows[0]["evidence"]): raise HTTPException(404,"Evidence not found")
    path = C.EVIDENCE/key/filename
    if not path.is_file() or C.EVIDENCE.resolve() not in path.resolve().parents: raise HTTPException(404,"Evidence expired")
    return FileResponse(path,media_type="image/jpeg")


@app.get("/health")
def health():
    temp = None; attached = "unknown"
    try: temp = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text())/1000
    except OSError: pass
    try: attached = next(Path("/sys/class/udc").glob("*/state")).read_text().strip()
    except (OSError,StopIteration): pass
    q = db.query("SELECT lane,COUNT(*) AS count,MIN(created) AS oldest,MIN(capture) AS oldest_capture FROM sightings WHERE state IN ('collecting','pending','working') GROUP BY lane")
    clips = db.query("SELECT COUNT(*) AS n,MIN(capture) AS oldest_capture FROM clips WHERE state IN ('pending','working')")[0]
    captures = [row["oldest_capture"] for row in [clips]+q if row["oldest_capture"] is not None]
    return {"time":time.time(),"temperature_c":temp,"usb_state":attached,
            "free_bytes":shutil.disk_usage(C.ROOT).free,"services":db.query("SELECT * FROM service_health"),
            "queue":q,"clips_pending":clips["n"],"oldest_clip_capture":clips["oldest_capture"],
            "oldest_footage_capture":min(captures) if captures else None,
            "metrics":{r["key"]:json.loads(r["value"]) for r in db.query("SELECT * FROM metrics")},
            "models":{p.name:p.exists() for p in (C.GEMMA,C.MODELS/"yolox_nano.onnx",C.MODELS/"plate.onnx",C.MODELS/"ocr.onnx")}}


@app.get("/stream")
async def stream(request:Request):
    async def generate():
        last = None
        while not await request.is_disconnected():
            if not logged_in(request): return
            payload = json.dumps(events_payload(),sort_keys=True)
            if payload != last:
                yield "event: events\ndata: "+payload+"\n\n"
                last = payload
            else: yield ": heartbeat\n\n"
            await asyncio.sleep(3)
    return StreamingResponse(generate(),media_type="text/event-stream")
