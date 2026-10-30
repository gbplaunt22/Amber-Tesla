# Amber-Tesla

A local TeslaUSB vehicle-alert pipeline for a 64-bit Raspberry Pi 4. It ingests completed snapshots, prioritizes vehicle sightings, runs FastALPR on selected full-resolution crops, and serves a local review dashboard. Gemma 3 270M Instruct drafts vehicle records from pasted alert text.

The dashboard is named **Amber Local**. This project's repository is [gbplaunt22/Amber-Tesla](https://github.com/gbplaunt22/Amber-Tesla). Application code uses the repository's MIT license; third-party dependencies and model weights retain their respective licenses, including Gemma's terms.

## Using the deployed Pi

1. Join **AMBER-Local** on your phone. For the existing installation, generated Wi-Fi and dashboard credentials were delivered separately in a private `CONNECT.md`. Credentials and this private guide are excluded from Git; installation also stores recovery credentials under `/mutable/amber` on the Pi.
2. Open **http://192.168.66.1:8088/** and sign in. The access point intentionally provides no internet gateway or DNS service; your phone may ask to stay connected to a network without internet.
3. Paste an alert under **Add an alert**, or enter the vehicle directly. Review all fields, especially plate, jurisdiction, multiple vehicles, and expiry. The default expiry is 24 hours unless you enter one.
4. Activate the reviewed record. Processing is idle when there are no active plate alerts.
5. Keep the page open and select **Enable sound**. Backgrounded or disconnected phone pages cannot reliably sound an alert. Use the controls while safely parked.
6. Review possible matches and their vehicle/plate crops. A repeated exact OCR read is still a possible match, not an independently confirmed identification.

Gemma is experimental at this size. Tests found missed details, omitted vehicles, and invented values. The application clears values unsupported by the quoted source excerpt and flags incomplete drafts. Manual review is required; the model cannot activate or cancel alerts, run commands, or access tools. Make/model information is recorded for review, not used as an implemented visual classifier.

## Runtime architecture

| Service | Responsibility | Networking |
|---|---|---|
| `amber-ingest` | Snapshot discovery, clip staging, retention | Private network namespace; IP sockets denied |
| `amber-video` | YOLOX-Nano, short sightings, priority scheduler, FastALPR | Private network namespace; IP sockets denied |
| `amber-parser` | On-demand Gemma through llama.cpp | Private network namespace; loopback only |
| `amber-web` | Authenticated FastAPI dashboard, APIs, SSE, evidence | AP/loopback ingress; outbound `connect()` denied |
| `amber-ap` | Concurrent Wi-Fi AP following the station channel | System network configuration |
| `amber-dhcp` | AP address allocation, without routing or DNS | DHCP only |

Application source is installed at `/backingfiles/amber/app`; the virtual environment, root-owned model assets, and llama.cpp runtime live beside it. Writable runtime data is under `/backingfiles/amber/data`. Root-only recovery credentials/configuration live under `/mutable/amber`. The service account is `amber` with no interactive shell.

All model paths are explicit. Runtime services never download missing models. SHA-256 pins and installation URLs are in `models.lock.json`; Python versions are pinned in `requirements.lock.txt`. The compiled llama.cpp source commit is `a34de8618b25dfc32ee43749db47d767a9f0a25d`. Upstream model/runtime licenses continue to apply, including Gemma's terms.

### Video ingestion and timing

TeslaUSB's existing snapshot loop is configured with `SNAPSHOT_INTERVAL=60`. Its original idle checks and locking remain in place; the idle check can add up to 90 seconds. The live USB disk image is never opened for modification by this application. Snapshot copying is serialized with TeslaUSB cleanup, one clip per lock; decoding and inference occur after releasing the lock.

MP4 embedded UTC creation time is authoritative. The newest readable clip calibrates filename prefiltering so a car/Pi timezone difference does not silently discard all new recordings. Incomplete files are retried. Clip name and size deduplicate repeated snapshot links and SavedClips copies.

Discovery starts at 1 FPS front/back and 0.25 FPS on each side view. If the oldest selected clip is over 120 seconds old, rates halve; beyond 180 seconds they quarter. This is explicit, visible load shedding: **brief vehicles can be missed**. The 2–5 minute target is not a guarantee, and a sustained in-car latency/recall trial is still required.

### Scheduling and matching

- SQLite WAL with full synchronous commits stores clips, sighting jobs, alert versions, draft status, and evidence metadata. Interrupted jobs resume after process restart.
- One video worker allocates approximately 70% of busy processing time to discovery and 30% to OCR; idle capacity is reused.
- OCR lanes receive 50% verification, 30% likely appearances, and 20% other/uncertain vehicles by measured service time. Within each lane, older sightings go first.
- Each sighting starts with up to three sharp, temporally separated crops. Promising sightings expand to at most 12 frames, with fresh localization at added timestamps.
- Actual decoded frame identities prevent a repeated seek to the same frame from counting as multiple confirmations.
- One exact plate read creates a review candidate. Two distinct exact frame reads with minimum per-character confidence >=0.90 qualify for sound. Near matches create clearly labeled review candidates and request more processing; they cannot trigger sound.
- Plate normalization changes case and separators only. O/0 and B/8 are never equated. Every event rechecks the current alert version, active state, and expiry.
- At most 500 collecting/pending/working sightings are retained. Old low-priority work is evicted first. Queue pressure and skipped coverage appear in the dashboard.

### Retention and resource limits

- Clip/crop buffer: 15 minutes from admission or 8 GiB, whichever comes first.
- Match evidence: seven days or 2 GiB. Manual save bookmarks an event but bypasses neither limit.
- New ingestion pauses below 20 GiB free. Expired jobs cannot pin old TeslaUSB snapshots.
- Nonmatching OCR strings are not written to a searchable history.
- Gemma uses one thread, 2,048 context tokens, at most 512 output tokens, one request at a time, and a 90-second deadline. It unloads after approximately 30 idle seconds.
- Memory accounting was enabled with `cgroup_enable=memory` and verified after reboot. Systemd enforces 1 GiB for the parser, 1.5 GiB for video, and 256 MiB each for ingestion and the web service. The parser also has a process-tree RSS watchdog. Queues and batches are bounded.
- Video processing pauses at 80°C. CPU and I/O scheduling prioritize continued Tesla recording.

## Development and reproduction

The application is run as a module from the source directory, not installed into the system Python. On the deployed device:

```sh
cd /backingfiles/amber/app
/backingfiles/amber/venv/bin/python -m pytest -q
sudo systemctl status amber-ingest amber-video amber-parser amber-web amber-ap amber-dhcp
sudo journalctl -u amber-parser -u amber-video --since '10 minutes ago'
```

`deploy/install.py` is idempotent and installs service definitions, generated credentials, and the snapshot setting. It assumes the `amber` account, application source, virtual environment, and dependencies already exist. It does not overwrite existing credentials or activate the AP until explicitly started.

For a new compatible TeslaUSB installation:

1. Temporarily make the system partition writable with TeslaUSB's `remountfs_rw` helper. Create the system account `amber`, the application/models/data directories, and a Python 3.11 virtual environment under `/backingfiles/amber`.
2. Copy this source into `/backingfiles/amber/app`; keep source, runtime, and models root-owned. Give only the data directory to `amber`.
3. Install `requirements.lock.txt` into that virtual environment. The current Pi also retains an offline wheel cache at `/backingfiles/amber/build/wheels`; use `pip install --no-index --find-links=... -r requirements.lock.txt` to avoid downloads.
4. Run `deploy/provision_models.py /backingfiles/amber/models` during the explicit installation phase, or copy the model files and run it with `--verify-only`. A mismatched hash fails installation.
5. Build the pinned llama.cpp commit for AArch64 with CPU inference and `llama-server`. The deployment used `GGML_NATIVE=ON`, `GGML_OPENMP=OFF`, `LLAMA_CURL=OFF`, `LLAMA_BUILD_TESTS=OFF`, and `LLAMA_BUILD_SERVER=ON`. Copy the build's `bin/` contents into `/backingfiles/amber/llama/bin/`, root-owned. The parser sets its library path to that directory.
6. Ensure FFmpeg, NetworkManager, `iw`, and `dnsmasq-base` are available. Run `sudo python3 deploy/install.py`, then start `amber-ap` and `amber-dhcp` after verifying the original uplink remains available. Installation backs up the boot command line and enables memory accounting.
7. Reboot only while no USB host is attached, or at the next planned recording downtime. Confirm `memory` appears in `/sys/fs/cgroup/cgroup.controllers` and the service `memory.max` values are enforced. The deployed Pi was rebooted and verified with the system and boot partitions read-only.

The AP is created as an in-memory NetworkManager connection because TeslaUSB's read-only root can prevent persistent connection additions. Its SSID and password persist in root-only JSON and the service recreates the AP at boot. Channel changes use temporary NetworkManager settings, so read-only operation is supported.

## APIs

All state/data endpoints require the authenticated session cookie. Mutations require `X-Amber-Request: 1` and reject cross-origin browser requests. The actual local socket address must be the AP or loopback, regardless of the Host header.

| Endpoint | Purpose |
|---|---|
| `POST /login`, `POST /logout` | Local session |
| `POST /alert-drafts` | Queue pasted text; returns 202 and draft ID |
| `GET /alert-drafts`, `GET /alert-drafts/{id}` | Draft results, source text, issues/errors |
| `POST /alerts` | Activate 1–4 reviewed vehicle records |
| `GET /alerts`, `PUT /alerts/{id}` | List/edit vehicle alerts |
| `POST /alerts/{id}/cancel` | Cancel selected alert; optionally apply cancellation draft |
| `GET /events`, `PATCH /events/{id}`, `DELETE /events/{id}` | Review/save/delete evidence |
| `GET /evidence/{event}/{filename}` | Authenticated event evidence only |
| `GET /health`, `GET /stream` | Device health and server-sent event updates |

Alert activation includes `vehicles`, optional `reference`, `source`, `draft_id`, timezone-aware `expires`, and mandatory `reviewed: true`. Each vehicle has `plate` and optional `jurisdiction`, `color`, `vehicle_type`, `make`, `model`, and `year`. Updating an existing alert accepts exactly one vehicle and increments its version.

## Verification and rollback

Read `VERIFICATION.md` and the JSON benchmark files for measured outcomes and limits. The synthetic parser regression corpus is in `tests/parser_cases.json`, and the separate final holdout is in `tests/parser_holdout.json`. `deploy/evaluate_parser.py` exercises the actual parser service without activating alerts; use `--cases` to select a corpus. These small test sets do not establish production accuracy.

To stop processing immediately without affecting TeslaUSB:

```sh
sudo systemctl stop amber-ingest amber-video amber-parser amber-web
```

To also stop the AP:

```sh
sudo systemctl stop amber-ap amber-dhcp
```

Disable the six `amber-*` services to prevent startup. The original TeslaUSB configuration backup is `/mutable/amber/teslausb_setup_variables.conf.before`; restore only the previous `SNAPSHOT_INTERVAL` setting so later unrelated changes are preserved. The original setup had no active override, so removing the added active `SNAPSHOT_INTERVAL=60` line restores TeslaUSB's default on its next safe restart. No Tesla recordings need to be deleted for rollback.

The original boot command line is backed up at `/mutable/amber/cmdline.txt.before`. Removing only the added `cgroup_enable=memory` parameter reverses memory accounting on the next reboot, but also removes kernel enforcement of the service memory limits. Raspberry Pi's kernel maintainers describe this override in [their memory-controller discussion](https://github.com/raspberrypi/linux/issues/6980).
