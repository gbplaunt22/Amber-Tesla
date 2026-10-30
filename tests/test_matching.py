import json,time
from amber import db
from amber.domain import normalize_plate,distance_one,add_observation,FairScheduler

def create_alert(plate="AB0C8",expires=None):
    now=time.time();key=db.uid()
    db.execute("INSERT INTO alerts(id,plate,plate_raw,created,expires,expiry_default) VALUES (?,?,?,?,?,1)",(key,plate,plate,now,expires or now+100))
    return key

def observe(plate="AB0C8",score=.99,key="1"):
    return add_observation({"id":"track","camera":"front","capture":time.time()-60},plate,score,{"key":key,"t":float(key)},lambda *_:[])

def test_normalization_preserves_ambiguous_characters():
    assert normalize_plate(" ab-0.c 8 ")=="AB0C8"
    assert normalize_plate("ABOCB")!="AB0C8"
    assert distance_one("AB0C8","ABOC8")
    assert not distance_one("AB0C8","ABOCB")

def test_two_distinct_confident_frames_required():
    create_alert()
    assert observe()==["exact"]
    assert observe()==["exact"]
    assert observe(score=.7,key="2")==["exact"]
    assert observe(key="3")==["strong"]
    rows=db.query("SELECT * FROM events")
    assert len(rows)==1 and len(json.loads(rows[0]["observations"]))==3

def test_near_matches_remain_candidates_and_cannot_trigger_sound():
    create_alert()
    assert observe(plate="ABOC8")==["near"]
    assert observe(plate="ABOC8",key="2")==["near"]
    rows=db.query("SELECT * FROM events")
    assert len(rows)==1 and rows[0]["strong"]==0
    from amber.api import events_payload
    assert events_payload()[0]["match_kind"]=="near"
    assert not events_payload()[0]["notify"]
    assert observe(key="3")==["exact"]
    assert observe(key="4")==["strong"]
    assert events_payload()[0]["match_kind"]=="exact"

def test_cancelled_expired_and_changed_alert_revalidation():
    key=create_alert();observe()
    db.execute("UPDATE alerts SET active=0,version=version+1 WHERE id=?",(key,))
    assert observe(key="2")==[]
    db.execute("UPDATE alerts SET active=1,expires=? WHERE id=?",(time.time()-1,key))
    assert observe(key="3")==[]
    db.execute("UPDATE alerts SET expires=?,version=version+1 WHERE id=?",(time.time()+100,key))
    assert observe(key="4")==["exact"]
    assert all(r["strong"]==0 for r in db.query("SELECT * FROM events"))

def test_scheduler_allocates_time_and_reuses_idle_capacity():
    scheduler=FairScheduler();used={k:0 for k in ("verify","likely","other")}
    costs={"verify":.2,"likely":1.1,"other":.7}
    for _ in range(2000):
        lane=scheduler.choose(used);used[lane]+=costs[lane];scheduler.charge(lane,costs[lane])
    total=sum(used.values())
    for k,share in (("verify",.5),("likely",.3),("other",.2)):assert abs(used[k]/total-share)<.01
    assert scheduler.choose(["other"])=="other"
