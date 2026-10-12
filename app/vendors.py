import array
import asyncio
import base64
import io
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


async def synthesize(text, lang):
    mode = _mode(lang)
    if not text.strip():
        raise ValueError("Speech text must not be blank")
    if mode == "fake":
        rate = 24000
        hz = 440 if text.startswith("[") else 550
        samples = array.array("h", (
            int(8000 * math.sin(2 * math.pi * hz * i / rate))
            for i in range(rate * 3 // 5)
        ))
        if sys.byteorder != "little":
            samples.byteswap()
        return samples.tobytes(), rate
    if len(text) > 2500:
        raise ValueError("Bulbul speech input must not exceed 2500 characters")
    result = await _post("text-to-speech", json={
        "text": text, "language_code": lang, "model": "bulbul:v3", "speaker": "shubh",
        "speech_sample_rate": 24000, "output_audio_codec": "wav",
    })
    audios = result.get("audios")
    if not isinstance(audios, list) or not audios:
        raise ValueError("Sarvam returned no audio")
    chunks, rate = [], None
    for encoded in audios:
        pcm, chunk_rate = _pcm_wav(io.BytesIO(base64.b64decode(encoded, validate=True)))
        if rate is not None and rate != chunk_rate:
            raise ValueError("Sarvam audio chunks have different sample rates")
        chunks.append(pcm)
        rate = chunk_rate
    return b"".join(chunks), rate


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

    # Realtime STT uses or-IN; REST translation, STT and TTS use od-IN.
    params = {
        "model": "saaras:v4", "language_code": "or-IN" if lang == "od-IN" else lang,
        "mode": "transcribe", "stream_type": "fast", "encoding": "linear16",
        "sample_rate": str(sample_rate), "endpointing": "vad",
        "silence_duration_ms": "500", "min_speech_duration_ms": "300",
    }
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=10)
    async with aiohttp.ClientSession(headers=_headers(), timeout=timeout) as session:
        async with session.ws_connect(
            "wss://api.sarvam.ai/speech-to-text-realtime/ws", params=params, heartbeat=20,
        ) as socket:
            input_ended = asyncio.Event()

            async def send_audio():
                try:
                    async for pcm in chunks:
                        _samples(pcm)
                        if pcm:
                            await socket.send_json({"event": "audio_input", "audio": base64.b64encode(pcm).decode("ascii")})
                    input_ended.set()
                    await socket.send_json({"event": "end"})
                    await asyncio.sleep(10)
                    raise TimeoutError("Sarvam STT did not finish after end of input")
                except Exception:
                    await socket.close()
                    raise

            sender = asyncio.create_task(send_audio())
            try:
                while True:
                    message = await socket.receive()
                    if message.type != aiohttp.WSMsgType.TEXT:
                        raise RuntimeError("Sarvam STT socket closed before session.end")
                    event = json.loads(message.data)
                    if not isinstance(event, dict):
                        raise ValueError("Invalid Sarvam STT event")
                    kind = event.get("event")
                    if kind == "error":
                        raise RuntimeError("Sarvam STT reported an error")
                    if kind == "session.end":
                        if not input_ended.is_set():
                            raise RuntimeError("Sarvam STT session ended before audio input finished")
                        return
                    if kind == "vad.speech_start" and on_speech_start is not None:
                        on_speech_start()
                    elif kind == "vad.speech_end" and on_speech_end is not None:
                        on_speech_end()
                    elif kind == "transcript.final":
                        text = event.get("text")
                        if not isinstance(text, str):
                            raise ValueError("Sarvam returned an invalid final transcript")
                        if text.strip():
                            yield text
            finally:
                if not sender.done():
                    sender.cancel()
                try:
                    await sender
                except asyncio.CancelledError:
                    pass
