import asyncio
import base64
import io
import json
import struct
import wave
from contextlib import asynccontextmanager
from types import SimpleNamespace

import aiohttp
import httpx
import pytest

from app import vendors
from app.pipeline import run_direction


def pcm(ms, rate=16000, amplitude=4000):
    return struct.pack("<h", amplitude) * (rate * ms // 1000)


def wav_bytes(data, rate=16000, channels=1, width=2):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(width)
        audio.setframerate(rate)
        audio.writeframes(data)
    return buffer.getvalue()


async def chunks(data, size=320):
    for offset in range(0, len(data), size):
        yield data[offset:offset + size]


@pytest.fixture(autouse=True)
def fake_mode(monkeypatch):
    monkeypatch.setenv("VENDORS", "fake")
    monkeypatch.delenv("SARVAM_API_KEY", raising=False)


@pytest.fixture
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Fake vendors must not create network clients")

    monkeypatch.setattr(vendors.httpx, "AsyncClient", forbidden)
    monkeypatch.setattr(vendors.aiohttp, "ClientSession", forbidden)


@pytest.fixture
def mock_http(monkeypatch):
    monkeypatch.setenv("VENDORS", "sarvam")
    monkeypatch.setenv("SARVAM_API_KEY", "test-sarvam-key")
    original = httpx.AsyncClient

    def install(handler):
        monkeypatch.setattr(vendors.httpx, "AsyncClient", lambda **kwargs: original(
            transport=httpx.MockTransport(handler), **kwargs,
        ))

    return install


@pytest.mark.parametrize("src,tgt", [("od-IN", "hi-IN"), ("hi-IN", "od-IN")])
async def test_fake_translation_without_network(src, tgt, no_network):
    assert await vendors.translate("gate 3", src, tgt) == f"[{tgt}] gate 3"


@pytest.mark.parametrize("text,hz", [("[hi-IN] fake", 440), ("Please wait", 550)])
async def test_fake_tts_duration_rate_and_frequency(text, hz, no_network):
    streamed = [item async for item in vendors.synthesize(text, "od-IN")]
    assert len(streamed) > 1
    assert {rate for _, rate in streamed} == {24000}
    data = b"".join(chunk for chunk, _ in streamed)
    assert len(data) == int(0.6 * 24000) * 2
    samples = [sample[0] for sample in struct.iter_unpack("<h", data)]
    crossings = sum((a < 0) != (b < 0) for a, b in zip(samples, samples[1:]))
    assert abs(crossings / 2 / 0.6 - hz) < 2
    assert max(samples) > 7000


@pytest.mark.parametrize("rate", [8000, 16000])
@pytest.mark.parametrize("speech_ms,silence_ms,expected", [
    (299, 500, []), (300, 499, []), (300, 500, ["fake"]),
    (0, 900, []), (700, 200, []),
])
async def test_fake_stt_timing_boundaries(rate, speech_ms, silence_ms, expected, no_network):
    async def audio():
        for data in (pcm(speech_ms, rate), pcm(silence_ms, rate, 0)):
            async for frame in chunks(data, rate // 50 * 2):
                yield frame

    assert [text async for text in vendors.transcribe_stream(audio(), "od-IN", rate)] == expected


async def test_fake_stt_rearms_and_fires_speech_callbacks(no_network):
    events = []
    utterance = pcm(300) + pcm(500, amplitude=0)
    results = [text async for text in vendors.transcribe_stream(
        chunks(utterance * 2), "hi-IN",
        on_speech_start=lambda: events.append("start"), on_speech_end=lambda: events.append("end"),
    )]
    assert results == ["fake", "fake"]
    assert events == ["start", "end", "start", "end"]


async def test_fake_stt_ignores_low_amplitude_noise_and_empty_chunks(no_network):
    async def quiet():
        yield b""
        yield pcm(600, amplitude=400)
        yield pcm(600, amplitude=0)

    assert [text async for text in vendors.transcribe_stream(quiet(), "hi-IN")] == []


async def test_fake_file_uses_audio_without_a_transcript_sidecar(tmp_path, no_network):
    path = tmp_path / "phone.wav"
    path.write_bytes(wav_bytes(pcm(300, 8000) + pcm(500, 8000, 0), rate=8000))
    assert await vendors.transcribe_file(path, "od-IN") == "fake"


@pytest.mark.parametrize("rate", [0, 24000, 48000])
async def test_stream_rejects_unsupported_rates(rate, no_network):
    with pytest.raises(ValueError, match="sample rate"):
        await anext(vendors.transcribe_stream(chunks(b""), "hi-IN", rate))


async def test_stream_rejects_partial_pcm_samples(no_network):
    with pytest.raises(ValueError, match="whole PCM16"):
        await anext(vendors.transcribe_stream(chunks(b"x"), "hi-IN"))


@pytest.mark.parametrize("mode,key,lang", [("unknown", "", "hi-IN"), ("sarvam", "", "hi-IN"), ("fake", "", "xx-XX")])
async def test_invalid_configuration_fails_before_network(mode, key, lang, monkeypatch, no_network):
    monkeypatch.setenv("VENDORS", mode)
    monkeypatch.setenv("SARVAM_API_KEY", key)
    with pytest.raises(ValueError):
        await vendors.translate("hello", lang, "od-IN")


@pytest.mark.parametrize("src,tgt,output", [("od-IN", "hi-IN", "मैं यहाँ हूँ"), ("hi-IN", "od-IN", "ମୁଁ ଏଠାରେ ଅଛି")])
async def test_sarvam_translation_request_and_response(mock_http, src, tgt, output):
    def handler(request):
        assert str(request.url) == "https://api.sarvam.ai/translate"
        assert request.headers["api-subscription-key"] == "test-sarvam-key"
        assert json.loads(request.content) == {
            "input": "gate 3", "source_language_code": src, "target_language_code": tgt,
            "model": "sarvam-translate:v1",
        }
        return httpx.Response(200, json={"translated_text": output})

    mock_http(handler)
    assert await vendors.translate("gate 3", src, tgt) == output


async def test_sarvam_file_sends_wav_and_native_language(mock_http, tmp_path):
    data = wav_bytes(pcm(800, 8000), rate=8000)
    path = tmp_path / "odia.wav"
    path.write_bytes(data)

    def handler(request):
        assert request.url.path == "/speech-to-text"
        assert request.headers["content-type"].startswith("multipart/form-data;")
        assert data in request.content
        for value in (b"od-IN", b"saaras:v4", b"transcribe"):
            assert value in request.content
        return httpx.Response(200, json={"transcript": "ମୁଁ ଏଠାରେ ଅଛି"})

    mock_http(handler)
    assert await vendors.transcribe_file(path, "od-IN") == "ମୁଁ ଏଠାରେ ଅଛି"


@pytest.mark.parametrize("payload", [{}, {"translated_text": " "}, {"translated_text": 123}, []])
async def test_missing_or_invalid_translation_is_an_error(mock_http, payload):
    mock_http(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(ValueError):
        await vendors.translate("source", "od-IN", "hi-IN")


@pytest.mark.parametrize("status", [403, 429, 500])
async def test_http_errors_propagate_without_a_fake_fallback(mock_http, status):
    mock_http(lambda request: httpx.Response(status, json={"error": "vendor unavailable"}))
    with pytest.raises(httpx.HTTPStatusError):
        await vendors.translate("source", "od-IN", "hi-IN")


async def test_network_timeout_propagates(mock_http):
    def handler(request):
        raise httpx.ReadTimeout("vendor timed out", request=request)

    mock_http(handler)
    with pytest.raises(httpx.ReadTimeout):
        await vendors.translate("source", "od-IN", "hi-IN")


async def test_failed_vendor_translation_never_speaks_original(mock_http):
    spoken, errors = [], []

    def handler(request):
        if json.loads(request.content)["input"] == "bad":
            return httpx.Response(500)
        return httpx.Response(200, json={"translated_text": "अनुवाद"})

    async def finals():
        yield "bad"
        yield "good"

    async def translate(text):
        return await vendors.translate(text, "od-IN", "hi-IN")

    async def speak(text):
        spoken.append(text)

    mock_http(handler)
    await run_direction(finals(), translate, speak, errors.append)
    assert spoken == ["अनुवाद"]
    assert len(errors) == 1 and isinstance(errors[0], httpx.HTTPStatusError)


class Socket:
    """Gnani pushes events independently of client sends; close() always follows."""

    def __init__(self, events):
        self.received = asyncio.Queue()
        for event in events:
            self.received.put_nowait(SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(event)))
        self.sent = []
        self.closed = False

    async def send_bytes(self, data):
        self.sent.append(data)

    async def send_json(self, data):
        self.sent.append(data)

    async def receive(self):
        return await self.received.get()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        await self.received.put(SimpleNamespace(type=aiohttp.WSMsgType.CLOSED))


@pytest.fixture
def mock_socket(monkeypatch):
    monkeypatch.setenv("VENDORS", "sarvam")
    monkeypatch.setenv("GNANI_API_KEY", "test-gnani-key")
    connections = []

    def install(socket):
        @asynccontextmanager
        async def connect(url, **kwargs):
            connections.append(url)
            try:
                yield socket
            finally:
                await socket.close()

        @asynccontextmanager
        async def session(**kwargs):
            socket.headers = {key.lower(): value for key, value in kwargs["headers"].items()}
            assert socket.headers["x-api-key-id"] == "test-gnani-key"
            yield SimpleNamespace(ws_connect=connect)

        monkeypatch.setattr(vendors.aiohttp, "ClientSession", session)
        return connections

    return install


def tts_audio(index, data, final=False):
    kind = "complete" if final else "audio"
    return {"type": kind, "data": {"chunk_index": index, "audio": base64.b64encode(data).decode(), "is_final": final}}


@pytest.mark.parametrize("lang,voice", [("bn-IN", "Kirra"), ("hi-IN", "Nalini")])
async def test_gnani_tts_streams_decoded_chunks_in_order(mock_socket, lang, voice):
    first, second = pcm(40, 24000), pcm(30, 24000, 3000)
    socket = Socket([
        {"type": "start", "message": "Streaming started"},
        tts_audio(1, first), tts_audio(2, second), tts_audio(3, b"", final=True),
    ])
    connections = mock_socket(socket)
    assert [item async for item in vendors.synthesize("text", lang)] == [(first, 24000), (second, 24000)]
    assert connections == ["wss://api.vachana.ai/api/v1/tts"]
    request = socket.sent[0]
    assert (request["text"], request["voice"], request["language"], request["model"]) == ("text", voice, lang, "timbre-v2.5")
    assert request["audio_config"]["sample_rate"] == 24000
    assert request["audio_config"]["container"] == "raw"
    assert socket.closed


@pytest.mark.parametrize("events", [
    [{"type": "error", "message": "Invalid text or audio configuration."}],
    [tts_audio(1, b"x")],  # odd byte count is not whole PCM16
    [{"type": "audio", "data": {"chunk_index": 1, "audio": "not base64!", "is_final": False}}],
    [[]],
    [tts_audio(1, pcm(20, 24000))],  # socket closes before the final chunk
])
async def test_gnani_tts_invalid_stream_is_an_error(mock_socket, events):
    socket = Socket(events)
    await socket.close()  # the server hangs up after the scripted events
    mock_socket(socket)
    with pytest.raises((RuntimeError, ValueError)):
        async for _ in vendors.synthesize("text", "hi-IN"):
            pass


async def test_gnani_tts_rejects_unsupported_language_before_network(monkeypatch, no_network):
    monkeypatch.setenv("VENDORS", "sarvam")
    monkeypatch.setenv("GNANI_API_KEY", "test-gnani-key")
    with pytest.raises(ValueError, match="does not support od-IN"):
        await anext(vendors.synthesize("text", "od-IN"))


@pytest.mark.parametrize("lang,rate", [("bn-IN", 8000), ("hi-IN", 16000)])
async def test_realtime_stt_yields_transcripts_and_fires_speech_events(mock_socket, lang, rate):
    events = []
    socket = Socket([
        {"type": "connected", "config": {"sample_rate": rate, "chunk_size": 512}},
        {"type": "speech_start", "utterance_id": "u1"},
        {"type": "transcript", "text": "final", "is_final": True},
    ])
    connections = mock_socket(socket)
    audio = pcm(20, rate)
    results = [text async for text in vendors.transcribe_stream(
        chunks(audio), lang, rate,
        lambda: events.append("start"), lambda: events.append("end"),
    )]
    assert results == ["final"]
    assert events == ["start", "end"]
    assert connections[0] == "wss://api.vachana.ai/stt/v3/stream"
    assert socket.headers["lang_code"] == lang
    assert socket.headers["x-sample-rate"] == str(rate)
    sent_audio = b"".join(socket.sent)
    assert sent_audio.startswith(audio)
    assert len(sent_audio) % 1024 == 0
    assert socket.closed


@pytest.mark.parametrize("event", [
    {"type": "error", "message": "vendor failure"}, {"type": "transcript", "text": None},
])
async def test_realtime_errors_are_not_transcripts(mock_socket, event):
    socket = Socket([event])
    mock_socket(socket)
    with pytest.raises((RuntimeError, ValueError)):
        await anext(vendors.transcribe_stream(chunks(pcm(20)), "hi-IN"))
    assert socket.closed


async def test_realtime_consumer_close_cancels_audio_producer(mock_socket):
    started = asyncio.Event()
    stopped = asyncio.Event()
    socket = Socket([{"type": "transcript", "text": "final"}])
    mock_socket(socket)

    async def microphone():
        try:
            started.set()
            yield pcm(20)
            await asyncio.Future()
        finally:
            stopped.set()

    stream = vendors.transcribe_stream(microphone(), "hi-IN")
    assert await asyncio.wait_for(anext(stream), 1) == "final"
    await started.wait()  # ensure the sender task actually entered the generator before cancelling it
    await stream.aclose()
    assert stopped.is_set()
    assert socket.closed


async def test_realtime_audio_producer_failure_is_propagated(mock_socket):
    socket = Socket([])
    mock_socket(socket)

    async def broken_microphone():
        yield b"x"

    with pytest.raises(ValueError, match="whole PCM16"):
        await asyncio.wait_for(anext(vendors.transcribe_stream(broken_microphone(), "hi-IN")), 1)
    assert socket.closed


async def test_realtime_unexpected_close_before_input_ends_is_an_error(mock_socket):
    socket = Socket([])
    mock_socket(socket)
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def microphone():
        try:
            started.set()
            yield pcm(20)
            await asyncio.Future()
        finally:
            stopped.set()

    async def close_once_sending_started():
        await started.wait()
        await socket.close()  # server hangs up mid-stream, before audio input finished

    asyncio.create_task(close_once_sending_started())
    with pytest.raises(RuntimeError, match="before audio input finished"):
        await asyncio.wait_for(anext(vendors.transcribe_stream(microphone(), "hi-IN")), 1)
    assert stopped.is_set()


async def test_gnani_api_key_required_before_network(monkeypatch, no_network):
    monkeypatch.setenv("VENDORS", "sarvam")
    monkeypatch.setenv("SARVAM_API_KEY", "test-sarvam-key")
    monkeypatch.delenv("GNANI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="GNANI_API_KEY"):
        await anext(vendors.transcribe_stream(chunks(pcm(20)), "hi-IN"))


@pytest.mark.parametrize("method,limit", [("translate", 1000), ("synthesize", 2500)])
async def test_vendor_text_limits_fail_before_network(method, limit, monkeypatch, no_network):
    monkeypatch.setenv("VENDORS", "sarvam")
    with pytest.raises(ValueError, match="must not exceed"):
        if method == "translate":
            await vendors.translate("x" * (limit + 1), "od-IN", "hi-IN")
        else:
            await anext(vendors.synthesize("x" * (limit + 1), "hi-IN"))
