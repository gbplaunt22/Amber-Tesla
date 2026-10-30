import time,json
from amber import db

def body(plate="ABCDEF"):
    return {"vehicles":[{"plate":plate,"color":"black","make":"Toyota","model":"Corolla"}],"reviewed":True}

def test_authentication_and_csrf(client):
    assert client.get("/health").status_code==200
    assert client.post("/alerts",json=body(),headers={"X-Amber-Request":""}).status_code==403
    assert client.post("/alerts",json=body(),headers={"Origin":"https://external.example"}).status_code==403
    client.post("/logout")
    assert client.get("/alerts").status_code==401
    assert client.get("/events").status_code==401

def test_manual_alert_lifecycle(client):
    assert client.post("/alerts",json={**body(),"reviewed":False}).status_code==400
    assert client.post("/alerts",json=body("AB??")).status_code==400
    response=client.post("/alerts",json=body());assert response.status_code==201
    result=response.json();key=result["ids"][0]
    assert result["expiry_default"] and abs(result["expires"]-time.time()-86400)<2
    assert client.put("/alerts/"+key,json=body("NEW123")).status_code==200
    assert client.get("/alerts").json()[0]["version"]==2
    assert client.post("/alerts/"+key+"/cancel",json={}).status_code==200
    assert not db.active()

def test_draft_is_never_autoactivated(client):
    result=client.post("/alert-drafts",json={"text":"Active alert: black Corolla, plate ABCDEF."})
    assert result.status_code==202 and db.active()==[]
    key=result.json()["id"]
    assert client.get("/alert-drafts/"+key).json()["status"]=="pending"
    assert client.post("/alert-drafts",json={"text":"x"*9001}).status_code==422

def test_draft_activation_is_idempotent(client):
    key=client.post("/alert-drafts",json={"text":"Black Corolla, plate ABCDEF."}).json()["id"]
    request={**body(),"draft_id":key}
    assert client.post("/alerts",json=request).status_code==201
    assert client.post("/alerts",json=request).status_code==409
    assert len(db.active())==1

def test_cancellation_requires_valid_selection(client):
    alert=client.post("/alerts",json=body()).json()["ids"][0]
    key=client.post("/alert-drafts",json={"text":"AMBER alert ABCDEF is cancelled."}).json()["id"]
    db.execute("UPDATE drafts SET result=?,status='ready' WHERE id=?",(json.dumps({"status":"cancelled"}),key))
    assert client.post("/alerts",json={**body(),"draft_id":key}).status_code==400
    assert client.post("/alerts/not-an-alert/cancel",json={"draft_id":key}).status_code==404
    assert client.get("/alert-drafts/"+key).json()["reviewed"]==0
    assert client.post("/alerts/"+alert+"/cancel",json={"draft_id":key}).status_code==200
    assert not db.active()

def test_old_event_does_not_notify_after_edit(client):
    from amber.domain import add_observation
    key=client.post("/alerts",json=body()).json()["ids"][0]
    for i in (1,2):add_observation({"id":"t","camera":"front","capture":time.time()},"ABCDEF",.99,{"key":str(i),"t":i},lambda *_:[])
    assert client.get("/events").json()[0]["notify"]
    client.put("/alerts/"+key,json=body())
    assert not client.get("/events").json()[0]["notify"]


def test_health_reports_capture_age_before_queue_creation_age(client):
    now=time.time()
    empty=client.get("/health").json()
    assert empty["oldest_clip_capture"] is None and empty["oldest_footage_capture"] is None
    db.execute("INSERT INTO clips(id,source_name,camera,capture,path,size,duration,width,height,admitted) VALUES ('old','test.mp4','front',?,'unused',1,60,160,120,?)",(now-540,now-5))
    db.execute("INSERT INTO sightings(id,clip_id,camera,capture,created,lane,state,frames,alert_versions) VALUES ('s','old','front',?,?,'other','collecting','[]','{}')",(now-600,now-2))
    health=client.get("/health").json()
    assert health["clips_pending"]==1
    assert health["oldest_clip_capture"]==now-540
    assert health["oldest_footage_capture"]==now-600
    assert health["queue"][0]["oldest"]==now-2
    db.execute("UPDATE clips SET state='complete'")
    db.execute("UPDATE sightings SET state='complete'")
    assert client.get("/health").json()["oldest_footage_capture"] is None
