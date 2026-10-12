# Call Translator: Implementation Plan

## Current scope (updated 2026-10-11)

Test server-mediated voice translation between exactly two mobile phones:

| Party | Mobile | Language |
|---|---|---|
| A | +919123260733 | Odia (`od-IN`) |
| B | +916392306428 | Hindi (`hi-IN`) |

Either phone can initiate the call. The server routes A to B or B to A using the configured pair. There are no orders, rider IDs, session registrations, leases, expiry checks, virtual-number pools, or database requirements. Unknown callers are rejected so this test server cannot dial arbitrary destinations.

Both call legs terminate at the server. Each phone hears translated audio only; never bridge the original audio. Ordinary calls directly between the two mobiles do not pass through this server. For real calls, either phone dials the provider-owned inbound SIP number (`SIP_NUMBER`), and the server calls the other mobile.

**Stack:** uv-managed Python environment → FastAPI routing API + LiveKit Agents worker → Sarvam STT, translation and TTS. Local tests use Docker and a local LiveKit server. Real calls use a voice-capable US Twilio number, authenticated Programmable Voice/TwiML inbound SIP, Twilio Elastic SIP Trunking outbound, and LiveKit Cloud with India pinning. Background: `NOTES.md`; setup: `SETUP.md`.

**Provider change (2026-10-11):** The user selected Twilio instead of Plivo. Either Indian mobile calls the US `SIP_NUMBER` using an ISD-enabled plan; the worker dials the other Indian mobile with that US number as caller ID. Twilio supports calls to India only from non-Indian numbers. The phone plan covers only the caller's international leg; Twilio, LiveKit and Sarvam are billed separately. This changes deployment configuration, not the fixed-pair routing or translation interfaces. The `.env` is now prepared for `sip`/`sarvam`; missing cloud/provider values must be filled before starting services. Previous local settings are backed up under ignored `data/`.

**Current progress:** P7 implementation and automated checks are complete. P4's real noisy-phone recording validation remains **PENDING and deferred**. P6 cloud/provider provisioning, Mumbai deployment and real-phone validation remain **PENDING**. The user authorized P7 implementation without completing these human gates; automated results do not satisfy them.

**Immediate runtime (2026-10-11):** The user will run FastAPI and the LiveKit translation worker on their own PC, with LiveKit Cloud handling SIP and rooms. The US Twilio number has been procured. A hosted application server and Mumbai deployment are deferred; Twilio/LiveKit provisioning and real-phone checks are the next steps. Use Docker's `API_URL=http://api:8000` and start only `api`/`agent` with `--no-deps`, not the local LiveKit dev service. No public FastAPI endpoint, tunnel or Twilio application webhook is needed: inbound TwiML is hosted by Twilio, and the worker initiates its LiveKit/vendor connections. Keep `.env` in SIP/Sarvam mode and fill the missing cloud/provider values. This is real-phone testing with a local application server, not fake room-mode E2E testing.

**Twilio configuration verification (2026-10-11):** `.\.venv\Scripts\python.exe -m pytest tests\test_sip_config.py tests\test_agent.py -q -p no:cacheprovider` passed **55 tests**. The updated template check parses TwiML, matches inbound credentials to the LiveKit trunk, preserves caller ID, excludes a direct mobile dial, and validates the outbound Twilio domain placeholder against the installed SIP schema. Environment preservation checks passed for the API/Sarvam secrets and phone pair. Only documentation, environment settings, SIP templates and their configuration test changed; application code and dependencies are unchanged. No resources were provisioned, services restarted or live calls made. Four warnings concern existing short development JWT secrets in local test fixtures.

## Rules for the implementing agent

1. Use `uv sync --extra test` and `uv run --extra test ...` for local development. Docker creates a separate Linux uv environment at `/opt/venv`; never reuse the Windows `.venv` inside containers.
2. Work one phase at a time. Run its checkpoint before starting the next phase. Rebuild images after changes to `Dockerfile`, `pyproject.toml`, or `uv.lock`.
3. Never weaken tests to hide a failure. The user explicitly changed the contracts on 2026-10-10, so lease/session tests were replaced with fixed-pair routing tests and downstream contracts updated. Further contract changes require a new user instruction.
4. HUMAN GATE phases require human actions or confirmation. Existing authorization remains valid; do not ask again for prerequisites already confirmed.
5. Keep one deployment, two configured phones, and logs. Add no database, order integration, queues, or lease management for this demo.
6. Report the checkpoint command, result, and skipped work. Do not label blocked checkpoints as passing.

## Repository layout

```
app/
  routing.py   lookup(caller, parties)
  api.py       create_app(parties, api_key); module-level app from environment (P2)
  pipeline.py  run_direction / run_call (P3)
  vendors.py   translate / synthesize / transcribe_file + fake vendors (P4)
  agent.py     LiveKit worker, room/SIP calling and hardening, agent_name="translator" (P5–P7)
tests/
  test_routing.py       P1
  test_api.py           P2
  test_pipeline.py      P3
  test_vendors.py       P4, fake vendors and mocked Sarvam protocols
  test_vendors_live.py  P4, opt-in live marker
  test_agent.py        P5/P6, mocked lookup, audio, interruption, SIP and lifecycle
  test_sip_config.py   P6, provider authentication, India routing and dispatch templates
  test_hardening.py    P7, mocked failure prompts, deadlines, retries, lifecycle and drain
  test_e2e_livekit.py   P5, opt-in e2e marker
  fixtures/            real 8 kHz phone recordings + transcripts
config/sip/            Twilio inbound TwiML and LiveKit trunk/dispatch JSON examples (P6)
```

## Contracts

### `app/routing.py`

- `lookup(caller: str, parties: dict[str, str]) -> dict | None`
- `parties` maps the two distinct mobile numbers to their language codes.
- A known caller receives exactly `{"caller_lang", "other_no", "other_lang"}` for the other phone. An unknown caller receives `None`.
- A configuration with anything other than two distinct phone numbers raises `ValueError`.
- Repeated calls require no registration or stored state. No order or session IDs are returned.

### `app/api.py` (P2)

- `create_app(parties: dict[str, str], api_key: str) -> FastAPI`.
- At startup validate two distinct E.164 numbers (`^\+\d{10,15}$`), supported and different languages, and a nonempty API key. Invalid configuration raises `ValueError`.
- Module-level `app` uses `API_KEY` and the mapping `{PARTY_A_NO: PARTY_A_LANG, PARTY_B_NO: PARTY_B_LANG}` from environment variables.
- `GET /healthz` → 200, no authentication.
- `GET /lookup?caller=...` → 200 with the routing dictionary; 404 for unknown callers; 422 if caller is missing.
- `/lookup` requires `X-API-Key`; missing or wrong keys return 401.
- No `/sessions` or `/calls` endpoints. The inbound SIP number belongs to telephony configuration, not the routing lookup.
- Supported Sarvam codes: `hi-IN kn-IN ta-IN te-IN ml-IN mr-IN bn-IN gu-IN pa-IN od-IN en-IN`. This demo uses `od-IN` and `hi-IN` only.

### `app/pipeline.py` (P3)

- `async run_direction(finals, translate, speak, on_error=None)` processes nonblank final transcripts sequentially: `await speak(await translate(text))`.
- Blank transcripts are skipped. Translation errors call `on_error(exc)` if provided, skip the sentence, and continue. Never speak the original text.
- `async run_call(a_finals, b_finals, translate_ab, translate_ba, speak_to_a, speak_to_b)` runs both directions concurrently. A's translation reaches B only, and B's reaches A only.

### `app/vendors.py` (P4)

- `async translate(text, src, tgt) -> str`.
- `async synthesize(text, lang) -> (pcm16_bytes, sample_rate)`.
- `async transcribe_file(path, lang) -> str` for accuracy tests.
- Streaming STT uses a vendor LiveKit plugin or the vendor streaming API.
- `async transcribe_stream(chunks, lang, sample_rate=16000, on_speech_start=None, on_speech_end=None)` yields final transcript strings from mono little-endian PCM16 chunks at 8 or 16 kHz. Optional speech callbacks are synchronous and support P5 interruption/latency measurement. Partial transcripts are never translated or spoken.
- `VENDORS=fake` makes no vendor requests: STT emits `"fake"` after at least 300 ms of speech (RMS above threshold) followed by at least 500 ms of silence; MT returns `f"[{tgt}] {text}"`; TTS returns 0.6 seconds of sine wave at 24 kHz, 440 Hz if text starts with `[` and 550 Hz otherwise.

### `app/agent.py` (P5–P7)

- Register with `agent_name="translator"` for explicit dispatch.
- Room A is the dispatched room; the caller is its first remote participant. Read the caller number from metadata `{"caller": "..."}` for local tests, otherwise from the SIP participant's `sip.phoneNumber` attribute.
- Call authenticated `GET {API_URL}/lookup?caller=...`. On 404, delete room A. No order, VN lease, or session API calls.
- Room B is `f"{room_a}-b"`; the agent joins it as `translator`.
- `DIAL_MODE=room`: wait for identity `receiver` in room B.
- `DIAL_MODE=sip`: dial `other_no` via `LIVEKIT_SIP_OUTBOUND_TRUNK_ID`, playing a hold tone to the caller until the other phone answers. Apply the SIP provider's inbound caller verification settings.
- Streaming STT on each side → `run_call` → translated TTS published in the other room. Raw audio is never forwarded.
- Clear a listener's queued TTS audio when that listener starts speaking.
- Discard translation and synthesis that were in flight when the listener interrupted.
- In SIP mode, announce connection in each phone's language; announce busy/no-answer/failure to the caller before cleanup.
- Translation and sentence synthesis each have a 3-second deadline per attempt. On timeout, give a bounded listener-language wait prompt, retry that operation once, then skip the sentence. Never replay original speech or redial automatically.
- When either participant leaves, delete both rooms.
- Share cancellation-safe cleanup between entrypoint and shutdown callback. Bound task cancellation, media closure and room deletion; retry deletion once, treating already-deleted rooms as success.
- Use SDK draining on shutdown: stop accepting new jobs, allow active calls up to 30 seconds to finish, then allow 30 seconds for process shutdown. Compose allows 75 seconds before killing the worker.
- Log `call_summary call_id=<room_a> utterances=N p50_ms=... p95_ms=...`. `call_id` is a room identifier, not a registered session. Measure latency from end of speech to first published TTS frame.

## Phases and checkpoints

### P0: Prerequisites (HUMAN GATE)

- [x] Original Plivo/Exotel India SIP account and numbers: confirmed ready on 2026-10-10; superseded by Twilio selection on 2026-10-11.
- [ ] Twilio funded/upgraded account, voice-capable US number and India outbound SIP dialing permissions: required for the new provider setup.
- [x] LiveKit Cloud with India region pinning: confirmed ready by the user on 2026-10-10.
- [x] Sarvam API key: confirmed ready by the user on 2026-10-10.
- [x] `.env` created with a random API key and the configured Odia/Hindi phone pair. Initial vendor/LiveKit defaults were local; the 2026-10-11 Twilio update prepares live settings with cloud/provider values still pending.
- [x] Install uv and create the project `.venv`: uv 0.13.0, Python 3.12.10, 81 Windows project/test packages. `uv sync --extra test`, offline locked sync, and dependency checks passed on 2026-10-10.

**Current setup status:** `uv.lock` is generated. Docker API/agent/test images rebuild using locked dependencies and a staged non-OneDrive build context. LiveKit runs and its HTTP endpoint returns `OK`. The P2 API runs in Docker and `/healthz` returns 200. P5 now supplies `app.agent`, and the Docker worker registers as `translator`. The host agent SDK import also passed during P5; the earlier PyAV policy failure is retained in `SETUP.md` as setup history. See `SETUP.md` for cache, offline installation, and build-context workarounds. No real phone calls were run.

### P1: Fixed-pair routing

Implement `app/routing.py` without a database.

**Checkpoint:** `uv run --extra test pytest tests/test_routing.py` → 5 passed. Docker alternative: `docker compose run --rm --no-deps test pytest tests/test_routing.py`.

**Status:** Routing implemented and unchanged during setup verification. Project `.venv` checkpoint: `uv run --extra test --offline pytest tests/test_routing.py -q` → **5 passed, 8 subtests passed**. Docker checkpoint: `docker compose run --rm --no-deps test pytest tests/test_routing.py -q` → **5 passed, 8 subtests passed**. Compose configuration and installed dependency checks pass. Removed lease modules and replaced their tests because the user changed the contract; no stored database existed.

### P2: Routing API

Implement `app/api.py` with the fixed-pair lookup contract.

**Checkpoint:** `uv run --extra test pytest tests/test_api.py` → all passed. Then start the API using `uv run --env-file .env uvicorn app.api:app --host 127.0.0.1 --port 8000` or Docker and check `/healthz` → 200.

**Status:** Complete on 2026-10-10. `app/api.py` validates configuration, authenticates lookups, and reuses unchanged P1 routing. API tests use isolated environment settings; no production credentials are required for unit tests. `uv run --extra test --offline pytest tests/test_api.py tests/test_routing.py -q` and `docker compose run --rm --no-deps test pytest tests/test_api.py tests/test_routing.py -q` each passed **33 tests (28 API, 5 routing), 8 routing subtests**. Started Docker API with `docker compose up -d --no-build --no-deps api`; `/healthz` returned 200. HTTP checks against the running service verified both language directions, missing/wrong keys (401), unknown callers (404), and missing callers (422). No vendor requests or live calls were run.

### P3: Translation core

Implement `app/pipeline.py`.

**Checkpoint:** `uv run --extra test pytest tests/test_pipeline.py` → all passed.

**Status:** Complete on 2026-10-10. `app/pipeline.py` processes each direction sequentially and both directions concurrently, skipping blank transcripts and failed translations without speaking the original text. Translation errors reach the optional synchronous callback. Audio output errors propagate; a task group cancels the other direction on failure and raises an `ExceptionGroup`. The focused uv checkpoint passed **8 pipeline tests**. Full unit checks in uv and Docker passed **41 tests, 8 routing subtests**, with 14 live/e2e cases deselected. Pytest discovery is restricted to `tests/` so it does not collect dependency tests from the workspace uv cache. The host full-suite command was `uv run --extra test --offline --find-links data/windows-wheels pytest -q`; the Docker command was `docker compose run --rm --no-deps test pytest -q`. P4 vendors and the recordings gate are next. No vendor requests or live calls were run.

### P4: Vendors and accuracy (HUMAN GATE for recordings)

Implement Sarvam and fake vendors. The human supplies at least 10 noisy real-phone recordings per language, Odia and Hindi, at 8 kHz mono WAV with exact native-script `.txt` transcripts under `tests/fixtures/od-IN_<nn>` and `hi-IN_<nn>`.

**Checkpoint:** `uv run --extra test --env-file .env pytest -m live -v` with `SARVAM_API_KEY`: translation/TTS checks pass and average word overlap across recordings is at least 0.6. Compare Google only if Sarvam falls short; the human chooses the vendor.

**Current status (2026-10-10):** P4 vendor implementation and the live checkpoint pass on the available synthetic fixtures: **9 passed, 0 failed, 0 skipped**. The real noisy-phone recordings human gate remains **pending and explicitly deferred by the user on 2026-10-10**. P4 is not complete; this gate does not block P5. `app/vendors.py` implements fake translation/TTS/STT, Sarvam REST translation/TTS/file transcription, and realtime streaming STT with final-only output and cancellation cleanup. Models are now `sarvam-translate:v1`, `bulbul:v3` (voice `shubh`), and `saaras:v4`; only the realtime STT adapter maps application `od-IN` to endpoint-specific `or-IN`. `aiohttp` is a declared direct dependency, reusing the installed locked version without adding another SDK. See the verified fix below; the earlier failing measurements are retained as history.

Unit commands `uv run --extra test --locked --offline pytest -q` (workspace-local `TMP`/`TEMP`) and `docker compose run --rm --no-deps test pytest -q` each passed **93 tests (52 vendor tests), 8 routing subtests**, with 14 live/e2e cases deselected. All three Docker images rebuilt with the updated lockfile and source snapshot; the restarted API still returns `/healthz` 200. P1 routing remains unchanged.

**Initial live results before the fix (2026-10-10):** The local `.env` has a nonblank `SARVAM_API_KEY`; its value was not logged. Live tests force Sarvam mode even though `.env` retains `VENDORS=fake`. Both commands below ran from the inner `call-translator` project directory.

- Requested Windows command: `uv run --extra test --env-file .env pytest -m live -v` (using the installed `uv.exe` path and workspace-local uv cache). **Passed: 0. Failed: 5. Skipped: 0. Deselected: 102.** Every selected test failed with `httpx.ConnectError: All connection attempts failed`. The Windows execution environment could not connect to Sarvam; these failures do not establish vendor rejection or invalid credentials.
- Docker fallback: `docker compose run --rm --no-deps -e 'PYTEST_ADDOPTS=-rP --junitxml=data/p4-live-results.xml' test uv run --extra test --env-file .env pytest -m live -v`. **Passed: 3. Failed: 2. Skipped: 0. Deselected: 102.** Duration: 15.92 seconds. The report is saved in `data/p4-live-results.xml`.
- Passed: Hindi TTS, Odia TTS, and transcription accuracy across all 20 fixtures. Average literal word overlap was **0.94 overall**, **0.90 Odia**, and **0.97 Hindi**, above the existing overall minimum of 0.60. This metric is word overlap, not word error rate.
- Failed: Hindi-to-Odia translation took **2.408 seconds** and Odia-to-Hindi took **1.894 seconds**, exceeding the unchanged **1.5-second** limit. Both translations passed the native-script check. An earlier Docker run also failed both latency checks at 2.196 and 1.989 seconds. No HTTP status, authentication, quota, or vendor timeout errors occurred in these Docker runs.

The existing fixtures were generated with Sarvam TTS at the user's request: 10 Odia and 10 Hindi WAVs with matching transcripts. All **40 files** were checked locally: exact filenames, mono PCM16 at 8000 Hz, complete audio frames, durations **1.109–2.987 seconds**, and nonempty single-line UTF-8 transcripts without BOM. No fixture repairs were needed. These are synthetic recordings, so the passing transcription test does **not** satisfy the noisy real-phone recordings human gate.

Only P4 test diagnostics were changed: translation assertions now report elapsed seconds, and accuracy output includes each language's average. The acceptance thresholds were not relaxed. Application modules, including routing and vendors, are unchanged. A fresh regression command, `docker compose run --rm --no-deps test uv run --extra test --env-file .env pytest -q`, passed **93 tests and 8 routing subtests**, with **14 deselected** and one existing Starlette/httpx deprecation warning.

**Blockers before the fix:** Translation latency exceeded the P4 limit in this environment, and noisy real-phone accuracy was unverified. Live realtime streaming STT had not been exercised by these five REST-based checks. The fix and current remaining gate are documented below.

#### P4 translation latency investigation (2026-10-10)

The original two `test_translate` cases measure one `await vendors.translate(...)`, starting after imports/fixtures and ending before native-script assertions. The timer includes input/configuration validation, `_headers()`, construction of a fresh HTTPX client (including transport/TLS-context setup), request serialization/pool overhead, DNS plus TCP establishment, TLS handshake, one HTTP POST to `/translate`, response-header wait, response-body transfer, status/JSON/output validation, and client cleanup. Container startup, uv setup, imports and pytest setup are excluded. No transcription, synthesis, audio playback or pipeline invocation occurs. `_post` creates and closes a client on every call; default connection retries are zero, and there are no application retries.

Diagnostics now use HTTPX/httpcore trace events in a test-only client subclass. Trace metadata is discarded, so headers and credentials are not logged. Reported stages are client setup, combined DNS/TCP, TLS, request sending, response-header wait, body transfer, response release, JSON decoding, client cleanup, retry wait, and the remaining local overhead; these reconcile with the outer translation timer. Response-header wait includes network travel plus Sarvam queue/translation work. The client cannot split those components or establish vendor model cold-start time without vendor-side timing. Nothing was removed from the production call or its native-script/1.5-second acceptance checks.

**Command:** `docker compose run --rm --no-deps test uv run --extra test --env-file .env pytest tests/test_vendors_live.py -m live -k test_translate -v -rP --junitxml=data/p4-translation-timings.xml -o junit_logging=all`.

**Result:** **0 passed, 4 failed, 0 skipped, 3 deselected**, in 12.01 seconds. These are the original two translation cases plus two new connection-reuse diagnostic cases. Each diagnostic performs a first request and a reused-connection request; both retain native-script validation and the 1.5-second limit. Six real Sarvam POSTs completed with valid native-script output. All six exceeded the limit. There were no HTTP status errors, authentication failures, timeouts or retries. Only translation live tests ran; TTS and recording accuracy were deselected. Structured measurements are saved in `data/p4-translation-timings.json` and the XML report above.

Production-path measurements, in milliseconds:

| Stage | Hindi to Odia | Odia to Hindi |
|---|---:|---:|
| Client setup | 103.255 | 4.682 |
| DNS + TCP | 122.404 | 30.192 |
| TLS handshake | 64.476 | 31.460 |
| Request sending | 0.376 | 0.380 |
| Response wait (network + vendor work) | 1628.252 | 1776.355 |
| Body transfer | 0.208 | 0.354 |
| Response release | 0.063 | 0.226 |
| JSON decoding | 0.052 | 0.085 |
| Client cleanup | 0.160 | 0.139 |
| Other local overhead | 1.883 | 1.796 |
| Retry wait | 0 | 0 |
| Transcription / synthesis | 0 / 0 (not called) | 0 / 0 (not called) |
| **Overall** | **1921.130** | **1845.669** |

Connection-reuse comparison, in milliseconds:

| Direction / sample | DNS + TCP | TLS | Response wait | Overall |
|---|---:|---:|---:|---:|
| Hindi to Odia, shared client's first request | 39.731 | 101.078 | 1643.965 | 1786.014 |
| Hindi to Odia, reused connection | 0 | 0 | 1674.744 | 1675.729 |
| Odia to Hindi, shared client's first request | 77.210 | 36.702 | 1832.810 | 1948.304 |
| Odia to Hindi, reused connection | 0 | 0 | 1671.124 | 1672.195 |

Shared client construction took 4.212 ms for Hindi-to-Odia and 3.918 ms for Odia-to-Hindi. These setup costs are reported separately from the per-request reuse timers; shared-client cleanup occurs after both samples. The first production construction took 103.255 ms versus approximately 4 ms for subsequent constructions, consistent with process initialization overhead. Connection traces confirm one connection attempt for each fresh request and none for reused requests. One reused sample per direction demonstrates connection reuse, not a statistically established steady-state distribution or vendor cold-start behavior.

**Observed bottleneck:** Response wait dominates; it alone exceeds 1500 ms for every sample. Persistent connections reduce setup overhead but did not meet the threshold, so client setup alone is not the cause. The relative contribution of network travel, vendor queueing and model execution remains unknown. No production optimization was made. The next smallest corrective step is to repeat these same probes from the intended India deployment to separate the local network path from vendor response latency; if response wait still exceeds the budget, investigate it with Sarvam before choosing a translation-path change. Keep the existing model, scripts, accuracy criteria and threshold for that comparison.

Changed files: `tests/vendor_timing.py`, `tests/test_vendor_timing.py`, `tests/test_vendors_live.py`, and this plan. `docker compose run --rm --no-deps test uv run --extra test pytest tests/test_vendor_timing.py -q` passed **2 focused unit tests**, verifying aggregation of successful/failed trace stages, retry counts, credential-safe output, request/response preservation and client cleanup. Application-file hashes match the previous snapshot, including `app/routing.py` and `app/vendors.py`. No P5 work or phone calls occurred. P4 remains blocked on translation latency and the real noisy-phone recordings gate.

#### Verified P4 fix (2026-10-10)

The previous section records the pre-fix investigation. A bounded comparison on the same Docker host found that `mayura:v1` took **2.013 seconds Hindi-to-Odia** and **1.993 seconds Odia-to-Hindi**, while `sarvam-translate:v1` took **1.202 seconds** and **1.180 seconds** respectively. Both models returned native-script output, and the candidate preserved the meaning of the two checkpoint phrases. These four comparison requests used real Sarvam, not fake output. Measurements are saved in `data/p4-model-comparison.json`.

The production fix changes only the translation model to `sarvam-translate:v1` and removes the unsupported `output_script` parameter. Sarvam's [translation API reference](https://docs.sarvam.ai/api-reference/text/translate-text) documents support for both configured languages and specifies that this model does not support transliteration parameters. The existing 1000-character application limit remains; no new model-selection setting, vendor fallback, retries or cache was added. The native-script check, 1.5-second translation threshold, TTS checks and 0.60 word-overlap threshold remain unchanged. No required pipeline stage was removed.

**Final live checkpoint:** `docker compose run --rm --no-deps test uv run --extra test --env-file .env pytest -m live -v -rP --junitxml=data/p4-fixed-live-results.xml -o junit_logging=all` returned **9 passed, 0 failed, 0 skipped, 104 deselected**, in **25.00 seconds**. This is the plan's uv checkpoint inside the Docker test service, with output/report options. The local Sarvam key was configured and never logged. No vendor HTTP status, authentication, timeout or quota errors occurred.

| Translation sample | Hindi to Odia | Odia to Hindi |
|---|---:|---:|
| Production fresh-client overall | **1.462 s** | **1.421 s** |
| Production response wait (network + vendor work) | 1.117 s | 1.115 s |
| Shared client's first request | 1.420 s | 1.232 s |
| Reused-connection request | 1.199 s | 1.193 s |

All translation samples passed the original native-script and latency assertions. Both TTS languages passed. File STT passed across all 20 synthetic recordings, with average literal word overlap **0.94 overall**, **0.90 Odia**, and **0.97 Hindi**. Two new live streaming STT checks sent real PCM audio at real-time pace, checked final transcripts, and observed both speech-start and speech-end callbacks; each had **1.00 word overlap** against its fixture transcript. Streaming uses the existing adapter and endpoint-specific Odia mapping.

The regression command `docker compose run --rm --no-deps test uv run --extra test --env-file .env pytest -q` passed **95 tests and 8 routing subtests**, with 16 live/e2e cases deselected before the two streaming live checks were added. One existing Starlette/httpx deprecation warning remains. Changed application code is confined to `app/vendors.py`; the matching request expectation in `tests/test_vendors.py`, live streaming checks in `tests/test_vendors_live.py`, this plan and `SETUP.md` were updated. Routing, API, pipeline, dependency files, and the 40 fixture files are unchanged. No P5 work or live phone calls were performed.

**Remaining human gate — PENDING (deferred; not a P5 blocker):** Supply at least 10 real noisy-phone recordings per language with exact transcripts, then rerun this same checkpoint. The current synthetic recordings validate integration but cannot establish accuracy on traffic, bike noise or real phone speech. The observed cold-request latency has only about 38 ms headroom in the slower case; one passing run is not a sustained latency guarantee. Verify the same threshold on the intended India deployment and representative speech. P4 remains incomplete solely because real noisy-phone accuracy is not yet verified. The user authorized P5 to proceed without this gate on 2026-10-10.

### P5: Local LiveKit agent (no phones)

Implement `app/agent.py` with `DIAL_MODE=room`, `VENDORS=fake`, and the fixed pair.

**Checkpoint:** `docker compose up -d livekit api agent`, then `docker compose run --rm test` → **9 end-to-end cases pass**: translated audio in both directions with either phone as caller, no raw audio leakage, hangup on either side ending both legs, and unknown caller rejection.

**Implementation (2026-10-10):** `app/agent.py` registers `translator` using the installed LiveKit `AgentServer` API. It authenticates the P2 lookup, joins the receiver room as `translator`, subscribes only to the selected participants' microphone tracks, and reuses P3 `run_call` and P4 streaming STT/translation/TTS. Each side reads STT independently of translation/playback so speech-start callbacks clear that listener's queued TTS and stop remaining or in-flight synthesis output. Caller hangup while waiting for a receiver, either participant leaving, media failure, room disconnect, and job shutdown clean up rooms and media tasks. Unknown callers delete only the dispatched room. Summaries report successful first-frame publications and nearest-rank p50/p95 latency from the last audible input frame; this includes STT endpointing delay. No audio or transcripts are stored.

**Scope at the P5 checkpoint:** P5 implemented `DIAL_MODE=room`; it rejected `DIAL_MODE=sip` until P6 implemented outbound dialing and hold tone. P7 busy/unanswered handling, retry prompts and draining were deferred. No new dependencies or changes to P1–P4 contracts or fixture files.

**Status: Complete (2026-10-10).** Started the existing-image local stack with `docker compose up -d --no-build livekit api agent`; the worker registered as `translator` and `/healthz` returned 200. The prescribed `docker compose run --rm test` checkpoint passed **9 E2E tests, 0 failed, 0 skipped, 128 deselected**, in **73.35 seconds**. The original nine E2E cases and their assertions were unchanged: both configured phones initiating, translated-only audio in both directions, no raw-tone leakage, hangup from either leg, and unknown caller rejection.

Focused commands `uv run --extra test --locked --offline pytest tests/test_agent.py -q` and `docker compose run --rm --no-deps test pytest tests/test_agent.py -q` each passed **24 tests**. Full regressions `uv run --extra test --locked --offline pytest -q` (workspace-local uv cache and `TMP`/`TEMP`) and `docker compose run --rm --no-deps test pytest -q` each passed **119 tests, 8 routing subtests, 18 live/E2E deselected**. Warnings concern the existing Starlette/httpx deprecation and short local LiveKit development secret. An initial Docker `uv run --locked --offline` attempt could not rebuild the editable project because its no-cache image lacks cached setuptools; direct `pytest` in the existing `/opt/venv` passed. No dependencies, lockfile, Docker configuration, P1–P4 implementation, or fixture files changed.

The P4 human gate requiring **at least 10 real noisy-phone recordings per language** remains **PENDING**, explicitly deferred by the user and not a P5 blocker. No Sarvam requests or real phone calls were run during P5. The P6/P7 implementations below supersede P5's temporary SIP rejection and hardening deferrals.

### P6: Real phone calls (HUMAN GATE for phones)

Configure an inbound SIP number and translator dispatch rule, outbound trunk, LiveKit Cloud credentials, `SIP_NUMBER`, `DIAL_MODE=sip`, and `VENDORS=sarvam`. Run the server in India (Mumbai for deployment). There is no session creation step.

For the current test stage, run the API/worker on the user's PC with LiveKit Cloud; no application deployment is required. Mumbai remains a later deployment target. Complete phone validation locally before planning hosted deployment.

**Current provider setup (2026-10-11):** Follow `SETUP.md` section 6. Inbound calls use a TwiML Bin containing `config/sip/twilio-inbound.example.xml`, with digest credentials matching the LiveKit inbound trunk. Twilio Elastic inbound origination does not support those credentials, so keep the US number configured for Programmable Voice; do not attach it to Elastic SIP origination. Outbound calls use an Elastic termination domain and credential list configured on the LiveKit outbound trunk. A Twilio number owned by the same account can be the outbound caller ID. Keep `destinationCountry=in`, the fixed-pair allowlists and the `translator` dispatch rule. `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN` are not application requirements: the worker talks to LiveKit, which authenticates SIP to Twilio.

**Current deployment gate — PENDING:** Set the US `SIP_NUMBER`, cloud `LIVEKIT_URL`/API key/secret, and `LIVEKIT_SIP_OUTBOUND_TRUNK_ID` in `.env`. API and Sarvam secrets and both phones are preserved. The worker intentionally fails preflight until the missing values are configured. No Twilio/LiveKit resources or real calls were created during this provider change. P4's real noisy-phone gate remains pending. The 2026-10-10 paragraphs below describe historical Plivo/local verification, not a completed Twilio deployment.

**Implementation (2026-10-10):** The user selected **Plivo**. `app/agent.py` now supports SIP mode using `ctx.api.sip.create_sip_participant`: it dials the authenticated lookup's `other_no` into room B as `receiver`, sets outbound `sip_number=SIP_NUMBER`, and waits until answer with a 30-second ringing timeout. A generated PCM16 hold tone plays only in room A until the outbound answer/failure; it never forwards SIP early media or original speech. Translation/STT starts only after the receiver joins. Cancellation, caller hangup during ringing, dial/audio failure, or either leg leaving stops hold/media tasks and deletes both rooms. P7 retry/announcement behavior was not part of this checkpoint; its verified implementation is below.

SIP mode requires genuine SIP participants and reads the caller only from `sip.phoneNumber`, ignoring local dispatch caller metadata. Startup validates room/SIP mode, nonblank API/vendor/trunk credentials, a secure LiveKit Cloud URL and non-development credentials, a provider E.164 number distinct from both mobiles, and `VENDORS=sarvam`. Existing room/fake mode stays available for P5 tests.

`config/sip/` now supplies authenticated Twilio inbound TwiML, a LiveKit inbound trunk restricted to the fixed pair, an India-routed outbound trunk whose only caller ID is the US provider number, and a trunk-bound individual-room dispatch rule for `translator`. `SETUP.md` describes matching Twilio/LiveKit credentials and India routing, configuration, Mumbai deployment, rollback and the manual checkpoint. The old `plivo-origination.example.json` is historical only. Configured copies with passwords belong under ignored `data/`; committed examples contain placeholders only.

**Implementation status: complete; deployment/manual gate pending.** Focused commands `uv run --extra test --locked --offline pytest tests/test_agent.py tests/test_sip_config.py -q` and `docker compose run --rm --no-deps test pytest tests/test_agent.py tests/test_sip_config.py -q` each passed **53 tests (50 agent, 3 SIP configuration)**. Coverage includes both caller/language directions, caller ID/trunk/room targeting, answer-gated translation, hold PCM/silence without vendor calls, unknown-caller rejection without dialing, genuine SIP identity requirements, invalid live settings, busy/no-answer cleanup, caller hangup during dialing, and hold failure cancellation.

Full unit regressions `uv run --extra test --locked --offline pytest -q` (workspace-local uv cache and `TMP`/`TEMP`) and `docker compose run --rm --no-deps test pytest -q` each passed **148 tests, 8 routing subtests, 18 live/E2E deselected**. After `docker compose restart agent`, the unchanged P5 checkpoint `docker compose run --rm test` passed **9 E2E tests, 0 failed, 0 skipped, 157 deselected**, in **73.39 seconds**. Existing Starlette/httpx and development-secret warnings remain. No dependencies, lockfile, Dockerfile, Compose settings, P1–P4 implementation or fixtures changed; existing images use the updated source bind mount.

Historical SIP configuration preflight (2026-10-10): `docker compose run --rm --no-deps -e DIAL_MODE=sip test python -c "from app.agent import validate_config; validate_config()"` exited **1**, correctly reporting `ValueError: LIVEKIT_SIP_OUTBOUND_TRUNK_ID is required for SIP calls`. This was an unavailable deployment prerequisite, not a passing live checkpoint. At that time `.env` remained local/fake/room; the current Twilio settings are described above.

**Historical deployment status (2026-10-10) — PENDING:** At that checkpoint `.env` used local LiveKit development credentials and fake/room mode. `SIP_NUMBER` and `LIVEKIT_SIP_OUTBOUND_TRUNK_ID` were blank, and no cloud LiveKit URL/key/secret or Mumbai host was available in this workspace. Sarvam's key was configured; its value was not logged. No cloud trunks were created or changed, no deployment occurred, and no real calls were placed. P6 remains incomplete until the current Twilio/cloud setup and every manual checkbox below are verified. Already-confirmed LiveKit/Sarvam prerequisites do not need reconfirmation. P4's real noisy-phone recordings gate remains **PENDING and deferred**.

**Manual checkpoint:**

- [ ] From +919123260733, dial the US Twilio `SIP_NUMBER` using ISD; +916392306428 rings. Odia speech is heard as Hindi and replies as Odia. No original voice crosses either leg.
- [ ] Repeat with +916392306428 initiating; +919123260733 rings. Languages stay attached to their configured phones.
- [ ] The receiver sees the provider's number, not the other mobile number.
- [ ] After 10 utterances, `call_summary` shows p50 latency below 2000 ms.
- [ ] An unconfigured third caller is dropped.
- [ ] Hangup on either side ends both legs.
- [ ] The inbound SIP participant retains the original Indian `sip.phoneNumber`; wrong inbound SIP credentials cannot join a room.
- [ ] Real busy/no-answer prompts, interruptions and deployed worker draining behave as specified in P7.

### P7: Hardening

Add a focused test per automatable item: busy/unanswered receiver handling; announcements in each phone's language; interruption handling; vendor timeout above 3 seconds with a listener-language wait prompt, one retry, then skipping; worker draining. Keep summaries in logs for this demo.

**Status: implementation and automated checkpoint complete (2026-10-10).** P6 dialing, fixed routing, genuine SIP identity checks, provider caller ID, isolated rooms and the 30-second provider ringing window remain intact. No phone calls, external vendor/cloud API requests, provisioning, deployment or git commands were run during P7. P4 and P6 human gates remain pending.

- [x] Busy responses (SIP 486/600), unanswered responses (408/480), dial deadlines and generic dial failures announce to the caller in Hindi/Odia, stop hold audio and clean both rooms. Connection announcements use each leg's own language. Announcement synthesis is bounded to 3 seconds and playback to 5 seconds; failed prompts are logged without masking the dial failure or recursively retrying prompt TTS.
- [x] Translation and sentence TTS use a 3-second deadline per attempt, including HTTP timeout exceptions. First timeout prompts the listener and retries once; a second timeout skips only that sentence, allowing subsequent sentences to proceed. Wait prompts do not count as translated utterances. STT remains a continuous stream; silence is not treated as a 3-second translation timeout, and an STT failure ends the call safely.
- [x] Listener speech interrupts queued/in-flight TTS and translation, including the timeout/retry path. Closed legs publish no further audio or prompts. Setup checks prevent dialing or starting media after caller/receiver departures during lookup, join or track publication.
- [x] Connection and participant waits are bounded to 30 seconds; the outbound request has a 45-second application deadline around the existing 30-second provider ring window. Hangup and job cancellation terminate setup/media tasks and unregister event handlers.
- [x] Cleanup runs once in a shared shielded task. Cancellation grace is 2 seconds, media/room disconnect operations have 2-second deadlines, and each room deletion gets at most two 2-second attempts. Both rooms and all media close concurrently after task cancellation; one failure cannot skip the other resources. Missing rooms count as success. Permanent failures and slow task cancellation are logged, with exactly one final summary. Remote room deletion remains dependent on LiveKit API availability; permanent deletion failures require checking the logged room IDs.
- [x] Native SDK draining rejects new jobs, waits for active jobs up to 30 seconds and then shuts down processes with a 30-second limit. `docker-compose.yml` gives the agent `stop_grace_period: 75s`. Tests exercise active-job completion, drain timeout and the shared shutdown callback. An idle local Docker SIGTERM check exited normally with code 0 and `OOMKilled=false`; the agent was started again. This does not prove cloud or active real-call draining.

**Regression evidence:** Two media-cleanup regression tests first failed against P6: stream close failure and queue-clear failure both skipped source closure. Both pass after the fix. Focused host command `uv run --extra test --locked --offline pytest tests/test_agent.py tests/test_hardening.py -q` passed **110 tests** (52 agent, 58 hardening).

Full host command `uv run --extra test --locked --offline pytest -q --junitxml=data/p7-host-unit.xml` passed **208 tests and 8 routing subtests**, with 18 live/E2E cases deselected, in **7.20 seconds**. Docker command `docker compose run --rm --no-deps -e VENDORS=fake -e DIAL_MODE=room test pytest -q --junitxml=data/p7-docker-unit.xml` passed the same **208 tests and 8 subtests**, in **10.69 seconds**. Host used workspace-local uv cache and `TMP`/`TEMP`. Existing Starlette/httpx and local short-secret warnings remain.

After checking `.env` was still local/room/fake and recreating the worker with `docker compose up -d --no-build livekit api agent`, `docker compose run --rm -e VENDORS=fake -e DIAL_MODE=room test pytest -m e2e -v --junitxml=data/p7-local-e2e.xml` passed **all nine unchanged local E2E cases**, **0 failed, 0 skipped, 217 deselected**, in **73.45 seconds**. Both caller directions, translated-only audio, both hangup directions and unknown-caller rejection remain verified. `docker compose config --quiet` also passed. Reports are under ignored `data/`.

Changes are confined to `app/agent.py`, its test fixture support/two cleanup regressions in `tests/test_agent.py`, `tests/test_hardening.py`, the Compose shutdown grace period and this plan/setup documentation. P1–P4 implementation, original E2E tests, dependencies, lockfile, Dockerfile, credentials and SIP templates are unchanged. Existing images use the source bind mount; no rebuild was needed.

**Human gates — PENDING:** P4 still requires **at least 10 real noisy-phone recordings per language** and transcript-based accuracy validation. P6 now requires Twilio/LiveKit provisioning, Mumbai deployment and every phone checkpoint above. Real Hindi/Odia announcement audibility, real busy/no-answer codes, deployed draining and sustained phone latency remain unverified. No scope beyond P7 was added.

**Stop condition:** P6 passes with the two configured phones, Odia ↔ Hindi. Nothing beyond P7 without a new plan.

## Deferred

Order/rider integration, leases, virtual-number pools, databases, stored call records, more language pairs, analytics, and platform integrations require a new scope. Audio/transcript storage requires an explicit need and consent.

## Rollback

The pre-change code, tests, documentation and configuration templates are saved in `data/p1-leases-backup.zip`. No existing database files were found or deleted. Restore the archive's files and the old `.env.example` settings if returning to leases; keep the actual `.env` credentials private.
