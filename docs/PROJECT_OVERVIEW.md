# Call Translator — Project Overview

Based on the project-file review on **12 October 2026**. This explains the code as it exists, with completed implementation separated from unverified real-phone behavior.

## 1. What the project does

The project translates a phone conversation between two configured people:

| Party | Phone in examples/tests | Language |
| --- | --- | --- |
| A | `+919123260733` | Bengali (`bn-IN`) |
| B | `+916392306428` | Hindi (`hi-IN`) |

Either person can start a translated call by dialing the provider-owned **`SIP_NUMBER`**. The server calls the other person. Bengali speech becomes Hindi audio, and Hindi replies become Bengali audio.

**The phones stay in separate LiveKit rooms**, so the application sends generated translations without forwarding the other person's original voice. Dialing the other mobile directly makes an ordinary call that bypasses this project.

The current demo has no frontend, mobile app, database, order IDs, session registration, or number-leasing system.

## 2. The main parts

| Part | Responsibility | Source |
| --- | --- | --- |
| FastAPI | Identifies the configured caller and returns the other phone and both languages. | [api.py](app/api.py), [routing.py](app/routing.py) |
| LiveKit worker | Manages the two rooms, outgoing call, audio, interruptions, and cleanup. | [agent.py](app/agent.py) |
| Translation pipeline | Processes sentences in order and runs both conversation directions concurrently. | [pipeline.py](app/pipeline.py) |
| Vendor adapters | Gnani recognizes live speech and generates audio; Sarvam translates text and transcribes files. | [vendors.py](app/vendors.py) |
| Twilio/LiveKit templates | Describe inbound SIP, outbound SIP, and worker dispatch configuration. | [config/sip/](config/sip/) |

The selected provider setup uses **Twilio inbound Programmable Voice/TwiML** and **outbound Elastic SIP Trunking**. SIP connects phone calls to LiveKit. The worker talks to LiveKit APIs; provider credentials and routing are configured outside the application.

The latest project notes describe API and worker running on the user's PC, with LiveKit Cloud handling rooms and SIP. Hosted Mumbai deployment is deferred.

## 3. Call flow

1. **Caller dials `SIP_NUMBER`.** Twilio's inbound SIP route puts the caller in LiveKit room A and dispatches the `translator` worker.
2. **Worker identifies the caller.** In real-call mode, it reads the SIP participant's `sip.phoneNumber`. Local test metadata cannot override that identity.
3. **API checks the caller.** The worker calls authenticated `GET /lookup`. A known caller receives the other phone and both languages. An unknown caller is rejected without an outgoing call.
4. **Worker creates the second side.** It joins room B, named `<room-A-name>-b`, and plays a generated hold tone to the caller.
5. **Other phone rings.** The worker dials through the configured LiveKit outbound trunk, using `SIP_NUMBER` as caller ID. Translation begins only after answer and the receiver joining.
6. **Conversation is translated both ways.** Both phones receive a connection announcement in their own language. Each room receives generated translations of speech from the other room.
7. **Either person hangs up.** The worker stops processing, closes audio resources, disconnects, and attempts to delete both rooms.

Busy, unanswered, or failed dialing produces a best-effort caller-language announcement before cleanup. The worker does not redial automatically.

**Local testing:** `DIAL_MODE=room` uses simulated room participants instead of phone calls. `VENDORS=fake` produces fixed transcripts and tones instead of real AI output.

## 4. Data flow

Each spoken sentence follows this path:

```mermaid
flowchart LR
    A[Speaker audio] --> B[Speech to text]
    B --> C[Final transcript]
    C --> D[Translate text]
    D --> E[Generate speech]
    E --> F[Listener audio]
```

For example, when Party A speaks:

- The worker receives A's microphone audio in room A.
- Gnani recognizes the speech as Bengali text.
- The worker queues the final transcript and its speech-end timestamp.
- Sarvam translates the text into Hindi; Gnani generates Hindi audio.
- The worker publishes that audio in room B for Party B.

The reverse direction uses the same steps with the languages swapped. Each direction preserves sentence order, while both directions run concurrently. Blank and partial transcripts are not translated.

The routing API returns three fields. For the Party A example:

```json
{
  "caller_lang": "bn-IN",
  "other_no": "+916392306428",
  "other_lang": "hi-IN"
}
```

Audio and conversation text remain in application memory during processing. **The application does not save live-call recordings or transcripts.** Worker logs contain room IDs, error categories, and latency summaries. Test fixtures and the local SIP capture are stored separately; external-provider retention settings were not checked.

## 5. Main specifications

### API and AI

| Item | Implemented behavior |
| --- | --- |
| Phone configuration | Exactly two distinct numbers matching `\+\d{10,15}`, with different supported languages. |
| `GET /healthz` | Returns `{"status":"ok"}` without authentication. Checks API liveness, not vendor or call readiness. |
| `GET /lookup?caller=...` | Requires `X-API-Key`; returns the route for an exact configured-number match. |
| API errors | 401 for missing/wrong key; 404 for unknown caller; 422 for missing caller. |
| Speech recognition | Gnani streaming STT for live calls; Sarvam `saaras:v4` for file transcription. |
| Translation | Sarvam `sarvam-translate:v1`; maximum 1000 characters in Sarvam mode. |
| Speech synthesis | Gnani `timbre-v2.5`; Bengali voice `Kirra`, Hindi voice `Nalini`; maximum 2500 characters in live mode. |
| Party A language code | `bn-IN` throughout routing, STT, translation and TTS. |

API/vendor validation accepts 11 language codes: `hi-IN`, `kn-IN`, `ta-IN`, `te-IN`, `ml-IN`, `mr-IN`, `bn-IN`, `gu-IN`, `pa-IN`, `od-IN`, and `en-IN`. Built-in announcements cover Bengali, Hindi and Odia. Party A changed to Bengali on 14 October 2026. Earlier Odia/Hindi fixture results remain historical; Bengali real-phone accuracy and latency require new validation.

### Audio, failures, and shutdown

| Item | Specification |
| --- | --- |
| Worker audio | Mono PCM16; input 16000 Hz, generated output 24000 Hz, 20 ms frames. |
| File transcription | Uncompressed mono PCM16 WAV at 8000/16000 Hz, maximum 30 seconds. |
| Speech endpointing | 500 ms silence and 300 ms minimum speech. |
| Final-transcript queue | Up to 16 items per input leg, in memory. |
| Outgoing call | 30-second ringing window; 45-second application dial deadline. |
| Connection/participant waits | 30 seconds per wait. |
| Translation/sentence TTS timeout | 3 seconds per attempt. First timeout gives a best-effort wait prompt and one retry; second timeout skips the sentence. |
| Other translation errors | Skip the sentence. Non-timeout TTS/playback errors and STT failures end the call. |
| Listener interruption | Clear queued audio and discard stale translation/synthesis results and remaining playback frames. |
| Cleanup | Shared cleanup runs once; 2-second cancellation/media bounds; room deletion retries once. |
| Worker shutdown | 30-second drain and 30-second process-shutdown limits; Compose allows 75 seconds. |

Sarvam errors never switch to fake output or forward original speech. Remote room deletion still depends on LiveKit API availability.

`call_summary` reports translated first-frame publications and p50/p95 latency. It measures from speech end to the first queued generated-audio frame, **not to audible playback on the phone**. Prompts are excluded.

Acceptance targets in the project plan are translation-request time below **1.5 seconds**, fixture word overlap of at least **0.60**, and real-call p50 below **2000 ms** after at least ten utterances. These are validation targets, not guaranteed production performance.

### Configuration and runtime

Settings come from `.env`: the two phone/language pairs, `API_KEY`, `API_URL`, `SIP_NUMBER`, Sarvam credentials, LiveKit URL/credentials, and the outbound LiveKit trunk ID.

- Real phones use `DIAL_MODE=sip` and `VENDORS=sarvam`.
- Simulated tests use `DIAL_MODE=room` and `VENDORS=fake`.
- SIP mode requires a secure LiveKit Cloud URL, non-development credentials, and a provider number distinct from both phones.
- All 15 expected `.env` assignments were nonblank during review. Their presence does not establish valid credentials or working trunks.

The package is version `0.1.0`, requires Python `>=3.11`, and selects Python 3.12 locally. Docker uses Python 3.12 and uv 0.13.0 with locked dependencies. Compose defines API (port 8000), local development LiveKit (port 7880), worker, and an optional test service. Cloud startup instructions avoid starting local LiveKit.

See [.env.example](.env.example), [pyproject.toml](pyproject.toml), [Dockerfile](Dockerfile), [docker-compose.yml](docker-compose.yml), and [SETUP.md](SETUP.md) for exact settings.

## 6. Work completed and what remains

| Work | Current evidence |
| --- | --- |
| Fixed-pair routing and authenticated API | Code and tests are present. |
| Two-direction translation pipeline | Code and tests are present. |
| Fake and Sarvam adapters | Translation, TTS, file STT, and realtime STT are implemented. |
| LiveKit room-mode worker | Isolated audio, interruption handling, summaries, and cleanup are implemented. |
| SIP calling | Outgoing dialing, caller ID, hold tone, SIP identity checks, and Twilio templates are implemented. |
| Hardening | Native prompts, timeout retry, bounded cleanup, and drain configuration are implemented. |

[PLAN.md](PLAN.md) records earlier passes for **208 unit tests plus eight routing subtests**, **nine local E2E tests**, and **nine live vendor checks on synthetic fixtures**. These are historical results, not fresh passes from this review.

The review's unit-test attempt stopped during collection with:

```text
ImportError: DLL load failed while importing sidedata: An Application Control policy has blocked this file.
```

The local SIP capture shows authentication challenge, ringing, answer, and teardown. It does not prove that translated audio reached either phone.

**Still unverified:** noisy real-phone accuracy, complete Twilio/LiveKit provisioning, both real-call directions, translated-only audibility, prompts, interruptions, hangups, active-call draining, and sustained phone latency. No live calls or running services were checked during this review.

## 7. Files reviewed

All **73 tracked project files** were reviewed, along with private configuration and the local SIP capture. Installed third-party dependency files, bytecode, and Git internals were excluded.

| Files | Purpose |
| --- | --- |
| `app/` — six files | Routing, API, pipeline, vendors, worker, and package marker. |
| `tests/` — eleven Python files | Routing/API/pipeline/vendor tests, worker failures and lifecycle, SIP templates, live checks, E2E checks, timing diagnostics. |
| `tests/fixtures/` — forty files | Ten Hindi and ten Odia WAV/transcript pairs. All WAVs were checked as complete 8000 Hz mono PCM16; recordings are synthetic. |
| `config/sip/` — five templates | Twilio inbound, LiveKit inbound/outbound trunks and dispatch, plus a historical Plivo example. |
| Project/configuration files — eight files | `pyproject.toml`, `uv.lock`, `.python-version`, `Dockerfile`, `docker-compose.yml`, `.env.example`, `.gitignore`, `.dockerignore`. |
| [NOTES.md](NOTES.md), [PLAN.md](PLAN.md), [SETUP.md](SETUP.md) | Background, implementation history/status, and setup instructions. |

This document summarizes implementation evidence. It does not mark pending real-phone checks as completed.
