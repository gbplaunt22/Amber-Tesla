"""Exercise the deployed service using synthetic held-out examples, without activation."""
import argparse,json,pathlib,time,urllib.request,http.cookiejar

p=argparse.ArgumentParser();p.add_argument("--url",default="http://127.0.0.1:8088");p.add_argument("--credentials",type=pathlib.Path,required=True);p.add_argument("--output",type=pathlib.Path,required=True);p.add_argument("--cases",type=pathlib.Path);a=p.parse_args()
credentials=json.loads(a.credentials.read_text());jar=http.cookiejar.CookieJar()
opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar),urllib.request.ProxyHandler({}))
def call(path,data=None):
    request=urllib.request.Request(a.url+path,data=json.dumps(data).encode() if data is not None else None,headers={"Content-Type":"application/json","X-Amber-Request":"1"})
    return json.load(opener.open(request,timeout=10))
call("/login",{"username":credentials["dashboard_username"],"password":credentials["dashboard_password"]})
cases=json.loads((a.cases or pathlib.Path(__file__).resolve().parents[1]/"tests/parser_cases.json").read_text())
results=[]
for case in cases:
    start=time.monotonic();draft=call("/alert-drafts",{"text":case["text"]});key=draft["id"]
    deadline=start+120
    while time.monotonic()<deadline:
        draft=call("/alert-drafts/"+key)
        if draft["status"] in ("ready","error"):break
        time.sleep(1)
    actual=draft.get("result") or {};vehicles=actual.get("vehicles",[])
    expected=case["expected"]
    exact=len(vehicles)==len(expected) and all(all(v.get(k)==value for k,value in e.items()) for v,e in zip(vehicles,expected))
    if "expected_status" in case:exact=exact and actual.get("status")==case["expected_status"]
    row={"name":case["name"],"draft_id":key,"seconds":time.monotonic()-start,"status":draft["status"],"exact_fields":exact,"result":actual,"issues":draft.get("issues"),"error":draft.get("error")}
    results.append(row);a.output.write_text(json.dumps(results,indent=2))
    print(case["name"],draft["status"],"exact_fields",exact,"seconds",round(row["seconds"],1),flush=True)
print("No alerts activated. Evaluation drafts may be removed by their recorded IDs.")
