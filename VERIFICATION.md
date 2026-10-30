# Deployment verification — 30 October 2026

Implemented and deployed on the existing 4 GB Raspberry Pi 4 running TeslaUSB, Debian 12 AArch64. The system is ready for a supervised in-car trial; the full driving acceptance criteria are **not yet established**.

## Live trial update, 30 October 2026 at 15:42 Central

The Pi was connected to the car and newly recorded, completed snapshots from all six cameras were confirmed. A reviewed controlled-test alert was activated at 15:35:52 Central. At the 15:42 inspection, the approximately 15:38 encounter had not yet been processed: admission had backfilled recordings from before alert activation, delaying current footage. No positive plate match or sound notification was verified at that checkpoint.

This exposed a startup-backlog defect, rather than establishing recognition failure on the controlled encounter. The correction bounds new admission to clips overlapping an active alert and selects the newest eligible clips first. Older already-admitted work requires a separate operational decision; restarting the process alone does not erase the durable queue. The live trial still does not establish uninterrupted recording over a sustained route, positive-match recall, or the five-minute 95th-percentile target.

The correction passed **10 focused tests in 11.98 seconds on the actual Pi**, using isolated source: `tests/test_ingest_window.py` and `tests/test_video_ingest.py`. These cover pre-activation rejection before copying, overlapping clips, newest-first bounded admission, expiry during a scan, existing ingestion behavior, and video scheduling behavior. This is targeted regression validation, not a rerun of every deployment test or a positive in-car match demonstration.

A separate local Windows CPU diagnostic used FastALPR's public `test_image.png` and the same application inference functions with pinned model files. Vehicle-crop OCR read `5AU5341`, matching the upstream test, with minimum OCR confidence 0.999570. Confidence was a Python list, compatible with the application's normalization. YOLOX incorrectly labeled the main passenger car as a truck, confirming that appearance must remain a soft priority. This was one public still image, not Tesla footage or a Pi benchmark, and did not exercise two-frame confirmation or notification. No private test plate, recording, credentials, or runtime database is included in this repository.

The remaining sections document the earlier deployment baseline and its measurements; they should not be read as a completed in-car acceptance report.

## Implemented and checked

- Local YOLOX-Nano and FastALPR with explicit, SHA-256-verified model files; no runtime downloads.
- Gemma 3 270M Instruct Q8_0 through a pinned, locally compiled llama.cpp server. Draft generation never activates alerts.
- Safe completed-snapshot copying using TeslaUSB's cleanup lock, deduplication, incomplete-file retries, and UTC metadata handling. Snapshot interval is 60 seconds, with original idle checks retained.
- SQLite durable queues, appearance priorities, measured-time OCR lane scheduling, selected-frame expansion, version revalidation, and exact/near candidate evidence.
- Authenticated local dashboard, manual and multiple-vehicle entry, cancellation, event review, server-sent updates, and user-enabled sound.
- Fifteen-minute/8 GiB processing cap, seven-day/2 GiB evidence cap including bookmarks, and a 20 GiB ingestion free-space floor.
- Four processing services plus AP and DHCP services are enabled. All started successfully after a reboot. Root and boot partitions returned to read-only operation.
- Kernel memory accounting was enabled and checked after reboot. Actual parser `memory.max` is 1,073,741,824 bytes; video is limited to 1.5 GiB, ingestion and web to 256 MiB each. The parser additionally watches process-tree RSS.

## Automated and browser checks

**31 automated tests passed on the earlier deployment build on the Pi in 47.30 seconds.** The suite covers matching confidence/distinct frames, near-to-exact upgrades, edits/cancellations/expiry, authenticated activation, draft idempotence, weighted scheduling, capacity eviction, job recovery, incomplete clips, deduplication, timestamp offsets, retention and orphan cleanup, parser grounding, token limits, and parser failure preserving active alerts/manual entry. One dependency deprecation warning concerns Starlette's test client using HTTPX; it did not fail a test.

Headless Edge exercised the actual Pi dashboard through an SSH loopback forward, both before and after reboot. Login, manual create/edit/cancel, adding/removing vehicle editors, and a 390-pixel mobile layout passed with no JavaScript errors or horizontal overflow. The AP's own HTTP endpoint returned 200, and `amber0` returned with address 192.168.66.1 after reboot. A physical phone's association, DHCP lease, and audible sound still need checking.

Outbound restriction checks denied IP socket creation for ingestion/video, external routing from the parser's network namespace, and outbound `connect()` for the web service. Parser-to-llama communication remains on private loopback. All dashboard assets are local. This checks the pipeline's restrictions; existing unrelated Pi services such as the user's uplink remain outside the pipeline.

## Video measurements

Six-camera timing samples used existing Tesla recordings, three sampled frames per camera. These are small performance samples, not plate-recognition accuracy measurements. Full details are in `benchmark-vision.json`.

| Stage | Observed mean range per sampled frame |
|---|---:|
| Decode/seek | 0.104–0.389 seconds |
| YOLOX-Nano | 0.158–0.175 seconds |
| Full-frame ALPR timing baseline | 0.255–0.323 seconds |
| Vehicle-crop ALPR, two views with detected vehicles | 0.258–0.263 seconds |
| Benchmark process RSS | 239–250 MiB |

A replay of six approximately one-minute clips produced 18 sightings and processed 54 OCR frames while Gemma was also running. The worker was forcibly killed and restarted during the replay; all six clips and all 18 sightings finished. The last OCR job was recorded 177.13 seconds after admission. `replay-check.json` was collected later, at 340 seconds; its `elapsed` field is inspection time, **not** processing completion latency.

The replay used rebased capture timestamps and a synthetic target that was not present. It produced no matching events. Adaptive sampling reached 0.25 FPS front/back and 0.0625 FPS side views as the batch aged. This demonstrates bounded work and recovery under load, but also shows the coverage tradeoff. It does not demonstrate sustainable processing at the baseline rates, positive-match accuracy, or five-minute driving latency. A process kill is not a physical power-loss test.

## Gemma assessment

The six regression examples finished in 19.9–51.2 seconds on the final prompt/grounding implementation; three passed all vehicle-field checks in that small corpus. The checks are explicitly listed in `tests/parser_cases.json`; they do not score every top-level JSON field. Results and validation issues are in `benchmark-parser.json`.

Observed failures included an omitted year, an omitted second vehicle, example-plate leakage, and following an embedded instruction to supply the wrong plate. Grounding cleared the unsupported plate/jurisdiction values, but did not recover the missing facts. In some outputs the reference was generic alert wording rather than a useful ID. A supported excerpt does not prove correct association or resolve conflicting statements.

The model is therefore labeled experimental in the dashboard, and manual entry is recommended. Review is mandatory. No model-size change was made. The model unloaded after its idle period during the deployment checks.

The separate four-case holdout passed only **1 of 4 vehicle-field checks**, taking 22.9–27.4 seconds per case. It exposed make/model strings in the plate field, a missed correction, and an omitted second vehicle. It was run after freezing the prompt and initial grounding implementation; results remain unmodified in `benchmark-parser-holdout.json`.

That holdout exposed a validation gap: removing spaces made `Honda Accord` look syntactically plate-like. A subsequent deterministic validation fix now requires explicit plate context in the source excerpt and preserves its original character case. The original holdout results are retained as evidence of model failure; they are not relabeled as a fresh evaluation of that fix. A regression test checks the new validator. Broader unseen examples remain necessary.

After reboot, the parser cgroup peaked at 469,749,760 bytes (about 448 MiB) during this holdout with zero recorded OOM events and zero service restarts. It subsequently unloaded llama-server. Cgroup memory can include reclaimable file cache after process exit.

## In-car acceptance procedure still required

1. While parked, join AMBER-Local with the supplied private connection guide. Confirm dashboard login, DHCP, and sound after tapping Enable sound. Verify Tesla's recording indicator and all six new camera files.
2. Enter a reviewed test plate belonging to a cooperating vehicle. Include daylight, shade, distance, motion, side/rear views, and visually similar nonmatching plates. Record the ground-truth camera encounter times independently of successful detections.
3. Drive a representative sustained route with all six cameras and submit a parser draft while processing is busy. Track expected encounters, misses, false candidates, dropped coverage, temperatures, queue age, and recording continuity. A passenger should operate the dashboard.
4. For each eligible known encounter, measure time from visible plate to dashboard candidate and repeated exact-match notification. Report misses separately; do not compute latency only on an easy subset without disclosure. The target is measured 95th-percentile delay no greater than 300 seconds, with an explicit recall assessment.
5. Confirm temporary footage disappears by its configured age/cap and evidence expires independently. Check restart recovery on a planned power cycle while parked and after ensuring recording is safely stopped.

USB reported **not attached** during deployment. Uninterrupted live Tesla recording, actual phone behavior, representative positive ALPR recall/precision, physical power-loss recovery, and the five-minute 95th-percentile target remain unverified. No installation test alerts are intended to remain active.

Final handoff checks: all six Amber services active and enabled; TeslaUSB active; SQLite integrity check `ok`; zero active alerts and zero test drafts; approximately 79 GiB free on `/backingfiles`; no running llama-server after idle. Original TeslaUSB recordings were not deleted. Temporary replay copies and synthetic installation records were removed.
