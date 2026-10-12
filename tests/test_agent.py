import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from livekit import rtc

from app import agent


@pytest.mark.parametrize("metadata,expected", [
    ('{"caller":"+919123260733"}', "+919123260733"), ("", ""), ("[]", ""),
])
def test_local_caller_metadata(metadata, expected):
    participant = SimpleNamespace(kind=rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD)
    assert agent.caller_number(metadata, participant) == expected


def test_sip_identity_cannot_be_overridden_by_dispatch_metadata():
    participant = SimpleNamespace(
        kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP,
        attributes={"sip.phoneNumber": "+916392306428"},
    )
    assert agent.caller_number('{"caller":"+919123260733"}', participant) == "+916392306428"
    participant.attributes = {}
    assert agent.caller_number('{"caller":"+919123260733"}', participant) == ""


def test_invalid_caller_metadata():
    participant = SimpleNamespace(kind=rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD)
    with pytest.raises(ValueError):
        agent.caller_number('{"caller":123}', participant)


@pytest.mark.parametrize("status", [200, 404, 401, 503])
async def test_authenticated_lookup(monkeypatch, status):
    monkeypatch.setenv("API_URL", "http://routing/")
    monkeypatch.setenv("API_KEY", "test-key")
    route = {"caller_lang": "od-IN", "other_lang": "hi-IN", "other_no": "+916392306428"}

    def handle(request):
        assert request.url.path == "/lookup"
        assert request.url.params["caller"] == "+919123260733"
        assert request.headers["X-API-Key"] == "test-key"
        return httpx.Response(status, json=route)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: client)
    if status not in {200, 404}:
        with pytest.raises(httpx.HTTPStatusError):
            await agent.lookup("+919123260733")
    else:
        assert await agent.lookup("+919123260733") == (route if status == 200 else None)


@pytest.fixture
def leg(monkeypatch):
    source = SimpleNamespace(
        sample_rate=24000, capture_frame=AsyncMock(), clear_queue=Mock(),
        wait_for_playout=AsyncMock(), aclose=AsyncMock(),
    )
    stream = SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr(agent.rtc, "AudioSource", lambda *args, **kwargs: source)
    monkeypatch.setattr(agent.rtc.AudioStream, "from_participant", lambda **kwargs: stream)
    return agent.Leg(SimpleNamespace(), "hi-IN", [])


async def test_speaks_only_synthesized_audio_and_records_first_frame_latency(monkeypatch, leg):
    pcm = bytes(24000 * 2 * 3 // 5)
    synthesize = AsyncMock(return_value=(pcm, 24000))
    monkeypatch.setattr(agent.vendors, "synthesize", synthesize)
    monkeypatch.setattr(agent.time, "monotonic", lambda: 10)
    await leg.speak("[hi-IN] fake", SimpleNamespace(utterance_end=9.5))
    synthesize.assert_awaited_once_with("[hi-IN] fake", "hi-IN")
    frames = [call.args[0] for call in leg.source.capture_frame.await_args_list]
    assert b"".join(frame.data.tobytes() for frame in frames) == pcm
    assert all(frame.sample_rate == 24000 and frame.num_channels == 1 for frame in frames)
    assert leg.latencies == [500]
    leg.source.wait_for_playout.assert_awaited_once()


async def test_interrupt_during_synthesis_discards_pending_audio(monkeypatch, leg):
    async def synthesize(*args):
        leg.interrupt()
        return bytes(960), 24000

    monkeypatch.setattr(agent.vendors, "synthesize", synthesize)
    await leg.speak("translated", SimpleNamespace(utterance_end=0))
    leg.source.capture_frame.assert_not_awaited()
    leg.source.clear_queue.assert_called_once()
    assert leg.latencies == []


async def test_interrupt_stops_remaining_frames(monkeypatch, leg):
    monkeypatch.setattr(agent.vendors, "synthesize", AsyncMock(return_value=(bytes(960 * 10), 24000)))
    leg.source.capture_frame.side_effect = lambda frame: leg.interrupt()
    await leg.speak("translated", SimpleNamespace(utterance_end=0))
    assert leg.source.capture_frame.await_count == 1
    assert leg.source.clear_queue.call_count == 2
    leg.source.wait_for_playout.assert_not_awaited()


@pytest.mark.parametrize("pcm,rate", [(b"", 24000), (b"a", 24000), (bytes(960), 16000)])
async def test_invalid_tts_audio_fails_without_publication(monkeypatch, leg, pcm, rate):
    monkeypatch.setattr(agent.vendors, "synthesize", AsyncMock(return_value=(pcm, rate)))
    with pytest.raises(ValueError):
        await leg.speak("translated", SimpleNamespace(utterance_end=0))
    leg.source.capture_frame.assert_not_awaited()


async def test_stt_runs_while_translation_consumer_is_idle(monkeypatch, leg):
    monkeypatch.setattr(agent.time, "monotonic", lambda: 20)
    leg.last_speech = 19.5

    async def transcribe(chunks, lang, on_speech_start, on_speech_end):
        assert lang == "hi-IN"
        on_speech_start()
        on_speech_end()
        yield "first"
        on_speech_start()
        leg.last_speech = 21
        on_speech_end()
        yield "second"

    monkeypatch.setattr(agent.vendors, "transcribe_stream", transcribe)
    await leg.transcribe()
    assert leg.source.clear_queue.call_count == 2
    texts, ends = [], []
    async for text in leg.finals():
        texts.append(text)
        ends.append(leg.utterance_end)
    assert texts == ["first", "second"]
    assert ends == [19.5, 21]


def test_summary_percentiles_and_empty_calls(caplog):
    with caplog.at_level(logging.INFO, logger="translator"):
        agent.log_summary("room-a", [400, 100, 300, 200])
        agent.log_summary("room-b", [])
    assert "call_summary call_id=room-a utterances=4 p50_ms=200.0 p95_ms=400.0" in caplog.text
    assert "call_summary call_id=room-b utterances=0 p50_ms=0.0 p95_ms=0.0" in caplog.text


class Room:
    def __init__(self):
        self.callbacks = {}
        self.remote_participants = {}
        self.connect = AsyncMock()
        self.disconnect = AsyncMock()

    def on(self, event, callback):
        self.callbacks.setdefault(event, []).append(callback)

    def off(self, event, callback):
        self.callbacks[event].remove(callback)

    def emit(self, event, *args):
        for callback in self.callbacks.get(event, []):
            callback(*args)


@pytest.fixture
def context(monkeypatch):
    monkeypatch.setenv("DIAL_MODE", "room")
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("LIVEKIT_API_KEY", "devkey")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "secret")
    room_a, room_b = Room(), Room()
    caller = SimpleNamespace(
        identity="caller", kind=rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD, track_publications={},
    )
    receiver = SimpleNamespace(identity="receiver", track_publications={})
    room_a.remote_participants[caller.identity] = caller
    room_b.remote_participants[receiver.identity] = receiver
    monkeypatch.setattr(agent.rtc, "Room", lambda: room_b)
    monkeypatch.setattr(agent.utils, "wait_for_participant", AsyncMock(return_value=receiver))
    ctx = SimpleNamespace(
        room=room_a, job=SimpleNamespace(room=SimpleNamespace(name="room-a"), metadata=json.dumps({"caller": "+919123260733"})),
        connect=AsyncMock(), wait_for_participant=AsyncMock(return_value=caller),
        api=SimpleNamespace(room=SimpleNamespace(delete_room=AsyncMock())),
        add_shutdown_callback=Mock(), shutdown=Mock(),
    )
    return ctx, room_b, caller, receiver


async def test_unknown_caller_deletes_only_dispatched_room(monkeypatch, context):
    ctx, room_b, *_ = context
    monkeypatch.setattr(agent, "lookup", AsyncMock(return_value=None))
    await agent.entrypoint(ctx)
    assert [call.args[0].room for call in ctx.api.room.delete_room.await_args_list] == ["room-a"]
    room_b.connect.assert_not_awaited()
    ctx.shutdown.assert_called_once()
    await ctx.add_shutdown_callback.call_args.args[0]()
    assert ctx.api.room.delete_room.await_count == 1


async def test_lookup_failure_closes_room_without_joining_receiver(monkeypatch, context):
    ctx, room_b, *_ = context
    monkeypatch.setattr(agent, "lookup", AsyncMock(side_effect=RuntimeError("routing unavailable")))
    with pytest.raises(RuntimeError):
        await agent.entrypoint(ctx)
    assert ctx.api.room.delete_room.await_args.args[0].room == "room-a"
    room_b.connect.assert_not_awaited()


async def test_caller_hangup_while_receiver_is_absent_cleans_both_rooms(monkeypatch, context):
    ctx, room_b, caller, _ = context
    monkeypatch.setattr(agent, "lookup", AsyncMock(return_value={"caller_lang": "od-IN", "other_lang": "hi-IN"}))
    waiting = asyncio.Event()

    async def wait_for_receiver(*args, **kwargs):
        waiting.set()
        await asyncio.Future()

    monkeypatch.setattr(agent.utils, "wait_for_participant", wait_for_receiver)
    task = asyncio.create_task(agent.entrypoint(ctx))
    await asyncio.wait_for(waiting.wait(), 1)
    ctx.room.emit("participant_disconnected", caller)
    await asyncio.wait_for(task, 1)
    assert [call.args[0].room for call in ctx.api.room.delete_room.await_args_list] == ["room-a", "room-a-b"]
    room_b.disconnect.assert_awaited_once()


@pytest.mark.parametrize("hangup", ["caller", "receiver", "connection"])
async def test_hangup_stops_media_tasks_and_deletes_both_rooms(monkeypatch, context, hangup):
    ctx, room_b, caller, receiver = context
    monkeypatch.setattr(agent, "lookup", AsyncMock(return_value={"caller_lang": "od-IN", "other_lang": "hi-IN"}))
    started, stopped = asyncio.Event(), asyncio.Event()
    legs = []

    class FakeLeg:
        def __init__(self, participant, language, latencies):
            self.language = language
            self.close = AsyncMock()
            self.publish = AsyncMock()
            self.finals = Mock()
            self.translate_to = AsyncMock()
            self.speak = AsyncMock()
            legs.append(self)

        async def transcribe(self):
            await asyncio.Future()

    async def pipeline(*args):
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    monkeypatch.setattr(agent, "Leg", FakeLeg)
    monkeypatch.setattr(agent, "run_call", pipeline)
    task = asyncio.create_task(agent.entrypoint(ctx))
    await asyncio.wait_for(started.wait(), 1)
    # An unrelated participant leaving must not terminate the call.
    ctx.room.emit("participant_disconnected", SimpleNamespace(identity="observer"))
    assert not task.done()
    if hangup == "caller":
        ctx.room.emit("participant_disconnected", caller)
    elif hangup == "receiver":
        room_b.emit("participant_disconnected", receiver)
    else:
        room_b.emit("disconnected", "network lost")
    await asyncio.wait_for(task, 1)
    assert stopped.is_set()
    assert [call.args[0].room for call in ctx.api.room.delete_room.await_args_list] == ["room-a", "room-a-b"]
    assert [leg.language for leg in legs] == ["od-IN", "hi-IN"]
    for leg in legs:
        leg.close.assert_awaited_once()


async def test_invalid_dial_mode_is_rejected(monkeypatch, context):
    ctx, room_b, *_ = context
    monkeypatch.setenv("DIAL_MODE", "invalid")
    with pytest.raises(ValueError, match="DIAL_MODE"):
        await agent.entrypoint(ctx)
    ctx.connect.assert_not_awaited()
    room_b.connect.assert_not_awaited()


@pytest.fixture
def sip_config(monkeypatch):
    values = {
        "DIAL_MODE": "sip", "VENDORS": "sarvam", "SARVAM_API_KEY": "test-vendor-key",
        "API_KEY": "test-api-key", "API_URL": "http://routing:8000",
        "LIVEKIT_URL": "wss://test.livekit.cloud", "LIVEKIT_API_KEY": "test-cloud-key",
        "LIVEKIT_API_SECRET": "test-cloud-secret-with-at-least-32-bytes",
        "LIVEKIT_SIP_OUTBOUND_TRUNK_ID": "ST_outbound", "SIP_NUMBER": "+918000000000",
        "PARTY_A_NO": "+919123260733", "PARTY_B_NO": "+916392306428",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


def test_valid_sip_config(sip_config):
    agent.validate_config()


@pytest.mark.parametrize("key,value", [
    ("API_KEY", ""), ("API_URL", ""), ("SARVAM_API_KEY", " "),
    ("LIVEKIT_SIP_OUTBOUND_TRUNK_ID", ""), ("LIVEKIT_API_KEY", ""),
    ("LIVEKIT_API_SECRET", ""), ("LIVEKIT_API_KEY", "devkey"),
    ("LIVEKIT_API_SECRET", "secret"), ("SIP_NUMBER", ""), ("SIP_NUMBER", "8000000000"),
    ("SIP_NUMBER", "+919123260733"), ("SIP_NUMBER", "+916392306428"),
    ("LIVEKIT_URL", "ws://livekit:7880"), ("LIVEKIT_URL", "wss://example.com"),
    ("VENDORS", "fake"),
])
def test_invalid_sip_config_fails_before_any_dial(monkeypatch, sip_config, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        agent.validate_config()


@pytest.fixture
def sip_call(monkeypatch, context, sip_config):
    ctx, room_b, caller, receiver = context
    room_b.name = "room-a-b"
    caller.kind = receiver.kind = rtc.ParticipantKind.PARTICIPANT_KIND_SIP
    caller.attributes = {"sip.phoneNumber": sip_config["PARTY_A_NO"]}
    ctx.api.sip = SimpleNamespace(create_sip_participant=AsyncMock())
    route = {"caller_lang": "od-IN", "other_lang": "hi-IN", "other_no": sip_config["PARTY_B_NO"]}
    lookup = AsyncMock(return_value=route)
    monkeypatch.setattr(agent, "lookup", lookup)
    started, hold_started, hold_stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    legs = []

    class FakeLeg:
        def __init__(self, participant, language, latencies):
            self.language = language
            self.publish = AsyncMock()
            self.close = AsyncMock()
            self.interrupt = Mock()
            self.finals = Mock()
            self.translate_to = AsyncMock()
            self.speak = AsyncMock()
            self.announce = AsyncMock()
            legs.append(self)

        async def hold(self):
            hold_started.set()
            try:
                await asyncio.Future()
            finally:
                hold_stopped.set()

        async def transcribe(self):
            await asyncio.Future()

    async def pipeline(*args):
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(agent, "Leg", FakeLeg)
    monkeypatch.setattr(agent, "run_call", pipeline)
    return SimpleNamespace(
        ctx=ctx, room_b=room_b, caller=caller, receiver=receiver, legs=legs,
        started=started, hold_started=hold_started, hold_stopped=hold_stopped,
        route=route, lookup=lookup,
    )


@pytest.mark.parametrize("caller_lang,other_lang,caller_no,other_no", [
    ("od-IN", "hi-IN", "+919123260733", "+916392306428"),
    ("hi-IN", "od-IN", "+916392306428", "+919123260733"),
])
async def test_sip_dials_other_phone_after_lookup_and_holds_until_answer(sip_call, caller_lang, other_lang, caller_no, other_no):
    call = sip_call
    call.caller.attributes["sip.phoneNumber"] = caller_no
    call.route.update(caller_lang=caller_lang, other_lang=other_lang, other_no=other_no)
    answer = asyncio.Event()

    async def dial(request):
        assert call.legs[0].publish.await_count == 1
        await answer.wait()

    call.ctx.api.sip.create_sip_participant.side_effect = dial
    task = asyncio.create_task(agent.entrypoint(call.ctx))
    await asyncio.wait_for(call.hold_started.wait(), 1)
    assert not call.started.is_set()
    assert not call.hold_stopped.is_set()
    request = call.ctx.api.sip.create_sip_participant.call_args.args[0]
    assert request.sip_call_to == other_no
    assert request.sip_number == "+918000000000"
    assert request.sip_trunk_id == "ST_outbound"
    assert request.room_name == "room-a-b"
    assert request.participant_identity == "receiver"
    assert request.wait_until_answered and not request.play_dialtone
    assert request.ringing_timeout.seconds == 30
    call.lookup.assert_awaited_once_with(caller_no)
    answer.set()
    await asyncio.wait_for(call.started.wait(), 1)
    assert call.hold_stopped.is_set()
    call.legs[0].interrupt.assert_called_once()
    assert [leg.language for leg in call.legs] == [caller_lang, other_lang]
    call.room_b.emit("participant_disconnected", call.receiver)
    await asyncio.wait_for(task, 1)
    assert [call.args[0].room for call in call.ctx.api.room.delete_room.await_args_list] == ["room-a", "room-a-b"]


async def test_unknown_sip_caller_never_dials(sip_call):
    sip_call.lookup.return_value = None
    sip_call.caller.attributes["sip.phoneNumber"] = "+919999999999"
    await agent.entrypoint(sip_call.ctx)
    sip_call.lookup.assert_awaited_once_with("+919999999999")
    sip_call.ctx.api.sip.create_sip_participant.assert_not_awaited()
    assert sip_call.legs == []
    assert sip_call.ctx.api.room.delete_room.await_args.args[0].room == "room-a"


async def test_sip_mode_rejects_local_metadata_caller(sip_call):
    sip_call.caller.kind = rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD
    with pytest.raises(ValueError, match="SIP caller"):
        await agent.entrypoint(sip_call.ctx)
    sip_call.lookup.assert_not_awaited()
    sip_call.ctx.api.sip.create_sip_participant.assert_not_awaited()


async def test_sip_mode_rejects_non_sip_receiver(sip_call):
    sip_call.receiver.kind = rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD
    with pytest.raises(ValueError, match="SIP receiver"):
        await agent.entrypoint(sip_call.ctx)
    assert sip_call.hold_stopped.is_set()
    assert not sip_call.started.is_set()
    assert sip_call.ctx.api.room.delete_room.await_count == 2


@pytest.mark.parametrize("failure", [RuntimeError("busy"), TimeoutError("no answer")])
async def test_sip_dial_failure_stops_hold_and_deletes_both_legs(sip_call, failure):
    sip_call.ctx.api.sip.create_sip_participant.side_effect = failure
    with pytest.raises(type(failure)):
        await agent.entrypoint(sip_call.ctx)
    assert sip_call.hold_stopped.is_set()
    assert not sip_call.started.is_set()
    assert sip_call.ctx.api.room.delete_room.await_count == 2
    sip_call.legs[0].close.assert_awaited_once()


async def test_caller_hangup_cancels_sip_dial_and_hold(sip_call):
    dial_stopped = asyncio.Event()

    async def dial(request):
        try:
            await asyncio.Future()
        finally:
            dial_stopped.set()

    sip_call.ctx.api.sip.create_sip_participant.side_effect = dial
    task = asyncio.create_task(agent.entrypoint(sip_call.ctx))
    await asyncio.wait_for(sip_call.hold_started.wait(), 1)
    sip_call.ctx.room.emit("participant_disconnected", sip_call.caller)
    await asyncio.wait_for(task, 1)
    assert dial_stopped.is_set() and sip_call.hold_stopped.is_set()
    assert sip_call.ctx.api.room.delete_room.await_count == 2
    assert not sip_call.started.is_set()


async def test_hold_failure_cancels_pending_sip_dial(monkeypatch, sip_call):
    async def broken_hold(self):
        await asyncio.sleep(0)
        raise RuntimeError("audio unavailable")

    dial_stopped = asyncio.Event()

    async def dial(request):
        try:
            await asyncio.Future()
        finally:
            dial_stopped.set()

    monkeypatch.setattr(agent.Leg, "hold", broken_hold)
    sip_call.ctx.api.sip.create_sip_participant.side_effect = dial
    with pytest.raises(RuntimeError, match="audio unavailable"):
        await agent.entrypoint(sip_call.ctx)
    assert dial_stopped.is_set()
    assert sip_call.ctx.api.room.delete_room.await_count == 2


async def test_hold_tone_contains_pcm_and_silence_without_vendor_calls(monkeypatch, leg):
    frames = []

    async def capture(frame):
        frames.append(frame)
        if len(frames) == 100:
            raise asyncio.CancelledError()

    leg.source.capture_frame.side_effect = capture
    synthesize = AsyncMock()
    monkeypatch.setattr(agent.vendors, "synthesize", synthesize)
    with pytest.raises(asyncio.CancelledError):
        await leg.hold()
    assert len(frames) == 100
    assert all(frame.sample_rate == 24000 and frame.num_channels == 1 for frame in frames)
    assert any(sample for frame in frames[:50] for sample in frame.data)
    assert all(sample == 0 for frame in frames[50:] for sample in frame.data)
    synthesize.assert_not_awaited()
    assert leg.latencies == []


async def test_leg_close_releases_source_after_stream_failure(leg):
    leg.stream.aclose.side_effect = RuntimeError("stream close failed")
    try:
        await leg.close()
    except RuntimeError:
        pass
    leg.source.aclose.assert_awaited_once()


async def test_leg_close_releases_media_after_queue_clear_failure(leg):
    leg.source.clear_queue.side_effect = RuntimeError("queue clear failed")
    try:
        await leg.close()
    except RuntimeError:
        pass
    leg.stream.aclose.assert_awaited_once()
    leg.source.aclose.assert_awaited_once()
