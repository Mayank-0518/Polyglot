"""P4: real vendor checks. Needs SARVAM_API_KEY; recordings in tests/fixtures/<lang>_<nn>.wav + .txt."""
import asyncio
import os
import time
import wave
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from vendor_timing import RequestTiming, TimedAsyncClient

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.getenv("SARVAM_API_KEY"), reason="needs SARVAM_API_KEY"),
]

ODIA = (0x0B00, 0x0B7F)
DEVANAGARI = (0x0900, 0x097F)
FIXTURES = sorted(Path(__file__).parent.joinpath("fixtures").glob("*.wav"))
MIN_OVERLAP = 0.6


@pytest.fixture(autouse=True)
def real_vendors(monkeypatch):
    monkeypatch.setenv("VENDORS", "sarvam")


@pytest.fixture
def translation_timing(monkeypatch):
    from app import vendors
    timing = RequestTiming()
    monkeypatch.setattr(vendors.httpx, "AsyncClient", lambda **kwargs: TimedAsyncClient(timing, **kwargs))
    return timing


def script_ratio(text, block):
    letters = [c for c in text if c.isalpha()]
    return sum(block[0] <= ord(c) <= block[1] for c in letters) / max(len(letters), 1)


def word_overlap(expected, got):
    exp, g = expected.split(), set(got.split())
    return sum(w in g for w in exp) / max(len(exp), 1)


@pytest.mark.parametrize("text,src,tgt,block", [
    ("तीसरी मंज़िल पर आ जाइए", "hi-IN", "od-IN", ODIA),
    ("ମୁଁ ଗେଟ୍ ପାଖରେ ଅଛି", "od-IN", "hi-IN", DEVANAGARI),
])
async def test_translate(text, src, tgt, block, translation_timing):
    from app import vendors
    t0 = time.perf_counter()
    try:
        out = await vendors.translate(text, src, tgt)
    finally:
        elapsed = time.perf_counter() - t0
        translation_timing.report(f"{src} to {tgt}", "production_fresh_client", elapsed)
    assert script_ratio(out, block) > 0.8, out
    assert elapsed < 1.5, f"{src} to {tgt}: {elapsed:.3f}s exceeds the 1.5s limit"


@pytest.mark.parametrize("text,src,tgt,block", [
    ("तीसरी मंज़िल पर आ जाइए", "hi-IN", "od-IN", ODIA),
    ("ମୁଁ ଗେଟ୍ ପାଖରେ ଅଛି", "od-IN", "hi-IN", DEVANAGARI),
])
async def test_translate_connection_reuse_diagnostic(text, src, tgt, block, translation_timing, monkeypatch):
    """Compare connection establishment with reuse; production still creates a client per call."""
    from app import vendors
    options = {"timeout": 10, "headers": vendors._headers()}
    elapsed_samples = []
    outputs = []
    async with TimedAsyncClient(translation_timing, **options) as client:
        shared_setup = translation_timing.seconds["client_setup"]

        @asynccontextmanager
        async def reuse_client(**kwargs):
            assert kwargs == options
            yield client

        monkeypatch.setattr(vendors.httpx, "AsyncClient", reuse_client)
        for sample in ("shared_client_first_request", "shared_client_reused_request"):
            translation_timing.reset()
            t0 = time.perf_counter()
            try:
                outputs.append(await vendors.translate(text, src, tgt))
            finally:
                elapsed = time.perf_counter() - t0
                elapsed_samples.append(elapsed)
                translation_timing.report(
                    f"{src} to {tgt}", sample, elapsed,
                    shared_client_setup_ms=round(shared_setup * 1000, 3),
                )
    assert all(script_ratio(out, block) > 0.8 for out in outputs), outputs
    assert all(elapsed < 1.5 for elapsed in elapsed_samples), elapsed_samples


@pytest.mark.parametrize("text,lang", [("मैं गेट के पास हूँ", "hi-IN"), ("ମୁଁ ଗେଟ୍ ପାଖରେ ଅଛି", "od-IN")])
async def test_synthesize(text, lang):
    from app import vendors
    pcm, rate = await vendors.synthesize(text, lang)
    assert len(pcm) / 2 / rate > 0.5  # at least half a second of 16-bit audio


@pytest.mark.parametrize("lang,block", [("od-IN", ODIA), ("hi-IN", DEVANAGARI)])
@pytest.mark.skipif(not FIXTURES, reason="needs WAV fixtures for streaming STT")
async def test_transcribe_stream(lang, block):
    from app import vendors
    path = Path(__file__).parent / "fixtures" / f"{lang}_01.wav"
    expected = path.with_suffix(".txt").read_text(encoding="utf-8").strip()
    pcm, rate = vendors._pcm_wav(str(path))
    events = []

    async def audio():
        data = pcm + bytes(rate * 2)  # one second of silence to finish the utterance
        frame_bytes = rate // 50 * 2
        for offset in range(0, len(data), frame_bytes):
            yield data[offset:offset + frame_bytes]
            await asyncio.sleep(0.02)

    async def collect():
        return [text async for text in vendors.transcribe_stream(
            audio(), lang, rate,
            on_speech_start=lambda: events.append("start"),
            on_speech_end=lambda: events.append("end"),
        )]

    finals = await asyncio.wait_for(collect(), timeout=30)
    got = " ".join(finals)
    print(f"{lang} streaming: {got}; overlap={word_overlap(expected, got):.2f}; events={events}")
    assert script_ratio(got, block) > 0.8, got
    assert word_overlap(expected, got) >= MIN_OVERLAP, got
    assert "start" in events and "end" in events, events


@pytest.mark.skipif(not FIXTURES, reason="no recordings in tests/fixtures yet (P4 human gate)")
async def test_noisy_phone_audio_accuracy():
    from app import vendors
    for lang in ("od-IN", "hi-IN"):
        assert sum(wav.stem.startswith(lang + "_") for wav in FIXTURES) >= 10, f"Need at least 10 {lang} recordings"
    transcripts = {}
    for wav in FIXTURES:
        lang = wav.stem.rsplit("_", 1)[0]
        assert lang in {"od-IN", "hi-IN"}, f"Unexpected recording language: {wav.name}"
        with wave.open(str(wav), "rb") as audio:
            assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getcomptype()) == (8000, 1, 2, "NONE"), wav.name
            assert 0 < audio.getnframes() <= 30 * 8000, wav.name
        transcripts[wav.name] = wav.with_suffix(".txt").read_text(encoding="utf-8").strip()
        assert transcripts[wav.name], f"Empty transcript: {wav.name}"
    scores = {}
    for wav in FIXTURES:
        lang = wav.stem.rsplit("_", 1)[0]
        got = await vendors.transcribe_file(str(wav), lang)
        scores[wav.name] = word_overlap(transcripts[wav.name], got)
    avg = sum(scores.values()) / len(scores)
    print("\n".join(f"{k}: {v:.2f}" for k, v in scores.items()), f"\navg: {avg:.2f}")
    for lang in ("od-IN", "hi-IN"):
        language_scores = [score for name, score in scores.items() if name.startswith(lang + "_")]
        print(f"{lang} avg: {sum(language_scores) / len(language_scores):.2f}")
    assert avg >= MIN_OVERLAP
