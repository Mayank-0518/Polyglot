import array
import asyncio
import json
import logging
import math
import os
import re
import sys
import time
from contextlib import suppress
from functools import partial
from urllib.parse import urlsplit

import httpx
from livekit import api, rtc
from livekit.agents import AgentServer, AutoSubscribe, JobContext, cli, utils

from app import vendors
from app.pipeline import run_call

logger = logging.getLogger("translator")
VENDOR_TIMEOUT = 3
CLEANUP_TIMEOUT = 2
PARTICIPANT_TIMEOUT = 30
DIAL_TIMEOUT = 45
server = AgentServer(num_idle_processes=1, drain_timeout=30, shutdown_process_timeout=30)
PROMPTS = {
    "hi-IN": {
        "connected": "कॉल जुड़ गई है। आपको केवल अनुवाद सुनाई देगा।",
        "busy": "दूसरा फ़ोन व्यस्त है। कृपया बाद में कॉल करें।",
        "unanswered": "दूसरे फ़ोन ने जवाब नहीं दिया। कृपया बाद में कॉल करें।",
        "failed": "कॉल नहीं जुड़ सकी। कृपया बाद में कॉल करें।",
        "wait": "कृपया प्रतीक्षा करें। अनुवाद में समय लग रहा है।",
    },
    "od-IN": {
        "connected": "କଲ୍ ସଂଯୋଗ ହୋଇଛି। ଆପଣ କେବଳ ଅନୁବାଦ ଶୁଣିବେ।",
        "busy": "ଅନ୍ୟ ଫୋନ୍ ବ୍ୟସ୍ତ ଅଛି। ଦୟାକରି ପରେ କଲ୍ କରନ୍ତୁ।",
        "unanswered": "ଅନ୍ୟ ଫୋନ୍ ଉତ୍ତର ଦେଲା ନାହିଁ। ଦୟାକରି ପରେ କଲ୍ କରନ୍ତୁ।",
        "failed": "କଲ୍ ସଂଯୋଗ ହୋଇପାରିଲା ନାହିଁ। ଦୟାକରି ପରେ କଲ୍ କରନ୍ତୁ।",
        "wait": "ଦୟାକରି ଅପେକ୍ଷା କରନ୍ତୁ। ଅନୁବାଦ ପାଇଁ ସମୟ ଲାଗୁଛି।",
    },
}


def validate_config():
    mode = os.getenv("DIAL_MODE", "room")
    if mode not in {"room", "sip"}:
        raise ValueError("DIAL_MODE must be room or sip")
    if mode == "room":
        return
    for key in ("API_KEY", "API_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
                "LIVEKIT_SIP_OUTBOUND_TRUNK_ID", "SARVAM_API_KEY"):
        if not os.getenv(key, "").strip():
            raise ValueError(f"{key} is required for SIP calls")
    url = urlsplit(os.getenv("LIVEKIT_URL", ""))
    if url.scheme != "wss" or not (url.hostname or "").endswith(".livekit.cloud"):
        raise ValueError("SIP calls require a LiveKit Cloud wss URL")
    if os.environ["LIVEKIT_API_KEY"] == "devkey" or os.environ["LIVEKIT_API_SECRET"] == "secret":
        raise ValueError("SIP calls require LiveKit Cloud credentials")
    number = os.getenv("SIP_NUMBER", "")
    if re.fullmatch(r"\+\d{10,15}", number, re.ASCII) is None:
        raise ValueError("SIP_NUMBER must use E.164 format")
    if number in {os.getenv("PARTY_A_NO"), os.getenv("PARTY_B_NO")}:
        raise ValueError("SIP_NUMBER must be the provider-owned number, distinct from both phones")
    if os.getenv("VENDORS") != "sarvam":
        raise ValueError("SIP calls require VENDORS=sarvam")


def caller_number(metadata, participant):
    # Dispatch metadata is a local-test convenience, never a SIP identity override.
    if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
        return participant.attributes.get("sip.phoneNumber", "")
    data = json.loads(metadata or "{}")
    caller = data.get("caller", "") if isinstance(data, dict) else ""
    if not isinstance(caller, str):
        raise ValueError("Caller metadata must be a string")
    return caller


async def lookup(caller):
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            f"{os.environ['API_URL'].rstrip('/')}/lookup",
            params={"caller": caller}, headers={"X-API-Key": os.environ["API_KEY"]},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()


class Leg:
    def __init__(self, participant, language, latencies):
        self.language = language
        self.latencies = latencies
        self.source = rtc.AudioSource(24000, 1, queue_size_ms=100)
        self.stream = rtc.AudioStream.from_participant(
            participant=participant, track_source=rtc.TrackSource.SOURCE_MICROPHONE,
            sample_rate=16000, num_channels=1, frame_size_ms=20,
        )
        self.pending = asyncio.Queue(maxsize=16)
        self.generation = 0
        self.last_speech = None
        self.speech_end = None
        self.utterance_end = None
        self.closed = False

    async def publish(self, room):
        track = rtc.LocalAudioTrack.create_audio_track("translation", self.source)
        await room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
        )

    async def hold(self):
        rate = self.source.sample_rate
        tone = array.array("h", (
            int(2000 * math.sin(2 * math.pi * 425 * i / rate)) for i in range(rate)
        ))
        if sys.byteorder != "little":
            tone.byteswap()
        pcm = tone.tobytes() + bytes(rate * 2)
        frame_bytes = rate // 50 * 2
        while not self.closed:
            for offset in range(0, len(pcm), frame_bytes):
                if self.closed:
                    return
                frame = pcm[offset:offset + frame_bytes]
                await self.source.capture_frame(rtc.AudioFrame(frame, rate, 1, len(frame) // 2))

    def interrupt(self):
        self.generation += 1
        self.source.clear_queue()

    def end_speech(self):
        self.speech_end = self.last_speech or time.monotonic()

    async def chunks(self):
        async for event in self.stream:
            samples = event.frame.data
            # Timestamp the last audible input frame, including endpointing delay in latency.
            if samples and sum(sample * sample for sample in samples) / len(samples) > 500 ** 2:
                self.last_speech = time.monotonic()
            yield event.frame.data.tobytes()

    async def transcribe(self):
        async for text in vendors.transcribe_stream(
            self.chunks(), self.language, on_speech_start=self.interrupt,
            on_speech_end=self.end_speech,
        ):
            await self.pending.put((text, self.speech_end))
        await self.pending.put(None)

    async def finals(self):
        while (item := await self.pending.get()) is not None:
            text, self.utterance_end = item
            yield text

    async def announce(self, kind):
        if self.closed:
            return
        generation = self.generation
        try:
            async with asyncio.timeout(VENDOR_TIMEOUT):
                pcm, rate = await vendors.synthesize(PROMPTS[self.language][kind], self.language)
            async with asyncio.timeout(5):
                await self.play(pcm, rate, None, generation)
        except Exception as exc:
            with suppress(Exception):
                if not self.closed and generation == self.generation:
                    self.source.clear_queue()
            logger.warning("announcement_failed kind=%s error=%s", kind, type(exc).__name__)

    async def vendor_call(self, operation, generation):
        for attempt in range(2):
            if self.closed or generation != self.generation:
                raise InterruptedError("Listener interrupted the sentence")
            try:
                async with asyncio.timeout(VENDOR_TIMEOUT):
                    return await operation()
            except (TimeoutError, httpx.TimeoutException):
                if attempt == 1:
                    logger.warning("vendor_sentence_skipped error=TimeoutError")
                    raise TimeoutError("Vendor timed out after one retry") from None
                if not self.closed and generation == self.generation:
                    await self.announce("wait")

    async def translate_to(self, text, listener):
        generation = listener.generation
        translated = await listener.vendor_call(
            partial(vendors.translate, text, src=self.language, tgt=listener.language), generation,
        )
        if listener.closed or generation != listener.generation:
            raise InterruptedError("Listener interrupted the translation")
        return translated

    async def speak(self, text, speaker):
        generation = self.generation
        try:
            pcm, rate = await self.vendor_call(partial(vendors.synthesize, text, self.language), generation)
        except (TimeoutError, InterruptedError):
            return
        await self.play(pcm, rate, speaker, generation)

    async def play(self, pcm, rate, speaker, generation):
        if rate != self.source.sample_rate or not pcm or len(pcm) % 2:
            raise ValueError("TTS must return nonempty mono PCM16 at 24000 Hz")
        frame_bytes = rate // 50 * 2
        for offset in range(0, len(pcm), frame_bytes):
            if self.closed or generation != self.generation:
                return
            frame = pcm[offset:offset + frame_bytes]
            await self.source.capture_frame(rtc.AudioFrame(frame, rate, 1, len(frame) // 2))
            if self.closed or generation != self.generation:
                self.source.clear_queue()
                return
            if offset == 0 and speaker is not None and speaker.utterance_end is not None:
                self.latencies.append((time.monotonic() - speaker.utterance_end) * 1000)
        await self.source.wait_for_playout()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.interrupt()
        finally:
            results = await asyncio.gather(self.stream.aclose(), self.source.aclose(), return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    logger.error("media_cleanup_failed error=%s", type(result).__name__)


async def dial_receiver(ctx, room, caller_leg, other_no):
    request = api.CreateSIPParticipantRequest(
        sip_trunk_id=os.environ["LIVEKIT_SIP_OUTBOUND_TRUNK_ID"],
        sip_call_to=other_no, sip_number=os.environ["SIP_NUMBER"],
        room_name=room.name, participant_identity="receiver", participant_name="Receiver",
        wait_until_answered=True, ringing_timeout={"seconds": 30}, play_dialtone=False,
    )
    # Room B stays isolated: play our hold tone in room A, never forward SIP early media.
    hold = asyncio.create_task(caller_leg.hold())
    dial = asyncio.create_task(ctx.api.sip.create_sip_participant(request))
    try:
        done, _ = await asyncio.wait([hold, dial], timeout=DIAL_TIMEOUT, return_when=asyncio.FIRST_COMPLETED)
        if not done:
            raise TimeoutError("SIP dial did not complete")
        for task in done:
            task.result()
        if hold in done:
            raise RuntimeError("Caller hold tone ended before answer")
    finally:
        for task in (hold, dial):
            task.cancel()
        await asyncio.gather(hold, dial, return_exceptions=True)
        caller_leg.interrupt()
    receiver = await asyncio.wait_for(utils.wait_for_participant(room, identity="receiver"), PARTICIPANT_TIMEOUT)
    if receiver.kind != rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
        raise ValueError("SIP receiver must be a SIP participant")
    return receiver


def dial_failure(exc):
    code = exc.sip_status_code if isinstance(exc, api.SipCallError) else None
    if code in {486, 600}:
        return "busy"
    if isinstance(exc, TimeoutError) or code in {408, 480}:
        return "unanswered"
    return "failed"


async def delete_room(lk, name):
    for attempt in range(2):
        try:
            async with asyncio.timeout(CLEANUP_TIMEOUT):
                await lk.room.delete_room(api.DeleteRoomRequest(room=name))
            return
        except api.TwirpError as exc:
            if exc.code == "not_found":
                return
            if attempt == 1:
                logger.error("room_cleanup_failed call_id=%s code=%s", name, exc.code)
        except Exception as exc:
            if attempt == 1:
                logger.error("room_cleanup_failed call_id=%s error=%s", name, type(exc).__name__)


async def close_media(operation):
    try:
        async with asyncio.timeout(CLEANUP_TIMEOUT):
            await operation()
    except Exception as exc:
        logger.error("media_cleanup_failed error=%s", type(exc).__name__)


def log_summary(call_id, latencies):
    ordered = sorted(latencies)

    def percentile(fraction):
        return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] if ordered else 0

    logger.info(
        "call_summary call_id=%s utterances=%d p50_ms=%.1f p95_ms=%.1f",
        call_id, len(ordered), percentile(0.5), percentile(0.95),
    )


@server.rtc_session(agent_name="translator")
async def entrypoint(ctx: JobContext):
    room_a = ctx.room
    room_b = rtc.Room()
    names = [ctx.job.room.name]
    legs, tasks, latencies = [], [], []
    ended = asyncio.Event()
    cleanup_task = None
    handlers = []
    caller_identity = None
    departed = set()

    def watch(room, event, callback):
        room.on(event, callback)
        handlers.append((room, event, callback))

    def disconnected(*_):
        ended.set()

    watch(room_a, "disconnected", disconnected)
    watch(room_b, "disconnected", disconnected)

    def caller_left(participant):
        departed.add(participant.identity)
        if participant.identity == caller_identity:
            ended.set()

    watch(room_a, "participant_disconnected", caller_left)

    async def finish_cleanup():
        try:
            ended.set()
            for room, event, callback in handlers:
                room.off(event, callback)
            for task in tasks:
                task.cancel()
            if tasks:
                done, pending = await asyncio.wait(tasks, timeout=CLEANUP_TIMEOUT)
                for task in done:
                    if not task.cancelled():
                        task.exception()
                for task in pending:
                    logger.error("call_task_cleanup_timeout call_id=%s", names[0])
                    task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            await asyncio.gather(
                *(delete_room(ctx.api, name) for name in names),
                *(close_media(leg.close) for leg in legs),
                close_media(room_a.disconnect), close_media(room_b.disconnect),
            )
        finally:
            log_summary(names[0], latencies)

    async def cleanup():
        nonlocal cleanup_task
        if cleanup_task is None:
            cleanup_task = asyncio.create_task(finish_cleanup())
        await asyncio.shield(cleanup_task)

    ctx.add_shutdown_callback(cleanup)

    async def serve():
        nonlocal caller_identity
        validate_config()
        sip = os.getenv("DIAL_MODE", "room") == "sip"
        await asyncio.wait_for(ctx.connect(auto_subscribe=AutoSubscribe.SUBSCRIBE_NONE), PARTICIPANT_TIMEOUT)
        caller = await asyncio.wait_for(ctx.wait_for_participant(), PARTICIPANT_TIMEOUT)
        caller_identity = caller.identity
        if caller_identity in departed or ended.is_set():
            return
        if sip and caller.kind != rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            raise ValueError("SIP caller must be a SIP participant")

        route = await lookup(caller_number(ctx.job.metadata, caller))
        if route is None or ended.is_set():
            return
        names.append(f"{names[0]}-b")
        token = (
            api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
            .with_identity("translator")
            .with_grants(api.VideoGrants(room_join=True, room=names[1]))
            .to_jwt()
        )

        def receiver_left(participant):
            if participant.identity == "receiver":
                ended.set()

        watch(room_b, "participant_disconnected", receiver_left)
        await asyncio.wait_for(
            room_b.connect(os.environ["LIVEKIT_URL"], token, options=rtc.RoomOptions(auto_subscribe=False)),
            PARTICIPANT_TIMEOUT,
        )
        if ended.is_set():
            return
        if sip:
            a = Leg(caller, route["caller_lang"], latencies)
            legs.append(a)
            await a.publish(room_a)
            if ended.is_set():
                return
            try:
                receiver = await dial_receiver(ctx, room_b, a, route["other_no"])
            except Exception as exc:
                if not ended.is_set():
                    await a.announce(dial_failure(exc))
                raise
        else:
            receiver = await asyncio.wait_for(utils.wait_for_participant(room_b, identity="receiver"), PARTICIPANT_TIMEOUT)
            a = Leg(caller, route["caller_lang"], latencies)
            legs.append(a)
            await a.publish(room_a)
        if ended.is_set():
            return
        b = Leg(receiver, route["other_lang"], latencies)
        legs.append(b)
        await b.publish(room_b)
        if ended.is_set():
            return

        def subscribe(track, participant, expected):
            if participant.identity == expected and track.kind == rtc.TrackKind.KIND_AUDIO:
                track.set_subscribed(True)

        for room, participant in ((room_a, caller), (room_b, receiver)):
            watch(room, "track_published", partial(subscribe, expected=participant.identity))
            for track in participant.track_publications.values():
                subscribe(track, participant, participant.identity)

        async def conversation():
            if sip:
                await asyncio.gather(a.announce("connected"), b.announce("connected"))
            await run_call(
                a.finals(), b.finals(),
                partial(a.translate_to, listener=b), partial(b.translate_to, listener=a),
                partial(a.speak, speaker=b), partial(b.speak, speaker=a),
            )

        tasks.extend([
            asyncio.create_task(a.transcribe()), asyncio.create_task(b.transcribe()),
            asyncio.create_task(conversation()),
        ])
        done, _ = await asyncio.wait(tasks[-3:], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()

    call_task = asyncio.create_task(serve())
    hangup_task = asyncio.create_task(ended.wait())
    tasks.extend([call_task, hangup_task])
    try:
        done, _ = await asyncio.wait([call_task, hangup_task], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except Exception as exc:
        # Do not log request URLs, phone numbers, credentials, or transcript text.
        logger.error("call_failed call_id=%s error=%s", names[0], type(exc).__name__)
        raise
    finally:
        try:
            await cleanup()
        finally:
            ctx.shutdown("call ended")


if __name__ == "__main__":
    validate_config()
    cli.run_app(server)
