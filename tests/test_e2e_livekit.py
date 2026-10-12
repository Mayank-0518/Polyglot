"""P5: agent end-to-end on a LiveKit server, no phones.

Needs: LiveKit server, API, agent running with VENDORS=fake DIAL_MODE=room.
Run:   docker compose up -d livekit api agent; docker compose run --rm test

Fake vendors (see PLAN.md): STT emits "fake" after speech+silence, MT prefixes "[tgt]",
TTS plays 440 Hz for translations. Humans here play pure tones, so the frequency the
listener hears tells us exactly what reached them.
"""
import array
import asyncio
import json
import math
import os
import random

import pytest

pytestmark = pytest.mark.e2e  # opt-in: pytest -m e2e

CALLER_HZ, RECEIVER_HZ, TRANSLATION_HZ = 1000, 700, 440
WIN_S = 0.05  # 50 ms windows -> ~10 Hz resolution from zero crossings


def env(k):
    return os.environ[k]


def token(identity, room):
    from livekit import api
    return (api.AccessToken(env("LIVEKIT_API_KEY"), env("LIVEKIT_API_SECRET"))
            .with_identity(identity)
            .with_grants(api.VideoGrants(room_join=True, room=room))
            .to_jwt())


async def join(identity, room):
    from livekit import rtc
    r = rtc.Room()
    await r.connect(env("LIVEKIT_URL"), token(identity, room))
    return r


async def dispatch(room, caller):
    from livekit import api
    lk = api.LiveKitAPI(env("LIVEKIT_URL").replace("ws", "http", 1), env("LIVEKIT_API_KEY"), env("LIVEKIT_API_SECRET"))
    try:
        await lk.agent_dispatch.create_dispatch(api.CreateAgentDispatchRequest(
            agent_name="translator", room=room, metadata=json.dumps({"caller": caller})))
    finally:
        await lk.aclose()


async def speak_tone(room, hz, seconds=1.5, silence=1.5, rate=48000):
    from livekit import rtc
    src = rtc.AudioSource(rate, 1)
    track = rtc.LocalAudioTrack.create_audio_track("mic", src)
    await room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
    n = rate // 100  # 10 ms frames
    for i in range(int((seconds + silence) * 100)):
        if i < seconds * 100:
            data = array.array("h", (int(8000 * math.sin(2 * math.pi * hz * (i * n + j) / rate)) for j in range(n))).tobytes()
        else:
            data = bytes(2 * n)
        await src.capture_frame(rtc.AudioFrame(data, rate, 1, n))


async def listen(room, humans, seconds):
    """Collect audio from the first non-human participant in `room` for `seconds`."""
    from livekit import rtc
    got = asyncio.get_running_loop().create_future()

    def offer(track, participant):
        if track and track.kind == rtc.TrackKind.KIND_AUDIO and participant.identity not in humans and not got.done():
            got.set_result(track)

    room.on("track_subscribed", lambda track, pub, p: offer(track, p))
    for p in room.remote_participants.values():
        for pub in p.track_publications.values():
            offer(pub.track, p)

    samples, rate = [], 48000

    async def read():
        track = await got
        async for ev in rtc.AudioStream(track, sample_rate=rate, num_channels=1):
            samples.extend(ev.frame.data)

    try:
        await asyncio.wait_for(read(), seconds)
    except asyncio.TimeoutError:
        pass
    return samples, rate


def loud_window_freqs(samples, rate):
    n = int(rate * WIN_S)
    freqs = []
    for i in range(0, len(samples) - n, n):
        w = samples[i:i + n]
        if math.sqrt(sum(s * s for s in w) / n) < 500:
            continue
        crossings = sum((a < 0) != (b < 0) for a, b in zip(w, w[1:]))
        freqs.append(crossings / 2 / WIN_S)
    return freqs


def near(freqs, hz, tol=40):
    return sum(abs(f - hz) <= tol for f in freqs)


async def call_setup(caller_env):
    room_a = f"e2e-{random.randrange(10**9)}"
    caller = await join("caller", room_a)
    receiver = await join("receiver", f"{room_a}-b")
    await dispatch(room_a, env(caller_env))
    return caller, receiver


async def assert_only_translation(speaker, listener, speaker_hz):
    humans = {"caller", "receiver"}
    (samples, rate), _ = await asyncio.gather(listen(listener, humans, 12), speak_tone(speaker, speaker_hz))
    freqs = loud_window_freqs(samples, rate)
    assert near(freqs, TRANSLATION_HZ) * WIN_S >= 0.3, f"no translated audio heard: {freqs[:20]}"
    assert near(freqs, speaker_hz) == 0, "raw speaker audio leaked to the listener"


@pytest.mark.parametrize("caller_env", ["PARTY_A_NO", "PARTY_B_NO"])
async def test_caller_reaches_receiver_only_as_translation(caller_env):
    caller, receiver = await call_setup(caller_env)
    try:
        await asyncio.sleep(2)  # let the agent join both rooms
        assert "caller" not in receiver.remote_participants
        await assert_only_translation(caller, receiver, CALLER_HZ)
    finally:
        await caller.disconnect()
        await receiver.disconnect()


@pytest.mark.parametrize("caller_env", ["PARTY_A_NO", "PARTY_B_NO"])
async def test_receiver_reaches_caller_only_as_translation(caller_env):
    caller, receiver = await call_setup(caller_env)
    try:
        await asyncio.sleep(2)
        await assert_only_translation(receiver, caller, RECEIVER_HZ)
    finally:
        await caller.disconnect()
        await receiver.disconnect()


async def wait_disconnected(room, timeout=8):
    for _ in range(int(timeout * 10)):
        if not room.isconnected():
            return True
        await asyncio.sleep(0.1)
    return False


@pytest.mark.parametrize("caller_env", ["PARTY_A_NO", "PARTY_B_NO"])
@pytest.mark.parametrize("hangup_caller", [True, False])
async def test_hangup_ends_other_side(caller_env, hangup_caller):
    caller, receiver = await call_setup(caller_env)
    try:
        await asyncio.sleep(2)
        first, second = (caller, receiver) if hangup_caller else (receiver, caller)
        await first.disconnect()
        assert await wait_disconnected(second), "other call leg stayed up after hangup"
    finally:
        await caller.disconnect()
        await receiver.disconnect()


async def test_unknown_caller_is_dropped():
    room_a = f"e2e-{random.randrange(10**9)}"
    stranger = await join("caller", room_a)
    try:
        await dispatch(room_a, "+919999999999")
        assert await wait_disconnected(stranger), "stranger's call was not dropped"
    finally:
        await stranger.disconnect()
