import json
import re
import time
from . import db

FIELDS = ("plate", "jurisdiction", "color", "vehicle_type", "make", "model", "year")
LANE_SHARES = {"verify": .5, "likely": .3, "other": .2}


def normalize_plate(value):
    """Only case and separators change; never merge O/0 or B/8."""
    return re.sub(r"[\s\-·.]", "", value.upper())


def valid_plate(value):
    return bool(re.fullmatch(r"[A-Z0-9]{2,12}", normalize_plate(value)))


def distance_one(a, b):
    if abs(len(a)-len(b)) > 1: return False
    if len(a) == len(b): return sum(x != y for x, y in zip(a,b)) == 1
    if len(a) > len(b): a,b = b,a
    return any(a == b[:i] + b[i+1:] for i in range(len(b)))


def priority(vehicle_type, color, alerts):
    for a in alerts:
        vt = (a.get("vehicle_type") or "").lower()
        co = (a.get("color") or "").lower()
        # No make/model classification is implied by this broad detector.
        vt = {"sedan":"car", "coupe":"car", "suv":"car", "pickup":"truck"}.get(vt,vt)
        if (not vt or vt == vehicle_type) and (not co or co == color):
            return "likely"
    return "other"


class FairScheduler:
    """Weighted service TIME, not job count; unused lanes don't accumulate debt."""
    def __init__(self):
        self.virtual = dict.fromkeys(LANE_SHARES, 0.0)
        self.present = set()

    def choose(self, available):
        available = set(available)
        if not available: return None
        floor = min((self.virtual[k] for k in self.present & available), default=min(self.virtual.values()))
        for key in available - self.present: self.virtual[key] = floor
        self.present = available
        return min(available, key=lambda k:(self.virtual[k], -LANE_SHARES[k]))

    def charge(self, lane, seconds):
        self.virtual[lane] += max(seconds, .001)/LANE_SHARES[lane]


def extraction_schema():
    nullable = {"type":["string", "null"]}
    vehicle_properties = {k:nullable for k in FIELDS}
    vehicle_properties["excerpt"] = {"type":"string"}
    props = {"reference":nullable, "status":{"enum":["active","updated","cancelled","unknown"]},
             "issued_text":nullable, "expires_text":nullable,
             "vehicles":{"type":"array", "maxItems":4, "items":{"type":"object", "properties":vehicle_properties,
                "required":list(vehicle_properties), "additionalProperties":False}}}
    return {"type":"object", "properties":props, "required":list(props), "additionalProperties":False}


def validate_extraction(result, text):
    issues = []
    if not isinstance(result, dict) or set(result) != set(extraction_schema()["properties"]):
        raise ValueError("Parser returned an unexpected object")
    if result["status"] not in ("active","updated","cancelled","unknown"):
        raise ValueError("Invalid alert status")
    for name in ("reference", "issued_text", "expires_text"):
        value = result[name]
        if value is not None and not isinstance(value,str): raise ValueError("Invalid field type")
        if value and value.casefold() not in text.casefold(): issues.append(f"{name}: not found verbatim in source")
    if not isinstance(result["vehicles"],list) or len(result["vehicles"]) > 4: raise ValueError("Invalid vehicle list")
    for i, v in enumerate(result["vehicles"]):
        if not isinstance(v,dict) or set(v) != set(FIELDS) | {"excerpt"}: raise ValueError("Invalid vehicle fields")
        excerpt = v["excerpt"]
        if not isinstance(excerpt,str) or not excerpt or excerpt not in text:
            issues.append(f"Vehicle {i+1}: supporting excerpt is missing or not verbatim")
        for field in FIELDS:
            value = v[field]
            if value is not None and not isinstance(value,str): raise ValueError("Invalid vehicle field type")
            if value and (not isinstance(excerpt,str) or value.casefold() not in excerpt.casefold()):
                issues.append(f"Vehicle {i+1} {field}: verify against source; not present in its excerpt")
        if not v["plate"]: issues.append(f"Vehicle {i+1}: plate missing; cannot activate plate matching")
        elif not valid_plate(v["plate"]): issues.append(f"Vehicle {i+1}: plate is partial or unsupported")
    if not result["vehicles"] and result["status"] != "cancelled": issues.append("No vehicles extracted")
    issues.append("Review vehicle-to-plate association and jurisdiction before saving; source text is not authenticated.")
    return issues


def ground_extraction(result,text):
    """Conservative source checks: erase unsupported guesses instead of presenting facts.

    This does not establish semantic correctness. Reviewed activation remains mandatory.
    """
    validate_extraction(result,text)  # Validate shape/types before traversing output.
    notes=[]
    for field in ("reference","issued_text","expires_text"):
        value=result[field]
        if value:
            value=value.strip()
            if value.casefold() not in text.casefold():
                notes.append(f"Removed unsupported {field}; enter it manually if known")
                value=None
            result[field]=value or None
    for i,v in enumerate(result["vehicles"]):
        excerpt=v["excerpt"].strip()
        start=text.casefold().find(excerpt.casefold()) if excerpt else -1
        if start>=0:
            excerpt=text[start:start+len(excerpt)]
            v["excerpt"]=excerpt
        else:
            notes.append(f"Vehicle {i+1}: no verifiable supporting excerpt; fields need manual entry")
            excerpt=""
            v["excerpt"]=""
        for field in FIELDS:
            value=v[field].strip() if v[field] else None
            if value and (not excerpt or value.casefold() not in excerpt.casefold()):
                notes.append(f"Vehicle {i+1}: removed unsupported {field}")
                value=None
            v[field]=value
        if v["plate"] and not valid_plate(v["plate"]):
            notes.append(f"Vehicle {i+1}: removed invalid or partial plate")
            v["plate"]=None
        if v["plate"]:
            # A make/model can normalize to a valid alphanumeric string. Require
            # explicit plate context rather than accepting any excerpt substring.
            value=re.escape(v["plate"])
            stated=re.search(rf"\bplates?\s*(?:(?:number|no\.?|is)\s*)?[:#-]?\s*(?P<value>{value})(?![A-Za-z0-9])",excerpt,re.I)
            if stated:
                v["plate"]=stated.group('value')  # Preserve source character case.
            else:
                notes.append(f"Vehicle {i+1}: plate is not explicitly supported in plate context; enter it manually")
                v["plate"]=None
        if v["jurisdiction"]:
            state=re.escape(v["jurisdiction"])
            before=rf"\b{state}\s+(?:(?:license|licence)\s+)?plates?\b"
            after=rf"\bplates?\b[^.\n]{{0,35}}\b(?:from|issued in|registered in|state[: ]+)\s*{state}\b"
            if not (re.search(before,excerpt,re.I) or re.search(after,excerpt,re.I)):
                notes.append(f"Vehicle {i+1}: plate jurisdiction not explicitly supported; left unknown")
                v["jurisdiction"]=None
    candidates=re.findall(r"\bplates?\s+(?:number\s+)?(?:is\s+)?[:#]?\s*([A-Za-z0-9][A-Za-z0-9-]{1,11})\b",text,re.I)
    candidates={normalize_plate(p) for p in candidates if p.isupper() or any(c.isdigit() for c in p)}
    extracted={normalize_plate(v["plate"]) for v in result["vehicles"] if v["plate"]}
    if candidates-extracted:
        notes.append("Source contains plate-like identifiers not covered by this draft. Check for missing vehicles or conflicting text.")
    return notes+validate_extraction(result,text)


def add_observation(sighting, plate_raw, confidence, frame, evidence_factory, now=None):
    """Atomic revalidation and idempotence. Non-target OCR is never persisted."""
    now = time.time() if now is None else now
    plate = normalize_plate(plate_raw)
    outcomes = []
    with db.transaction() as conn:
        alerts = db.active(conn,now)
        for alert in alerts:
            exact = alert["plate"] == plate
            if not exact and not distance_one(alert["plate"],plate): continue
            existing = conn.execute("SELECT * FROM events WHERE alert_id=? AND alert_version=? AND sighting_id=?",
                                    (alert["id"],alert["version"],sighting["id"])).fetchone()
            event_id = existing["id"] if existing else db.uid()
            observations = json.loads(existing["observations"]) if existing else []
            evidence = json.loads(existing["evidence"]) if existing else []
            if not any(o["frame"] == frame["key"] for o in observations):
                observations.append({"frame":frame["key"], "offset":frame["t"], "confidence":float(confidence), "text":plate_raw, "exact":exact})
                if len(evidence) < 6:
                    evidence.extend(evidence_factory(event_id,len(evidence)))
            strong = sum(o["confidence"] >= .90 and normalize_plate(o["text"]) == alert["plate"] for o in observations) >= 2
            capture = sighting["capture"] + frame["t"]
            conn.execute("""INSERT INTO events(id,alert_id,alert_version,sighting_id,plate,camera,capture,created,updated,strong,observations,evidence)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(alert_id,alert_version,sighting_id) DO UPDATE SET
                updated=excluded.updated,strong=excluded.strong,observations=excluded.observations,evidence=excluded.evidence""",
                (event_id,alert["id"],alert["version"],sighting["id"],alert["plate"],sighting["camera"],capture,now,now,int(strong),json.dumps(observations),json.dumps(evidence)))
            outcomes.append("strong" if strong else "exact" if exact else "near")
    return outcomes
