import hashlib,json
import pytest
from amber import config as C,db

@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    monkeypatch.setattr(C,"ROOT",tmp_path)
    for name,tail in (("DATA","data"),("MODELS","models"),("DB","data/amber.sqlite3"),("SPOOL","data/spool"),("CROPS","data/crops"),("EVIDENCE","data/evidence"),("AUTH_FILE","auth.json")):
        monkeypatch.setattr(C,name,tmp_path/tail)
    db.init()
    C.AUTH_FILE.write_text(json.dumps({"username":"test","salt":"00"*16,"password_hash":hashlib.pbkdf2_hmac("sha256",b"testpass",bytes(16),200000).hex(),"secret":"12"*32}))

@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from amber.api import app,failures
    failures.clear()
    with TestClient(app) as c:
        c.headers["X-Amber-Request"]="1"
        assert c.post("/login",json={"username":"test","password":"testpass"}).status_code==200
        yield c
