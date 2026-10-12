# Setup on Windows

Use **uv** for a project-local Python 3.12 `.venv`. Use **Docker Desktop** for the local LiveKit server and optional containerized API/agent/tests. Docker builds its own Linux uv environment at `/opt/venv`; it never uses the Windows `.venv`.

`PLAN.md` describes the phases and contracts. `NOTES.md` explains the architecture. The current demo has exactly two configured phones, Odia ↔ Hindi, with either phone able to initiate. There are no order/session registrations or leases.

**Provider update (2026-10-11):** Use a US Twilio voice number with LiveKit Cloud. `.env` and `.env.example` now target `DIAL_MODE=sip` and `VENDORS=sarvam`, with missing cloud/provider values left blank. Fill them before starting services. Existing API/Sarvam secrets and the phone pair are preserved; the previous local `.env` is backed up under ignored `data/`. Application logic is unchanged. Follow section 6 for the current setup; dated local verification below is historical.

**Current test stage:** The US Twilio number has been procured. Run FastAPI and the translation worker on this PC using Docker; LiveKit Cloud supplies public SIP and rooms. No hosted application server, FastAPI tunnel or inbound port forwarding is required. Section 4's local LiveKit/fake room tests are a separate checkpoint. Use section 6's cloud/SIP settings for the current real-phone test, and keep the PC running and connected throughout calls. Hosted Mumbai deployment is deferred.

## 1. Install tools

Install uv following the [official Windows instructions](https://docs.astral.sh/uv/getting-started/installation/), then open a new PowerShell terminal and check:

```powershell
uv --version
```

For Docker, install WSL 2 (`wsl --install` in Administrator PowerShell, then restart) and Docker Desktop with its WSL 2 backend. Start Docker Desktop and verify:

```powershell
docker --version
docker compose version
```

An editor and Git are optional. Windows must support Docker Desktop; enable hardware virtualization if WSL reports it is disabled.

## 2. Prepare the project

Run these commands in the folder containing `pyproject.toml` and `docker-compose.yml` (in this checkout it is the inner `call-translator` folder):

```powershell
cd .\call-translator
uv sync --extra test
```

uv creates `.venv`, installs Python 3.12 if needed, installs the project and test dependencies, and generates `uv.lock`. Keep the lockfile in version control once generated. See [uv project environments](https://docs.astral.sh/uv/guides/projects/).

Use `uv run --extra test ...` to run commands without manual activation. If desired, activate with `.\.venv\Scripts\Activate.ps1`.

Only if `.env` does not already exist:

```powershell
Copy-Item .env.example .env
```

Keep the generated random `API_KEY` in the current `.env`. A fresh copy of the example needs `API_KEY=change-me` replaced with a strong random value. Never commit or share `.env`.

Verified on 2026-10-10: uv 0.13.0 created the Python 3.12.10 `.venv`; all 81 Windows project/test packages are installed. `uv sync --extra test`, offline locked sync, and `uv pip check` pass. `uv.lock` is generated, and Docker builds now use `uv sync --locked`. The five routing tests and eight subtests pass in both the project `.venv` and Docker.

### Workarounds used in this checkout

The current terminal does not have uv on PATH and cannot write its default user cache. Use workspace-local directories:

```powershell
$uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
$env:UV_CACHE_DIR = Join-Path (Get-Location).Path 'data\uv-cache'
$env:UV_PYTHON_INSTALL_DIR = Join-Path (Get-Location).Path 'data\uv-python'
$env:UV_LINK_MODE = 'copy'
& $uv sync --extra test --locked --offline
& $uv run --extra test --offline pytest tests/test_routing.py -q
```

After editing `pyproject.toml`, uv may need to rebuild the editable project. If offline build requirements are missing from its cache, use the retained local wheels:

```powershell
& $uv run --extra test --offline --find-links data/windows-wheels pytest -q
```

If pytest cannot access its default Windows temporary directory, set a workspace-local temporary directory before running tests:

```powershell
$testTemp = Join-Path (Get-Location).Path ('data\test-temp-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testTemp | Out-Null
$env:TMP = $testTemp
$env:TEMP = $testTemp
```

Host package downloads were denied, while Docker build downloads worked. Windows wheels matching the lockfile were downloaded through Docker and retained under `data/windows-wheels`. If the local environment must be restored without host network access:

```powershell
& $uv pip install --python .\.venv\Scripts\python.exe --offline --no-index --find-links .\data\windows-wheels --requirement .\data\requirements-windows.txt --editable .
& $uv sync --extra test --locked --offline
```

Windows Application Control blocked a PyAV DLL when importing `livekit.agents` during initial setup. During P5, the host `app.agent` import and all mocked agent tests passed. Docker remains the verified environment for the local LiveKit checkpoint.

## 3. Configuration

| Variable | Local work | Real phone calls |
|---|---|---|
| `API_KEY` | Random secret | Same strong secret shared by API and agent |
| `PARTY_A_NO` | `+919123260733` | Same |
| `PARTY_A_LANG` | `od-IN` (Odia) | Same |
| `PARTY_B_NO` | `+916392306428` | Same |
| `PARTY_B_LANG` | `hi-IN` (Hindi) | Same |
| `SIP_NUMBER` | Empty | Your Twilio-owned voice-capable US `+1` number |
| `API_URL` | `http://api:8000` inside Docker | API address reachable by the agent |
| `VENDORS` | `fake` | `sarvam` |
| `SARVAM_API_KEY` | Empty except for P4 | Your key |
| `DIAL_MODE` | `room` | `sip` |
| `LIVEKIT_URL` | `ws://livekit:7880` inside Docker | `wss://<project>.livekit.cloud` |
| `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | `devkey` / `secret` | Cloud project credentials |
| `LIVEKIT_SIP_OUTBOUND_TRUNK_ID` | Empty | Your outbound trunk ID |

For an API/agent run on the Windows host, use `API_URL=http://127.0.0.1:8000` and `LIVEKIT_URL=ws://127.0.0.1:7880` when LiveKit runs locally. Compose service names resolve only inside Docker. Restart services after changing configuration.

The runtime needs no `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, or new provider flag. LiveKit stores the outbound SIP domain and credentials. Configure inbound SIP credentials in the TwiML Bin and LiveKit inbound trunk. Store configured copies privately under ignored `data/`, not in committed templates.

Both numbers are participants, not server inbound numbers. To test real server-mediated calls, either phone dials `SIP_NUMBER`. Dialing the other mobile directly makes an ordinary call without this translation server.

## 4. Tests and services

| Check | Command |
|---|---|
| P1 routing | `uv run --extra test pytest tests/test_routing.py` |
| P2 API | `uv run --extra test pytest tests/test_api.py` |
| P3 pipeline | `uv run --extra test pytest tests/test_pipeline.py` |
| P4 fake/mocked vendors | `uv run --extra test pytest tests/test_vendors.py` |
| P5 agent unit tests | `uv run --extra test pytest tests/test_agent.py` |
| P6 mocked SIP and config checks | `uv run --extra test pytest tests/test_agent.py tests/test_sip_config.py` |
| P7 failure/lifecycle/drain checks | `uv run --extra test pytest tests/test_agent.py tests/test_hardening.py` |
| P5 local LiveKit E2E | `docker compose up -d livekit api agent`, then `docker compose run --rm test` |
| Unit tests once P2/P3 exist | `uv run --extra test pytest` |
| P4 real vendors | `uv run --extra test --env-file .env pytest -m live -v` |
| Dependency-free routing check | `python -m unittest discover -s tests -p test_routing.py -v` |

P1–P4 vendor code and the P5–P7 agent are implemented: five routing tests (plus eight subtests), 28 API tests, eight pipeline tests, 52 fake/mocked vendor tests, two timing-diagnostic unit tests, 52 agent unit tests, three SIP configuration tests and 58 hardening tests. The latest full regression reports **208 passed and 8 subtests passed** on host and Docker, with 18 live/e2e cases deselected. All nine unchanged local E2E cases also pass. Pytest discovers only `tests/`, excluding the workspace dependency cache. Live/e2e tests remain opt-in; they require credentials or running services. P4's nine live checks previously passed with synthetic recordings; P7 did not rerun live tests. The human gate requiring at least 10 real noisy-phone recordings per language remains **pending and deferred**, as authorized on 2026-10-10; it does not block this implementation. P6 deployment and real-phone checks remain pending.

For Docker:

Before starting any local stack or E2E checkpoint below, restore local settings: `DIAL_MODE=room`, `VENDORS=fake`, `LIVEKIT_URL=ws://livekit:7880`, `LIVEKIT_API_KEY=devkey`, `LIVEKIT_API_SECRET=secret`, and `API_URL=http://api:8000`. Alternatively restore the saved `.env.before-twilio-*.bak` from `data/`. Keep a separate private copy of filled live settings. Changing the test container's environment alone does not change the worker's configuration.

```powershell
docker compose build
docker compose run --rm --no-deps test pytest tests/test_routing.py
```

In this OneDrive checkout, `Dockerfile` and project metadata have reparse attributes, and direct builds fail with `invalid file request Dockerfile`. Identical source files were staged in a local temporary directory; the build override is `data/docker-build.local.yml`. The staged files exclude credentials, caches, and the Windows `.venv`.

```powershell
docker compose -f docker-compose.yml -f data/docker-build.local.yml build api agent test
docker compose -f docker-compose.yml -f data/docker-build.local.yml up -d --no-build livekit api agent
```

The override points to a source snapshot in the directory recorded in `data/build-stage-path.txt`. Refresh that snapshot before rebuilding after source/lockfile changes; temporary directories may later be cleaned by Windows. Moving the checkout outside OneDrive avoids needing this build workaround. Runtime code still mounts from the original project folder.

All three images rebuilt successfully during setup, P3, and P4. P5 uses the same locked dependencies and existing images through the source bind mount; no dependency or Dockerfile changes require a rebuild. Refresh the staged source snapshot before any future rebuild so it includes `app/agent.py`. LiveKit is running; its HTTP endpoint returned `OK` during setup. The API runs using the source bind mount and `http://127.0.0.1:8000/healthz` returns 200 with `{"status":"ok"}`; this health check passed again during P5. Both routing directions and rejection responses were verified during P2. The agent now runs and registers as `translator`. No real phone calls were run.

To start only the API:

```powershell
docker compose up -d --no-build --no-deps api
curl.exe --fail --silent --show-error http://127.0.0.1:8000/healthz
```

For P5, keep `DIAL_MODE=room` and `VENDORS=fake` in `.env`, then start the local stack:

```powershell
docker compose up -d livekit api agent
docker compose run --rm test
docker compose logs -f agent
docker compose down
```

Code edits apply through the bind mount. Rebuild after changing `Dockerfile`, `pyproject.toml`, or `uv.lock`. Linux packages live at `/opt/venv` so the Windows mount cannot replace them; see [uv in Docker](https://docs.astral.sh/uv/guides/integration/docker/).

Restart the agent after source changes with `docker compose restart agent`; the `start` command does not hot-reload. The E2E runner explicitly dispatches `translator` with local caller metadata, joins `caller` to room A and `receiver` to room A's `-b` room, and verifies the existing nine audio/rejection/hangup cases. The worker reads only those participants' microphones and publishes translated TTS across rooms. Speech-start events clear the listener's queued output and discard remaining frames or synthesis that was in flight. Agent logs include `call_summary call_id=<room_a> utterances=N p50_ms=... p95_ms=...` from the last audible input frame to the first queued TTS frame, including endpointing delay. No recordings or transcripts are stored.

Verified P5 checkpoint on 2026-10-10: `docker compose up -d --no-build livekit api agent`, then `docker compose run --rm test` passed **all nine original E2E cases** in 73.35 seconds, with no failures or skips. API health returned 200 and the agent registered as `translator`. Host and Docker unit regressions each passed **119 tests and eight routing subtests**. The local LiveKit `secret` development credential produces a short-HMAC-key warning; keep the cloud credentials for P6 separate from these development values. P5 used fake vendors and made no real phone calls or Sarvam requests.

P6 implements `DIAL_MODE=sip`, including outbound dialing and a caller-only hold tone. Complete the cloud/Twilio setup below before starting the live worker. P7 adds bounded retries, native-language prompts, lifecycle cleanup and worker draining; see the hardening section below.

For the host API after P2:

```powershell
uv run --env-file .env uvicorn app.api:app --host 127.0.0.1 --port 8000
curl.exe -s http://127.0.0.1:8000/healthz
```

## 5. P4 recordings

The vendor implementation uses Sarvam [REST translation](https://docs.sarvam.ai/api-reference/text/translate-text), [REST TTS](https://docs.sarvam.ai/api-reference/text-to-speech/convert), [file STT](https://docs.sarvam.ai/api-reference/speech-to-text/transcribe), and [realtime STT](https://docs.sarvam.ai/api/api-guides-tutorials/speech-to-text/realtime-streaming). Models are `sarvam-translate:v1` for native-script translation (application limit remains 1000 characters), `bulbul:v3` with voice `shubh` for PCM16 TTS (maximum 2500 characters), and `saaras:v4` for transcription. The translation request omits `output_script`, which this model does not support. Realtime STT uses `or-IN` for Odia; the adapter maps from `od-IN`, leaving phone configuration and REST language codes unchanged.

`VENDORS=fake` requires no Sarvam key or network. It provides deterministic STT, translation markers, and test tones. Sarvam failures raise errors; they never fall back to fake output or original speech. P7 applies translation/TTS deadlines, one retry and bounded wait prompts in the worker, without changing the vendor adapter contracts.

Latest recorded live checkpoint (2026-10-10): **9 passed, 0 failed, 0 skipped**, using the configured local Sarvam key and the 20 synthetic speech fixtures. Both translation directions pass the unchanged 1.5-second limit, both TTS languages pass, file-STT word overlap averages 0.94, and streaming STT passes for Odia and Hindi. Reports and the model comparison are recorded in `PLAN.md`. This does not complete the real noisy-phone recordings human gate. Keep credentials out of chat. Live tests explicitly select Sarvam; `.env` used `VENDORS=fake` at that historical checkpoint.

Record at least 10 real noisy-phone clips per language, Odia and Hindi. Save each as PCM16 8 kHz mono WAV, no longer than 30 seconds, with an exact native-script transcript in UTF-8:

```
tests\fixtures\od-IN_01.wav + od-IN_01.txt
tests\fixtures\hi-IN_01.wav + hi-IN_01.txt
```

Then run the P4 checkpoint:

```powershell
uv run --extra test --env-file .env pytest -m live -v
# Docker alternative (loads .env through Compose):
docker compose run --rm --no-deps test pytest -m live -v
```

Both translation directions, both TTS languages, and recorded-audio accuracy must pass. The required average word overlap is at least 0.6. The latest run verifies credentials, connectivity, streaming, synthetic-clip transcription and translation latency for these samples. Real noisy-phone accuracy and sustained deployment latency remain unverified; do not treat synthetic or mocked tests as that evidence.

Use phrases about gates, floors, flat numbers and landmarks. Convert recordings using ffmpeg in Docker if needed:

```powershell
docker run --rm -v "${PWD}:/w" -w /w jrottenberg/ffmpeg -i input.m4a -ar 8000 -ac 1 tests/fixtures/od-IN_01.wav
```

## 6. P6 real calls

The user selected **Twilio** on 2026-10-11, replacing the original Plivo deployment plan. SIP code is implemented; Twilio/LiveKit provisioning, Mumbai deployment and real phone checks remain pending. `.env` is prepared for live SIP/Sarvam but cannot pass preflight until the missing values are filled. The P4 real noisy-phone recordings gate remains pending and deferred.

### Prepare Twilio and LiveKit

1. Use an upgraded, funded Twilio account and purchase a **voice-capable US local number**. Set it as `SIP_NUMBER` in E.164 form (`+1...`). Both Indian test phones need ISD access to this number; confirm your mobile plan covers the destination. Enable India in **Elastic SIP Trunking geographic permissions**. Twilio permits calls to India only with a non-Indian caller ID; never substitute either test mobile as outbound caller ID. See [Twilio India guidelines](https://www.twilio.com/en-us/guidelines/in/voice) and [SIP geographic permissions](https://www.twilio.com/docs/sip-trunking/voice-dialing-geographic-permissions).
2. In LiveKit Cloud, obtain the project's `wss://...livekit.cloud` URL, API key/secret and SIP subdomain. Keep the already-confirmed India pinning. Inbound SIP will target `<LIVEKIT_SIP_SUBDOMAIN>.india.sip.livekit.cloud`; the WebSocket URL is a different value. Set `destinationCountry=in` on the outbound LiveKit trunk and deploy the API/agent in Mumbai. See [LiveKit region pinning](https://docs.livekit.io/telephony/features/region-pinning/).
3. Choose a dedicated **inbound SIP username/password**. Create a TwiML Bin from `config/sip/twilio-inbound.example.xml`; replace all placeholders and use the same credentials in the LiveKit inbound trunk. For the Twilio US number, configure **A call comes in** to use that TwiML Bin. Omit `callerId` on `<Dial>` so the original Indian caller number reaches LiveKit. The SIP address must include the US number: `sip:+1...@<LIVEKIT_SIP_SUBDOMAIN>.india.sip.livekit.cloud;transport=tcp`. `<Dial><Sip>` connects only the inbound leg to its room; never dial the other mobile from TwiML or use a conference. See [LiveKit authenticated Twilio inbound setup](https://docs.livekit.io/telephony/accepting-calls/inbound-twilio/).
4. Create an **outbound-only Twilio Elastic SIP trunk**. Set a termination domain such as `call-translator.pstn.twilio.com` and attach a Credential List with a separate **outbound SIP username/password**. Use that domain and those credentials in the LiveKit outbound trunk. Keep the US number on Programmable Voice/TwiML; **do not attach it to Elastic SIP origination**, which would replace the incoming TwiML route. The outbound caller ID can be a Twilio number owned by the same account without configuring Elastic origination. See [Twilio termination caller-ID requirements](https://www.twilio.com/docs/sip-trunking#allowed-caller-id-numbers-in-termination-calls) and [LiveKit Twilio trunk setup](https://docs.livekit.io/telephony/start/providers/twilio/).

Twilio Elastic SIP **inbound** origination does not support username/password authentication. This demo uses authenticated Programmable Voice/TwiML inbound instead, preserving provider authentication without adding an application webhook or requiring LiveKit IP-allowlist enablement. See [LiveKit inbound authentication](https://docs.livekit.io/telephony/accepting-calls/inbound-trunk/). Keep call recording disabled. A Twilio trial account has destination restrictions; the upgraded account avoids relying on trial verification.

Copy the supplied examples to the ignored data directory:

```powershell
New-Item -ItemType Directory -Path data/p6-sip -Force | Out-Null
Copy-Item config/sip/inbound-trunk.example.json data/p6-sip/inbound-trunk.json
Copy-Item config/sip/outbound-trunk.example.json data/p6-sip/outbound-trunk.json
Copy-Item config/sip/dispatch-rule.example.json data/p6-sip/dispatch-rule.json
Copy-Item config/sip/twilio-inbound.example.xml data/p6-sip/twilio-inbound.xml
```

Replace every `<...>` placeholder in these copies. XML placeholders are escaped as `&lt;...&gt;`; replace the whole escaped token with its value and XML-escape special characters in passwords. Paste the completed XML into a private TwiML Bin. Set both LiveKit trunks' `numbers` to the owned US number, and set the outbound `address` to the exact Twilio termination hostname, without `sip:` or transport suffixes. Keep inbound `allowedNumbers` and dispatch `inboundNumbers` limited to the two configured mobiles. Preserve individual-room dispatch and `roomConfig.agents[0].agentName=translator`; there must be no automatic second dispatch to room B. If trunks already exist, update their settings and reuse IDs rather than creating duplicates. The old Plivo origination example is historical and must not be used for this setup.

After connecting the LiveKit CLI to the cloud project, create missing resources in order:

```powershell
lk sip inbound create data/p6-sip/inbound-trunk.json
# Put the returned inbound LiveKit trunk ID into dispatch-rule.json's trunkIds.
lk sip outbound create data/p6-sip/outbound-trunk.json
lk sip dispatch create data/p6-sip/dispatch-rule.json
```

Save the returned outbound **LiveKit** trunk ID as `LIVEKIT_SIP_OUTBOUND_TRUNK_ID`; a Twilio `TK...` trunk SID cannot be used here. The dispatch rule's trunk ID is the inbound LiveKit trunk ID. Alternatively create these resources through the LiveKit dashboard using the corresponding trunk/rule JSON fields. The templates are checked against the installed protobuf schemas; the [`lk` trunk/dispatch setup](https://docs.livekit.io/telephony/start/sip-trunk-setup/) explains provisioning. No resources were provisioned during this provider change.

### Configure and run on your PC

On this PC, use Docker Desktop and the existing project. Fill the private `.env` with the existing two phone/language mappings, preserved strong `API_KEY`, `API_URL=http://api:8000`, US Twilio `SIP_NUMBER`, cloud `LIVEKIT_URL` (`wss://...livekit.cloud`), cloud `LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET`, outbound LiveKit trunk ID, and preserved `SARVAM_API_KEY`. `DIAL_MODE=sip` and `VENDORS=sarvam` are already set. Cloud/provider placeholders remain blank until supplied; do not restore development `devkey`/`secret` credentials for live calls. Configured copies and deployment secrets stay outside version control. FastAPI stays local; the worker registers with LiveKit Cloud through outbound connections. No new HTTP webhook or public API URL is required. For a future Mumbai deployment, copy this project with its lockfile and the private live configuration to that host.

Check the deployed configuration before starting the worker:

```powershell
uv run --extra test --env-file .env python -c "import os; from app.agent import validate_config; assert os.environ.get('DIAL_MODE') == 'sip', 'Set DIAL_MODE=sip'; validate_config(); print('SIP configuration OK')"
docker compose build api agent
docker compose up -d --no-deps api agent
curl.exe --fail --silent --show-error http://127.0.0.1:8000/healthz
docker compose logs -f agent
```

Use the staged build workaround above only if building from this OneDrive checkout; refresh the snapshot with the updated worker and `config/sip/`. An ordinary server checkout can build directly. The `--no-deps` startup keeps the local development LiveKit service out of a cloud deployment. Compose mounts source code and loads `.env`; restart/recreate API and agent after configuration changes. Protect the API endpoint with the deployment firewall; only the agent needs access to `/lookup`.

The worker validates SIP settings before registration and checks again for each call. It accepts genuine SIP callers only, reads `sip.phoneNumber`, performs authenticated lookup, joins room B, publishes caller hold tone in room A and dials the other phone with a 30-second ring window. It uses [`wait_until_answered`](https://docs.livekit.io/telephony/making-calls/outbound-calls/) before starting STT/translation. Hold output stops on answer, failure or caller hangup. P7 announces busy/no-answer/failure to the caller in that phone's language before cleanup; it never redials automatically. Raw audio and outbound SIP early media never cross rooms.

Implementation verification on 2026-10-10: host and Docker focused checks passed **53 tests**; full unit regressions passed **148 tests and eight routing subtests**. After restarting the worker, `docker compose run --rm test` passed **all nine unchanged P5 E2E cases** in 73.39 seconds. A SIP preflight against the current local environment correctly failed with `LIVEKIT_SIP_OUTBOUND_TRUNK_ID is required for SIP calls`; cloud credentials and the provider number are also absent. These checks made no SIP or Sarvam calls and do not mark the P6 manual gate complete.

### Manual checkpoint and rollback

From +919123260733, call the US `SIP_NUMBER` using ISD; +916392306428 must ring and see the US number. Verify inbound `sip.phoneNumber` retains the original Indian caller and that incorrect inbound credentials are rejected. Check the hold tone, Odia-to-Hindi translation and Hindi-to-Odia replies. Hang up, then repeat with +916392306428 initiating. Check both hangup directions, an unconfigured third caller, interruptions, busy/no-answer prompts and deployed worker draining. After at least 10 utterances, record `call_summary` and verify p50 below 2000 ms; also judge audible lag because the metric ends at audio publication. Complete every checkbox in `PLAN.md`; mocked SIP and local LiveKit checks do not satisfy these phone checks. No session, order, rider ID or database setup is needed. The mobile ISD plan covers only the caller's international call; Twilio inbound/SIP/outbound usage, number rental, LiveKit and Sarvam are billed separately.

To roll back application mode, stop the cloud agent, restore the private local configuration (`DIAL_MODE=room`, `VENDORS=fake`, local LiveKit URL and development keys), and start the local stack. Leave shared provider/cloud trunks untouched. Disable only the translator dispatch rule you created if withdrawing the demo; do not delete existing provider numbers or shared trunks.

## 7. P7 hardening

P7 implementation and automated verification are complete. For local checks, use the room/fake settings from section 4. The current live `.env` must not be used to start the worker until all P6 deployment prerequisites are configured. Native connection prompts run only in SIP mode, preserving the original local audio checkpoint.

After SIP answer, each phone receives a connection announcement in its configured language. Busy SIP responses (486/600), unanswered responses (408/480), dial timeouts and other dialing failures receive caller-language prompts, then both rooms close. The provider ringing timeout remains 30 seconds; an application deadline limits the outbound request to 45 seconds, and connection/participant waits to 30 seconds. Setup hangups prevent later dialing or media startup.

Each translation or sentence TTS attempt has a 3-second deadline. A first timeout triggers a wait prompt in the listener's language and one retry of that operation. A second timeout skips the sentence; subsequent sentences continue. HTTP timeout errors follow the same policy. Prompt synthesis has a separate 3-second bound and playback a 5-second bound; failed prompts are logged and do not recursively retry or mask call failures. If TTS itself is unavailable, the wait prompt is best effort. Streaming STT is not subject to an idle 3-second deadline; STT failures end the call and clean up. Original speech and fake vendor output are never fallbacks for a Sarvam error.

Listener speech clears queued audio and discards translation/synthesis already in flight, including during a retry. Cleanup is shared between the entrypoint and shutdown callback and runs once despite cancellation or duplicate callbacks. It unregisters handlers, gives tasks 2 seconds to cancel, closes media/disconnects rooms with 2-second operation deadlines and deletes both rooms concurrently, retrying each deletion once. Already-deleted rooms are successful cleanup. Failed closure cannot skip other resources. Permanent deletion failures log the room ID for investigation; remote deletion depends on LiveKit API availability. Summaries remain logs only, appear once per call and exclude prompts from utterance/latency counts.

The worker uses the installed SDK's graceful draining in `start` mode. SIGTERM stops new jobs, allows active calls up to 30 seconds to finish, then gives processes 30 seconds to shut down. Compose's agent `stop_grace_period` is 75 seconds. Recreate the container after changing this Compose setting; restarting alone does not apply it:

```powershell
# First ensure .env uses only local LiveKit, room mode and fake vendors.
docker compose config --quiet
docker compose up -d --no-build livekit api agent
docker compose run --rm --no-deps -e VENDORS=fake -e DIAL_MODE=room test pytest -q
docker compose run --rm -e VENDORS=fake -e DIAL_MODE=room test pytest -m e2e -v
# This stops the local worker gracefully; start it again when finished.
docker compose stop agent
docker compose up -d --no-build --no-deps agent
```

The runner's `-e` flags configure only the test container; the running agent must also use local/fake settings. No new dependencies or rebuild are required for P7; source remains mounted from the checkout. Do not force-kill the worker if you want draining and cleanup to finish.

Verified on 2026-10-10: focused host checks passed **110 tests**; full host and Docker regressions each passed **208 tests and eight routing subtests** (18 live/E2E deselected), in **7.20 seconds** and **10.69 seconds** respectively. The unchanged nine local E2E tests passed in **73.45 seconds**, with no failures or skips. Compose validation passed. An idle local worker stopped on SIGTERM with **exit code 0**, `OOMKilled=false`, and was started again. Active-job draining, timeout and shutdown cleanup were verified through SDK/mocked tests; cloud and real-call draining remain unverified. JUnit reports are `data/p7-host-unit.xml`, `data/p7-docker-unit.xml` and `data/p7-local-e2e.xml`.

P7 made no real calls, Sarvam/LiveKit Cloud requests, deployment/provisioning changes or git commands. The P4 gate requiring **at least 10 real noisy-phone recordings per language** remains **PENDING and deferred**. Twilio/LiveKit provisioning, Mumbai deployment, real Hindi/Odia announcement audibility and all P6 manual phone checks remain **PENDING**. Local fake tones and mocked SIP responses do not establish those results.

## Troubleshooting

- `uv` not found: finish its installation and reopen PowerShell.
- Download failures or Docker permission denied: environment permissions must permit package downloads and Docker engine access before these checkpoints can run.
- Docker daemon unavailable: start Docker Desktop and wait for the engine.
- `.env` missing: copy the example once and configure the API key.
- Port conflicts: adjust the host side of the Compose mapping or stop the conflicting service.
- Agent cannot resolve `api` or `livekit` when run on Windows: use the host URLs described above.
- Slow bind mounts/file locks: OneDrive synchronization can interfere; prefer an unsynced local project folder.
- E2E timeout: check agent/LiveKit logs and ensure fake vendors and room dialing.
- India SIP failures: confirm region pinning and provider routing with the providers.
