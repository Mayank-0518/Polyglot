import array
import asyncio
import base64
import json
import math
import os
import sys
import wave
from pathlib import Path

import aiohttp
import httpx

LANGUAGES = {
    "hi-IN", "kn-IN", "ta-IN", "te-IN", "ml-IN", "mr-IN",
    "bn-IN", "gu-IN", "pa-IN", "od-IN", "en-IN",
}


def _mode(*languages):
    if any(language not in LANGUAGES for language in languages):
        raise ValueError("Unsupported language")
    mode = os.getenv("VENDORS", "fake")
    if mode not in {"fake", "sarvam"}:
        raise ValueError("VENDORS must be fake or sarvam")
    return mode


def _headers():
    key = os.getenv("SARVAM_API_KEY", "")
    if not key.strip():
        raise ValueError("SARVAM_API_KEY is required for Sarvam vendors")
    return {"api-subscription-key": key}


async def _post(endpoint, **kwargs):
    async with httpx.AsyncClient(timeout=10, headers=_headers()) as client:
        response = await client.post(f"https://api.sarvam.ai/{endpoint}", **kwargs)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("Invalid Sarvam response")
        return result


async def translate(text, src, tgt):
    mode = _mode(src, tgt)
    if not text.strip():
        raise ValueError("Translation text must not be blank")
    if mode == "fake":
        return f"[{tgt}] {text}"
    if len(text) > 1000:
        raise ValueError("Translation input must not exceed 1000 characters")
    result = await _post("translate", json={
        "input": text,
        "source_language_code": src,
        "target_language_code": tgt,
        # This model returns native script and does not support output_script.
        "model": "sarvam-translate:v1",
    })
    translated = result.get("translated_text")
    if not isinstance(translated, str) or not translated.strip():
        raise ValueError("Sarvam returned no translation")
    return translated


def _pcm_wav(source):
    with wave.open(source, "rb") as audio:
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getcomptype() != "NONE":
            raise ValueError("Audio must be uncompressed mono PCM16 WAV")
        pcm = audio.readframes(audio.getnframes())
        if len(pcm) != audio.getnframes() * 2 or not pcm:
            raise ValueError("Audio is empty or truncated")
        return pcm, audio.getframerate()


_TTS_VOICES = {
    "en-IN": "Kaveri", "hi-IN": "Nalini", "ta-IN": "Asmita", "te-IN": "Suhana",
    "kn-IN": "Saanvi", "ml-IN": "Reshma", "mr-IN": "Zahira", "bn-IN": "Kirra",
    "gu-IN": "Falak", "pa-IN": "Mehuli",
}


async def synthesize(text, lang):
    """Yield (pcm_chunk, rate) pairs of mono PCM16 audio at 24000 Hz, streamed as it's synthesized."""
    mode = _mode(lang)
    if not text.strip():
        raise ValueError("Speech text must not be blank")
    rate = 24000
    if mode == "fake":
        hz = 440 if text.startswith("[") else 550
        samples = array.array("h", (
            int(8000 * math.sin(2 * math.pi * hz * i / rate))
            for i in range(rate * 3 // 5)
        ))
        if sys.byteorder != "little":
            samples.byteswap()
        pcm = samples.tobytes()
        chunk_bytes = rate // 50 * 2  # 20ms chunks, matching real streaming granularity
        for offset in range(0, len(pcm), chunk_bytes):
            yield pcm[offset:offset + chunk_bytes], rate
        return

    # VENDORS=sarvam also selects Gnani for TTS; translate() still uses Sarvam.
    if len(text) > 2500:
        raise ValueError("Gnani speech input must not exceed 2500 characters")
    voice = _TTS_VOICES.get(lang)
    if voice is None:
        raise ValueError(f"Gnani TTS does not support {lang}")
    key = os.getenv("GNANI_API_KEY", "")
    if not key.strip():
        raise ValueError("GNANI_API_KEY is required for Gnani TTS")
    headers = {"X-API-Key-ID": key}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.ws_connect("wss://api.vachana.ai/api/v1/tts") as ws:
            await ws.send_json({
                "text": text, "voice": voice, "model": "timbre-v2.5", "language": lang,
                "audio_config": {
                    "sample_rate": rate, "num_channels": 1, "sample_width": 2,
                    "encoding": "linear_pcm", "container": "raw",
                },
            })
            while True:
                message = await ws.receive()
                if message.type != aiohttp.WSMsgType.TEXT:
                    raise RuntimeError("Gnani TTS socket closed unexpectedly")
                event = json.loads(message.data)
                if not isinstance(event, dict):
                    raise ValueError("Invalid Gnani TTS event")
                kind = event.get("type")
                if kind == "error":
                    raise RuntimeError(f"Gnani TTS reported an error: {event.get('message')}")
                data = event.get("data") if isinstance(event.get("data"), dict) else {}
                if kind in {"audio", "complete"}:
                    encoded = data.get("audio")
                    if encoded:
                        pcm = base64.b64decode(encoded, validate=True)
                        if len(pcm) % 2:
                            raise ValueError("Gnani returned malformed PCM16 audio")
                        yield pcm, rate
                    if data.get("is_final"):
                        return


async def transcribe_file(path, lang):
    mode = _mode(lang)
    pcm, rate = _pcm_wav(str(path))
    if rate not in {8000, 16000}:
        raise ValueError("STT audio sample rate must be 8000 or 16000 Hz")
    if len(pcm) / (2 * rate) > 30:
        raise ValueError("REST transcription clips must not exceed 30 seconds")
    if mode == "fake":
        async def chunks():
            frame_bytes = rate // 50 * 2
            for offset in range(0, len(pcm), frame_bytes):
                yield pcm[offset:offset + frame_bytes]

        return " ".join([text async for text in transcribe_stream(chunks(), lang, rate)])
    with open(path, "rb") as audio:
        result = await _post("speech-to-text", files={"file": (Path(path).name, audio, "audio/wav")}, data={
            "model": "saaras:v4", "mode": "transcribe", "language_code": lang,
        })
    transcript = result.get("transcript")
    if not isinstance(transcript, str):
        raise ValueError("Sarvam returned an invalid transcript")
    return transcript


def _samples(pcm):
    if len(pcm) % 2:
        raise ValueError("Streaming audio must contain whole PCM16 samples")
    samples = array.array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


async def transcribe_stream(chunks, lang, sample_rate=16000, on_speech_start=None, on_speech_end=None):
    """Yield final transcripts from mono little-endian PCM16 chunks; callbacks are synchronous."""
    mode = _mode(lang)
    if sample_rate not in {8000, 16000}:
        raise ValueError("STT audio sample rate must be 8000 or 16000 Hz")
    if mode == "fake":
        speech_samples = silence_samples = 0
        async for pcm in chunks:
            samples = _samples(pcm)
            if not samples:
                continue
            if math.sqrt(sum(sample * sample for sample in samples) / len(samples)) > 500:
                if speech_samples == 0 and on_speech_start is not None:
                    on_speech_start()
                speech_samples += len(samples)
                silence_samples = 0
            elif speech_samples:
                silence_samples += len(samples)
                if silence_samples >= sample_rate // 2:
                    valid = speech_samples >= sample_rate * 3 // 10
                    speech_samples = silence_samples = 0
                    if on_speech_end is not None:
                        on_speech_end()
                    if valid:
                        yield "fake"
        return

    # VENDORS=sarvam also selects Gnani for live STT; translate()/synthesize() still use Sarvam.
    key = os.getenv("GNANI_API_KEY", "")
    if not key.strip():
        raise ValueError("GNANI_API_KEY is required for Gnani STT")
    FRAME_BYTES = 1024  # Gnani requires exactly 1024-byte PCM16 frames (512 samples).
    input_ended = asyncio.Event()
    buffer = bytearray()

    async def send_audio(ws):
        try:
            async for pcm in chunks:
                _samples(pcm)  # validates whole PCM16 samples
                buffer.extend(pcm)
                while len(buffer) >= FRAME_BYTES:
                    await ws.send_bytes(bytes(buffer[:FRAME_BYTES]))
                    del buffer[:FRAME_BYTES]
            if buffer:
                await ws.send_bytes(bytes(buffer) + bytes(FRAME_BYTES - len(buffer)))
            input_ended.set()
        finally:
            await ws.close()

    headers = {"x-api-key-id": key, "lang_code": lang, "x-sample-rate": str(sample_rate)}
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=10)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        async with session.ws_connect("wss://api.vachana.ai/stt/v3/stream") as ws:
            sender = asyncio.create_task(send_audio(ws))
            try:
                while True:
                    message = await ws.receive()
                    if message.type != aiohttp.WSMsgType.TEXT:
                        if input_ended.is_set():
                            return
                        raise RuntimeError("Gnani STT socket closed before audio input finished")
                    event = json.loads(message.data)
                    if not isinstance(event, dict):
                        raise ValueError("Invalid Gnani STT event")
                    kind = event.get("type")
                    if kind == "error":
                        raise RuntimeError(f"Gnani STT reported an error: {event.get('message')}")
                    if kind == "speech_start":
                        # Undocumented but confirmed live: fires once per utterance.
                        if on_speech_start is not None:
                            on_speech_start()
                    elif kind == "transcript":
                        # No separate end-of-speech event is sent; the final transcript doubles as one.
                        if on_speech_end is not None:
                            on_speech_end()
                        text = event.get("text")
                        if not isinstance(text, str):
                            raise ValueError("Gnani returned an invalid transcript")
                        if text.strip():
                            yield text
            finally:
                if not sender.done():
                    sender.cancel()
                try:
                    await sender
                except asyncio.CancelledError:
                    pass
