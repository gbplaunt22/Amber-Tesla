#!/usr/bin/env python3
"""Explicit installation step, never imported or called by a running service."""
import argparse,hashlib,json,pathlib,urllib.request

p=argparse.ArgumentParser();p.add_argument("directory",type=pathlib.Path);p.add_argument("--verify-only",action="store_true");a=p.parse_args()
manifest=json.loads((pathlib.Path(__file__).resolve().parents[1]/"models.lock.json").read_text())
a.directory.mkdir(parents=True,exist_ok=True)
for name,info in manifest.items():
    path=a.directory/name
    if not path.exists():
        if a.verify_only:raise SystemExit("Missing model: "+name)
        partial=path.with_suffix(path.suffix+".part")
        urllib.request.urlretrieve(info["url"],partial)
        if hashlib.file_digest(partial.open("rb"),"sha256").hexdigest()!=info["sha256"]:raise SystemExit("Model checksum failed: "+name)
        partial.replace(path)
    with path.open("rb") as f:digest=hashlib.file_digest(f,"sha256").hexdigest()
    if digest!=info["sha256"]:raise SystemExit("Model checksum failed: "+name)
    print(name,"verified")
