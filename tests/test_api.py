import os
import subprocess
import sys
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

KEY = "test-key"
H = {"X-API-Key": KEY}
A, B = "+919123260733", "+916392306428"
PARTIES = {A: "od-IN", B: "hi-IN"}
ENV = {
    "API_KEY": KEY,
    "PARTY_A_NO": A,
    "PARTY_A_LANG": "od-IN",
    "PARTY_B_NO": B,
    "PARTY_B_LANG": "hi-IN",
}

with patch.dict(os.environ, ENV):
    from app.api import create_app


@pytest.fixture
def client():
    return TestClient(create_app(parties=PARTIES, api_key=KEY))


def test_healthz_needs_no_key(client):
    assert client.get("/healthz").status_code == 200


@pytest.mark.parametrize("caller,other,src,tgt", [
    (A, B, "od-IN", "hi-IN"),
    (B, A, "hi-IN", "od-IN"),
])
def test_lookup_routes_either_caller(client, caller, other, src, tgt):
    response = client.get("/lookup", params={"caller": caller}, headers=H)
    assert response.status_code == 200
    assert response.json() == {"caller_lang": src, "other_no": other, "other_lang": tgt}


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}])
def test_auth_required(client, headers):
    assert client.get("/lookup", params={"caller": A}, headers=headers).status_code == 401


@pytest.mark.parametrize("caller", ["+919999999999", "+918328878286"])
def test_unknown_caller_is_rejected(client, caller):
    response = client.get("/lookup", params={"caller": caller}, headers=H)
    assert response.status_code == 404


def test_missing_caller_is_rejected(client):
    assert client.get("/lookup", headers=H).status_code == 422


@pytest.mark.parametrize("parties", [
    {A: "od-IN"},
    {**PARTIES, "+919999999999": "en-IN"},
    {"12345": "od-IN", B: "hi-IN"},
    {A: "od-IN", B: "xx-XX"},
    {A: "hi-IN", B: "hi-IN"},
    {"+123456789": "od-IN", B: "hi-IN"},
    {"+1234567890123456": "od-IN", B: "hi-IN"},
    {A + "\n": "od-IN", B: "hi-IN"},
    {"+９１８３２８８７８２８６": "od-IN", B: "hi-IN"},
])
def test_invalid_configuration_is_rejected(parties):
    with pytest.raises(ValueError):
        create_app(parties=parties, api_key=KEY)


@pytest.mark.parametrize("api_key", ["", " ", "\t"])
def test_empty_api_key_is_rejected(api_key):
    with pytest.raises(ValueError):
        create_app(parties=PARTIES, api_key=api_key)


@pytest.mark.parametrize("number", ["+1234567890", "+123456789012345"])
def test_phone_length_boundaries_are_accepted(number):
    client = TestClient(create_app(parties={number: "od-IN", B: "hi-IN"}, api_key=KEY))
    assert client.get("/lookup", params={"caller": number}, headers=H).status_code == 200


@pytest.mark.parametrize("caller", [A + " ", A[1:], "", "' OR 1=1 --"])
def test_malformed_caller_does_not_route(client, caller):
    assert client.get("/lookup", params={"caller": caller}, headers=H).status_code == 404


def test_module_app_uses_environment():
    script = """
from fastapi.testclient import TestClient
from app.api import app
client = TestClient(app)
headers = {"X-API-Key": "test-key"}
assert client.get("/healthz").status_code == 200
assert client.get("/lookup", params={"caller": "+919123260733"}, headers=headers).json() == {
    "caller_lang": "od-IN", "other_no": "+916392306428", "other_lang": "hi-IN",
}
assert client.get("/lookup", params={"caller": "+916392306428"}, headers=headers).json() == {
    "caller_lang": "hi-IN", "other_no": "+919123260733", "other_lang": "od-IN",
}
"""
    result = subprocess.run(
        [sys.executable, "-c", script], env={**os.environ, **ENV}, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("overrides", [{"API_KEY": ""}, {"PARTY_B_NO": A}])
def test_module_app_rejects_invalid_environment(overrides):
    result = subprocess.run(
        [sys.executable, "-c", "import app.api"],
        env={**os.environ, **ENV, **overrides}, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "ValueError" in result.stderr
