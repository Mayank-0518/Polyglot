"""Verify the configured Bengali/Hindi pair without vendor or phone calls."""
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app import vendors


@pytest.mark.parametrize("caller, caller_lang, other, other_lang", [
    ("+919123260733", "bn-IN", "+916392306428", "hi-IN"),
    ("+916392306428", "hi-IN", "+919123260733", "bn-IN"),
])
async def test_api_routes_bengali_pair(monkeypatch, caller, caller_lang, other, other_lang):
    for key, value in {
        "PARTY_A_NO": "+919123260733", "PARTY_A_LANG": "bn-IN",
        "PARTY_B_NO": "+916392306428", "PARTY_B_LANG": "hi-IN",
        "API_KEY": "test-key",
    }.items():
        monkeypatch.setenv(key, value)
    from app.api import create_app

    app = create_app({"+919123260733": "bn-IN", "+916392306428": "hi-IN"}, "test-key")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/lookup", params={"caller": caller}, headers={"X-API-Key": "test-key"})
    assert response.status_code == 200
    assert response.json() == {"caller_lang": caller_lang, "other_no": other, "other_lang": other_lang}


def test_example_config_uses_bengali():
    example = (Path(__file__).parents[1] / ".env.example").read_text(encoding="utf-8")
    assert "PARTY_A_LANG=bn-IN" in example.splitlines()
    assert "PARTY_B_LANG=hi-IN" in example.splitlines()


@pytest.mark.parametrize("kind", ["connected", "busy", "unanswered", "failed", "wait"])
def test_bengali_announcements(kind):
    from app.agent import PROMPTS

    assert any("\u0980" <= char <= "\u09ff" for char in PROMPTS["bn-IN"][kind])


@pytest.mark.parametrize("src, tgt", [("bn-IN", "hi-IN"), ("hi-IN", "bn-IN")])
async def test_translation_keeps_bengali_code(monkeypatch, src, tgt):
    monkeypatch.setenv("VENDORS", "sarvam")

    async def post(endpoint, *, json):
        assert endpoint == "translate"
        assert json["source_language_code"] == src
        assert json["target_language_code"] == tgt
        return {"translated_text": "translated"}

    monkeypatch.setattr(vendors, "_post", post)
    assert await vendors.translate("text", src, tgt) == "translated"


async def test_bengali_synthesis_uses_bengali_voice(monkeypatch):
    monkeypatch.setenv("VENDORS", "sarvam")
    monkeypatch.setenv("GNANI_API_KEY", "test-key")

    async def stream(key, request):
        assert key == "test-key"
        assert request["language"] == "bn-IN"
        assert request["voice"] == "Kirra"
        yield b"\x00\x00"

    connection = SimpleNamespace(stream=stream)
    chunks = [chunk async for chunk in vendors.synthesize("নমস্কার", "bn-IN", connection)]
    assert chunks == [(b"\x00\x00", 24000)]
