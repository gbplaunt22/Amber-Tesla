import os
from pathlib import Path

ROOT = Path(os.getenv("AMBER_ROOT", "/backingfiles/amber"))
DATA = ROOT / "data"
MODELS = ROOT / "models"
DB = DATA / "amber.sqlite3"
SPOOL = DATA / "spool"
CROPS = DATA / "crops"
EVIDENCE = DATA / "evidence"
SOURCE = Path(os.getenv("AMBER_SOURCE", "/mutable/TeslaCam"))
SNAPSHOTS = Path(os.getenv("AMBER_SNAPSHOTS", "/backingfiles/snapshots"))
AUTH_FILE = Path(os.getenv("AMBER_AUTH_FILE", "/mutable/amber/auth.json"))
LLAMA = Path(os.getenv("AMBER_LLAMA", str(ROOT / "llama/bin/llama-server")))
GEMMA = MODELS / "gemma-3-270m-it-Q8_0.gguf"
LLAMA_URL = "http://127.0.0.1:18791"
AP_IP = os.getenv("AMBER_AP_IP", "192.168.66.1")
PORT = int(os.getenv("AMBER_PORT", "8088"))
SPOOL_AGE = 900
SPOOL_BYTES = 8 * 1024**3
EVIDENCE_AGE = 7 * 86400
EVIDENCE_BYTES = 2 * 1024**3
FREE_FLOOR = 20 * 1024**3
MAX_JOBS = 500
PARSER_TIMEOUT = 90
CONTEXT = 2048
OUTPUT_TOKENS = 512


def initialize():
    for path in (DATA, SPOOL, CROPS, EVIDENCE):
        path.mkdir(parents=True, exist_ok=True)

