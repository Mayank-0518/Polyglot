# Real-Time Call Translation for Gig Platforms: Project Notes

## Current demo scope (updated 2026-10-11)

The user has narrowed the first implementation to server-mediated translation between +919123260733 (Odia, `od-IN`) and +916392306428 (Hindi, `hi-IN`). Either phone may initiate. The server routes the configured caller to the other configured phone; unknown callers are rejected. No order IDs, rider IDs, session registrations, virtual-number leases or database are needed for this demo. The previous lease implementation is backed up under `data/p1-leases-backup.zip`.

Use a uv-managed Python `.venv` locally and a separate uv environment in Docker. See `SETUP.md` for commands and `PLAN.md` for the current contracts and phase status. Sarvam uses [`od-IN` for Odia](https://docs.sarvam.ai/api/getting-started/models/sarvam-translate).

Real calls still need a telephony ingress: either mobile dials the provider-owned inbound SIP number, and the server dials the other mobile. A direct ordinary call between the two mobiles bypasses this server.

The selected demo provider is now **Twilio with a US voice number**. Indian callers need ISD access to that number. Inbound Programmable Voice uses an authenticated TwiML SIP connection to LiveKit; outbound uses Twilio Elastic SIP Trunking with the same US number as caller ID. Twilio requires non-Indian caller IDs for calls to India. The caller's mobile plan, Twilio call legs, LiveKit and Sarvam have separate charges. `SETUP.md` is the current provisioning guide; Plivo-specific history in `PLAN.md` is not the active setup.

For the current real-phone test, the US number is already procured. FastAPI and the translation worker run on the user's PC; LiveKit Cloud handles SIP and rooms. No application hosting or public FastAPI tunnel is needed. A Mumbai application deployment is deferred. Keep SIP/Sarvam settings for this stage; fake room-mode tests with local LiveKit are a separate option.

## Goal
Translate calls live in both directions between **drivers or delivery partners and customers** on platforms like Uber, Zomato, Swiggy and Rapido. Each person hears the other in their own language, on a normal phone call, with no app install and the regular phone dialer.

**Current example:** the Odia phone calls the server number, which calls the Hindi phone. Odia speech is heard as Hindi, and replies are heard as Odia. The Hindi phone can also initiate.

This is a standalone project, separate from Arrowhead.

### Why this fits gig platforms
- These platforms **already route every rider↔customer call through a server** for number masking (through providers like Exotel and Knowlarity). Translation replaces the step that connects the two calls; nothing new sits in the call path.
- **Both languages are already known** from the rider's app language setting and the customer's profile, so no auto-detection is needed.
- Calls are short and formulaic ("I'm at the gate", "which floor", "leave at the door"), so 1–2 s of lag per sentence is acceptable.

---

## Is it feasible?
Yes, with about **1–2 s of lag after each sentence**. It is not true simultaneous interpretation; it feels like taking turns.

### Where the latency comes from (measured after the speaker stops)
| Step | Time |
|---|---|
| Detecting the end of speech | 300–600 ms ← the biggest cost |
| Final speech-to-text result | 100–300 ms |
| Translation by an LLM / MT (first token) | 200–500 ms |
| Text-to-speech (first audio byte) | 100–300 ms |
| Phone network delay, both ways | 200–400 ms |
| **Total** | **about 1–2 s** |

---

## Architecture

### Core rule: never connect the two call legs directly
Both legs end at **the server**. Each person hears **only** the audio the server sends back on their stream. Raw RTP never flows between the two phones, so neither person hears the other's raw voice.

```
Caller ⇄ RTP ⇄ Telephony provider ⇄ LiveKit (room A) ⇄ SERVER ⇄ LiveKit (room B) ⇄ Telephony provider ⇄ RTP ⇄ Receiver

Inside the server:
  Odia phone audio → [speech-to-text (od) → translate od→hi → text-to-speech (hi)] → Hindi phone
  Hindi phone audio → [speech-to-text (hi) → translate hi→od → text-to-speech (od)] → Odia phone
```

- **Leg A (inbound):** either configured phone dials the provider's server number and joins LiveKit room A.
- **Leg B (outbound):** the server calls the other configured mobile through the outbound SIP trunk and joins it to separate LiveKit room B.
- The server runs one translation pipeline per direction; each phone keeps its configured language regardless of caller/receiver role.
- **Don't** use `<Dial><Number>` to connect the two mobiles, a conference bridge or a carrier three-way merge. Inbound Twilio `<Dial><Sip>` connects only the caller to their isolated LiveKit room; the agent separately creates the other call leg and sends translated audio.

### Call flow
1. Either configured phone dials the provider-owned inbound SIP number.
2. The server checks the caller against the fixed two-phone mapping and gets the other number and both languages. An unknown caller is dropped.
3. The demo always takes the Odia ↔ Hindi translation path, with no order lookup, lease allocation or direct audio bridge.
4. The server dials the other phone, which rings normally.
5. A short announcement on both legs, each in its own language: "This call is translated automatically."
6. Both pipelines run until either side hangs up.

### Self-hosted SIP alternative
Same rule: don't bridge the two channels, and keep the media routed through the server.
- **Asterisk:** `ExternalMedia` or `AudioSocket` per channel; set `directmedia=no`.
- **FreeSWITCH:** `mod_audio_fork` or `uuid_audio_stream` per leg; set `bypass_media=false`.

---

## Technology choices

### Telephony (current two-phone demo)
- Use a voice-capable **US Twilio number**, not an Indian domestic number. Both Indian mobiles remain the participants.
- Use authenticated **Programmable Voice/TwiML inbound SIP** and **Elastic SIP Trunking outbound** with LiveKit. Keep the US number's incoming configuration on TwiML.
- Enable India outbound dialing and use the US number as outbound caller ID. See [Twilio's India voice guidelines](https://www.twilio.com/en-us/guidelines/in/voice).
- Keep LiveKit SIP/media in India and deploy the worker/API in Mumbai. International carrier routing still adds a latency risk and is not a domestic India masking service.
- Plivo/Exotel remain possible future domestic-provider choices; no platform integration is part of this demo.

### Translation pipeline
**Three-step pipeline: speech-to-text → translation → text-to-speech.** Use the real-phone accuracy gate to choose vendors for the current Odia/Hindi pair.
- Candidates: **Sarvam AI** (speech-to-text, translation and text-to-speech built for Indian languages) or **Google** behind the same vendor interface if Sarvam fails the accuracy gate.
- Before committing, test on real **8 kHz phone recordings with street and bike noise**. For riders, accuracy on noisy audio matters more than speed.

---

## Hard problems
1. **Word order:** translations need enough sentence context. Waiting for final speech transcripts limits latency; this demo does not translate partial sentences.
2. **Turn-taking:** people talk over the translated audio. You need rules for interruptions (stop the translated audio when the listener starts talking).
3. **Phone audio and noise:** 8 kHz audio plus traffic and wind. Accuracy suffers most on **addresses, flat numbers, landmarks, names and numbers**, which are exactly what delivery calls are about.
4. **Silence feels like a dropped call:** play hold music on leg A until leg B answers, and a short tone or cue when a translation starts.
5. **Cost:** speech-to-text, translation and text-to-speech run in both directions. This demo processes only calls between its two configured phones.
6. **Privacy and consent:** audio is processed by third-party AI services. This needs the announcement plus a DPDP Act (2023) review of data processing and retention (don't store audio or transcripts by default).

---

## Go-to-market
This is sold B2B; the call path belongs to the platform.
- **Direct to platforms** (Zomato, Swiggy, Uber, Rapido, Porter, Dunzo-like quick commerce): offer it as an API or drop-in replacement for the step that connects their masked calls.
- **Through masking providers** (Exotel, Knowlarity): offer it as an add-on to their existing masking product, so the platforms don't have to integrate anything.

---

## Proposed first version (demo)
- One US Twilio voice number with ISD access from the test phones.
- A fixed two-phone mapping: +919123260733 (`od-IN`) and +916392306428 (`hi-IN`), with no database or IDs.
- Either phone can initiate; keep the two audio legs separate and send translated audio only.
- Three-step pipeline (Sarvam first), starting with Odia ↔ Hindi.
- One Python deployment with a uv environment, routing API and LiveKit agent.
- Test with two real phones, ideally outdoors with traffic noise.

**Deferred:** orders/rider integration, leases, databases, more language pairs, analytics, and integrations with real platforms. Interruption handling belongs to the agent contract and P7 verification in `PLAN.md`.

### Open questions
- Does the real Twilio international route meet the phone latency target?
- Sarvam or Google: decide with a noisy-audio accuracy test.
- First pitch target: one platform directly, or a masking provider?

---

## Ditched: translating ordinary personal calls
Considered and rejected:
- **App that taps into the call:** iOS and Android don't let third-party apps capture or inject cellular call audio.
- **Merging a bot into a three-way call:** works, but both people hear the raw voice plus the translation, roughly doubling the wait.
- **Forward when busy (decline the call, the bot calls you back):** clean audio, but you have to decide before answering, and the callback shows the bot's number.
- **Forwarding all calls through a server:** you pay for every minute of every call and need your own calling app.
- **For personal use, the built-in features** (Samsung Live Translate, Pixel Voice Translate, iOS 26 Live Translation) already cover this, if they support the language pair.
